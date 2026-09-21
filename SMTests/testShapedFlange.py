# -*- coding: utf-8 -*-

import math
import os
import tempfile
import unittest
from unittest import mock

import FreeCAD as App
import Part
import SheetMetalShapedFlangeCmd as ShapedFlangeModule

from SheetMetalShapedFlangeCmd import (
    FACE_GEOMETRY_VERSION,
    SMShapedFlange,
    _adopt_profiles,
    _part_tip_feature,
    _SheetMetalPartDefaultsObserver,
    addSheetMetalPartProperties,
    createSheetMetalPart,
    makeShapedFlange,
    makeShapedFlangeStages,
    migrateDocumentFaceGeometry,
    upgradeSheetMetalPart,
)


class _SketchStub:
    def __init__(self, label, wire, placement=None, global_placement=None):
        self.Name = label.replace(" ", "_")
        self.Label = label
        self.Shape = wire
        self.Placement = placement or App.Placement()
        self._global_placement = global_placement or self.Placement

    def getGlobalPlacement(self):
        return self._global_placement


def _panel(label, points, placement=None):
    return _SketchStub(label, Part.makePolygon(points + [points[0]]), placement)


def _multi_loop_sketch(label, loops):
    wires = [Part.makePolygon(points + [points[0]]) for points in loops]
    return _SketchStub(label, Part.makeCompound(wires))


