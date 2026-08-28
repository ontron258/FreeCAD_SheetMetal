# -*- coding: utf-8 -*-

import math
import os
import tempfile
import unittest

import FreeCAD as App
import Part
import Sketcher  # noqa: F401 - registers Sketcher document object types

from SheetMetalBoltConnectionCmd import (
    SMBoltConnection,
    SMBoltConnectionCut,
    create_bolted_connection,
    cut_locator_records,
    locator_candidates,
    locator_frames,
    make_hole_cutter,
    round_clearance_diameter,
    set_connection_type,
    square_clearance_width,
)
from SheetMetalShapedFlangeCmd import addSheetMetalPartProperties


def _sheet_part(doc, name, z_offset):
    part = doc.addObject("App::Part", name)
    part.Label = name
    addSheetMetalPartProperties(part)
    part.Placement.Base.z = z_offset
    base = doc.addObject("Part::FeaturePython", name + "Base")
    base.Shape = Part.makeBox(30.0, 30.0, 2.0)
    part.addObject(base)
    part.Tip = base.Name
    return part, base


def _locator_sketch(doc):
    sketch = doc.addObject("Sketcher::SketchObject", "BoltLocators")
    sketch.addGeometry(Part.Point(App.Vector(10.0, 10.0, 0.0)), False)
    sketch.addGeometry(
        Part.Circle(App.Vector(20.0, 20.0, 0.0), App.Vector(0.0, 0.0, 1.0), 1.0),
        False,
    )
    return sketch


def _body_sheet_part(doc, name, z_offset):
    part = doc.addObject("App::Part", name)
    part.Label = name
    addSheetMetalPartProperties(part)
    part.Placement.Base.z = z_offset
    body = doc.addObject("PartDesign::Body", name + "Body")
    part.addObject(body)
    base = body.newObject("PartDesign::Feature", name + "Base")
    base.Shape = Part.makeBox(30.0, 30.0, 2.0)
    body.Tip = base
    part.Tip = base.Name
    return part, body, base


