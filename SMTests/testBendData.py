# -*- coding: utf-8 -*-

import json
import os
import tempfile
import unittest

import FreeCAD as App
import Part

from SheetMetalBaseCmd import SMBaseBend
import SheetMetalBendData
from SheetMetalCmd import SMBendWall
from SheetMetalNewUnfolder import BendInfo
from SheetMetalUnfoldCmd import SMUnfold


def _profile(doc):
    profile = doc.addObject("PartDesign::Feature", "BaseProfile")
    profile.Shape = Part.makePolygon(
        [
            App.Vector(0.0, 0.0, 0.0),
            App.Vector(60.0, 0.0, 0.0),
            App.Vector(60.0, 70.0, 0.0),
            App.Vector(0.0, 70.0, 0.0),
            App.Vector(0.0, 0.0, 0.0),
        ]
    )
    return profile


def _two_bend_part(doc):
    profile = _profile(doc)
    base = doc.addObject("Part::FeaturePython", "BaseBend")
    SMBaseBend(base, profile)
    base.Thickness = 1.5
    base.Radius = 2.0
    base.Length = 70.0
    base.BendSide = "Outside"
    doc.recompute()

    first = doc.addObject("Part::FeaturePython", "BendWall1")
    SMBendWall(first, base, ["Edge4"])
    first.radius = 2.0
    first.length = 25.0
    first.angle = 90.0
    first.invert = False
    first.BendType = "Material Outside"
    doc.recompute()

    second = doc.addObject("Part::FeaturePython", "BendWall2")
    SMBendWall(second, first, ["Edge10"])
    second.radius = 2.0
    second.length = 20.0
    second.angle = 90.0
    second.invert = True
    second.BendType = "Material Outside"
    doc.recompute()
    return first, second


class TestBendData(unittest.TestCase):
    def test_serializes_normalized_occurrence_in_unfold_local_frame(self):
        info = BendInfo(Part.makeLine(App.Vector(), App.Vector(60, 0, 0)), -90, 3.5)
        info.unfold_line = Part.makeLine(
            App.Vector(10, 20, 0), App.Vector(-50, 20, 0)
        )
        info.source_face_index = 12
        info.source_edge_index = 7
        info.thickness = 1.5

        occurrence = SheetMetalBendData.occurrence_from_bend_info(info)

        self.assertEqual("Face12", occurrence["key"])
        self.assertEqual("Edge7", occurrence["sourceEdge"])
        self.assertEqual("DOWN", occurrence["direction"])
        self.assertAlmostEqual(2.0, occurrence["insideRadiusMm"])
        self.assertAlmostEqual(3.5, occurrence["outsideRadiusMm"])
        self.assertEqual([-50.0, 20.0, 0.0], occurrence["line"]["start"])
        self.assertEqual([10.0, 20.0, 0.0], occurrence["line"]["end"])

    def test_real_unfold_persists_semantic_bends_on_recompute(self):
        doc = App.newDocument("SemanticBendData")
        try:
            first, source = _two_bend_part(doc)
            unfold = doc.addObject("Part::FeaturePython", "Unfold")
            SMUnfold(unfold, source, ["Face5"])
            unfold.KFactor = 0.38
            unfold.KFactorStandard = "ansi"
            doc.recompute()

            payload = json.loads(unfold.BendData)
            self.assertEqual(1, unfold.BendDataVersion)
            self.assertEqual("freecad-sheetmetal-bends", payload["schema"])
            self.assertEqual("unfold-object-local", payload["coordinateFrame"])
            self.assertEqual(2, len(payload["occurrences"]))
            self.assertEqual(2, len(unfold.BendLines.Edges))
            self.assertEqual(
                {"UP", "DOWN"},
                {item["direction"] for item in payload["occurrences"]},
            )
            self.assertTrue(
                all(item["insideRadiusMm"] == 2.0 for item in payload["occurrences"])
            )
            first_payload = unfold.BendData

            first.angle = 85.0
            doc.recompute()
            updated = json.loads(unfold.BendData)
            self.assertEqual(2, len(updated["occurrences"]))
            self.assertEqual(2, len(unfold.BendLines.Edges))
            self.assertNotEqual(first_payload, unfold.BendData)
            self.assertIn(
                95.0,
                {item["angleDegrees"] for item in updated["occurrences"]},
            )
            self.assertTrue(unfold.Shape.isValid())
        finally:
            App.closeDocument(doc.Name)

    def test_bend_data_survives_save_reopen_and_recompute(self):
        path = os.path.join(tempfile.gettempdir(), "SemanticBendData.FCStd")
        doc = App.newDocument("SemanticBendDataPersistence")
        try:
            _first, source = _two_bend_part(doc)
            unfold = doc.addObject("Part::FeaturePython", "Unfold")
            SMUnfold(unfold, source, ["Face5"])
            doc.recompute()
            original = json.loads(unfold.BendData)
            doc.saveAs(path)
            App.closeDocument(doc.Name)

            reopened = App.openDocument(path)
            reopened.recompute()
            restored = reopened.getObject("Unfold")
            payload = json.loads(restored.BendData)

            self.assertEqual(1, restored.BendDataVersion)
            self.assertEqual(2, len(payload["occurrences"]))
            self.assertEqual(2, len(restored.BendLines.Edges))
            self.assertEqual(
                [item["key"] for item in original["occurrences"]],
                [item["key"] for item in payload["occurrences"]],
            )
        finally:
            if App.ActiveDocument is not None:
                App.closeDocument(App.ActiveDocument.Name)
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
