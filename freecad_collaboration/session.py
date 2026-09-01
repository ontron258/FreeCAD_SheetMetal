"""Coordinate one FreeCAD document with a collaboration relay."""

from __future__ import annotations

from collections import deque

from .client import RelayClient
from .packet import TransactionPacket
from .recorder import TransactionRecorder
from .replay import apply_packet
from .state import document_state


class SessionError(RuntimeError):
    pass


class SessionConflictError(SessionError):
    pass


class DocumentSession:
    """Cooperative client session intended to run on FreeCAD's main thread.

    Network methods are asynchronous, but packet application remains explicit
    so a future GUI adapter can dispatch it through Qt's main event loop.
    """

    def __init__(
        self,
        document,
        base_url: str,
        client_id: str,
        *,
        environment_id: str = "",
        document_uid: str | None = None,
    ):
        self.document = document
        self.document_uid = document_uid or str(document.Uid)
        self.environment_id = environment_id
        self.revision = 0
        self.relay = RelayClient(base_url, self.document_uid, client_id)
        self.pending_local = deque()
        self.awaiting_acceptance = set()
        self.recorder = TransactionRecorder(
            document,
            base_revision=0,
            environment_id=environment_id,
            on_packet=self._record_local,
        ).install()

    def _record_local(self, packet):
        packet.document_uid = self.document_uid
        packet.base_revision = self.revision
        packet.environment_id = self.environment_id
        self.pending_local.append((packet, document_state(self.document)))

    async def share(self):
        await self.relay.register_document(
            self.document.Label,
            document_state(self.document),
            self.environment_id,
        )
        return await self.connect()

    async def connect(self):
        hello = await self.relay.connect()
        if hello.get("type") != "hello":
            raise SessionError(f"expected hello, received {hello!r}")
        if hello["environment_id"] and hello["environment_id"] != self.environment_id:
            raise SessionError(
                f"server environment {hello['environment_id']!r} does not match "
                f"client environment {self.environment_id!r}"
            )
        head_revision = int(hello["head_revision"])
        if head_revision:
            await self.catch_up(head_revision)
        else:
            local = document_state(self.document)
            if local.definition_hash != hello["definition_hash"]:
                raise SessionError("local checkpoint does not match server revision 0")
            self.revision = 0
            self.recorder.base_revision = 0
        return hello

    async def catch_up(self, expected_head=None):
        records = await self.relay.revisions_after(self.revision)
        for record in records:
            if int(record["parent_revision"]) != self.revision:
                raise SessionError("revision history is not contiguous")
            packet = TransactionPacket.from_dict(record["packet"])
            apply_packet(
                self.document,
                packet,
                recorder=self.recorder,
                validate_document_uid=False,
            )
            self.revision = int(record["revision"])
            self.recorder.base_revision = self.revision
        if expected_head is not None and self.revision != int(expected_head):
            raise SessionError(
                f"catch-up reached revision {self.revision}, expected {expected_head}"
            )
        if records:
            await self.relay.report_state(self.revision, document_state(self.document))
        return self.revision

    async def submit_next(self):
        if not self.pending_local:
            return None
        if self.awaiting_acceptance:
            raise SessionError("a local transaction is already awaiting acceptance")
        packet, state = self.pending_local.popleft()
        packet.base_revision = self.revision
        self.awaiting_acceptance.add(packet.transaction_uid)
        await self.relay.submit(packet, state)
        return packet.transaction_uid

    async def handle_next(self):
        message = await self.relay.receive()
        if message.get("type") != "accepted":
            return message

        revision = int(message["revision"])
        parent_revision = int(message["parent_revision"])
        packet = TransactionPacket.from_dict(message["packet"])
        if parent_revision != self.revision:
            raise SessionError(
                f"received revision {revision} with parent {parent_revision}; "
                f"local revision is {self.revision}"
            )

        if packet.transaction_uid in self.awaiting_acceptance:
            self.awaiting_acceptance.remove(packet.transaction_uid)
        else:
            if self.pending_local or self.awaiting_acceptance:
                raise SessionConflictError(
                    "remote edit arrived while local edits were pending"
                )
            apply_packet(
                self.document,
                packet,
                recorder=self.recorder,
                validate_document_uid=False,
            )

        self.revision = revision
        self.recorder.base_revision = revision
        await self.relay.report_state(revision, document_state(self.document))
        return message

    async def close(self):
        self.recorder.close()
        await self.relay.close()
