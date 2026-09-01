"""Revision sequencing and deterministic document-state tests."""

from __future__ import annotations

import os
import tempfile
import unittest

import FreeCAD as App
import Part

from freecad_collaboration import (
    CheckpointConflictError,
    EnvironmentMismatchError,
    ObjectLockConflictError,
    RevisionStore,
    StaleRevisionError,
    TransactionPacket,
    TransactionRecorder,
    apply_packet,
    bootstrap_document,
    document_state,
)


class RevisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.documents = []
        self.recorders = []
        handle, self.database_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        os.unlink(self.database_path)

    def tearDown(self):
        for recorder in self.recorders:
            recorder.close()
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

    def recorder(self, document, *, base_revision=0, environment_id="test-env"):
        recorder = TransactionRecorder(
            document,
            base_revision=base_revision,
            environment_id=environment_id,
        ).install()
        self.recorders.append(recorder)
        return recorder

    def clone_document_objects(self, source, name):
        target = self.new_document(name)
        target.restoreContent(source.dumpContent(9))
        return target

    def test_state_hash_is_repeatable_and_clone_independent(self):
        source = self.new_document("HashSource")
        box = source.addObject("Part::Box", "Box")
        box.Length = 12.5
        bootstrap_document(source)
        source.recompute()
        target = self.clone_document_objects(source, "HashTarget")

        first = document_state(source)
        second = document_state(source)
        cloned = document_state(target)

        self.assertEqual(first, second)
        self.assertEqual(first.definition_hash, cloned.definition_hash)
        self.assertEqual(first.result_hash, cloned.result_hash)
        target.Box.Visibility = not target.Box.Visibility
        self.assertEqual(first, document_state(target))

    def test_state_hash_accepts_null_shape_result_property(self):
        document = self.new_document("NullShapeHash")
        document.addObject("PartDesign::Feature", "Bend")
        bootstrap_document(document)

        state = document_state(document)

        self.assertEqual(state.object_count, len(document.Objects))
        self.assertGreater(state.result_property_count, 0)

    def test_state_hash_accepts_compound_without_center_of_mass(self):
        document = self.new_document("CompoundHash")
        feature = document.addObject("PartDesign::Feature", "SketchLikeResult")
        feature.Shape = Part.makeCompound(
            [Part.makeLine(App.Vector(0, 0, 0), App.Vector(10, 0, 0))]
        )
        bootstrap_document(document)

        state = document_state(document)

        self.assertEqual(state.object_count, len(document.Objects))
        self.assertGreater(state.result_property_count, 0)

    def test_revision_zero_checkpoint_is_immutable(self):
        source = self.new_document("CheckpointSource")
        source.addObject("Part::Box", "Box")
        bootstrap_document(source)
        source.recompute()
        document_uid = str(source.Uid)
        checkpoint = bytes(source.dumpContent(9))

        with RevisionStore(self.database_path) as store:
            store.register_document(document_uid, source.Label, document_state(source))
            self.assertTrue(store.store_checkpoint(document_uid, checkpoint))
            self.assertFalse(store.store_checkpoint(document_uid, checkpoint))
            self.assertEqual(store.checkpoint(document_uid), checkpoint)
            self.assertTrue(store.document_head(document_uid).has_checkpoint)
            with self.assertRaises(CheckpointConflictError):
                store.store_checkpoint(document_uid, b"different")

    def test_object_locks_reject_another_clients_packet(self):
        source = self.new_document("LockSource")
        box = source.addObject("Part::Box", "Box")
        bootstrap_document(source)
        source.recompute()
        recorder = self.recorder(source)
        document_uid = str(source.Uid)

        App.setActiveDocument(source.Name)
        source.openTransaction("Locked edit")
        box.Length = 45
        source.recompute()
        source.commitTransaction()
        packet = recorder.packets[-1]
        object_uid = packet.operations[0].object_uid

        with RevisionStore(self.database_path) as store:
            store.register_document(document_uid, source.Label, document_state(source))
            locks = store.acquire_locks(document_uid, [object_uid], "first")
            self.assertEqual(locks[0].client_id, "first")
            store.validate_packet_locks(packet, "first")
            with self.assertRaises(ObjectLockConflictError):
                store.validate_packet_locks(packet, "second")
            with self.assertRaises(ObjectLockConflictError):
                store.acquire_locks(document_uid, [object_uid], "second")
            self.assertEqual(store.release_locks(document_uid, [object_uid], "first"), [])
            store.validate_packet_locks(packet, "second")

    def test_revision_acceptance_replay_and_divergence_reporting(self):
        source = self.new_document("RevisionSource")
        box = source.addObject("Part::Box", "Box")
        bootstrap_document(source)
        source.recompute()
        target = self.clone_document_objects(source, "RevisionTarget")
        recorder = self.recorder(source)
        document_uid = str(source.Uid)

        with RevisionStore(self.database_path) as store:
            store.register_document(
                document_uid,
                source.Label,
                document_state(source),
                environment_id="test-env",
            )

            App.setActiveDocument(source.Name)
            source.openTransaction("Resize")
            box.Length = 75
            source.recompute()
            source.commitTransaction()

            packet = recorder.packets[0]
            accepted = store.append(packet, document_state(source))
            self.assertEqual(accepted.revision, 1)
            self.assertEqual(store.head_revision(document_uid), 1)
            jobs = store.pending_validation_jobs()
            self.assertEqual([(job.document_uid, job.revision) for job in jobs], [(document_uid, 1)])

            validation = store.report_validation(
                document_uid,
                1,
                "headless-worker",
                "test-env",
                document_state(source),
            )
            self.assertTrue(validation.valid)
            self.assertEqual(store.pending_validation_jobs(), [])

            duplicate = store.append(packet, document_state(source))
            self.assertTrue(duplicate.duplicate)
            self.assertEqual(duplicate.revision, 1)
            self.assertEqual(len(store.list_revisions(document_uid)), 1)

            apply_packet(target, packet, validate_document_uid=False)
            report = store.report_client_state(
                document_uid,
                1,
                "target-client",
                document_state(target),
            )
            self.assertTrue(report.definition_matches)
            self.assertTrue(report.result_matches)

            target.Box.Width = 99
            target.recompute()
            divergent = store.report_client_state(
                document_uid,
                1,
                "target-client",
                document_state(target),
            )
            self.assertFalse(divergent.definition_matches)

    def test_provisional_revision_is_finalized_by_headless_validation(self):
        source = self.new_document("ProvisionalSource")
        box = source.addObject("Part::Box", "Box")
        bootstrap_document(source)
        source.recompute()
        recorder = self.recorder(source)
        document_uid = str(source.Uid)

        with RevisionStore(self.database_path) as store:
            store.register_document(
                document_uid,
                source.Label,
                document_state(source),
                environment_id="test-env",
            )
            source.openTransaction("Resize provisionally")
            box.Length = 88
            source.recompute()
            source.commitTransaction()

            accepted = store.append(recorder.packets[0])
            self.assertEqual(accepted.definition_hash, "")
            self.assertEqual(accepted.result_hash, "")
            self.assertEqual(store.document_head(document_uid).definition_hash, "")

            expected = document_state(source)
            validation = store.report_validation(
                document_uid,
                1,
                "headless-worker",
                "test-env",
                expected,
            )
            self.assertTrue(validation.valid)
            finalized = store.document_head(document_uid)
            self.assertEqual(finalized.definition_hash, expected.definition_hash)
            self.assertEqual(finalized.result_hash, expected.result_hash)

    def test_stale_and_wrong_environment_packets_are_rejected(self):
        source = self.new_document("ConflictSource")
        box = source.addObject("Part::Box", "Box")
        bootstrap_document(source)
        source.recompute()
        recorder = self.recorder(source)
        document_uid = str(source.Uid)

        with RevisionStore(self.database_path) as store:
            store.register_document(
                document_uid,
                source.Label,
                document_state(source),
                environment_id="test-env",
            )
            App.setActiveDocument(source.Name)
            source.openTransaction("First")
            box.Length = 30
            source.recompute()
            source.commitTransaction()
            first = recorder.packets[-1]
            store.append(first, document_state(source))

            source.openTransaction("Stale")
            box.Width = 40
            source.recompute()
            source.commitTransaction()
            stale = recorder.packets[-1]
            with self.assertRaises(StaleRevisionError):
                store.append(stale, document_state(source))

            wrong_environment = TransactionPacket.from_dict(stale.to_dict())
            wrong_environment.base_revision = 1
            wrong_environment.environment_id = "different-env"
            with self.assertRaises(EnvironmentMismatchError):
                store.append(wrong_environment, document_state(source))


if __name__ == "__main__":
    unittest.main()
