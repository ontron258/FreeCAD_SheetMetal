"""Two-client WebSocket relay tests."""

from __future__ import annotations

import os
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer

from freecad_collaboration.packet import Operation, TransactionPacket
from freecad_collaboration.relay import create_app
from freecad_collaboration.revision_store import RevisionStore
from freecad_collaboration.state import DocumentState


def state(definition="definition-0", result="result-0"):
    return DocumentState(definition, result, 1, 1, 1)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handle, self.database_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.database_path)
        self.store = RevisionStore(self.database_path)
        self.document_uid = "11111111-1111-1111-1111-111111111111"
        self.store.register_document(
            self.document_uid,
            "Relay Test",
            state(),
            environment_id="test-env",
        )
        self.client = TestClient(TestServer(create_app(self.store)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            path = self.database_path + suffix
            if os.path.exists(path):
                os.remove(path)

    async def connect(self, client_id):
        socket = await self.client.ws_connect(
            f"/documents/{self.document_uid}/ws?client_id={client_id}"
        )
        hello = await socket.receive_json()
        self.assertEqual(hello["type"], "hello")
        self.assertEqual(hello["head_revision"], 0)
        return socket

    def packet(self, *, base_revision=0, environment_id="test-env"):
        return TransactionPacket(
            document_uid=self.document_uid,
            name="Set length",
            base_revision=base_revision,
            environment_id=environment_id,
            operations=[
                Operation(
                    kind="set_property",
                    object_uid="22222222-2222-2222-2222-222222222222",
                    object_name="Box",
                    property_name="Length",
                    property_type="App::PropertyLength",
                    after_content="payload",
                )
            ],
        )

    async def test_packet_is_sequenced_and_broadcast_to_two_clients(self):
        first = await self.connect("first")
        second = await self.connect("second")
        packet = self.packet()

        await first.send_json(
            {
                "type": "submit",
                "packet": packet.to_dict(),
                "state": state("definition-1", "result-1").to_dict(),
            }
        )
        first_message = await first.receive_json()
        second_message = await second.receive_json()

        self.assertEqual(first_message["type"], "accepted")
        self.assertEqual(second_message["type"], "accepted")
        self.assertEqual(first_message["revision"], 1)
        self.assertEqual(second_message["packet"]["transaction_uid"], packet.transaction_uid)

        response = await self.client.get(
            f"/documents/{self.document_uid}/revisions?after=0"
        )
        revisions = await response.json()
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["revision"], 1)

        await second.send_json(
            {
                "type": "state_report",
                "revision": 1,
                "state": state("definition-1", "result-1").to_dict(),
            }
        )
        report = await second.receive_json()
        self.assertEqual(report["type"], "state_report")
        self.assertTrue(report["definition_matches"])
        self.assertTrue(report["result_matches"])

        await first.close()
        await second.close()

    async def test_stale_packet_returns_structured_error(self):
        first = await self.connect("first")
        packet = self.packet(base_revision=-1)
        await first.send_json(
            {
                "type": "submit",
                "packet": packet.to_dict(),
                "state": state("definition-x", "result-x").to_dict(),
            }
        )

        error = await first.receive_json()
        self.assertEqual(error["type"], "error")
        self.assertEqual(error["error"], "StaleRevisionError")
        self.assertEqual(error["head_revision"], 0)
        await first.close()

    async def test_feature_lock_blocks_another_clients_packet(self):
        first = await self.connect("first")
        second = await self.connect("second")
        object_uid = "22222222-2222-2222-2222-222222222222"

        await first.send_json(
            {"type": "acquire_locks", "object_uids": [object_uid]}
        )
        first_lock_state = await first.receive_json()
        second_lock_state = await second.receive_json()
        self.assertEqual(first_lock_state["type"], "lock_state")
        self.assertEqual(second_lock_state["locks"][0]["client_id"], "first")

        await second.send_json(
            {
                "type": "submit",
                "packet": self.packet().to_dict(),
                "state": state("blocked", "blocked").to_dict(),
            }
        )
        error = await second.receive_json()
        self.assertEqual(error["error"], "ObjectLockConflictError")
        self.assertEqual(error["request_type"], "submit")

        await first.send_json(
            {"type": "release_locks", "object_uids": [object_uid]}
        )
        self.assertEqual((await first.receive_json())["locks"], [])
        self.assertEqual((await second.receive_json())["locks"], [])
        await first.close()
        await second.close()

    async def test_document_can_be_registered_over_http(self):
        document_uid = "33333333-3333-3333-3333-333333333333"
        response = await self.client.post(
            "/documents",
            json={
                "document_uid": document_uid,
                "name": "New shared model",
                "environment_id": "test-env",
                "state": state().to_dict(),
            },
        )
        self.assertEqual(response.status, 200)
        registered = await response.json()
        self.assertEqual(registered["document_uid"], document_uid)
        self.assertEqual(registered["revision"], 0)

        response = await self.client.get(f"/documents/{document_uid}/head")
        head = await response.json()
        self.assertEqual(head["name"], "New shared model")
        self.assertFalse(head["has_checkpoint"])

        checkpoint = b"PK\x03\x04freecad-checkpoint"
        response = await self.client.put(
            f"/documents/{document_uid}/checkpoint", data=checkpoint
        )
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["stored"])

        response = await self.client.get(f"/documents/{document_uid}/checkpoint")
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), checkpoint)


if __name__ == "__main__":
    unittest.main()
