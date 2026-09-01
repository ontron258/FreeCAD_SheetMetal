"""Asynchronous HTTP/WebSocket client for the collaboration relay."""

from __future__ import annotations

from aiohttp import ClientSession


class RelayClient:
    def __init__(self, base_url: str, document_uid: str, client_id: str):
        self.base_url = base_url.rstrip("/")
        self.document_uid = document_uid
        self.client_id = client_id
        self.http = None
        self.websocket = None

    async def _session(self):
        if self.http is None:
            self.http = ClientSession()
        return self.http

    async def register_document(self, name, state, environment_id=""):
        http = await self._session()
        async with http.post(
            f"{self.base_url}/documents",
            json={
                "document_uid": self.document_uid,
                "name": name,
                "environment_id": environment_id,
                "state": state.to_dict(),
            },
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def upload_checkpoint(self, checkpoint):
        http = await self._session()
        async with http.put(
            f"{self.base_url}/documents/{self.document_uid}/checkpoint",
            data=bytes(checkpoint),
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def download_checkpoint(self):
        http = await self._session()
        async with http.get(
            f"{self.base_url}/documents/{self.document_uid}/checkpoint"
        ) as response:
            response.raise_for_status()
            return await response.read()

    async def connect(self):
        http = await self._session()
        self.websocket = await http.ws_connect(
            f"{self.base_url}/documents/{self.document_uid}/ws",
            params={"client_id": self.client_id},
        )
        return await self.websocket.receive_json()

    async def revisions_after(self, revision: int):
        http = await self._session()
        async with http.get(
            f"{self.base_url}/documents/{self.document_uid}/revisions",
            params={"after": revision},
        ) as response:
            response.raise_for_status()
            return await response.json()

    async def submit(self, packet, state):
        await self.websocket.send_json(
            {
                "type": "submit",
                "packet": packet.to_dict(),
                "state": state.to_dict(),
            }
        )

    async def report_state(self, revision: int, state):
        await self.websocket.send_json(
            {
                "type": "state_report",
                "revision": revision,
                "state": state.to_dict(),
            }
        )

    async def acquire_locks(self, object_uids, ttl_seconds=120):
        await self.websocket.send_json(
            {
                "type": "acquire_locks",
                "object_uids": list(object_uids),
                "ttl_seconds": ttl_seconds,
            }
        )

    async def release_locks(self, object_uids):
        await self.websocket.send_json(
            {"type": "release_locks", "object_uids": list(object_uids)}
        )

    async def receive(self):
        return await self.websocket.receive_json()

    async def close(self):
        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None
        if self.http is not None:
            await self.http.close()
            self.http = None