class TestBoltConnection(unittest.TestCase):
    def test_clearance_tables_match_reference_feature(self):
        self.assertAlmostEqual(round_clearance_diameter("5/16 in", "Normal"), 0.344 * 25.4)
        self.assertAlmostEqual(square_clearance_width("5/16 in", "Normal"), 0.354 * 25.4)
        self.assertAlmostEqual(round_clearance_diameter("3/4 in", "Oversize"), 0.875 * 25.4)

    def test_profile_cutters_are_valid_solids(self):
        center = App.Vector(0.0, 0.0, 0.0)
        x_axis = App.Vector(1.0, 0.0, 0.0)
        z_axis = App.Vector(0.0, 0.0, 1.0)
        for hole_type in ("Round", "Square", "Slotted Round", "Slotted Square"):
            cutter = make_hole_cutter(
                hole_type, center, x_axis, z_axis, 8.0, 18.0, 20.0
            )
            self.assertTrue(cutter.isValid(), hole_type)
            self.assertEqual(len(cutter.Solids), 1, hole_type)

        round_cutter = make_hole_cutter(
            "Round", center, x_axis, z_axis, 8.0, 18.0, 20.0
        )
        self.assertAlmostEqual(round_cutter.Volume, math.pi * 4.0 * 4.0 * 20.0, places=5)

        auto_slot = make_hole_cutter(
            "Slotted Square", center, x_axis, z_axis, 8.0, 0.0, 20.0
        )
        square = make_hole_cutter(
            "Square", center, x_axis, z_axis, 8.0, 0.0, 20.0
        )
        self.assertAlmostEqual(auto_slot.Volume, square.Volume, places=7)

    def test_connection_cuts_two_placed_parts_with_independent_profiles(self):
        doc = App.newDocument("BoltConnectionPlacedParts")
        try:
            part_a, base_a = _sheet_part(doc, "PartA", 0.0)
            part_b, base_b = _sheet_part(doc, "PartB", 6.0)
            sketch = _locator_sketch(doc)
            part_a.addObject(sketch)

            connection, cuts = create_bolted_connection(
                doc, [(sketch, [])], [part_a, part_b], "Carriage Bolt"
            )
            cuts[1].HoleType = "Slotted Round"
            cuts[1].SlotLength = 16.0
            cuts[1].Rotation = 90.0
            doc.recompute()

            self.assertEqual(connection.BoltCount, 2)
            self.assertEqual(str(cuts[0].HoleType), "Square")
            self.assertEqual(str(cuts[1].HoleType), "Slotted Round")
            self.assertTrue(cuts[0].Shape.isValid())
            self.assertTrue(cuts[1].Shape.isValid())
            self.assertLess(cuts[0].Shape.Volume, base_a.Shape.Volume)
            self.assertLess(cuts[1].Shape.Volume, base_b.Shape.Volume)
            self.assertGreater(cuts[0].RemovedVolume.Value, 0.0)
            self.assertGreater(cuts[1].RemovedVolume.Value, 0.0)
            self.assertEqual(part_a.Tip, cuts[0].Name)
            self.assertEqual(part_b.Tip, cuts[1].Name)
            self.assertEqual(connection.ParticipantNames, [part_a.Name, part_b.Name])
            self.assertIn("Metadata only", connection.HardwareState)
        finally:
            App.closeDocument(doc.Name)

    def test_connection_and_cut_properties_upgrade_saved_objects(self):
        doc = App.newDocument("BoltConnectionPropertyUpgrade")
        try:
            connection = doc.addObject("App::FeaturePython", "BoltConnection")
            SMBoltConnection(connection)
            cut = doc.addObject("Part::FeaturePython", "BoltConnectionCut")
            SMBoltConnectionCut(cut, connection=connection)
            self.assertEqual(connection.SheetMetalType, "BoltConnection")
            self.assertEqual(cut.SheetMetalType, "BoltConnectionCut")
            self.assertTrue(cut.UseConnectionFit)
            self.assertEqual(str(cut.HoleType), "Round")
            self.assertAlmostEqual(cut.SlotLength.Value, 0.0)
            self.assertEqual(connection.LocatorCount, 0)
            self.assertEqual(connection.AuxiliaryHoleCount, 0)
        finally:
            App.closeDocument(doc.Name)

    def test_body_tip_overrides_stale_part_tip_after_native_feature(self):
        doc = App.newDocument("BoltConnectionNativeBodyTip")
        try:
            part_a, body_a, base_a = _body_sheet_part(
                doc, "NativeFeaturePart", 0.0
            )
            native_feature = body_a.newObject(
                "PartDesign::Feature", "NativeFlange"
            )
            native_feature.Shape = base_a.Shape.fuse(
                Part.makeBox(10.0, 10.0, 2.0, App.Vector(30.0, 0.0, 0.0))
            )
            body_a.Tip = native_feature
            part_a.Tip = base_a.Name
            part_b, _body_b, _base_b = _body_sheet_part(
                doc, "AdjacentPart", 6.0
            )
            sketch = _locator_sketch(doc)

            _connection, cuts = create_bolted_connection(
                doc, [(sketch, [])], [part_a, part_b]
            )
            doc.recompute()

            self.assertIs(cuts[0].PreviousFeature, native_feature)
            self.assertIs(cuts[0].BaseFeature, native_feature)
            self.assertAlmostEqual(cuts[0].Shape.BoundBox.XMax, 40.0)
            self.assertTrue(cuts[0].Shape.isValid())
        finally:
            App.closeDocument(doc.Name)

    def test_disjoint_participants_and_auxiliary_holes_need_no_common_part(self):
        doc = App.newDocument("BoltConnectionDisjointParticipants")
        try:
            part_a, base_a = _sheet_part(doc, "LeftPart", 0.0)
            part_b, base_b = _sheet_part(doc, "RightPart", 0.0)
            unused, unused_base = _sheet_part(doc, "FuturePatternPart", 0.0)
            base_a.Shape = Part.makeBox(20.0, 20.0, 2.0)
            base_b.Shape = Part.makeBox(
                20.0, 20.0, 2.0, App.Vector(20.0, 0.0, 0.0)
            )
            unused_base.Shape = Part.makeBox(
                20.0, 20.0, 2.0, App.Vector(100.0, 0.0, 0.0)
            )
            sketch = doc.addObject("Sketcher::SketchObject", "SplitLocators")
            sketch.addGeometry(Part.Point(App.Vector(5.0, 10.0, 0.0)), False)
            sketch.addGeometry(Part.Point(App.Vector(25.0, 10.0, 0.0)), False)

            connection, cuts = create_bolted_connection(
                doc, [(sketch, [])], [part_a, part_b, unused]
            )
            connection.HoleOnlyLocatorKeys = ["{}:1".format(sketch.Name)]
            doc.recompute()

            self.assertEqual(connection.CommonParticipantNames, [])
            self.assertEqual(connection.LocatorCount, 2)
            self.assertEqual(connection.BoltCount, 1)
            self.assertEqual(connection.AuxiliaryHoleCount, 1)
            self.assertEqual(cuts[0].LocatorKeys, ["{}:0".format(sketch.Name)])
            self.assertEqual(cuts[1].LocatorKeys, ["{}:1".format(sketch.Name)])
            self.assertEqual(cuts[2].LocatorKeys, [])
            self.assertEqual(cuts[2].LastError, "")
            self.assertAlmostEqual(cuts[2].RemovedVolume.Value, 0.0)
            self.assertAlmostEqual(cuts[2].Shape.Volume, unused_base.Shape.Volume)
            self.assertAlmostEqual(
                cuts[2].Shape.BoundBox.XMin, unused_base.Shape.BoundBox.XMin
            )
            self.assertAlmostEqual(
                cuts[2].Shape.BoundBox.XMax, unused_base.Shape.BoundBox.XMax
            )
        finally:
            App.closeDocument(doc.Name)

    def test_locator_label_matches_sketch_vertex_name(self):
        doc = App.newDocument("BoltLocatorDisplayName")
        try:
            sketch = doc.addObject("Sketcher::SketchObject", "DisplayLocators")
            sketch.addGeometry(
                Part.LineSegment(App.Vector(0.0, 0.0), App.Vector(10.0, 0.0)),
                False,
            )
            sketch.addGeometry(Part.Point(App.Vector(5.0, 5.0, 0.0)), False)
            connection = doc.addObject("App::FeaturePython", "DisplayConnection")
            SMBoltConnection(connection, [(sketch, [])])
            doc.recompute()

            records = locator_candidates(connection)
            self.assertEqual(len(records), 1)
            self.assertTrue(records[0]["subelement"].startswith("Vertex"))
            self.assertIn(
                "{}.{}".format(sketch.Name, records[0]["subelement"]),
                records[0]["label"],
            )
        finally:
            App.closeDocument(doc.Name)

    def test_connection_uses_part_design_features_inside_bodies(self):
        doc = App.newDocument("BoltConnectionBodies")
        try:
            part_a, body_a, _base_a = _body_sheet_part(doc, "BodyPartA", 0.0)
            part_b, body_b, _base_b = _body_sheet_part(doc, "BodyPartB", 6.0)
            sketch = _locator_sketch(doc)

            connection, cuts = create_bolted_connection(
                doc, [(sketch, [])], [part_a, part_b]
            )
            doc.recompute()

            self.assertEqual(connection.BoltCount, 2)
            self.assertEqual(cuts[0].TypeId, "PartDesign::FeaturePython")
            self.assertEqual(cuts[1].TypeId, "PartDesign::FeaturePython")
            self.assertIs(cuts[0].getParentGeoFeatureGroup(), body_a)
            self.assertIs(cuts[1].getParentGeoFeatureGroup(), body_b)
            self.assertIs(body_a.Tip, cuts[0])
            self.assertIs(body_b.Tip, cuts[1])
            self.assertTrue(cuts[0].Shape.isValid())
            self.assertTrue(cuts[1].Shape.isValid())
            self.assertNotIn("Connection", cuts[0].PropertiesList)
            self.assertEqual(cuts[0].ConnectionName, connection.Name)
        finally:
            App.closeDocument(doc.Name)

    def test_whole_sketch_locators_can_be_filtered_individually(self):
        doc = App.newDocument("BoltLocatorFilter")
        try:
            part_a, _base_a = _sheet_part(doc, "FilterPartA", 0.0)
            part_b, _base_b = _sheet_part(doc, "FilterPartB", 6.0)
            sketch = _locator_sketch(doc)
            sketch.addGeometry(
                Part.Circle(
                    App.Vector(15.0, 15.0, 0.0),
                    App.Vector(0.0, 0.0, 1.0),
                    1.0,
                ),
                True,
            )
            connection, _cuts = create_bolted_connection(
                doc, [(sketch, [])], [part_a, part_b]
            )
            doc.recompute()
            self.assertEqual(connection.BoltCount, 2)

            connection.LocatorGeometryFilter = ["{}:1".format(sketch.Name)]
            doc.recompute()
            self.assertEqual(connection.BoltCount, 1)
            self.assertTrue(
                locator_frames(connection)[0]["point"].isEqual(
                    App.Vector(20.0, 20.0, 0.0), 1.0e-9
                )
            )
        finally:
            App.closeDocument(doc.Name)

    def test_switching_to_carriage_bolt_defaults_head_side_to_square(self):
        doc = App.newDocument("CarriageBoltDefault")
        try:
            part_a, _base_a = _sheet_part(doc, "CarriagePartA", 0.0)
            part_b, _base_b = _sheet_part(doc, "CarriagePartB", 6.0)
            sketch = _locator_sketch(doc)
            connection, cuts = create_bolted_connection(
                doc, [(sketch, [])], [part_a, part_b]
            )
            self.assertEqual(str(cuts[0].HoleType), "Round")
            cuts[1].HoleType = "Slotted Square"
            set_connection_type(connection, "Carriage Bolt")
            doc.recompute()
            self.assertEqual(str(cuts[0].HoleType), "Square")
            self.assertEqual(str(cuts[1].HoleType), "Slotted Square")

            set_connection_type(connection, "Hex Bolt")
            cuts[1].HoleType = "Slotted Round"
            set_connection_type(connection, "Carriage Bolt")
            doc.recompute()
            self.assertEqual(str(cuts[0].HoleType), "Square")
            self.assertEqual(str(cuts[1].HoleType), "Square")
        finally:
            App.closeDocument(doc.Name)

    def test_nearest_sheet_layer_does_not_cut_opposite_side_of_folded_part(self):
        doc = App.newDocument("BoltNearestLayer")
        try:
            part_a, base_a = _sheet_part(doc, "FoldedPart", 0.0)
            near = Part.makeBox(30.0, 30.0, 2.0)
            bridge = Part.makeBox(2.0, 30.0, 18.0, App.Vector(0.0, 0.0, 2.0))
            far = Part.makeBox(30.0, 30.0, 2.0, App.Vector(0.0, 0.0, 20.0))
            base_a.Shape = near.fuse(bridge).fuse(far)
            part_b, _base_b = _sheet_part(doc, "AdjacentPart", -2.0)
            sketch = doc.addObject("Sketcher::SketchObject", "LayerLocator")
            sketch.addGeometry(Part.Point(App.Vector(15.0, 15.0, 0.0)), False)

            _connection, cuts = create_bolted_connection(
                doc, [(sketch, [])], [part_a, part_b]
            )
            doc.recompute()
            local_removed = cuts[0].RemovedVolume.Value
            diameter = round_clearance_diameter("5/16 in", "Normal")
            expected_one_layer = math.pi * (diameter * 0.5) ** 2 * 2.0
            self.assertAlmostEqual(local_removed, expected_one_layer, places=4)

            cuts[0].CutExtent = "Through Entire Part"
            doc.recompute()
            self.assertAlmostEqual(
                cuts[0].RemovedVolume.Value, expected_one_layer * 2.0, places=4
            )
        finally:
            App.closeDocument(doc.Name)

    def test_one_connection_set_assigns_locator_subsets_to_target_parts(self):
        doc = App.newDocument("BoltConnectionSet")
        try:
            common, common_base = _sheet_part(doc, "CommonRail", 0.0)
            target_a, base_a = _sheet_part(doc, "TargetA", 6.0)
            target_b, base_b = _sheet_part(doc, "TargetB", 6.0)
            target_c, base_c = _sheet_part(doc, "TargetC", 6.0)
            common_base.Shape = Part.makeBox(60.0, 20.0, 2.0)
            base_a.Shape = Part.makeBox(20.0, 20.0, 2.0)
            base_b.Shape = Part.makeBox(
                20.0, 20.0, 2.0, App.Vector(20.0, 0.0, 0.0)
            )
            base_c.Shape = Part.makeBox(
                20.0, 20.0, 2.0, App.Vector(40.0, 0.0, 0.0)
            )
            sketch = doc.addObject("Sketcher::SketchObject", "SetLocators")
            for x_value in (5.0, 15.0, 25.0, 35.0, 45.0, 55.0):
                sketch.addGeometry(
                    Part.Point(App.Vector(x_value, 10.0, 0.0)), False
                )

            connection, cuts = create_bolted_connection(
                doc,
                [(sketch, [])],
                [common, target_a, target_b, target_c],
                "Carriage Bolt",
            )
            doc.recompute()

            self.assertEqual(connection.BoltCount, 6)
            self.assertEqual(connection.CommonParticipantNames, [common.Name])
            self.assertTrue(cuts[0].UseAllLocators)
            self.assertEqual(len(cut_locator_records(cuts[0])), 6)
            self.assertEqual(str(cuts[0].HoleType), "Square")
            for index, cut in enumerate(cuts[1:]):
                self.assertFalse(cut.UseAllLocators)
                self.assertEqual(
                    cut.LocatorKeys,
                    [
                        "{}:{}".format(sketch.Name, index * 2),
                        "{}:{}".format(sketch.Name, index * 2 + 1),
                    ],
                )
                self.assertEqual(len(cut_locator_records(cut)), 2)
                self.assertEqual(str(cut.HoleType), "Square")
                self.assertEqual(str(cut.Role), "Nut Side")
                self.assertEqual(cut.LastError, "")
                self.assertGreater(cut.RemovedVolume.Value, 0.0)
            self.assertEqual(str(cuts[0].Role), "Head Side")
            self.assertTrue(connection.RoleDefaultsApplied)
        finally:
            App.closeDocument(doc.Name)

    def test_selected_sketch_vertex_resolves_through_nested_placements(self):
        doc = App.newDocument("BoltLocatorPlacement")
        try:
            container = doc.addObject("App::Part", "LocatorContainer")
            container.Placement.Base = App.Vector(100.0, 0.0, 0.0)
            sketch = doc.addObject("Sketcher::SketchObject", "PlacedLocators")
            sketch.addGeometry(Part.Point(App.Vector(1.0, 2.0, 0.0)), False)
            sketch.Placement.Base = App.Vector(10.0, 20.0, 30.0)
            container.addObject(sketch)
            connection = doc.addObject("App::FeaturePython", "PlacedConnection")
            SMBoltConnection(connection, [(sketch, ["Vertex1"])])
            doc.recompute()

            frames = locator_frames(connection)
            self.assertEqual(len(frames), 1)
            self.assertTrue(
                frames[0]["point"].isEqual(App.Vector(111.0, 22.0, 30.0), 1.0e-9)
            )
        finally:
            App.closeDocument(doc.Name)

    def test_connection_survives_save_and_reopen(self):
        doc = App.newDocument("BoltConnectionPersistence")
        path = os.path.join(tempfile.gettempdir(), "BoltConnectionPersistence.FCStd")
        try:
            part_a, _base_a = _sheet_part(doc, "SavedPartA", 0.0)
            part_b, _base_b = _sheet_part(doc, "SavedPartB", 6.0)
            sketch = _locator_sketch(doc)
            connection, cuts = create_bolted_connection(
                doc, [(sketch, [])], [part_a, part_b], "Carriage Bolt"
            )
            cuts[1].HoleType = "Slotted Square"
            cuts[1].Rotation = 30.0
            doc.recompute()
            connection_name = connection.Name
            cut_name = cuts[1].Name
            doc.saveAs(path)
            App.closeDocument(doc.Name)

            reopened = App.openDocument(path)
            reopened.recompute()
            restored_connection = reopened.getObject(connection_name)
            restored_cut = reopened.getObject(cut_name)
            self.assertEqual(restored_connection.BoltCount, 2)
            self.assertEqual(str(restored_connection.ConnectionType), "Carriage Bolt")
            self.assertEqual(str(restored_cut.HoleType), "Slotted Square")
            self.assertAlmostEqual(restored_cut.Rotation.Value, 30.0)
            self.assertTrue(restored_cut.Shape.isValid())
            self.assertGreater(restored_cut.RemovedVolume.Value, 0.0)
        finally:
            if App.ActiveDocument is not None:
                App.closeDocument(App.ActiveDocument.Name)
            if os.path.exists(path):
                os.remove(path)

    def test_cut_result_can_be_unfolded_with_holes_preserved(self):
        try:
            from SheetMetalUnfoldCmd import SMUnfold
        except ImportError as error:
            self.skipTest("Unfold dependencies are unavailable: {}".format(error))
        doc = App.newDocument("BoltConnectionUnfold")
        try:
            part_a, _base_a = _sheet_part(doc, "UnfoldPartA", 0.0)
            part_b, _base_b = _sheet_part(doc, "UnfoldPartB", 6.0)
            sketch = _locator_sketch(doc)
            _connection, cuts = create_bolted_connection(
                doc, [(sketch, [])], [part_a, part_b]
            )
            doc.recompute()

            cut = cuts[0]
            face_index = max(
                range(len(cut.Shape.Faces)),
                key=lambda index: cut.Shape.Faces[index].Area,
            )
            unfold = doc.addObject("Part::FeaturePython", "ConnectionUnfold")
            SMUnfold(unfold, cut, ["Face{}".format(face_index + 1)])
            doc.recompute()

            self.assertFalse(unfold.Shape.isNull())
            self.assertTrue(unfold.Shape.isValid())
            self.assertAlmostEqual(unfold.Shape.Volume, cut.Shape.Volume, places=5)
            self.assertEqual(len(unfold.Shape.Solids), 1)
        finally:
            App.closeDocument(doc.Name)


if __name__ == "__main__":
    unittest.main()
