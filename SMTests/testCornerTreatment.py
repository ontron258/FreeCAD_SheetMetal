# -*- coding: utf-8 -*-

import math
import os
import tempfile
import unittest

import FreeCAD as App
import Part

from SheetMetalCornerTreatmentCmd import (
    SMCornerTreatment, CornerSelectionGate, adoptCornerFeature, cornerEdges,
    cornerLengthUnit, makeCornerTreatment,
)


def _edge_names(shape, length=2.0):
    return ["Edge%d" % (index + 1) for index, edge in enumerate(shape.Edges)
            if abs(edge.Length - length) < 1.e-6
            and isinstance(edge.Curve, Part.Line)]


def _vertex_name(shape, point):
    return next("Vertex%d" % (index + 1) for index, vertex in enumerate(shape.Vertexes)
                if vertex.Point.distanceToPoint(point) < 1.e-6)


class TestCornerTreatment(unittest.TestCase):
    def setUp(self):
        self.sheet = Part.makeBox(60, 40, 2)
        self.edges = _edge_names(self.sheet)

    def assertSolid(self, shape):
        self.assertFalse(shape.isNull())
        self.assertTrue(shape.isValid())
        self.assertEqual(len(shape.Solids), 1)

    def test_round_multiple_corners_larger_than_thickness(self):
        result = makeCornerTreatment(self.sheet, self.edges, "Round", 6)
        self.assertSolid(result)
        self.assertAlmostEqual(result.Volume, 4800 - 4 * (36 - math.pi * 9) * 2)
        self.assertAlmostEqual(result.BoundBox.ZLength, 2)
        self.assertEqual(sum(isinstance(face.Surface, Part.Cylinder)
                             for face in result.Faces), 4)
        self.assertAlmostEqual(self.sheet.Volume, 4800)

    def test_chamfer_multiple_corners(self):
        result = makeCornerTreatment(self.sheet, self.edges[:2], "Chamfer", 6)
        self.assertSolid(result)
        self.assertAlmostEqual(result.Volume, 4800 - 2 * 36)
        self.assertAlmostEqual(result.BoundBox.ZLength, 2)
        self.assertTrue(all(isinstance(face.Surface, Part.Plane) for face in result.Faces))

    def test_vertex_and_edge_selection_deduplicates_same_corner(self):
        edge = self.sheet.getElement(self.edges[0])
        names = [_vertex_name(self.sheet, vertex.Point) for vertex in edge.Vertexes]
        names += [self.edges[0], names[0]]
        self.assertEqual(len(cornerEdges(self.sheet, names)), 1)
        result = makeCornerTreatment(self.sheet, names, "Round", 6)
        self.assertAlmostEqual(result.Volume, 4800 - (36 - math.pi * 9) * 2)

    def test_arbitrary_orientation_preserves_geometry(self):
        moved = self.sheet.copy()
        moved.Placement = App.Placement(App.Vector(41, -8, 19),
                                        App.Rotation(App.Vector(1, 3, 2), 57))
        result = makeCornerTreatment(moved, self.edges, "Chamfer", 5)
        expected = makeCornerTreatment(self.sheet, self.edges, "Chamfer", 5)
        expected.Placement = moved.Placement
        self.assertSolid(result)
        self.assertAlmostEqual(result.Volume, expected.Volume)
        self.assertAlmostEqual(result.common(expected).Volume, expected.Volume)

    def test_rejects_invalid_selection_and_size(self):
        long_edge = next("Edge%d" % (i + 1) for i, edge in enumerate(self.sheet.Edges)
                         if edge.Length > 2)
        for names in ([], ["Face1"], ["Edge999"], [long_edge], [self.edges[0], long_edge]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                makeCornerTreatment(self.sheet, names)
        for size in (0, -1, float("nan"), float("inf"), 1000):
            for mode in ("Round", "Chamfer"):
                with self.subTest(size=size, mode=mode), self.assertRaises(ValueError):
                    makeCornerTreatment(self.sheet, self.edges, mode, size)
        with self.assertRaises(ValueError):
            makeCornerTreatment(self.sheet, self.edges, "Invalid", 3)
        with self.assertRaises(ValueError):
            makeCornerTreatment(Part.makeCompound([self.sheet, self.sheet]), self.edges)

    def test_rejects_previously_rounded_corner(self):
        result = makeCornerTreatment(self.sheet, self.edges[:1], "Round", 6)
        curved = next("Edge%d" % (i + 1) for i, edge in enumerate(result.Edges)
                      if isinstance(edge.Curve, Part.Circle))
        with self.assertRaises(ValueError):
            makeCornerTreatment(result, [curved])

    def test_concave_outline_corner(self):
        sheet = self.sheet.cut(Part.makeBox(30, 20, 2, App.Vector(30, 20, 0))).removeSplitter()
        corner = _vertex_name(sheet, App.Vector(30, 20, 2))
        for mode, added in (("Round", (36 - math.pi * 9) * 2), ("Chamfer", 36)):
            with self.subTest(mode=mode):
                result = makeCornerTreatment(sheet, [corner], mode, 6)
                self.assertSolid(result)
                self.assertAlmostEqual(result.Volume, sheet.Volume + added)

    def test_sheet_with_center_hole_uses_interior_thickness_sample(self):
        sheet = self.sheet.cut(Part.makeCylinder(8, 2, App.Vector(30, 20, 0)))
        corner = _vertex_name(sheet, App.Vector(0, 0, 2))
        result = makeCornerTreatment(sheet, [corner], "Chamfer", 5)
        self.assertSolid(result)
        self.assertAlmostEqual(result.Volume, sheet.Volume - 25)

    def test_parametric_recompute_and_save_restore(self):
        doc = App.newDocument("CornerTreatmentRestore")
        try:
            base = doc.addObject("Part::Feature", "Base")
            base.Shape = self.sheet
            feature = doc.addObject("Part::FeaturePython", "Corners")
            SMCornerTreatment(feature, base, self.edges)
            feature.Radius = 6
            doc.recompute()
            self.assertSolid(feature.Shape)
            feature.Treatment = "Chamfer"
            feature.ChamferSize = 5
            doc.recompute()
            self.assertAlmostEqual(feature.Shape.Volume, 4700)
            base.Shape = Part.makeBox(60, 40, 3)
            doc.recompute()
            self.assertAlmostEqual(feature.Shape.Volume, 7050)
            with tempfile.TemporaryDirectory() as directory:
                filename = os.path.join(directory, "Corners.FCStd")
                doc.saveAs(filename)
                App.closeDocument(doc.Name)
                doc = App.openDocument(filename)
                feature = doc.getObject("Corners")
                feature.ChamferSize = 6
                doc.recompute()
                self.assertIsInstance(feature.Proxy, SMCornerTreatment)
                self.assertSolid(feature.Shape)
                self.assertAlmostEqual(feature.Shape.Volume, 7200 - 4 * 18 * 3)
        finally:
            App.closeDocument(doc.Name)

    def test_feature_stays_in_part_or_body_and_advances_tip(self):
        from SheetMetalShapedFlangeCmd import createSheetMetalPart
        from SheetMetalMaterial import _shape_from_part_tip
        for use_body in (False, True):
            doc = App.newDocument("CornerOwnership")
            try:
                part = createSheetMetalPart(doc)
                container = part
                if use_body:
                    container = doc.addObject("PartDesign::Body", "Body")
                    part.addObject(container)
                base = doc.addObject("PartDesign::Feature" if use_body else "Part::Feature", "Base")
                container.addObject(base)
                base.Shape = self.sheet
                part.Tip = base.Name
                feature = doc.addObject("PartDesign::FeaturePython" if use_body
                                        else "Part::FeaturePython", "Corners")
                SMCornerTreatment(feature, base, self.edges)
                feature.Radius = 6
                adoptCornerFeature(feature, base)
                adoptCornerFeature(feature, base)
                doc.recompute()
                self.assertEqual(container.Group.count(feature), 1)
                self.assertIs(feature.getParentGeoFeatureGroup(), container)
                self.assertEqual(part.Tip, feature.Name)
                self.assertAlmostEqual(_shape_from_part_tip(part).Volume, feature.Shape.Volume)
                if use_body:
                    self.assertIs(container.Tip, feature)
                    self.assertSolid(container.Shape)
            finally:
                App.closeDocument(doc.Name)

    def test_saved_duplicate_body_reference_is_repaired(self):
        doc = App.newDocument("DuplicatedCornerMembership")
        try:
            body = doc.addObject("PartDesign::Body", "Body")
            base = body.newObject("PartDesign::Feature", "Base")
            base.Shape = self.sheet
            feature = doc.addObject("PartDesign::FeaturePython", "Corners")
            SMCornerTreatment(feature, base, self.edges)
            adoptCornerFeature(feature, base)
            body.addObject(feature)  # The original GUI command's second insertion.
            doc.recompute()
            self.assertEqual(body.Group.count(feature), 2)
            volume = feature.Shape.Volume
            with tempfile.TemporaryDirectory() as directory:
                filename = os.path.join(directory, "Duplicate.FCStd")
                doc.saveAs(filename)
                App.closeDocument(doc.Name)
                doc = App.openDocument(filename)
                doc.recompute()
                self.assertEqual([o.Name for o in doc.Body.Group], ["Base", "Corners"])
                self.assertEqual(doc.Body.Tip, doc.Corners)
                self.assertAlmostEqual(doc.Corners.Shape.Volume, volume)
        finally:
            App.closeDocument(doc.Name)

    def test_error_clears_stale_shape_and_recovers(self):
        doc = App.newDocument("CornerError")
        try:
            base = doc.addObject("Part::Feature", "Base")
            base.Shape = self.sheet
            feature = doc.addObject("Part::FeaturePython", "Corners")
            SMCornerTreatment(feature, base, self.edges)
            feature.Proxy.execute(feature)
            feature.Radius = 1000
            with self.assertRaises(ValueError):
                feature.Proxy.execute(feature)
            self.assertTrue(feature.Shape.isNull())
            self.assertIn("Reduce the size", feature.LastError)
            feature.Radius = 3
            feature.baseObject = (base, ["Face1"])
            with self.assertRaises(ValueError):
                feature.Proxy.execute(feature)
            self.assertIn("Face1", feature.LastError)
            feature.baseObject = (base, self.edges)
            feature.Proxy.execute(feature)
            self.assertFalse(feature.LastError)
            self.assertSolid(feature.Shape)
        finally:
            App.closeDocument(doc.Name)

    def test_gate_rejects_non_corners_and_other_objects(self):
        doc = App.newDocument("CornerGate")
        try:
            base = doc.addObject("Part::Feature", "Base")
            base.Shape = self.sheet
            other = doc.addObject("Part::Feature", "Other")
            other.Shape = self.sheet
            gate = CornerSelectionGate(base)
            self.assertTrue(gate.allow(doc, base, self.edges[0]))
            self.assertTrue(gate.allow(doc, base, "Vertex1"))
            long_edge = next("Edge%d" % (i + 1) for i, edge in enumerate(self.sheet.Edges)
                             if edge.Length > 2)
            for name in ("", "Face1", long_edge, "Edge999"):
                self.assertFalse(gate.allow(doc, base, name), name)
            self.assertFalse(gate.allow(doc, other, self.edges[0]))
        finally:
            App.closeDocument(doc.Name)

    def test_length_units_follow_document_not_global_schema(self):
        schema = App.Units.getSchema()
        doc = App.newDocument("CornerUnits")
        try:
            doc.UnitSystem = 0
            App.Units.setSchema(3)
            self.assertEqual(cornerLengthUnit(doc), "mm")
            for unit_schema in (2, 3, 5, 7):
                doc.UnitSystem = unit_schema
                App.Units.setSchema(0)
                self.assertEqual(cornerLengthUnit(doc), "in")
        finally:
            App.closeDocument(doc.Name)
            App.Units.setSchema(schema)

    def test_bent_sheet_corners_on_different_planes_and_unfold(self):
        from SMTests.testShapedFlange import _panel
        from SheetMetalShapedFlangeCmd import makeShapedFlange
        from SheetMetalNewUnfolder import BendAllowanceCalculator, getUnfold
        base = _panel("Base", [App.Vector(0, 0, 0), App.Vector(60, 0, 0),
                               App.Vector(60, 40, 0), App.Vector(0, 40, 0)])
        wall = _panel("Wall", [App.Vector(0, 0, 0), App.Vector(0, 0, 30),
                               App.Vector(60, 0, 30), App.Vector(60, 0, 0)],
                      App.Placement(App.Vector(), App.Rotation(App.Vector(1, 0, 0), 90)))
        sheet = makeShapedFlange([base, wall], thickness=2, radius=3)
        names = []
        for i, edge in enumerate(sheet.Edges):
            name = "Edge%d" % (i + 1)
            try:
                cornerEdges(sheet, [name])
                names.append(name)
            except ValueError:
                pass
        self.assertEqual(len(names), 4)
        for mode in ("Round", "Chamfer"):
            with self.subTest(mode=mode):
                result = makeCornerTreatment(sheet, names, mode, 6)
                self.assertSolid(result)
                removal = 4 * 2 * (36 - math.pi * 9 if mode == "Round" else 18)
                self.assertAlmostEqual(sheet.Volume - result.Volume, removal)
                doc = App.newDocument("CornerUnfold")
                try:
                    source = doc.addObject("Part::Feature", "Source")
                    source.Shape = result
                    face_index = max((i for i, f in enumerate(result.Faces)
                                      if isinstance(f.Surface, Part.Plane)),
                                     key=lambda i: result.Faces[i].Area)
                    bac = BendAllowanceCalculator.from_single_value(0.4, "ansi")
                    unfolded = getUnfold(bac, source, "Face%d" % (face_index + 1))
                    flat = unfolded[1]
                    self.assertSolid(flat)
                    self.assertEqual(len(unfolded[2].Edges), 1)
                    source.Shape = sheet
                    original_face = max((i for i, f in enumerate(sheet.Faces)
                                         if isinstance(f.Surface, Part.Plane)),
                                        key=lambda i: sheet.Faces[i].Area)
                    original_flat = getUnfold(bac, source, "Face%d" % (original_face + 1))[1]
                    self.assertAlmostEqual(original_flat.Volume - flat.Volume, removal)
                    self.assertAlmostEqual(min(flat.BoundBox.XLength,
                                               flat.BoundBox.YLength,
                                               flat.BoundBox.ZLength), 2)
                finally:
                    App.closeDocument(doc.Name)