class TestShapedFlange(unittest.TestCase):
    @staticmethod
    def positioned_panels(angle=90, height=30):
        base = _panel("Base", [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
                              App.Vector(20, 20, 0), App.Vector(0, 20, 0)])
        rotation = App.Rotation(App.Vector(1, 0, 0), angle)
        wall = _panel("Wall", [rotation.multVec(p) for p in
                              (App.Vector(0, 0, 0), App.Vector(20, 0, 0),
                               App.Vector(20, height, 0), App.Vector(0, height, 0))],
                      App.Placement(App.Vector(), rotation))
        return base, wall

    def test_wall_positions_match_make_wall_solids(self):
        from SheetMetalCmd import smBend
        for radius, thickness in ((3, 2), (1.5, 0.8)):
            base, wall = self.positioned_panels(height=10 + radius + thickness)
            carrier = Part.makeBox(20, 20, thickness)
            edge = next("Edge" + str(i) for i, e in enumerate(carrier.Edges, 1)
                        if all(abs(v.Point.y) < 1e-7 and abs(v.Point.z - thickness) < 1e-7
                               for v in e.Vertexes))
            for mode, offset in (("Material Outside", 0), ("Material Inside", 0),
                                 ("Thickness Outside", 0), ("Offset", 3), ("Offset", -3)):
                with self.subTest(radius=radius, thickness=thickness, mode=mode, offset=offset):
                    shape = makeShapedFlangeStages(
                        [{"sketches": [base], "radius": radius, "thickness_side": "Normal"},
                         {"sketches": [wall], "radius": radius, "wall_position": mode, "offset": offset}],
                        thickness=thickness,
                    )
                    expected, _ = smBend(thk=thickness, bendR=radius, bendA=90, extLen=10,
                                         selFaceNames=[edge], MainObject=carrier,
                                         BendType=mode, offset=offset)
                    self.assertTrue(shape.isValid())
                    self.assertEqual(1, len(shape.Solids))
                    self.assertLess(shape.cut(expected).Volume, 1e-6)
                    self.assertLess(expected.cut(shape).Volume, 1e-6)

    def test_positioned_oblique_bends_unfold_with_constant_thickness(self):
        from SheetMetalNewUnfolder import BendAllowanceCalculator, unfold
        for angle in (55, 125):
            base, wall = self.positioned_panels(angle)
            for mode in ("Material Outside", "Material Inside", "Thickness Outside", "Offset"):
                with self.subTest(angle=angle, mode=mode):
                    shape = makeShapedFlangeStages(
                        [{"sketches": [base], "radius": 3, "thickness_side": "Normal"},
                         {"sketches": [wall], "radius": 3, "wall_position": mode, "offset": 2}],
                        thickness=2,
                    )
                    self.assertTrue(shape.isValid())
                    self.assertEqual(1, len(shape.Solids))
                    radii = sorted(round(f.Surface.Radius, 6) for f in shape.Faces
                                   if isinstance(f.Surface, Part.Cylinder))
                    self.assertEqual([3, 5], radii)
                    root = max((i for i, f in enumerate(shape.Faces)
                                if isinstance(f.Surface, Part.Plane) and f.normalAt(0, 0).z > 0.999),
                               key=lambda i: shape.Faces[i].Area)
                    edges, bends = unfold(shape, root, BendAllowanceCalculator.from_single_value(0.5, "ansi"))
                    self.assertEqual(1, len(bends))
                    self.assertTrue(edges)

    def test_later_sketch_plane_wall_follows_its_positioned_parent(self):
        base, wall = self.positioned_panels(height=20)
        top = _panel("Return", [App.Vector(0, 0, 20), App.Vector(20, 0, 20),
                                App.Vector(20, 12, 20), App.Vector(0, 12, 20)])
        stages = [{"sketches": [base], "radius": 3, "thickness_side": "Normal"},
                  {"sketches": [wall], "radius": 3}, {"sketches": [top], "radius": 3}]
        original = makeShapedFlangeStages(stages, thickness=2)
        stages[1]["wall_position"] = "Material Outside"
        positioned = makeShapedFlangeStages(stages, thickness=2)
        self.assertTrue(positioned.isValid())
        self.assertEqual(1, len(positioned.Solids))
        self.assertTrue(original.isInside(App.Vector(10, 11, 20), 1e-7, True))
        self.assertFalse(positioned.isInside(App.Vector(10, 11, 20), 1e-7, True))
        self.assertTrue(positioned.isInside(App.Vector(10, 7, 20), 1e-7, True))

    def test_outward_offset_retains_shaped_bend_ends_and_reliefs(self):
        base, _wall = self.positioned_panels()
        wall = _panel("Tapered", [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
                                  App.Vector(17, 0, 12), App.Vector(3, 0, 12)],
                      App.Placement(App.Vector(), App.Rotation(App.Vector(1, 0, 0), 90)))
        for relief in ("None", "Rectangle", "Round", "Tear"):
            with self.subTest(relief=relief):
                result = makeShapedFlangeStages(
                    [{"sketches": [base], "radius": 2, "thickness_side": "Normal"},
                     {"sketches": [wall], "radius": 2, "wall_position": "Offset", "offset": 2,
                      "relief_type": relief, "relief_width": 1, "relief_depth": 1}], thickness=1,
                )
                self.assertTrue(result.isValid())
                self.assertEqual(1, len(result.Solids))

    def test_wall_position_properties_recompute_and_survive_reopening(self):
        doc = App.newDocument("FaceWallPosition")
        try:
            part = createSheetMetalPart(doc)
            part.UseMaterialCatalog = False
            part.Thickness = 2
            part.DefaultBendRadius = 3
            profiles = []
            for index, stub in enumerate(self.positioned_panels()):
                profile = doc.addObject("Part::Feature", stub.Name + "Profile")
                depth = 20 if index == 0 else 30
                profile.Shape = Part.makePolygon(
                    [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
                     App.Vector(20, depth, 0), App.Vector(0, depth, 0), App.Vector()]
                )
                profile.Placement = stub.Placement
                part.addObject(profile)
                profiles.append(profile)
            first = doc.addObject("Part::FeaturePython", "BaseFace")
            SMShapedFlange(first, [profiles[0]], part)
            first.ThicknessSide = "Normal"
            part.addObject(first)
            second = doc.addObject("Part::FeaturePython", "WallFace")
            SMShapedFlange(second, [profiles[1]], part, first)
            part.addObject(second)
            part.Tip = second.Name
            doc.recompute()
            self.assertEqual("Sketch plane", second.WallPosition)
            for mode, offset, y_min in (("Material Outside", 0, -5),
                                        ("Thickness Outside", 0, -2),
                                        ("Material Inside", 0, 0),
                                        ("Offset", 3, -8), ("Offset", -1.25, -3.75)):
                second.WallPosition = mode
                second.BendOffset = offset
                doc.recompute()
                self.assertTrue(second.Shape.isValid())
                self.assertAlmostEqual(y_min, second.Shape.BoundBox.YMin)
            part.DefaultBendRadius = 4
            part.Thickness = 3
            doc.recompute()
            self.assertAlmostEqual(-5.75, second.Shape.BoundBox.YMin)
            second.BendOffset = "-0.125 in"
            doc.recompute()
            self.assertAlmostEqual(-3.175, second.BendOffset.Value)
            self.assertAlmostEqual(-3.825, second.Shape.BoundBox.YMin)
            volume = second.Shape.Volume
            with tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "PositionedFace.FCStd")
                doc.saveAs(path)
                App.closeDocument(doc.Name)
                doc = App.openDocument(path)
                self.assertIsInstance(doc.WallFace.Proxy, SMShapedFlange)
                doc.WallFace.touch()
                doc.recompute()
                self.assertEqual("Offset", doc.WallFace.WallPosition)
                self.assertAlmostEqual(-3.175, doc.WallFace.BendOffset.Value)
                self.assertTrue(doc.WallFace.Shape.isValid())
                self.assertAlmostEqual(volume, doc.WallFace.Shape.Volume)
        finally:
            App.closeDocument(doc.Name)

    def test_deleted_face_tip_retreats_to_previous_feature(self):
        doc = App.newDocument("ShapedFlangeDeletedTip")
        try:
            part = createSheetMetalPart(doc)
            first = doc.addObject("Part::FeaturePython", "ShapedFlange")
            first.addProperty("App::PropertyString", "SheetMetalType")
            first.SheetMetalType = "Face"
            first.addProperty("App::PropertyLink", "PreviousFeature")
            part.addObject(first)
            second = doc.addObject("Part::FeaturePython", "ShapedFlange")
            second.addProperty("App::PropertyString", "SheetMetalType")
            second.SheetMetalType = "Face"
            second.addProperty("App::PropertyLink", "PreviousFeature")
            second.PreviousFeature = first
            part.addObject(second)
            part.Tip = second.Name

            _SheetMetalPartDefaultsObserver().slotDeletedObject(second)

            self.assertEqual(part.Tip, first.Name)
            self.assertIs(_part_tip_feature(part), first)
        finally:
            App.closeDocument(doc.Name)

    def test_stale_face_tip_recovers_surviving_chain(self):
        doc = App.newDocument("ShapedFlangeStaleTip")
        try:
            part = createSheetMetalPart(doc)
            feature = doc.addObject("Part::FeaturePython", "ShapedFlange")
            feature.addProperty("App::PropertyString", "SheetMetalType")
            feature.SheetMetalType = "Face"
            feature.addProperty("App::PropertyLink", "PreviousFeature")
            part.addObject(feature)
            part.Tip = "DeletedShapedFlange"

            recovered = _part_tip_feature(part)

            self.assertIs(recovered, feature)
            self.assertEqual(part.Tip, feature.Name)
        finally:
            App.closeDocument(doc.Name)

    def test_saved_face_geometry_version_triggers_rebuild_migration(self):
        doc = App.newDocument("ShapedFlangeGeometryMigration")
        try:
            part = createSheetMetalPart(doc)
            profile = doc.addObject("Part::Feature", "Profile")
            profile.Shape = Part.makePolygon(
                [App.Vector(0, 0, 0), App.Vector(10, 0, 0),
                 App.Vector(10, 10, 0), App.Vector(0, 10, 0),
                 App.Vector(0, 0, 0)]
            )
            part.addObject(profile)
            feature = doc.addObject("Part::FeaturePython", "ShapedFlange")
            SMShapedFlange(feature, [profile], part)
            part.addObject(feature)
            part.Tip = feature.Name
            doc.recompute()
            self.assertEqual(
                feature.FaceGeometryVersion, FACE_GEOMETRY_VERSION
            )

            feature.removeProperty("FaceGeometryVersion")
            migrated = migrateDocumentFaceGeometry(doc)

            self.assertEqual(migrated, [feature])
            self.assertIn("Touched", feature.State)
            doc.recompute()
            self.assertEqual(
                feature.FaceGeometryVersion, FACE_GEOMETRY_VERSION
            )
            self.assertTrue(feature.Shape.isValid())
        finally:
            App.closeDocument(doc.Name)

    def test_saved_relief_enums_gain_tear_without_changing_selection(self):
        doc = App.newDocument("ShapedFlangeReliefEnumMigration")
        try:
            sheet_part = doc.addObject("App::Part", "SheetMetalPart")
            sheet_part.addProperty(
                "App::PropertyEnumeration", "DefaultReliefType", "Sheet Metal"
            )
            sheet_part.DefaultReliefType = ["None", "Rectangle", "Round"]
            sheet_part.DefaultReliefType = "Round"

            addSheetMetalPartProperties(sheet_part)

            self.assertEqual(str(sheet_part.DefaultReliefType), "Round")
            self.assertEqual(
                sheet_part.getEnumerationsOfProperty("DefaultReliefType"),
                ["None", "Tear", "Rectangle", "Round"],
            )
        finally:
            App.closeDocument(doc.Name)

    def test_sheet_metal_part_is_an_app_part_and_adopts_profiles(self):
        doc = App.newDocument("ShapedFlangePartScope")
        try:
            sheet_part = createSheetMetalPart(doc)
            profile = doc.addObject("Part::Feature", "Profile")
            profile.Shape = Part.makePolygon(
                [App.Vector(0, 0, 0), App.Vector(10, 0, 0),
                 App.Vector(10, 10, 0), App.Vector(0, 10, 0),
                 App.Vector(0, 0, 0)]
            )
            _adopt_profiles(sheet_part, [profile])
            self.assertEqual(sheet_part.TypeId, "App::Part")
            self.assertIs(profile.getParentGeoFeatureGroup(), sheet_part)
        finally:
            App.closeDocument(doc.Name)

    def test_drawing_name_uses_organizational_group_without_duplication(self):
        doc = App.newDocument("SheetMetalDrawingName")
        try:
            group = doc.addObject("App::DocumentObjectGroup", "FeedDrive")
            group.Label = "Feed Drive"
            part = doc.addObject("App::Part", "CrossBar")
            part.Label = "Cross Bar"
            group.addObject(part)
            addSheetMetalPartProperties(part)

            self.assertEqual(part.DrawingName, "Feed Drive – Cross Bar")

            part.Label = "Feed Drive Cross Bar"
            self.assertEqual(part.DrawingName, "Feed Drive Cross Bar")

            part.Label = "Cross Bar"
            group.Label = "Drive System"
            self.assertEqual(part.DrawingName, "Drive System – Cross Bar")

            part.UseGroupInDrawingName = False
            self.assertEqual(part.DrawingName, "Cross Bar")
        finally:
            App.closeDocument(doc.Name)

    def test_experimental_group_upgrades_to_an_app_part(self):
        doc = App.newDocument("ShapedFlangePartUpgrade")
        try:
            old_group = doc.addObject("App::DocumentObjectGroup", "SheetMetalPart")
            old_group.Label = "Existing Sheet Metal Part"
            old_group.addProperty("App::PropertyLink", "Tip", "Sheet Metal")
            addSheetMetalPartProperties(old_group)
            old_group.Thickness = 2.5
            profile = doc.addObject("Part::Feature", "Profile")
            profile.Shape = Part.makePolygon(
                [App.Vector(0, 0, 0), App.Vector(10, 0, 0),
                 App.Vector(10, 10, 0), App.Vector(0, 10, 0),
                 App.Vector(0, 0, 0)]
            )
            feature = doc.addObject("Part::FeaturePython", "ShapedFlange")
            SMShapedFlange(feature, [profile], old_group)
            old_group.addObject(feature)
            old_group.Tip = feature
            doc.recompute()

            sheet_part = upgradeSheetMetalPart(old_group)
            doc.recompute()
            self.assertEqual(sheet_part.Name, "SheetMetalPart")
            self.assertEqual(sheet_part.TypeId, "App::Part")
            self.assertEqual(sheet_part.Label, "Existing Sheet Metal Part")
            self.assertAlmostEqual(sheet_part.Thickness.Value, 2.5)
            self.assertIs(_part_tip_feature(sheet_part), feature)
            self.assertIs(profile.getParentGeoFeatureGroup(), sheet_part)
            self.assertIs(feature.getParentGeoFeatureGroup(), sheet_part)
            self.assertTrue(feature.Shape.isValid())
        finally:
            App.closeDocument(doc.Name)

    def test_single_panel(self):
        sketch = _panel(
            "Base",
            [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
             App.Vector(20, 10, 0), App.Vector(0, 10, 0)],
        )
        result = makeShapedFlange([sketch], thickness=2.0, radius=1.0)
        self.assertEqual(len(result.Solids), 1)
        self.assertAlmostEqual(result.Volume, 400.0, places=5)

    def test_stage_uses_profile_local_normal_inside_transformed_part(self):
        sketch = _SketchStub(
            "TransformedPartProfile",
            Part.makePolygon(
                [
                    App.Vector(0, 0, 0),
                    App.Vector(20, 0, 0),
                    App.Vector(20, 10, 0),
                    App.Vector(0, 10, 0),
                    App.Vector(0, 0, 0),
                ]
            ),
            placement=App.Placement(),
            global_placement=App.Placement(
                App.Vector(100, 200, 300),
                App.Rotation(App.Vector(1, 0, 0), 90),
            ),
        )

        result = makeShapedFlangeStages(
            [
                {
                    "sketches": [sketch],
                    "radius": 1.0,
                    "thickness_side": "Centered",
                }
            ],
            thickness=2.0,
        )

        self.assertTrue(result.isValid())
        self.assertEqual(len(result.Solids), 1)
        self.assertAlmostEqual(result.Volume, 400.0, places=5)
        self.assertAlmostEqual(result.BoundBox.ZLength, 2.0, places=5)

    def test_right_angle_panels_get_constant_thickness_bend(self):
        base = _panel(
            "Base",
            [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
             App.Vector(20, 10, 0), App.Vector(0, 10, 0)],
        )
        wall = _panel(
            "Wall",
            [App.Vector(0, 0, 0), App.Vector(0, 0, 12),
             App.Vector(20, 0, 12), App.Vector(20, 0, 0)],
            App.Placement(
                App.Vector(), App.Rotation(App.Vector(1, 0, 0), 90)
            ),
        )
        result = makeShapedFlange([base, wall], thickness=2.0, radius=3.0)
        self.assertEqual(len(result.Solids), 1)
        self.assertTrue(result.isValid())
        expected = 2.0 * (20.0 * (10.0 - 4.0) + 20.0 * (12.0 - 4.0))
        expected += math.pi * 0.25 * ((5.0 ** 2) - (3.0 ** 2)) * 20.0
        self.assertAlmostEqual(result.Volume, expected, places=4)

    def test_partial_collinear_edges_create_a_bend(self):
        base = _panel(
            "LongBase",
            [App.Vector(-30, 0, 0), App.Vector(10, 0, 0),
             App.Vector(10, 12, 0), App.Vector(-30, 12, 0)],
        )
        wall = _panel(
            "ShortWall",
            [App.Vector(-20, 0, 0), App.Vector(-20, 0, 10),
             App.Vector(0, 0, 10), App.Vector(0, 0, 0)],
        )
        result = makeShapedFlange([base, wall], thickness=2.0, radius=1.0)
        self.assertEqual(len(result.Solids), 1)
        self.assertTrue(result.isValid())
        self.assertAlmostEqual(result.BoundBox.XLength, 40.0, places=5)

    def test_bend_plane_controls_sheet_offset(self):
        base = _panel(
            "Base",
            [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
             App.Vector(20, 10, 0), App.Vector(0, 10, 0)],
        )
        wall = _panel(
            "Wall",
            [App.Vector(0, 0, 0), App.Vector(0, 0, 12),
             App.Vector(20, 0, 12), App.Vector(20, 0, 0)],
        )
        inside = makeShapedFlange(
            [base, wall], thickness=2.0, radius=3.0, bend_plane="Inside",
        )
        outside = makeShapedFlange(
            [base, wall], thickness=2.0, radius=3.0, bend_plane="Outside",
        )
        self.assertEqual(len(inside.Solids), 1)
        self.assertEqual(len(outside.Solids), 1)
        self.assertTrue(inside.isValid())
        self.assertTrue(outside.isValid())
        self.assertAlmostEqual(inside.BoundBox.YMin, -2.0, places=5)
        self.assertAlmostEqual(inside.BoundBox.ZMin, -2.0, places=5)
        self.assertAlmostEqual(outside.BoundBox.YMin, 0.0, places=5)
        self.assertAlmostEqual(outside.BoundBox.ZMin, 0.0, places=5)

    def test_overlapping_additive_regions_are_unioned(self):
        sketch = _multi_loop_sketch(
            "Overlap",
            [
                [App.Vector(0, 0, 0), App.Vector(10, 0, 0),
                 App.Vector(10, 10, 0), App.Vector(0, 10, 0)],
                [App.Vector(5, 0, 0), App.Vector(15, 0, 0),
                 App.Vector(15, 10, 0), App.Vector(5, 10, 0)],
            ],
        )
        result = makeShapedFlange([sketch], thickness=2.0, radius=1.0)
        self.assertEqual(len(result.Solids), 1)
        self.assertAlmostEqual(result.Volume, 300.0, places=5)

    def test_nested_region_defaults_to_a_hole(self):
        sketch = _multi_loop_sketch(
            "Nested",
            [
                [App.Vector(0, 0, 0), App.Vector(10, 0, 0),
                 App.Vector(10, 10, 0), App.Vector(0, 10, 0)],
                [App.Vector(3, 3, 0), App.Vector(7, 3, 0),
                 App.Vector(7, 7, 0), App.Vector(3, 7, 0)],
            ],
        )
        result = makeShapedFlange([sketch], thickness=1.0, radius=1.0)
        self.assertAlmostEqual(result.Volume, 84.0, places=5)

    def test_region_operation_can_subtract_an_overlapping_loop(self):
        sketch = _multi_loop_sketch(
            "Selected",
            [
                [App.Vector(0, 0, 0), App.Vector(10, 0, 0),
                 App.Vector(10, 10, 0), App.Vector(0, 10, 0)],
                [App.Vector(5, 0, 0), App.Vector(15, 0, 0),
                 App.Vector(15, 10, 0), App.Vector(5, 10, 0)],
            ],
        )
        result = makeShapedFlange(
            [sketch],
            thickness=1.0,
            radius=1.0,
            region_operations=["Selected:Wire1=Add", "Selected:Wire2=Subtract"],
        )
        self.assertAlmostEqual(result.Volume, 50.0, places=5)

    def test_disconnected_panels_are_rejected(self):
        first = _panel(
            "First",
            [App.Vector(0, 0, 0), App.Vector(5, 0, 0),
             App.Vector(5, 5, 0), App.Vector(0, 5, 0)],
        )
        second = _panel(
            "Second",
            [App.Vector(10, 0, 0), App.Vector(15, 0, 0),
             App.Vector(15, 5, 0), App.Vector(10, 5, 0)],
        )
        with self.assertRaisesRegex(ValueError, "connected part"):
            makeShapedFlange([first, second], thickness=1.0, radius=1.0)

    def test_stages_allow_opposite_thickness_sides(self):
        base = _panel(
            "Base",
            [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
             App.Vector(20, 10, 0), App.Vector(0, 10, 0)],
        )
        wall = _panel(
            "Wall",
            [App.Vector(0, 0, 0), App.Vector(0, 0, 12),
             App.Vector(20, 0, 12), App.Vector(20, 0, 0)],
            App.Placement(
                App.Vector(), App.Rotation(App.Vector(1, 0, 0), 90)
            ),
        )
        result = makeShapedFlangeStages(
            [
                {"sketches": [base], "radius": 2.0,
                 "thickness_side": "Normal"},
                {"sketches": [wall], "radius": 4.0,
                 "thickness_side": "Reversed"},
            ],
            thickness=2.0,
        )
        self.assertEqual(len(result.Solids), 1)
        self.assertTrue(result.isValid())
        cylinder_radii = sorted(
            round(face.Surface.Radius, 6)
            for face in result.Faces
            if type(face.Surface).__name__ == "Cylinder"
        )
        self.assertEqual(cylinder_radii, [4.0, 6.0])

    def test_angled_flange_edges_continue_through_bend_ends(self):
        base = _panel(
            "Base",
            [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
             App.Vector(20, 10, 0), App.Vector(0, 10, 0)],
        )
        wall = _panel(
            "AngledWall",
            [App.Vector(0, 0, 0), App.Vector(2, 0, 4),
             App.Vector(18, 0, 4), App.Vector(20, 0, 0)],
            App.Placement(
                App.Vector(), App.Rotation(App.Vector(1, 0, 0), 90)
            ),
        )

        result = makeShapedFlangeStages(
            [
                {"sketches": [base], "radius": 1.0,
                 "thickness_side": "Centered"},
                {"sketches": [wall], "radius": 1.0,
                 "thickness_side": "Centered"},
            ],
            thickness=1.0,
        )

        self.assertTrue(result.isValid())
        self.assertEqual(len(result.Solids), 1)
        cylindrical_bend_faces = [
            face for face in result.Faces
            if type(face.Surface).__name__ == "Cylinder"
        ]
        formed_end_faces = [
            face for face in result.Faces
            if type(face.Surface).__name__ == "BSplineSurface"
        ]
        self.assertEqual(len(cylindrical_bend_faces), 2)
        self.assertEqual(len(formed_end_faces), 2)
        self.assertTrue(all(face.Area > 0.0 for face in formed_end_faces))

    def test_angled_bend_clip_cache_reuses_identical_geometry(self):
        base = _panel(
            "Base",
            [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
             App.Vector(20, 10, 0), App.Vector(0, 10, 0)],
        )
        wall = _panel(
            "AngledWall",
            [App.Vector(0, 0, 0), App.Vector(2, 0, 4),
             App.Vector(18, 0, 4), App.Vector(20, 0, 0)],
            App.Placement(
                App.Vector(), App.Rotation(App.Vector(1, 0, 0), 90)
            ),
        )
        stages = [
            {"sketches": [base], "radius": 1.0,
             "thickness_side": "Centered"},
            {"sketches": [wall], "radius": 1.0,
             "thickness_side": "Centered"},
        ]
        ShapedFlangeModule._bend_clip_cache.clear()
        with mock.patch.object(
            ShapedFlangeModule,
            "_bend_end_cutting_solid",
            wraps=ShapedFlangeModule._bend_end_cutting_solid,
        ) as cutter:
            first = makeShapedFlangeStages(stages, thickness=1.0)
            first_call_count = cutter.call_count
            second = makeShapedFlangeStages(stages, thickness=1.0)

        self.assertGreater(first_call_count, 0)
        self.assertEqual(cutter.call_count, first_call_count)
        self.assertAlmostEqual(first.Volume, second.Volume)
        self.assertTrue(second.isValid())

    def test_rectangle_and_round_bend_reliefs_cut_both_bend_ends(self):
        base = _panel(
            "Base",
            [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
             App.Vector(20, 10, 0), App.Vector(0, 10, 0)],
        )
        wall = _panel(
            "Wall",
            [App.Vector(0, 0, 0), App.Vector(0, 0, 12),
             App.Vector(20, 0, 12), App.Vector(20, 0, 0)],
            App.Placement(
                App.Vector(), App.Rotation(App.Vector(1, 0, 0), 90)
            ),
        )
        stages = [
            {"sketches": [base], "radius": 3.0,
             "thickness_side": "Normal"},
            {"sketches": [wall], "radius": 3.0,
             "thickness_side": "Reversed"},
        ]
        no_relief = makeShapedFlangeStages(stages, thickness=2.0)

        rectangle_stages = [dict(stage) for stage in stages]
        rectangle_stages[-1].update(
            relief_type="Rectangle", relief_width=1.0, relief_depth=2.0,
        )
        rectangle = makeShapedFlangeStages(rectangle_stages, thickness=2.0)
        expected_removed_volume = 8.0 * math.pi + 16.0
        self.assertTrue(rectangle.isValid())
        self.assertEqual(len(rectangle.Solids), 1)
        self.assertAlmostEqual(
            no_relief.Volume - rectangle.Volume,
            expected_removed_volume,
            places=5,
        )

        round_stages = [dict(stage) for stage in stages]
        round_stages[-1].update(
            relief_type="Round", relief_width=1.0, relief_depth=2.0,
        )
        round_relief = makeShapedFlangeStages(round_stages, thickness=2.0)
        self.assertTrue(round_relief.isValid())
        self.assertEqual(len(round_relief.Solids), 1)
        self.assertLess(round_relief.Volume, no_relief.Volume)
        self.assertGreater(round_relief.Volume, rectangle.Volume)

    def test_tear_relief_uses_width_as_a_finite_kerf(self):
        base = _panel(
            "Base",
            [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
             App.Vector(20, 10, 0), App.Vector(0, 10, 0)],
        )
        wall = _panel(
            "Wall",
            [App.Vector(0, 0, 0), App.Vector(0, 0, 12),
             App.Vector(20, 0, 12), App.Vector(20, 0, 0)],
            App.Placement(
                App.Vector(), App.Rotation(App.Vector(1, 0, 0), 90)
            ),
        )
        stages = [
            {"sketches": [base], "radius": 3.0,
             "thickness_side": "Normal"},
            {"sketches": [wall], "radius": 3.0,
             "thickness_side": "Reversed"},
        ]
        no_relief = makeShapedFlangeStages(stages, thickness=2.0)
        tear_stages = [dict(stage) for stage in stages]
        tear_stages[-1].update(
            relief_type="Tear", relief_width=0.2, relief_depth=2.0,
        )
        tear = makeShapedFlangeStages(tear_stages, thickness=2.0)
        self.assertTrue(tear.isValid())
        self.assertEqual(len(tear.Solids), 1)
        self.assertLess(tear.Volume, no_relief.Volume)

        rectangle_stages = [dict(stage) for stage in stages]
        rectangle_stages[-1].update(
            relief_type="Rectangle", relief_width=0.2, relief_depth=2.0,
        )
        rectangle = makeShapedFlangeStages(rectangle_stages, thickness=2.0)
        self.assertAlmostEqual(tear.Volume, rectangle.Volume, places=6)

    def test_bend_relief_rejects_width_that_consumes_the_bend(self):
        base = _panel(
            "Base",
            [App.Vector(0, 0, 0), App.Vector(10, 0, 0),
             App.Vector(10, 10, 0), App.Vector(0, 10, 0)],
        )
        wall = _panel(
            "Wall",
            [App.Vector(0, 0, 0), App.Vector(0, 0, 10),
             App.Vector(10, 0, 10), App.Vector(10, 0, 0)],
            App.Placement(
                App.Vector(), App.Rotation(App.Vector(1, 0, 0), 90)
            ),
        )
        with self.assertRaisesRegex(ValueError, "too large"):
            makeShapedFlangeStages(
                [
                    {"sketches": [base], "radius": 1.0,
                     "thickness_side": "Normal"},
                    {"sketches": [wall], "radius": 1.0,
                     "thickness_side": "Reversed", "relief_type": "Rectangle",
                     "relief_width": 5.0, "relief_depth": 1.0},
                ],
                thickness=1.0,
            )

    def test_face_features_form_a_cumulative_part_history(self):
        doc = App.newDocument("ShapedFlangeFeatureHistory")
        try:
            sheet_part = createSheetMetalPart(doc)
            sheet_part.UseMaterialCatalog = False
            sheet_part.Thickness = 2.0
            sheet_part.DefaultBendRadius = 2.0

            base = doc.addObject("Part::Feature", "BaseProfile")
            base.Shape = Part.makePolygon(
                [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
                 App.Vector(20, 10, 0), App.Vector(0, 10, 0),
                 App.Vector(0, 0, 0)]
            )
            wall = doc.addObject("Part::Feature", "WallProfile")
            wall.Shape = Part.makePolygon(
                [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
                 App.Vector(20, 12, 0), App.Vector(0, 12, 0),
                 App.Vector(0, 0, 0)]
            )
            wall.Placement.Rotation = App.Rotation(App.Vector(1, 0, 0), 90)

            first = doc.addObject("Part::FeaturePython", "ShapedFlange")
            SMShapedFlange(first, [base], sheet_part)
            first.ThicknessSide = "Normal"
            sheet_part.addObject(first)
            sheet_part.Tip = first.Name
            doc.recompute()

            second = doc.addObject("Part::FeaturePython", "ShapedFlange001")
            SMShapedFlange(second, [wall], sheet_part, first)
            second.ThicknessSide = "Reversed"
            second.UseDefaultBendRadius = False
            second.BendRadius = 5.0
            sheet_part.addObject(second)
            sheet_part.Tip = second.Name
            doc.recompute()
            volume_without_relief = second.Shape.Volume
            sheet_part.DefaultReliefType = "Rectangle"
            sheet_part.DefaultReliefWidth = 1.0
            sheet_part.DefaultReliefDepth = 2.0
            doc.recompute()

            self.assertIs(second.PreviousFeature, first)
            self.assertIs(_part_tip_feature(sheet_part), second)
            self.assertAlmostEqual(sheet_part.Thickness.Value, 2.0)
            self.assertEqual(len(first.Shape.Solids), 1)
            self.assertEqual(len(second.Shape.Solids), 1)
            self.assertTrue(second.Shape.isValid())
            self.assertEqual(second.ReliefType, "None")
            self.assertTrue(second.UseDefaultRelief)
            self.assertLess(second.Shape.Volume, volume_without_relief)
            cylinder_radii = sorted(
                round(face.Surface.Radius, 6)
                for face in second.Shape.Faces
                if type(face.Surface).__name__ == "Cylinder"
            )
            self.assertEqual(cylinder_radii, [5.0, 7.0])
        finally:
            App.closeDocument(doc.Name)

    def test_feature_runtime_cache_reuses_and_invalidates_geometry(self):
        doc = App.newDocument("ShapedFlangeRuntimeCache")
        try:
            sheet_part = createSheetMetalPart(doc)
            sheet_part.UseMaterialCatalog = False
            sheet_part.Thickness = 2.0
            profile = doc.addObject("Part::Feature", "Profile")
            profile.Shape = Part.makePolygon(
                [App.Vector(0, 0, 0), App.Vector(10, 0, 0),
                 App.Vector(10, 10, 0), App.Vector(0, 10, 0),
                 App.Vector(0, 0, 0)]
            )
            feature = doc.addObject("Part::FeaturePython", "ShapedFlange")
            SMShapedFlange(feature, [profile], sheet_part)
            sheet_part.addObject(feature)
            sheet_part.Tip = feature.Name
            doc.recompute()
            initial_volume = feature.Shape.Volume

            with mock.patch.object(
                ShapedFlangeModule,
                "makeShapedFlangeStages",
                wraps=ShapedFlangeModule.makeShapedFlangeStages,
            ) as builder:
                feature.touch()
                doc.recompute()
                self.assertEqual(builder.call_count, 0)

                profile.Shape = Part.makePolygon(
                    [App.Vector(0, 0, 0), App.Vector(20, 0, 0),
                     App.Vector(20, 10, 0), App.Vector(0, 10, 0),
                     App.Vector(0, 0, 0)]
                )
                doc.recompute()
                self.assertEqual(builder.call_count, 1)

            self.assertAlmostEqual(feature.Shape.Volume, initial_volume * 2.0)
        finally:
            App.closeDocument(doc.Name)

    def test_feature_history_survives_save_and_reopen(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "ShapedFlangeHistory.FCStd")
            doc = App.newDocument("ShapedFlangePersistence")
            sheet_part = createSheetMetalPart(doc)
            sheet_part.UseMaterialCatalog = False
            sheet_part.Thickness = 2.0
            profile = doc.addObject("Part::Feature", "Profile")
            profile.Shape = Part.makePolygon(
                [App.Vector(0, 0, 0), App.Vector(10, 0, 0),
                 App.Vector(10, 10, 0), App.Vector(0, 10, 0),
                 App.Vector(0, 0, 0)]
            )
            feature = doc.addObject("Part::FeaturePython", "ShapedFlange")
            SMShapedFlange(feature, [profile], sheet_part)
            sheet_part.addObject(feature)
            sheet_part.Tip = feature.Name
            doc.recompute()
            doc.saveAs(path)
            App.closeDocument(doc.Name)

            reopened = App.openDocument(path)
            try:
                reopened.recompute()
                reopened_part = reopened.getObject("SheetMetalPart")
                reopened_feature = reopened.getObject("ShapedFlange")
                self.assertIs(_part_tip_feature(reopened_part), reopened_feature)
                self.assertEqual(
                    reopened_feature.Proxy.__class__.__name__, "SMShapedFlange"
                )
                self.assertTrue(reopened_feature.Shape.isValid())
                self.assertAlmostEqual(reopened_feature.Shape.Volume, 200.0)
            finally:
                App.closeDocument(reopened.Name)


if __name__ == "__main__":
    unittest.main()
