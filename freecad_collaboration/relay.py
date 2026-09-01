"""Minimal WebSocket relay backed by the SQLite revision sequencer."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
import json

from aiohttp import WSMsgType, web

from .packet import TransactionPacket
from .revision_store import (
    CheckpointConflictError,
    EnvironmentMismatchError,
    ObjectLockConflictError,
    RevisionStore,
    StaleRevisionError,
    UnknownDocumentError,
)
from .state import DocumentState


STORE_KEY = web.AppKey("revision_store", RevisionStore)
CLIENTS_KEY = web.AppKey("websocket_clients", dict)


def _error_payload(exc) -> dict:
    payload = {
        "type": "error",
        "error": type(exc).__name__,
        "message": str(exc),
    }
    if isinstance(exc, StaleRevisionError):
        payload["submitted_revision"] = exc.submitted_revision
        payload["head_revision"] = exc.head_revision
    return payload


async def _broadcast(application, document_uid: str, payload: dict) -> None:
    clients = application[CLIENTS_KEY].get(document_uid, set())
    dead = []
    for socket in tuple(clients):
        if socket.closed:
            dead.append(socket)
            continue
        try:
            await socket.send_json(payload)
        except ConnectionError:
            dead.append(socket)
    for socket in dead:
        clients.discard(socket)


async def health(request):
    return web.json_response({"status": "ok"})


async def register_document(request):
    message = await request.json()
    document_uid = message["document_uid"]
    request.app[STORE_KEY].register_document(
        document_uid,
        message["name"],
        DocumentState.from_dict(message["state"]),
        environment_id=message.get("environment_id", ""),
    )
    return web.json_response(asdict(request.app[STORE_KEY].document_head(document_uid)))


async def document_head(request):
    try:
        head = request.app[STORE_KEY].document_head(request.match_info["document_uid"])
    except UnknownDocumentError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.json_response(asdict(head))


async def put_checkpoint(request):
    document_uid = request.match_info["document_uid"]
    checkpoint = await request.read()
    if not checkpoint:
        raise web.HTTPBadRequest(text="checkpoint is empty")
    try:
        created = request.app[STORE_KEY].store_checkpoint(document_uid, checkpoint)
    except UnknownDocumentError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    except CheckpointConflictError as exc:
        raise web.HTTPConflict(text=str(exc)) from exc
    return web.json_response({"stored": created})


async def get_checkpoint(request):
    document_uid = request.match_info["document_uid"]
    try:
        checkpoint = request.app[STORE_KEY].checkpoint(document_uid)
    except UnknownDocumentError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc
    return web.Response(body=checkpoint, content_type="application/octet-stream")


async def revisions(request):
    document_uid = request.match_info["document_uid"]
    after = int(request.query.get("after", "0"))
    records = request.app[STORE_KEY].list_revisions(document_uid)
    return web.json_response(
        [
            {
                "revision": record.revision,
                "parent_revision": record.parent_revision,
                "transaction_uid": record.transaction_uid,
                "packet": json.loads(record.packet_json),
                "definition_hash": record.definition_hash,
                "result_hash": record.result_hash,
                "accepted_at": record.accepted_at,
            }
            for record in records
            if record.revision > after
        ]
    )


async def _handle_submit(request, socket, message, client_id):
    packet = TransactionPacket.from_dict(message["packet"])
    route_document_uid = request.match_info["document_uid"]
    if packet.document_uid != route_document_uid:
        raise ValueError("packet document does not match WebSocket route")
    state = DocumentState.from_dict(message["state"])
    request.app[STORE_KEY].validate_packet_locks(packet, client_id)
    record = request.app[STORE_KEY].append(packet, state)
    payload = {
        "type": "accepted",
        "revision": record.revision,
        "parent_revision": record.parent_revision,
        "duplicate": record.duplicate,
        "packet": json.loads(record.packet_json),
        "state": {
            "definition_hash": record.definition_hash,
            "result_hash": record.result_hash,
        },
    }
    if record.duplicate:
        await socket.send_json(payload)
    else:
        await _broadcast(request.app, route_document_uid, payload)


async def _handle_state_report(request, socket, message, client_id: str):
    report = request.app[STORE_KEY].report_client_state(
        request.match_info["document_uid"],
        int(message["revision"]),
        client_id,
        DocumentState.from_dict(message["state"]),
    )
    await socket.send_json({"type": "state_report", **asdict(report)})


def _lock_payload(locks):
    return {"type": "lock_state", "locks": [asdict(lock) for lock in locks]}


async def _handle_locks(request, message, client_id):
    store = request.app[STORE_KEY]
    document_uid = request.match_info["document_uid"]
    action = message.get("type")
    if action == "acquire_locks":
        locks = store.acquire_locks(
            document_uid,
            message.get("object_uids", []),
            client_id,
            ttl_seconds=float(message.get("ttl_seconds", 120)),
        )
    elif action == "release_locks":
        locks = store.release_locks(
            document_uid, message.get("object_uids", []), client_id
        )
    else:
        raise ValueError(f"unsupported lock action: {action!r}")
    await _broadcast(request.app, document_uid, _lock_payload(locks))


async def websocket(request):
    document_uid = request.match_info["document_uid"]
    client_id = request.query.get("client_id", "anonymous")
    try:
        head = request.app[STORE_KEY].document_head(document_uid)
    except UnknownDocumentError as exc:
        raise web.HTTPNotFound(text=str(exc)) from exc

    socket = web.WebSocketResponse(heartbeat=30)
    await socket.prepare(request)
    clients = request.app[CLIENTS_KEY].setdefault(document_uid, set())
    clients.add(socket)
    await socket.send_json(
        {
            "type": "hello",
            "document_uid": document_uid,
            "client_id": client_id,
            "head_revision": head.revision,
            "environment_id": head.environment_id,
            "definition_hash": head.definition_hash,
            "result_hash": head.result_hash,
            "locks": [asdict(lock) for lock in request.app[STORE_KEY].list_locks(document_uid)],
        }
    )

    try:
        async for incoming in socket:
            if incoming.type != WSMsgType.TEXT:
                if incoming.type == WSMsgType.ERROR:
                    break
                continue
            try:
                message = json.loads(incoming.data)
                if message.get("type") == "submit":
                    await _handle_submit(request, socket, message, client_id)
                elif message.get("type") == "state_report":
                    await _handle_state_report(request, socket, message, client_id)
                elif message.get("type") in {"acquire_locks", "release_locks"}:
                    await _handle_locks(request, message, client_id)
                else:
                    raise ValueError(f"unsupported message type: {message.get('type')!r}")
            except (
                EnvironmentMismatchError,
                KeyError,
                ObjectLockConflictError,
                StaleRevisionError,
                UnknownDocumentError,
                ValueError,
            ) as exc:
                payload = _error_payload(exc)
                payload["request_type"] = message.get("type")
                await socket.send_json(payload)
    finally:
        clients.discard(socket)
        locks = request.app[STORE_KEY].release_client_locks(document_uid, client_id)
        await _broadcast(request.app, document_uid, _lock_payload(locks))
    return socket


def create_app(store: RevisionStore) -> web.Application:
    application = web.Application(client_max_size=256 * 1024**2)
    application[STORE_KEY] = store
    application[CLIENTS_KEY] = defaultdict(set)
    application.router.add_get("/health", health)
    application.router.add_post("/documents", register_document)
    application.router.add_get("/documents/{document_uid}/head", document_head)
    application.router.add_put("/documents/{document_uid}/checkpoint", put_checkpoint)
    application.router.add_get("/documents/{document_uid}/checkpoint", get_checkpoint)
    application.router.add_get("/documents/{document_uid}/revisions", revisions)
    application.router.add_get("/documents/{document_uid}/ws", websocket)
    return application


def run(database_path: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    store = RevisionStore(database_path)
    web.run_app(create_app(store), host=host, port=port)
