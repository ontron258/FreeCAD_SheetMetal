# -*- coding: utf-8 -*-

import os
import tempfile
import unittest

import FreeCAD as App
import Part

from SheetMetalUnfoldCmd import (
    _isUnfoldObject,
    arrangeFlatPatternLinks,
    smExportLayeredUnfoldDXF,
)


class TestFlatPatternWorkspace(unittest.TestCase):
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
