"""Qt-main-thread controller for an interactive FreeCAD document."""

from __future__ import annotations

from collections import deque
import time

from PySide import QtCore

from .conflicts import packets_conflict
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
        self.validation_status = "not validated"
        self.validation_revision = 0
        self.validation_error = ""
        self._deferred_validation = None
        self.locks = {}
        self.requested_locks = set()
        self._next_lock_refresh = time.monotonic() + 60
        self.pending_local = deque()
        self.awaiting_acceptance = set()
        self.inflight = None
        self.rebasing_local = []
        self.conflicted_local = []
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
        if self.status == "conflict":
            self.conflicted_local.append(packet)
            return
        self.pending_local.append((packet, document_state(self.document)))
        if self.status in {"connected", "synchronized"}:
            self._submit_next()

    def _submit_next(self):
        if self.awaiting_acceptance or not self.pending_local:
            return
        packet, state = self.pending_local.popleft()
        packet.base_revision = self.revision
        self.inflight = (packet, state)
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
                self._deferred_validation = message.get("validation")
                self._set_status("catching_up")
                self.transport.request_revisions(self.revision)
            else:
                self._replace_validation(message.get("validation"))
                local = document_state(self.document)
                if local.definition_hash != message["definition_hash"]:
                    raise RuntimeError("local checkpoint does not match the server")
                self._set_status("connected")
                self._submit_next()
            return
        if message_type == "lock_state":
            self._replace_locks(message.get("locks", []))
            return
        if message_type == "validation_state":
            self._replace_validation(message)
            return
        if message_type == "catch_up":
            self._apply_records(message["records"])
            self._replace_validation(self._deferred_validation)
            self._deferred_validation = None
            self.recorder.base_revision = self.revision
            self.transport.report_state(self.revision, document_state(self.document))
            self._set_status("synchronized")
            self._submit_next()
            return
        if message_type == "rebase_catch_up":
            self._finish_rebase(message["records"])
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
        if (
            message_type == "error"
            and message.get("request_type") == "submit"
            and message.get("error") == "StaleRevisionError"
        ):
            if self.status != "rebasing":
                self._begin_rebase()
            return
        if message_type in {"error", "transport_error"}:
            raise RuntimeError(message.get("message", message_type))

    def _apply_records(self, records):
        packets = []
        for record in records:
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
            self.validation_revision = self.revision
            self.validation_status = "pending"
            self.validation_error = ""
            packets.append(packet)
        return packets

    def _begin_rebase(self):
        local = []
        if self.inflight is not None:
            local.append(self.inflight[0])
        local.extend(packet for packet, _state in self.pending_local)
        if not local:
            self.transport.request_revisions(self.revision)
            return
        with self.recorder.suspended():
            for _packet in reversed(local):
                self.document.undo()
        self.awaiting_acceptance.clear()
        self.inflight = None
        self.pending_local.clear()
        self.rebasing_local = local
        self._set_status("rebasing")
        self.transport.request_revisions(
            self.revision, message_type="rebase_catch_up"
        )

    def _finish_rebase(self, records):
        remote_packets = self._apply_records(records)
        self.recorder.base_revision = self.revision
        local_packets = self.rebasing_local
        self.rebasing_local = []
        conflict = packets_conflict(local_packets, remote_packets)
        replayed = []
        try:
            for packet in local_packets:
                apply_packet(
                    self.document,
                    packet,
                    recorder=self.recorder,
                    validate_document_uid=False,
                )
                replayed.append((packet, document_state(self.document)))
        except Exception:
            conflict = True
        if conflict:
            self.conflicted_local = [packet for packet, _state in replayed]
            self.last_error = "local edits overlap a newer server revision"
            self._set_status("conflict")
            return
        self.pending_local.extend(replayed)
        self._set_status("synchronized")
        self._submit_next()

    def discard_local_conflict(self):
        if not self.conflicted_local:
            return
        with self.recorder.suspended():
            for _packet in reversed(self.conflicted_local):
                self.document.undo()
        self.conflicted_local = []
        self.last_error = ""
        self.transport.report_state(self.revision, document_state(self.document))
        self._set_status("validating")

    def _replace_locks(self, locks):
        self.locks = {lock["object_uid"]: lock for lock in locks}

    def _replace_validation(self, validation):
        if not validation:
            return
        revision = int(validation["revision"])
        if revision < self.validation_revision:
            return
        self.validation_revision = revision
        self.validation_error = validation.get("error", "")
        self.validation_status = "valid" if validation.get("valid") else "invalid"

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
        if self.status == "rebasing":
            # The HTTP catch-up request will apply this accepted revision in
            # sequence; the broadcast may race the stale-submit error.
            return
        revision = int(message["revision"])
        parent_revision = int(message["parent_revision"])
        if parent_revision != self.revision:
            raise RuntimeError("accepted revision does not follow the local revision")
        packet = TransactionPacket.from_dict(message["packet"])
        if packet.transaction_uid in self.awaiting_acceptance:
            self.awaiting_acceptance.remove(packet.transaction_uid)
            self.inflight = None
        else:
            if self.pending_local or self.awaiting_acceptance:
                self._begin_rebase()
                return
            apply_packet(
                self.document,
                packet,
                recorder=self.recorder,
                validate_document_uid=False,
            )
        self.revision = revision
        self.validation_revision = revision
        self.validation_status = "pending"
        self.validation_error = ""
        self.recorder.base_revision = revision
        if self.pending_local:
            self._set_status("synchronized")
            self._submit_next()
        else:
            self.transport.report_state(revision, document_state(self.document))
            self._set_status("validating")

    def close(self):
        self.timer.stop()
        self.recorder.close()
        self.transport.close()
        self._set_status("disconnected")
