"""Integration tests against bundled command-line FreeCAD."""

from __future__ import annotations

import unittest

import FreeCAD as App
import Part
import Sketcher

from freecad_collaboration import (
    TransactionPacket,
    TransactionRecorder,
    apply_packet,
    bootstrap_document,
    ensure_object_uid,
    get_object_uid,
)


class CollaborationTransactionTests(unittest.TestCase):
    def setUp(self):
        self.documents = []
        self.recorders = []

    def tearDown(self):
        for recorder in self.recorders:
            recorder.close()
        for document in reversed(self.documents):
            try:
                App.closeDocument(document.Name)
            except Exception:
                pass

    def new_document(self, name):
        document = App.newDocument(name)
        document.UndoMode = 1
        self.documents.append(document)
        return document

    def recorder(self, document):
        recorder = TransactionRecorder(document).install()
        self.recorders.append(recorder)
        return recorder

    def clone_document_objects(self, source, name):
        target = self.new_document(name)
        # FreeCAD changes a duplicate Document.Uid when both documents are open,
        # but preserves all object properties, including CollaborationUid.
        target.restoreContent(source.dumpContent(9))
        return target

    def test_bootstrap_assigns_unique_persistent_ids(self):
        document = self.new_document("IdentitySource")
        part = document.addObject("App::Part", "Part")
        sketch = document.addObject("PartDesign::Feature", "Feature")

        mapping = bootstrap_document(document)

        self.assertEqual(len(mapping), len(document.Objects))
        self.assertEqual(get_object_uid(part), str(part.Uid))
        self.assertIsNotNone(get_object_uid(sketch))
        self.assertNotEqual(get_object_uid(part), get_object_uid(sketch))

    def test_property_packet_round_trip_and_no_echo(self):
        source = self.new_document("PropertySource")
        box = source.addObject("Part::Box", "Box")
        bootstrap_document(source)
        target = self.clone_document_objects(source, "PropertyTarget")

        source_recorder = self.recorder(source)
        target_recorder = self.recorder(target)
        App.setActiveDocument(source.Name)
        source.openTransaction("Resize box")
        box.Length = 42.0
        box.Label = "Shared Box"
        source.commitTransaction()

        self.assertEqual(len(source_recorder.packets), 1)
        packet = TransactionPacket.from_json(source_recorder.packets[0].to_json())
        apply_packet(
            target,
            packet,
            recorder=target_recorder,
            validate_document_uid=False,
        )

        self.assertAlmostEqual(target.Box.Length, 42.0)
        self.assertEqual(target.Box.Label, "Shared Box")
        self.assertEqual(target_recorder.packets, [])

    def test_property_editor_transaction_omits_recomputed_outputs(self):
        source = self.new_document("PropertyEditorSource")
        feature = source.addObject("Part::FeaturePython", "Parameters")
        feature.addProperty("App::PropertyInteger", "Count", "Inputs")
        feature.addProperty("App::PropertyInteger", "Derived", "Outputs")
        feature.setEditorMode("Derived", 1)
        bootstrap_document(source)
        recorder = self.recorder(source)

        source.openTransaction("Edit Parameters.Count")
        feature.Count = 4
        feature.setEditorMode("Derived", 0)
        feature.Derived = 8
        feature.setEditorMode("Derived", 1)
        source.commitTransaction()

        self.assertEqual(len(recorder.packets), 1)
        operations = recorder.packets[0].operations
        self.assertEqual(
            [(operation.object_name, operation.property_name) for operation in operations],
            [("Parameters", "Count")],
        )

    def test_created_objects_and_forward_link_replay(self):
        source = self.new_document("CreateSource")
        bootstrap_document(source)
        target = self.clone_document_objects(source, "CreateTarget")
        recorder = self.recorder(source)

        App.setActiveDocument(source.Name)
        source.openTransaction("Create linked objects")
        first = source.addObject("App::FeaturePython", "First")
        first.addProperty("App::PropertyLength", "Length", "Parameters")
        first.addProperty("App::PropertyLink", "Target", "Links")
        second = source.addObject("App::FeaturePython", "Second")
        first.Length = 25.4
        first.Target = second
        source.commitTransaction()

        packet = recorder.packets[0]
        apply_packet(target, packet, validate_document_uid=False)

        self.assertIsNotNone(target.First)
        self.assertIsNotNone(target.Second)
        self.assertAlmostEqual(target.First.Length, 25.4)
        self.assertIs(target.First.Target, target.Second)
        self.assertEqual(get_object_uid(target.First), get_object_uid(first))
        self.assertEqual(get_object_uid(target.Second), get_object_uid(second))

    def test_dynamic_property_removal_and_object_deletion(self):
        source = self.new_document("DeleteSource")
        feature = source.addObject("App::FeaturePython", "Feature")
        feature.addProperty("App::PropertyString", "Temporary", "Test")
        feature.Temporary = "remove me"
        doomed = source.addObject("App::FeaturePython", "Doomed")
        bootstrap_document(source)
        target = self.clone_document_objects(source, "DeleteTarget")
        recorder = self.recorder(source)

        App.setActiveDocument(source.Name)
        source.openTransaction("Remove content")
        feature.removeProperty("Temporary")
        source.removeObject(doomed.Name)
        source.commitTransaction()

        # The peer may already have removed a generated/dependent object while
        # applying an earlier operation from the same packet.
        target.removeObject("Doomed")
        apply_packet(target, recorder.packets[0], validate_document_uid=False)

        self.assertNotIn("Temporary", target.Feature.PropertiesList)
        self.assertIsNone(target.getObject("Doomed"))

    def test_sketch_geometry_and_constraints_use_generic_property_payloads(self):
        source = self.new_document("SketchSource")
        sketch = source.addObject("Sketcher::SketchObject", "Sketch")
        bootstrap_document(source)
        target = self.clone_document_objects(source, "SketchTarget")
        recorder = self.recorder(source)

        App.setActiveDocument(source.Name)
        source.openTransaction("Edit sketch")
        line_index = sketch.addGeometry(
            Part.LineSegment(App.Vector(0, 0, 0), App.Vector(20, 0, 0)),
            False,
        )
        sketch.addConstraint(Sketcher.Constraint("Distance", line_index, 20.0))
        source.commitTransaction()

        packet = recorder.packets[0]
        apply_packet(target, packet, validate_document_uid=False)

        self.assertEqual(target.Sketch.GeometryCount, 1)
        self.assertEqual(len(target.Sketch.Constraints), 1)
        self.assertAlmostEqual(target.Sketch.Geometry[0].length(), 20.0)

    def test_uuid_link_replay_does_not_require_matching_internal_names(self):
        source = self.new_document("LinkSource")
        owner = source.addObject("App::FeaturePython", "Owner")
        linked = source.addObject("App::FeaturePython", "Linked")
        owner.addProperty("App::PropertyLink", "Target", "Links")
        owner.addProperty("App::PropertyLinkSubList", "SubTargets", "Links")
        bootstrap_document(source)

        target = self.new_document("LinkTarget")
        local_owner = target.addObject("App::FeaturePython", "LocalOwner")
        local_linked = target.addObject("App::FeaturePython", "LocalLinked")
        local_owner.addProperty("App::PropertyLink", "Target", "Links")
        local_owner.addProperty("App::PropertyLinkSubList", "SubTargets", "Links")
        ensure_object_uid(local_owner, preferred=get_object_uid(owner))
        ensure_object_uid(local_linked, preferred=get_object_uid(linked))
        recorder = self.recorder(source)

        App.setActiveDocument(source.Name)
        source.openTransaction("Set UUID links")
        owner.Target = linked
        owner.SubTargets = [(linked, ["Edge1", "Face2"])]
        source.commitTransaction()

        packet = recorder.packets[0]
        link_operations = [
            operation
            for operation in packet.operations
            if operation.property_name in {"Target", "SubTargets"}
        ]
        self.assertTrue(all(operation.structured_value for operation in link_operations))
        apply_packet(target, packet, validate_document_uid=False)

        self.assertIs(local_owner.Target, local_linked)
        self.assertIs(local_owner.SubTargets[0][0], local_linked)
        self.assertEqual(tuple(local_owner.SubTargets[0][1]), ("Edge1", "Face2"))

    def test_created_object_link_is_rebound_by_uuid(self):
        source = self.new_document("CreatedLinkSource")
        linked = source.addObject("App::FeaturePython", "Linked")
        bootstrap_document(source)

        target = self.new_document("CreatedLinkTarget")
        local_linked = target.addObject("App::FeaturePython", "LocalLinked")
        ensure_object_uid(local_linked, preferred=get_object_uid(linked))
        recorder = self.recorder(source)

        App.setActiveDocument(source.Name)
        source.openTransaction("Create linked owner")
        owner = source.addObject("App::FeaturePython", "Owner")
        owner.addProperty("App::PropertyLink", "Target", "Links")
        owner.Target = linked
        source.commitTransaction()

        apply_packet(target, recorder.packets[0], validate_document_uid=False)

        self.assertIs(target.Owner.Target, local_linked)

    def test_duplicate_uuid_is_repaired(self):
        document = self.new_document("DuplicateIdentity")
        first = document.addObject("App::FeaturePython", "First")
        second = document.addObject("App::FeaturePython", "Second")
        uid = ensure_object_uid(first)
        second.addProperty("App::PropertyUUID", "CollaborationUid", "Collaboration")
        second.CollaborationUid = uid

        repaired = ensure_object_uid(second)

        self.assertNotEqual(repaired, uid)


if __name__ == "__main__":
    unittest.main()
