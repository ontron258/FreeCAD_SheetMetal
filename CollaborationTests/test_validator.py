"""Authoritative headless validation-worker integration test."""

from __future__ import annotations

import os
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer
import FreeCAD as App

from freecad_collaboration import TransactionRecorder, bootstrap_document, document_state
from freecad_collaboration.relay import create_app
from freecad_collaboration.revision_store import RevisionStore
from freecad_collaboration.validator import validate_pending


class ValidatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.database_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.database_path)
        self.store = RevisionStore(self.database_path)
        self.client = TestClient(TestServer(create_app(self.store)))
        await self.client.start_server()
        self.document = App.newDocument("ValidatorSource")
        self.box = self.document.addObject("Part::Box", "Box")
        bootstrap_document(self.document)
        self.document.recompute()
        self.recorder = TransactionRecorder(
            self.document, environment_id="validator-test"
        ).install()

    async def asyncTearDown(self):
        self.recorder.close()
        App.closeDocument(self.document.Name)
        await self.client.close()
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            path = self.database_path + suffix
            if os.path.exists(path):
                os.remove(path)

    async def test_worker_reconstructs_and_validates_revision(self):
        document_uid = str(self.document.Uid)
        response = await self.client.post(
            "/documents",
            json={
                "document_uid": document_uid,
                "name": self.document.Label,
                "environment_id": "validator-test",
                "state": document_state(self.document).to_dict(),
            },
        )
        self.assertEqual(response.status, 200)
        response = await self.client.put(
            f"/documents/{document_uid}/checkpoint",
            data=bytes(self.document.dumpContent(9)),
        )
        self.assertEqual(response.status, 200)

        socket = await self.client.ws_connect(
            f"/documents/{document_uid}/ws?client_id=validator-source"
        )
        self.assertEqual((await socket.receive_json())["type"], "hello")
        self.document.openTransaction("Validated resize")
        self.box.Length = 73
        self.document.recompute()
        self.document.commitTransaction()
        packet = self.recorder.packets[-1]
        await socket.send_json(
            {
                "type": "submit",
                "packet": packet.to_dict(),
                "state": document_state(self.document).to_dict(),
            }
        )
        self.assertEqual((await socket.receive_json())["type"], "accepted")

        reports = await validate_pending(
            str(self.client.make_url("/")).rstrip("/"),
            "test-worker",
            "validator-test",
        )
        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0]["valid"])
        broadcast = await socket.receive_json()
        self.assertEqual(broadcast["type"], "validation_state")
        self.assertTrue(broadcast["definition_matches"])
        self.assertTrue(broadcast["result_matches"])
        await socket.close()


if __name__ == "__main__":
    unittest.main()
