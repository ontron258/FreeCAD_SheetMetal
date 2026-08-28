# -*- coding: utf-8 -*-

import os
import tempfile
import unittest

import FreeCAD as App
import Part

from SheetMetalMirroredPartCmd import (
    create_mirrored_sheet_metal_part,
    reflection_matrix,
)
from SheetMetalShapedFlangeCmd import addSheetMetalPartProperties
from SheetMetalUnfoldCmd import SMUnfold


def _source_part(doc, name="SourcePart"):
    part = doc.addObject("App::Part", name)
    part.Label = name
    addSheetMetalPartProperties(part)
    part.UseMaterialCatalog = False
    part.Thickness = 2.0
    part.DefaultBendRadius = 3.0
    part.KFactor = 0.42
    part.Placement.Base = App.Vector(10.0, 0.0, 0.0)
    body = doc.addObject("PartDesign::Body", name + "Body")
    body.Placement.Base = App.Vector(1.0, 0.0, 0.0)
    part.addObject(body)
    base = body.newObject("PartDesign::Feature", name + "Base")
    sheet = Part.makeBox(30.0, 20.0, 2.0, App.Vector(2.0, 1.0, 0.0))
    hole = Part.makeCylinder(2.0, 6.0, App.Vector(10.0, 10.0, -2.0))
    base.Shape = sheet.cut(hole)
    body.Tip = base
    part.Tip = base.Name
    return part, body, base


class TestMirroredPart(unittest.TestCase):
    def test_reflection_matrix_supports_offset_planes(self):
        matrix = reflection_matrix(App.Vector(1.0, 0.0, 0.0), 5.0)
        point = matrix.multVec(App.Vector(13.0, 2.0, 3.0))
        self.assertTrue(point.isEqual(App.Vector(-3.0, 2.0, 3.0), 1e-9))

    def test_creates_distinct_body_product_with_copied_defaults(self):
        doc = App.newDocument("MirroredProduct")
        try:
            source, _source_body, _base = _source_part(doc)
            target, body, feature = create_mirrored_sheet_metal_part(
                doc, source, "YZ plane", 0.0
            )
            doc.recompute()

            self.assertIsNot(target, source)
            self.assertEqual(target.SheetMetalType, "Part")
            self.assertIs(target.DerivedFrom, source)
            self.assertEqual(target.VariantType, "Mirrored derivative")
            self.assertEqual(body.TypeId, "PartDesign::Body")
            self.assertIs(body.getParentGeoFeatureGroup(), target)
            self.assertIs(body.Tip, feature)
            self.assertEqual(target.Tip, feature.Name)
            self.assertEqual(feature.TypeId, "PartDesign::FeaturePython")
            self.assertIs(feature.SourcePart, source)
            self.assertTrue(feature.Shape.isValid())
            self.assertEqual(len(feature.Shape.Solids), 1)
            self.assertAlmostEqual(feature.Shape.BoundBox.XMin, -43.0)
            self.assertAlmostEqual(feature.Shape.BoundBox.XMax, -13.0)
            self.assertAlmostEqual(target.Thickness.Value, 2.0)
            self.assertAlmostEqual(target.DefaultBendRadius.Value, 3.0)
            self.assertAlmostEqual(float(target.KFactor), 0.42)
        finally:
            App.closeDocument(doc.Name)

    def test_source_tip_change_updates_mirrored_geometry(self):
        doc = App.newDocument("MirroredSourceUpdate")
        try:
            source, source_body, _base = _source_part(doc)
            _target, _target_body, mirror = create_mirrored_sheet_metal_part(
                doc, source
            )
            doc.recompute()
            self.assertAlmostEqual(mirror.Shape.BoundBox.XMin, -43.0)

            later = source_body.newObject("PartDesign::Feature", "LaterSourceFeature")
            later.Shape = Part.makeBox(
                5.0, 8.0, 2.0, App.Vector(4.0, 1.0, 0.0)
            )
            source_body.Tip = later
            source.Tip = later.Name
            doc.recompute()

            self.assertIs(mirror.SourceFeature, later)
            self.assertAlmostEqual(mirror.Shape.BoundBox.XMin, -20.0)
            self.assertAlmostEqual(mirror.Shape.BoundBox.XMax, -15.0)
            self.assertTrue(mirror.Shape.isValid())
        finally:
            App.closeDocument(doc.Name)

    def test_mirrored_product_can_own_unfold_outside_its_body(self):
        doc = App.newDocument("MirroredPartUnfold")
        try:
            source, _source_body, _base = _source_part(doc)
            target, body, mirror = create_mirrored_sheet_metal_part(doc, source)
            face_index = max(
                range(len(mirror.Shape.Faces)),
                key=lambda index: mirror.Shape.Faces[index].Area,
            )
            unfold = doc.addObject("Part::FeaturePython", "MirroredUnfold")
            target.addObject(unfold)
            SMUnfold(unfold, mirror, ["Face{}".format(face_index + 1)])
            doc.recompute()

            self.assertIs(unfold.getParentGeoFeatureGroup(), target)
            self.assertIsNot(unfold.getParentGeoFeatureGroup(), body)
            self.assertFalse(unfold.Shape.isNull())
            self.assertTrue(unfold.Shape.isValid())
            self.assertAlmostEqual(unfold.Shape.Volume, mirror.Shape.Volume, places=5)
        finally:
            App.closeDocument(doc.Name)

    def test_mirrored_product_survives_save_and_reopen(self):
        doc = App.newDocument("MirroredPartPersistence")
        path = os.path.join(tempfile.gettempdir(), "MirroredPartPersistence.FCStd")
        try:
            source, _source_body, _base = _source_part(doc)
            target, _body, mirror = create_mirrored_sheet_metal_part(
                doc, source, "YZ plane", 5.0
            )
            names = source.Name, target.Name, mirror.Name
            doc.saveAs(path)
            App.closeDocument(doc.Name)

            reopened = App.openDocument(path)
            reopened.recompute()
            restored_source = reopened.getObject(names[0])
            restored_target = reopened.getObject(names[1])
            restored_mirror = reopened.getObject(names[2])
            self.assertIs(restored_target.DerivedFrom, restored_source)
            self.assertIs(restored_mirror.SourcePart, restored_source)
            self.assertAlmostEqual(restored_mirror.PlaneOffset.Value, 5.0)
            self.assertAlmostEqual(restored_mirror.Shape.BoundBox.XMin, -33.0)
            self.assertAlmostEqual(restored_mirror.Shape.BoundBox.XMax, -3.0)
            self.assertTrue(restored_mirror.Shape.isValid())
        finally:
            if App.ActiveDocument is not None:
                App.closeDocument(App.ActiveDocument.Name)
            if os.path.exists(path):
                os.remove(path)


if __name__ == "__main__":
    unittest.main()
