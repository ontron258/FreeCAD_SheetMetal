"""End-to-end FreeCAD document sessions through the WebSocket relay."""

from __future__ import annotations

import os
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer
import FreeCAD as App

from freecad_collaboration import bootstrap_document
from freecad_collaboration.relay import create_app
from freecad_collaboration.revision_store import RevisionStore
from freecad_collaboration.session import DocumentSession


class DocumentSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.documents = []
        self.sessions = []
        handle, self.database_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.database_path)
        self.store = RevisionStore(self.database_path)
        self.http = TestClient(TestServer(create_app(self.store)))
        await self.http.start_server()
        self.base_url = str(self.http.make_url("/")).rstrip("/")

    async def asyncTearDown(self):
        for session in self.sessions:
            await session.close()
        await self.http.close()
        self.store.close()
        for document in reversed(self.documents):
            try:
                App.closeDocument(document.Name)
            except Exception:
                pass
        for suffix in ("", "-wal", "-shm"):
            path = self.database_path + suffix
            if os.path.exists(path):
                os.remove(path)

    def new_document(self, name):
        document = App.newDocument(name)
        document.UndoMode = 1
        self.documents.append(document)
        return document

    def session(self, document, client_id, document_uid=None):
        session = DocumentSession(
            document,
            self.base_url,
            client_id,
            environment_id="test-env",
            document_uid=document_uid,
        )
        self.sessions.append(session)
        return session

    async def test_two_freecad_documents_synchronize_through_relay(self):
        source = self.new_document("SessionSource")
        box = source.addObject("Part::Box", "Box")
        bootstrap_document(source)
        source.recompute()
        logical_uid = str(source.Uid)

        target = self.new_document("SessionTarget")
        target.restoreContent(source.dumpContent(9))
        late_target = self.new_document("LateSessionTarget")
        late_target.restoreContent(source.dumpContent(9))
        source_session = self.session(source, "source", logical_uid)
        target_session = self.session(target, "target", logical_uid)
        late_session = self.session(late_target, "late", logical_uid)
        await source_session.share()
        await target_session.connect()

        App.setActiveDocument(source.Name)
        source.openTransaction("Resize shared box")
        box.Length = 123.0
        source.recompute()
        source.commitTransaction()
        self.assertEqual(len(source_session.pending_local), 1)

        await source_session.submit_next()
        accepted_source = await source_session.handle_next()
        accepted_target = await target_session.handle_next()

        self.assertEqual(accepted_source["revision"], 1)
        self.assertEqual(accepted_target["revision"], 1)
        self.assertEqual(source_session.revision, 1)
        self.assertEqual(target_session.revision, 1)
        self.assertAlmostEqual(target.Box.Length, 123.0)

        source_report = await source_session.handle_next()
        target_report = await target_session.handle_next()
        self.assertTrue(source_report["definition_matches"])
        self.assertTrue(target_report["definition_matches"])
        self.assertTrue(target_report["result_matches"])

        await late_session.connect()
        self.assertEqual(late_session.revision, 1)
        self.assertAlmostEqual(late_target.Box.Length, 123.0)
        late_report = await late_session.handle_next()
        self.assertTrue(late_report["definition_matches"])
        self.assertTrue(late_report["result_matches"])


if __name__ == "__main__":
    unittest.main()
