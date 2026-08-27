# -*- coding: utf-8 -*-

import os
import tempfile
import unittest

import FreeCAD as App
import Part

from SheetMetalUnfoldCmd import (
    _isUnfoldObject,
    arrangeFlatPatternLinks,
    migrateDocumentUnfoldPartTips,
    resolveUnfoldSource,
    retargetUnfoldsToPartTip,
    smExportLayeredUnfoldDXF,
)


class TestFlatPatternWorkspace(unittest.TestCase):
    def test_unfold_source_follows_a_new_sheet_metal_part_tip(self):
        doc = App.newDocument("FlatPatternFollowPartTip")
        try:
            sheet_part = doc.addObject("App::Part", "SheetMetalPart")
            sheet_part.addProperty("App::PropertyString", "SheetMetalType")
            sheet_part.SheetMetalType = "Part"
            body = doc.addObject("PartDesign::Body", "Body")
            sheet_part.addObject(body)
            first = doc.addObject("PartDesign::Feature", "ShapedFlange")
            first.Shape = Part.makeBox(20.0, 10.0, 1.0)
            body.addObject(first)
            second = doc.addObject("PartDesign::Feature", "ShapedFlange001")
            second.Shape = first.Shape.fuse(
                Part.makeBox(20.0, 1.0, 5.0, App.Vector(0.0, 9.0, 1.0))
            )
            second.addProperty("App::PropertyLink", "PreviousFeature")
            second.PreviousFeature = first
            body.addObject(second)

            unfold = doc.addObject("Part::FeaturePython", "Body_Unfold")
            unfold.addProperty("App::PropertyLinkSub", "baseObject")
            unfold.addProperty("App::PropertyStringList", "UnfoldSketches")
            unfold.baseObject = (body, ["ShapedFlange.Face1"])
            unfold.UnfoldSketches = []
            sheet_part.addObject(unfold)
            doc.recompute()

            updated = retargetUnfoldsToPartTip(sheet_part, second, first)

            self.assertEqual(updated, [unfold])
            self.assertIs(unfold.baseObject[0], body)
            self.assertEqual(unfold.baseObject[1], ["ShapedFlange.Face1"])
            self.assertTrue(unfold.FollowPartTip)
            self.assertIs(unfold.FollowedTip, second)
            source, face_name = resolveUnfoldSource(unfold)
            self.assertIs(source, second)
            self.assertTrue(face_name.startswith("Face"))
        finally:
            App.closeDocument(doc.Name)

    def test_saved_unfold_source_migrates_to_the_current_part_tip(self):
        doc = App.newDocument("FlatPatternMigratePartTip")
        try:
            sheet_part = doc.addObject("App::Part", "SheetMetalPart")
            sheet_part.addProperty("App::PropertyString", "SheetMetalType")
            sheet_part.SheetMetalType = "Part"
            sheet_part.addProperty("App::PropertyString", "Tip")
            body = doc.addObject("PartDesign::Body", "Body")
            sheet_part.addObject(body)
            first = doc.addObject("PartDesign::Feature", "ShapedFlange")
            first.Shape = Part.makeBox(20.0, 10.0, 1.0)
            body.addObject(first)
            second = doc.addObject("PartDesign::Feature", "ShapedFlange001")
            second.Shape = first.Shape.fuse(
                Part.makeBox(20.0, 1.0, 5.0, App.Vector(0.0, 9.0, 1.0))
            )
            second.addProperty("App::PropertyLink", "PreviousFeature")
            second.PreviousFeature = first
            body.addObject(second)
            sheet_part.Tip = second.Name

            unfold = doc.addObject("Part::FeaturePython", "Body_Unfold")
            unfold.addProperty("App::PropertyLinkSub", "baseObject")
            unfold.addProperty("App::PropertyStringList", "UnfoldSketches")
            unfold.baseObject = (body, ["ShapedFlange.Face1"])
            unfold.UnfoldSketches = []
            sheet_part.addObject(unfold)
            doc.recompute()

            updated = migrateDocumentUnfoldPartTips(doc)

            self.assertEqual(updated, [unfold])
            self.assertIs(unfold.FollowedTip, second)
            self.assertEqual(unfold.baseObject[1], ["ShapedFlange.Face1"])
        finally:
            App.closeDocument(doc.Name)

    def test_part_tip_migration_recovers_a_cross_body_feature_prefix(self):
        doc = App.newDocument("FlatPatternRecoverCrossBodyPrefix")
        try:
            first_part = doc.addObject("App::Part", "FirstPart")
            first_part.addProperty("App::PropertyString", "SheetMetalType")
            first_part.SheetMetalType = "Part"
            first_part.addProperty("App::PropertyString", "Tip")
            first_body = doc.addObject("PartDesign::Body", "FirstBody")
            first_part.addObject(first_body)
            first_base = doc.addObject("PartDesign::Feature", "FirstBase")
            first_base.Shape = Part.makeBox(20.0, 10.0, 1.0)
            first_body.addObject(first_base)
            first_tip = doc.addObject("PartDesign::Feature", "FirstTip")
            first_tip.Shape = first_base.Shape.cut(
                Part.makeCylinder(1.0, 1.0, App.Vector(5.0, 5.0, 0.0))
            )
            first_tip.addProperty("App::PropertyLink", "PreviousFeature")
            first_tip.PreviousFeature = first_base
            first_body.addObject(first_tip)
            first_part.Tip = first_tip.Name

            other_part = doc.addObject("App::Part", "OtherPart")
            other_body = doc.addObject("PartDesign::Body", "OtherBody")
            other_part.addObject(other_body)
            other_tip = doc.addObject("PartDesign::Feature", "OtherTip")
            other_tip.Shape = Part.makeBox(5.0, 5.0, 1.0)
            other_body.addObject(other_tip)

            unfold = doc.addObject("Part::FeaturePython", "FirstUnfold")
            unfold.addProperty("App::PropertyLinkSub", "baseObject")
            unfold.addProperty("App::PropertyStringList", "UnfoldSketches")
            unfold.baseObject = (first_body, ["OtherTip.Face1"])
            unfold.UnfoldSketches = []
            first_part.addObject(unfold)
            doc.recompute()

            updated = migrateDocumentUnfoldPartTips(doc)

            self.assertEqual(updated, [unfold])
            self.assertIs(unfold.baseObject[0], first_body)
            self.assertEqual(unfold.baseObject[1], ["OtherTip.Face1"])
            self.assertIs(unfold.FollowedTip, first_tip)
            source, target_name = resolveUnfoldSource(unfold)
            self.assertIs(source, first_tip)
            target_face = first_tip.Shape.getElement(target_name)
            self.assertGreater(target_face.Area, 100.0)
        finally:
            App.closeDocument(doc.Name)

    def test_flat_pattern_links_are_not_treated_as_unfold_objects(self):
        doc = App.newDocument("FlatPatternLinkDiscovery")
        try:
            unfold = doc.addObject("Part::Feature", "Unfold")
            unfold.addProperty("App::PropertyString", "baseObject")
            unfold.addProperty("App::PropertyStringList", "UnfoldSketches")
            unfold.baseObject = "Face1"
            unfold.UnfoldSketches = []

            link = doc.addObject("App::Link", "FlatPattern")
            link.LinkedObject = unfold
            doc.recompute()

            self.assertTrue(hasattr(link, "baseObject"))
            self.assertTrue(hasattr(link, "UnfoldSketches"))
            self.assertTrue(_isUnfoldObject(unfold))
            self.assertFalse(_isUnfoldObject(link))
        finally:
            App.closeDocument(doc.Name)

    def test_dxf_export_uses_explicit_cut_and_bend_layers(self):
        doc = App.newDocument("FlatPatternDXFLayers")
        output_path = os.path.join(tempfile.gettempdir(), "sheetmetal-layer-test.dxf")
        try:
            outline = doc.addObject("Part::Feature", "Part_Sketch_Outline")
            outline.Shape = Part.makePolygon(
                [App.Vector(0.0, 0.0), App.Vector(20.0, 0.0)]
            )
            bends = doc.addObject("Part::Feature", "Part_Sketch_Bends")
            bends.Shape = Part.makePolygon(
                [App.Vector(0.0, 5.0), App.Vector(20.0, 5.0)]
            )
            doc.recompute()

            smExportLayeredUnfoldDXF(
                [outline, bends], output_path, useDialog=False
            )

            with open(output_path, "r", encoding="utf-8", errors="replace") as stream:
                dxf = stream.read()
            self.assertIn("\nCUT\n", dxf)
            self.assertIn("\nBEND\n", dxf)
            self.assertIs(App.ActiveDocument, doc)
        finally:
            if os.path.exists(output_path):
                os.remove(output_path)
            App.closeDocument(doc.Name)

    def test_links_are_aligned_to_xy_and_packed_without_overlap(self):
        doc = App.newDocument("FlatPatternWorkspaceLayout")
        try:
            first_source = doc.addObject("Part::Feature", "FirstUnfold")
            first_source.Shape = Part.makeBox(
                20.0, 8.0, 2.0, App.Vector(), App.Vector(0.0, 0.0, 1.0)
            )
            first_source.Placement.Base = App.Vector(100.0, 200.0, 300.0)
            first_link = doc.addObject("App::Link", "FirstFlatPattern")
            first_link.LinkedObject = first_source

            second_source = doc.addObject("Part::Feature", "SecondUnfold")
            second_source.Shape = Part.makeBox(
                12.0, 3.0, 7.0, App.Vector(), App.Vector(0.0, 1.0, 0.0)
            )
            second_source.Placement.Base = App.Vector(-50.0, 80.0, 40.0)
            second_link = doc.addObject("App::Link", "SecondFlatPattern")
            second_link.LinkedObject = second_source
            doc.recompute()

            arranged = arrangeFlatPatternLinks([first_link, second_link], 5.0)

            self.assertEqual(set(arranged), {first_link, second_link})
            first_bounds = first_link.Shape.BoundBox
            second_bounds = second_link.Shape.BoundBox
            self.assertAlmostEqual(first_bounds.ZMin, 0.0, places=7)
            self.assertAlmostEqual(second_bounds.ZMin, 0.0, places=7)
            self.assertAlmostEqual(first_bounds.ZLength, 2.0, places=7)
            self.assertAlmostEqual(second_bounds.ZLength, 3.0, places=7)
            separated_x = (
                first_bounds.XMax + 5.0 <= second_bounds.XMin + 1.0e-7
                or second_bounds.XMax + 5.0 <= first_bounds.XMin + 1.0e-7
            )
            separated_y = (
                first_bounds.YMax + 5.0 <= second_bounds.YMin + 1.0e-7
                or second_bounds.YMax + 5.0 <= first_bounds.YMin + 1.0e-7
            )
            self.assertTrue(separated_x or separated_y)
        finally:
            App.closeDocument(doc.Name)


if __name__ == "__main__":
    unittest.main()
