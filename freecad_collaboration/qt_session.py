"""Qt-main-thread controller for an interactive FreeCAD document."""

from __future__ import annotations

from collections import deque
import time

from PySide import QtCore

from .packet import TransactionPacket
from .recorder import TransactionRecorder
from .replay import apply_packet
from .state import document_state
from .threaded_transport import ThreadedRelayTransport


class QtDocumentSession(QtCore.QObject):
    """Synchronize a FreeCAD document while keeping mutations on the Qt thread."""

    statusChanged = QtCore.Signal(str)
    messageReceived = QtCore.Signal(object)

    def __init__(
        self,
        document,
        base_url: str,
        client_id: str,
        *,
        environment_id: str = "",
        document_uid: str | None = None,
        poll_interval_ms: int = 20,
        parent=None,
    ):
        super().__init__(parent)
        self.document = document
        self.document_uid = document_uid or str(document.Uid)
        self.environment_id = environment_id
        self.revision = 0
        self.status = "disconnected"
        self.last_error = ""
        self.locks = {}
        self.requested_locks = set()
        self._next_lock_refresh = time.monotonic() + 60
        self.pending_local = deque()
        self.awaiting_acceptance = set()
        self.transport = ThreadedRelayTransport(base_url, self.document_uid, client_id)
        self.recorder = TransactionRecorder(
            document,
            base_revision=0,
            environment_id=environment_id,
            on_packet=self._record_local,
        ).install()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(poll_interval_ms)
        self.timer.timeout.connect(self.poll)

    def _set_status(self, status):
        if self.status == status:
            return
        self.status = status
        self.statusChanged.emit(status)

    def start_share(self):
        self._set_status("connecting")
        self.transport.start(
            register=(
                self.document.Label,
                document_state(self.document),
                self.environment_id,
                bytes(self.document.dumpContent(9)),
            )
        )
        self.timer.start()

    def start_join(self, *, download_checkpoint=False):
        self._set_status("connecting")
        self.transport.start(download_checkpoint=download_checkpoint)
        self.timer.start()

    def _record_local(self, packet):
        packet.document_uid = self.document_uid
        packet.base_revision = self.revision
        packet.environment_id = self.environment_id
        self.pending_local.append((packet, document_state(self.document)))
        if self.status in {"connected", "synchronized"}:
            self._submit_next()

    def _submit_next(self):
        if self.awaiting_acceptance or not self.pending_local:
            return
        packet, state = self.pending_local.popleft()
        packet.base_revision = self.revision
        self.awaiting_acceptance.add(packet.transaction_uid)
        self.transport.submit(packet, state)
        self._set_status("submitting")

    @QtCore.Slot()
    def poll(self):
        if self.requested_locks and time.monotonic() >= self._next_lock_refresh:
            self.transport.acquire_locks(self.requested_locks)
            self._next_lock_refresh = time.monotonic() + 60
        while True:
            message = self.transport.poll()
            if message is None:
                return
            try:
                self._handle_message(message)
            except Exception as exc:
                self.last_error = str(exc)
                self._set_status("error")
            self.messageReceived.emit(message)

    def _handle_message(self, message):
        message_type = message.get("type")
        if message_type == "checkpoint":
            if self.document.Objects:
                raise RuntimeError("checkpoint download requires an empty document")
            with self.recorder.suspended():
                self.document.restoreContent(bytearray(message["content"]))
                self.document.recompute()
            return
        if message_type == "hello":
            self._replace_locks(message.get("locks", []))
            if message.get("environment_id") and message["environment_id"] != self.environment_id:
                raise RuntimeError("client and server environments do not match")
            head = int(message["head_revision"])
            if head > self.revision:
                self._set_status("catching_up")
                self.transport.request_revisions(self.revision)
            else:
                local = document_state(self.document)
                if local.definition_hash != message["definition_hash"]:
                    raise RuntimeError("local checkpoint does not match the server")
                self._set_status("connected")
                self._submit_next()
            return
        if message_type == "lock_state":
            self._replace_locks(message.get("locks", []))
            return
        if message_type == "catch_up":
            for record in message["records"]:
                if int(record["parent_revision"]) != self.revision:
                    raise RuntimeError("non-contiguous revision history")
                packet = TransactionPacket.from_dict(record["packet"])
                apply_packet(
                    self.document,
                    packet,
                    recorder=self.recorder,
                    validate_document_uid=False,
                )
                self.revision = int(record["revision"])
            self.recorder.base_revision = self.revision
            self.transport.report_state(self.revision, document_state(self.document))
            self._set_status("synchronized")
            self._submit_next()
            return
        if message_type == "accepted":
            self._handle_accepted(message)
            return
        if message_type == "state_report":
            if message.get("definition_matches") and message.get("result_matches"):
                self._set_status("synchronized")
            else:
                raise RuntimeError("local document diverged from the accepted revision")
            return
        if message_type == "error" and message.get("request_type") in {
            "acquire_locks",
            "release_locks",
        }:
            self.last_error = message.get("message", message_type)
            return
        if message_type in {"error", "transport_error"}:
            raise RuntimeError(message.get("message", message_type))

    def _replace_locks(self, locks):
        self.locks = {lock["object_uid"]: lock for lock in locks}

    def acquire_locks(self, object_uids, ttl_seconds=120):
        object_uids = {str(uid) for uid in object_uids if uid}
        self.requested_locks.update(object_uids)
        self._next_lock_refresh = time.monotonic() + 60
        self.transport.acquire_locks(object_uids, ttl_seconds=ttl_seconds)

    def release_locks(self, object_uids):
        object_uids = {str(uid) for uid in object_uids if uid}
        self.requested_locks.difference_update(object_uids)
        self.transport.release_locks(object_uids)

    def _handle_accepted(self, message):
        revision = int(message["revision"])
        parent_revision = int(message["parent_revision"])
        if parent_revision != self.revision:
            raise RuntimeError("accepted revision does not follow the local revision")
        packet = TransactionPacket.from_dict(message["packet"])
        if packet.transaction_uid in self.awaiting_acceptance:
            self.awaiting_acceptance.remove(packet.transaction_uid)
        else:
            if self.pending_local or self.awaiting_acceptance:
                raise RuntimeError("remote edit conflicts with pending local edits")
            apply_packet(
                self.document,
                packet,
                recorder=self.recorder,
                validate_document_uid=False,
            )
        self.revision = revision
        self.recorder.base_revision = revision
        self.transport.report_state(revision, document_state(self.document))
        self._set_status("validating")

    def close(self):
        self.timer.stop()
        self.recorder.close()
        self.transport.close()
        self._set_status("disconnected")
