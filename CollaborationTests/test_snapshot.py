"""Snapshot recovery, baseline replay, and optional Git metadata regression tests."""
import asyncio
import base64
import tempfile
import unittest
import uuid
from unittest.mock import patch
from pathlib import Path

import FreeCAD as App
from aiohttp.test_utils import TestClient, TestServer

from freecad_collaboration.checkpoint import native_checkpoint_bytes, open_checkpoint
from freecad_collaboration.identity import bootstrap_document
from freecad_collaboration.environment import build_environment_lock, environment_lock_id
from freecad_collaboration.relay import create_app
from freecad_collaboration.revision_store import RevisionStore
from freecad_collaboration.snapshot import publish_snapshot
from freecad_collaboration.state import document_state
from freecad_collaboration.recorder import TransactionRecorder
from freecad_collaboration.validator import validate_pending


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = RevisionStore(str(Path(self.temp.name) / "store.sqlite3"))
        self.client = TestClient(TestServer(create_app(self.store)))
        await self.client.start_server()
        self.doc = App.newDocument("SnapshotTest")
        self.box = self.doc.addObject("Part::Box", "Box")
        bootstrap_document(self.doc)
        self.doc.recompute()

    async def asyncTearDown(self):
        App.closeDocument(self.doc.Name)
        await self.client.close()
        self.store.close()
        self.temp.cleanup()

    async def test_publish_preserves_old_history_and_new_baseline_validates(self):
        old_uid = str(uuid.uuid4())
        self.store.create_snapshot(old_uid, "Old", document_state(self.doc),
                                   native_checkpoint_bytes(self.doc), environment_id="test")
        old_checkpoint = self.store.checkpoint(old_uid)
        self.box.Length = 42
        url = str(self.client.make_url("/")).rstrip("/")
        head = await asyncio.to_thread(publish_snapshot, self.doc, url, "test")
        new_uid = head["document_uid"]
        self.assertNotEqual(new_uid, old_uid)
        self.assertEqual(head["revision"], 0)
        self.assertEqual(self.store.checkpoint(old_uid), old_checkpoint)
        reopened = open_checkpoint(self.store.checkpoint(new_uid))
        try:
            self.assertEqual(reopened.getObject("Box").Length.Value, 42)
            self.assertEqual(document_state(reopened), document_state(self.doc))
        finally:
            App.closeDocument(reopened.Name)
        recorder = TransactionRecorder(self.doc, environment_id="test").install()
        try:
            self.doc.openTransaction("Resize after checkpoint")
            self.box.Length = 51
            self.doc.recompute()
            self.doc.commitTransaction()
            packet = recorder.packets[-1]
            packet.document_uid = new_uid
            self.store.append(packet, document_state(self.doc))
            reports = await validate_pending(url, "worker", "test")
            self.assertTrue(reports[0]["valid"], reports)
        finally:
            recorder.close()

    async def test_snapshot_collision_never_overwrites(self):
        uid = str(uuid.uuid4())
        payload = {"document_uid": uid, "name": "Snapshot", "environment_id": "test",
                   "state": document_state(self.doc).to_dict(),
                   "checkpoint": base64.b64encode(native_checkpoint_bytes(self.doc)).decode()}
        self.assertEqual((await self.client.post("/snapshots", json=payload)).status, 201)
        original = self.store.checkpoint(uid)
        self.assertEqual((await self.client.post("/snapshots", json=payload)).status, 409)
        self.assertEqual(self.store.checkpoint(uid), original)
        payload["document_uid"] = str(uuid.uuid4())
        payload["checkpoint"] = "invalid"
        self.assertEqual((await self.client.post("/snapshots", json=payload)).status, 400)
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)

    def test_git_availability_does_not_change_compatibility(self):
        lock = build_environment_lock()
        identity = environment_lock_id(lock)
        lock["addons"][0]["git_commit"] = ""
        self.assertEqual(environment_lock_id(lock), identity)
        with patch("freecad_collaboration.environment._git_output", return_value=""):
            self.assertEqual(environment_lock_id(build_environment_lock()), identity)

    def test_failed_upload_keeps_existing_session(self):
        from freecad_collaboration.gui import CollaborationController
        from unittest.mock import Mock
        from contextlib import nullcontext
        controller = CollaborationController()
        session = Mock()
        session.recorder.suspended.return_value = nullcontext()
        controller.sessions[self.doc.Name] = session
        try:
            with patch("freecad_collaboration.gui.publish_snapshot", side_effect=RuntimeError("offline")):
                with self.assertRaisesRegex(RuntimeError, "offline"):
                    controller.save_current_checkpoint(self.doc, "http://offline", "client", "test")
            self.assertIs(controller.session_for(self.doc), session)
            session.close.assert_not_called()
        finally:
            controller.sessions.clear()
            App.removeDocumentObserver(controller)


if __name__ == "__main__":
    unittest.main()
