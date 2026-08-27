# -*- coding: utf-8 -*-

import math
import os
import tempfile
import unittest

import FreeCAD as App
import Part

from SheetMetalShapedFlangeCmd import (
    FACE_GEOMETRY_VERSION,
    SMShapedFlange,
    _adopt_profiles,
    _part_tip_feature,
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
