# -*- coding: utf-8 -*-

import math
import os
import tempfile
import unittest

import FreeCAD as App
import Part

from SheetMetalMeshGeometry import (
    MeshSurfaceMap, family_collisions, generate_lines, mesh_layers,
    sketch_lines, sweep_wire, wire_positions,
)
from SheetMetalWeldedMeshCmd import (
    SMWeldedMesh, SMMeshDefinition, SMMeshPatternSketch,
    convert_to_editable, create_welded_mesh, regenerate_pattern,
)
from SheetMetalUnfoldCmd import _isFlatPatternObject, _isUnfoldObject, arrangeFlatPatternLinks
from SheetMetalNewUnfolder import BendAllowanceCalculator, unfold
from SheetMetalMeshPart import is_mesh_part
from SMTests.testBendData import _two_bend_part


class TestWeldedMesh(unittest.TestCase):
    def setUp(self):
        self.doc = App.newDocument("TestWeldedMesh")

    def tearDown(self):
        if self.doc is not None and self.doc.Name in App.listDocuments():
            App.closeDocument(self.doc.Name)

    def carrier(self, holes=False):
        source = self.doc.addObject("Part::Feature", "Carrier")
        shape = Part.makeBox(60, 40, 1.5)
        if holes:
            shape = shape.cut(Part.makeCylinder(6, 1.5, App.Vector(30, 20, 0)))
        source.Shape = shape
        self.doc.recompute()
        face_name = self.top_face(source.Shape)
        return source, face_name

    @staticmethod
    def top_face(shape):
        candidates = [i for i, f in enumerate(shape.Faces)
                      if isinstance(f.Surface, Part.Plane) and f.normalAt(0, 0).z > 0.9]
        index = max(candidates, key=lambda i: shape.Faces[i].Area)
        return "Face" + str(index + 1)

    def mesh(self, **settings):
        source, face_name = self.carrier()
        settings = dict({"LongitudePitch": 10, "LatitudePitch": 12}, **settings)
        part, mesh = create_welded_mesh(self.doc, source, face_name, **settings)
        return source, part, mesh

    def assert_valid_mesh(self, mesh):
        self.assertEqual("", mesh.Definition.LastError)
        self.assertEqual("", mesh.LastError)
        self.assertTrue(mesh.Shape.isValid())
        self.assertTrue(mesh.FlatShape.isValid())
        self.assertEqual(mesh.LongitudeWireCount + mesh.LatitudeWireCount, len(mesh.Shape.Solids))
        for solid in mesh.Shape.Solids:
            self.assertTrue(solid.isValid())
            self.assertGreater(solid.Volume, 0)

    def test_weld_envelope_unequal_diameters_and_layer_order(self):
        thickness, (lower, upper) = mesh_layers(2, 3, 0.4)
        self.assertAlmostEqual(4.6, thickness)
        self.assertAlmostEqual(2.1, upper - lower)
        self.assertAlmostEqual(-thickness / 2, lower - 1)
        self.assertAlmostEqual(thickness / 2, upper + 1.5)
        self.assertEqual((-lower, -upper), mesh_layers(2, 3, 0.4, True)[1])
        for values in [(0, 2, 0), (2, 2, -0.1), (2, 3, 2), (float("nan"), 2, 0)]:
            with self.assertRaises(ValueError):
                mesh_layers(*values)

    def test_pitch_count_single_wire_and_phase(self):
        self.assertEqual([5, 15, 25, 35], wire_positions(0, 40, "Pitch", 10, 5, 1))
        self.assertEqual([1, 20, 39], wire_positions(0, 40, "Count", 10, 3, 1))
        self.assertEqual([20], wire_positions(0, 40, "Count", 10, 1, 1))
        self.assertEqual([7, 17, 27, 37], wire_positions(0, 40, "Pitch", 10, 5, 1, 2))
        with self.assertRaises(ValueError):
            wire_positions(0, 40, "Pitch", 0, 5)
        with self.assertRaises(ValueError):
            wire_positions(0, 40, "Count", 10, 5, 30)

    def test_flat_solids_thickness_counts_and_stock_mass(self):
        _source, part, mesh = self.mesh()
        self.assert_valid_mesh(mesh)
        self.assertEqual((4, 5), (mesh.LongitudeWireCount, mesh.LatitudeWireCount))
        self.assertAlmostEqual(3.7, mesh.Shape.BoundBox.ZLength)
        self.assertAlmostEqual(3.7, mesh.FlatShape.BoundBox.ZLength)
        self.assertAlmostEqual(3.7, part.MeshThickness.Value)
        length = 4 * 60 + 5 * 40
        self.assertAlmostEqual(length, mesh.FlatWireLength.Value)
        expected_mass = math.pi * length * mesh.Definition.Density.Value
        self.assertAlmostEqual(expected_mass, mesh.Weight.Value)
        self.assertAlmostEqual(expected_mass, part.Weight.Value)
        mesh.Definition.WeldPenetration = 0.6
        self.doc.recompute()
        self.assertAlmostEqual(3.4, mesh.Shape.BoundBox.ZLength)
        self.assertAlmostEqual(expected_mass, mesh.Weight.Value)

    def test_one_part_owns_body_flats_and_one_active_sketch_pair(self):
        source, part, mesh = self.mesh()
        self.assertTrue(is_mesh_part(part))
        self.assertEqual(1, len([o for o in self.doc.Objects if o.TypeId == "App::Part"]))
        body = self.doc.getObject(part.SheetMetalBody)
        self.assertEqual([body], [o for o in part.Group if o.TypeId == "PartDesign::Body"])
        self.assertIs(body, mesh.Definition.Source[0])
        self.assertEqual(body.Tip.Name, part.Tip)
        for name in (part.FormedWire, part.FlatPattern, part.FlatSheetMetal):
            self.assertIs(self.doc.getObject(name).getParentGeoFeatureGroup(), part)
        before = [mesh.LongitudeSketch, mesh.LatitudeSketch]
        self.assertEqual(before, convert_to_editable(mesh))
        self.assertEqual(2, len([o for o in part.Group if o.isDerivedFrom("Sketcher::SketchObject")]))
        self.assertIs(source.getParentGeoFeatureGroup(), part)

    def test_body_and_sheet_flat_follow_full_welded_envelope(self):
        _source, part, mesh = self.mesh()
        body = self.doc.getObject(part.SheetMetalBody)
        flat = self.doc.getObject(part.FlatSheetMetal)
        for diameter, penetration, expected in [(2, 0.3, 3.7), (3, 0.5, 5.5)]:
            mesh.Definition.LongitudeDiameter = diameter
            mesh.Definition.WeldPenetration = penetration
            self.doc.recompute()
            self.assert_valid_mesh(mesh)
            self.assertTrue(body.Shape.isValid())
            self.assertTrue(flat.Shape.isValid())
            self.assertAlmostEqual(expected, part.Thickness.Value)
            self.assertAlmostEqual(expected, body.Shape.BoundBox.ZLength)
            self.assertAlmostEqual(expected, flat.Shape.BoundBox.ZLength)
            self.assertAlmostEqual(body.Shape.BoundBox.ZMin, mesh.Shape.BoundBox.ZMin)
            self.assertAlmostEqual(body.Shape.BoundBox.ZMax, mesh.Shape.BoundBox.ZMax)

    def test_editing_the_generated_sketch_switches_to_manual_in_place(self):
        _source, _part, mesh = self.mesh()
        sketch = mesh.LongitudeSketch
        sketch.delGeometry(0)
        self.doc.recompute()
        self.assertEqual("Editable", mesh.Definition.PatternMode)
        self.assertIs(sketch, mesh.LongitudeSketch)
        self.assertEqual(3, mesh.LongitudeWireCount)

    def test_native_body_is_reused_and_its_new_tip_drives_all_views(self):
        body = self.doc.addObject("PartDesign::Body", "CarrierBody")
        feature = body.newObject("PartDesign::Feature", "CarrierSolid")
        feature.Shape = Part.makeBox(60, 40, 1.5)
        self.doc.recompute()
        part, mesh = create_welded_mesh(self.doc, body, self.top_face(body.Shape))
        self.assertEqual(body.Name, part.SheetMetalBody)
        self.assertAlmostEqual(3.7, body.Shape.BoundBox.ZLength)
        before = mesh.FlatWireLength.Value
        # A later operation remains in the same Body and becomes the source.
        cut = body.newObject("PartDesign::Feature", "LaterCut")
        cut.Shape = body.Tip.Shape if body.Tip is not cut else mesh.Definition.Source[0].Shape
        cut.Shape = cut.Shape.cut(Part.makeCylinder(10, 10, App.Vector(30, 20, -3)))
        body.Tip = cut
        self.doc.recompute()
        self.assertEqual(cut.Name, part.Tip)
        self.assert_valid_mesh(mesh)
        self.assertLess(mesh.FlatWireLength.Value, before)

    def test_generated_pair_remains_generated_after_restore(self):
        _source, _part, mesh = self.mesh()
        name = mesh.Name
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "GeneratedMesh.FCStd")
            self.doc.saveAs(path)
            App.closeDocument(self.doc.Name)
            self.doc = App.openDocument(path)
            mesh = self.doc.getObject(name)
            self.assertEqual("Generated", mesh.Definition.PatternMode)
            mesh.Definition.LongitudePitch = 5
            self.doc.recompute()
            self.assertEqual("Generated", mesh.Definition.PatternMode)
            self.assertGreater(mesh.LongitudeWireCount, 4)
            self.assert_valid_mesh(mesh)

    def test_count_and_independent_diameters(self):
        _source, _part, mesh = self.mesh(LongitudeMode="Count", LongitudeCount=1,
                                       LatitudeMode="Count", LatitudeCount=3,
                                       SameDiameter=False, LatitudeDiameter=3)
        self.assert_valid_mesh(mesh)
        self.assertEqual((1, 3), (mesh.LongitudeWireCount, mesh.LatitudeWireCount))
        self.assertAlmostEqual(4.7, mesh.FlatShape.BoundBox.ZLength)

    def test_generator_clips_holes_into_separate_wire_pieces(self):
        source, face_name = self.carrier(holes=True)
        _part, mesh = create_welded_mesh(self.doc, source, face_name,
                                       LongitudeMode="Count", LongitudeCount=1,
                                       LatitudeMode="Count", LatitudeCount=1)
        self.assert_valid_mesh(mesh)
        self.assertEqual((2, 2), (mesh.LongitudeWireCount, mesh.LatitudeWireCount))
        self.assertAlmostEqual(60 + 40 - 24, mesh.FlatWireLength.Value, places=4)

    def test_generated_layout_tracks_carrier_dimensions(self):
        source, _part, mesh = self.mesh()
        before = mesh.FlatWireLength.Value
        source.Shape = Part.makeBox(84, 40, 1.5)
        self.doc.recompute()
        self.assert_valid_mesh(mesh)
        self.assertGreater(mesh.FlatWireLength.Value, before)
        self.assertEqual(7, mesh.LatitudeWireCount)

    def test_editable_trims_survive_pitch_change_and_carrier_recompute(self):
        source, _part, mesh = self.mesh()
        longitude, _latitude = convert_to_editable(mesh)
        geometry = longitude.Geometry[0]
        longitude.delGeometry(0)
        longitude.addGeometry(Part.LineSegment(
            geometry.StartPoint * 0.75 + geometry.EndPoint * 0.25,
            geometry.StartPoint * 0.25 + geometry.EndPoint * 0.75,
        ), False)
        snapshot = [(g.StartPoint, g.EndPoint) for g in longitude.Geometry]
        mesh.Definition.LongitudePitch = 5
        source.Shape = Part.makeBox(84, 40, 1.5)
        self.doc.recompute()
        self.assert_valid_mesh(mesh)
        self.assertEqual(snapshot, [(g.StartPoint, g.EndPoint) for g in longitude.Geometry])
        self.assertEqual(4, mesh.LongitudeWireCount)
        self.assertEqual("Editable", mesh.Definition.PatternMode)

    def test_regeneration_reuses_pair_and_is_undoable(self):
        _source, _part, mesh = self.mesh()
        self.doc.UndoMode = 1
        longitude, latitude = convert_to_editable(mesh)
        longitude.delGeometry(0)
        self.doc.recompute()
        self.doc.openTransaction("Regenerate mesh")
        regenerate_pattern(mesh)
        self.doc.commitTransaction()
        self.assertEqual("Generated", mesh.Definition.PatternMode)
        self.assertIs(mesh.LongitudeSketch, longitude)
        self.assertIs(self.doc.getObject(longitude.Name), longitude)
        self.assertEqual(4, longitude.GeometryCount)
        self.doc.undo()
        self.doc.recompute()
        self.assertIs(mesh.LongitudeSketch, longitude)
        self.assertIs(mesh.LatitudeSketch, latitude)
        self.assertEqual("Editable", mesh.Definition.PatternMode)
        self.assertEqual(3, longitude.GeometryCount)
        self.assertEqual(2, len([o for o in mesh.getParentGeoFeatureGroup().Group
                                 if o.isDerivedFrom("Sketcher::SketchObject")]))

    def test_manual_diagonal_and_construction_lines(self):
        _source, _part, mesh = self.mesh()
        longitude, _latitude = convert_to_editable(mesh)
        longitude.addGeometry(Part.LineSegment(App.Vector(2, 2, 0), App.Vector(58, 38, 0)), False)
        longitude.addGeometry(Part.LineSegment(App.Vector(-50, -50, 0), App.Vector(90, 90, 0)), True)
        self.doc.recompute()
        self.assert_valid_mesh(mesh)
        self.assertEqual(5, mesh.LongitudeWireCount)
        self.assertTrue(any("overlap" in warning for warning in mesh.Warnings))

    def test_collision_check_respects_flat_wire_ends(self):
        def line(x1, y1, x2, y2):
            return Part.makeLine(App.Vector(x1, y1, 0), App.Vector(x2, y2, 0))

        first = line(0, 0, 10, 0)
        # Cut boundaries can divide one row into pieces that share only an end.
        self.assertFalse(family_collisions([first, line(10, 0, 20, 0)], 2))
        self.assertFalse(family_collisions([first, line(10.5, 0, 20, 0)], 2))
        self.assertTrue(family_collisions([first, line(9, 0, 20, 0)], 2))
        self.assertTrue(family_collisions([first, line(0, 1, 10, 1)], 2))
        self.assertTrue(family_collisions([first, line(5, -5, 5, 5)], 2))
        self.assertFalse(family_collisions([first, line(0, 2, 10, 2)], 2))

    def test_invalid_parameters_clear_all_derived_shapes_and_recover(self):
        _source, part, mesh = self.mesh()
        mesh.Definition.WeldPenetration = 3
        self.doc.recompute()
        self.assertTrue(mesh.Shape.isNull())
        self.assertTrue(mesh.FlatShape.isNull())
        self.assertTrue(self.doc.getObject(part.FlatPattern).Shape.isNull())
        self.assertTrue(mesh.Definition.LastError)
        self.assertEqual(0, mesh.Weight.Value)
        mesh.Definition.WeldPenetration = 0.3
        self.doc.recompute()
        self.assert_valid_mesh(mesh)

    def test_invalid_spacing_cannot_silently_drop_a_family(self):
        _source, _part, mesh = self.mesh()
        mesh.Definition.LongitudePitch = 0
        self.doc.recompute()
        self.assertTrue(mesh.LongitudeSketch.LastError)
        self.assertTrue(mesh.LastError)
        self.assertTrue(mesh.Shape.isNull())

    def test_outside_wire_and_unsupported_arc_are_reported(self):
        _source, _part, mesh = self.mesh()
        longitude, _latitude = convert_to_editable(mesh)
        index = longitude.addGeometry(Part.LineSegment(App.Vector(-5, 10, 0), App.Vector(30, 10, 0)), False)
        self.doc.recompute()
        self.assertIn("outside", mesh.LastError)
        self.assertTrue(mesh.Shape.isNull())
        longitude.delGeometry(index)
        longitude.addGeometry(Part.Arc(App.Vector(2, 2, 0), App.Vector(10, 6, 0), App.Vector(18, 2, 0)), False)
        self.doc.recompute()
        self.assertIn("straight line", mesh.LastError)

    def test_single_and_opposing_bends_produce_valid_solids(self):
        first, second = _two_bend_part(self.doc)
        for source in (first, second):
            _part, mesh = create_welded_mesh(self.doc, source, self.top_face(source.Shape),
                                           LongitudePitch=18, LatitudePitch=18)
            self.assert_valid_mesh(mesh)
            self.assertGreater(mesh.Shape.BoundBox.ZLength, 10)
            self.assertAlmostEqual(3.7, mesh.FlatShape.BoundBox.ZLength)
            self.assertTrue(mesh.Warnings)

    def test_oblique_wire_crosses_bend_with_continuous_valid_sweep(self):
        first, _second = _two_bend_part(self.doc)
        mapping = MeshSurfaceMap(first.Shape, self.top_face(first.Shape), 0.5)
        bounds = mapping.boundary.BoundBox
        line = Part.makeLine(App.Vector(bounds.XMin + 4, bounds.YMin + 4, 0),
                             App.Vector(bounds.XMax - 4, bounds.YMax - 4, 0))
        path = mapping.map_line(line, -mapping.thickness / 2)
        self.assertEqual(3, len(path.Edges))
        self.assertTrue(any(e.Curve.TypeId == "Part::GeomBSplineCurve" for e in path.Edges))
        self.assertTrue(sweep_wire(path, 1).isValid())
        self.assertAlmostEqual(line.Length, path.Length, places=3)

    def test_layer_offset_changes_bend_length(self):
        first, _second = _two_bend_part(self.doc)
        mapping = MeshSurfaceMap(first.Shape, self.top_face(first.Shape), 0.5)
        bounds = mapping.boundary.BoundBox
        x = (bounds.XMin + bounds.XMax) / 2
        line = Part.makeLine(App.Vector(x, bounds.YMin, 0), App.Vector(x, bounds.YMax, 0))
        path1 = mapping.map_line(line, -mapping.thickness / 2 - 0.85)
        path2 = mapping.map_line(line, -mapping.thickness / 2 + 0.85)
        self.assertAlmostEqual(1.7 * math.pi / 2, abs(path1.Length - path2.Length), places=5)

    def test_source_placement_and_nested_container_are_applied_once(self):
        def centre(shape):
            total = sum(s.Volume for s in shape.Solids)
            return sum((s.CenterOfMass * s.Volume for s in shape.Solids), App.Vector()) * (1 / total)

        source, _part, mesh = self.mesh()
        expected = mesh.Shape.copy()
        placement = App.Placement(App.Vector(90, -20, 15), App.Rotation(App.Vector(0, 0, 1), 35))
        source.Placement = placement
        self.doc.recompute()
        self.assert_valid_mesh(mesh)
        target = expected.transformed(placement.toMatrix())
        self.assertLess(centre(mesh.Shape).distanceToPoint(centre(target)), 1e-5)
        parent = self.doc.addObject("App::Part", "CarrierAssembly")
        parent.addObject(source)
        parent.Placement = App.Placement(App.Vector(100, 0, 0), App.Rotation())
        mesh.Definition.touch()
        self.doc.recompute()
        self.assertLess(centre(mesh.Shape).distanceToPoint(centre(target) + App.Vector(100, 0, 0)), 1e-5)
        self.assertAlmostEqual(0, mesh.FlatShape.BoundBox.XMin, places=4)

    def test_save_reopen_editable_inputs_and_recompute(self):
        _source, part, mesh = self.mesh()
        longitude, _latitude = convert_to_editable(mesh)
        longitude.delGeometry(0)
        self.doc.recompute()
        part_name, mesh_name, longitude_name = part.Name, mesh.Name, longitude.Name
        volume, length = mesh.Shape.Volume, mesh.FlatWireLength.Value
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "WeldedMesh.FCStd")
            self.doc.saveAs(path)
            App.closeDocument(self.doc.Name)
            self.doc = App.openDocument(path)
            mesh = self.doc.getObject(mesh_name)
            self.assertIsInstance(mesh.Proxy, SMWeldedMesh)
            self.assertIsInstance(mesh.Definition.Proxy, SMMeshDefinition)
            self.assertIsInstance(mesh.LatitudeSketch.Proxy, SMMeshPatternSketch)
            mesh.Definition.touch()
            self.doc.recompute()
            self.assert_valid_mesh(mesh)
            self.assertEqual(longitude_name, mesh.LongitudeSketch.Name)
            self.assertEqual(3, mesh.LongitudeWireCount)
            self.assertAlmostEqual(volume, mesh.Shape.Volume, places=4)
            self.assertAlmostEqual(length, mesh.FlatWireLength.Value)
            self.assertAlmostEqual(mesh.Weight.Value, self.doc.getObject(part_name).Weight.Value)
            App.closeDocument(self.doc.Name)
            self.doc = None

    def test_flat_workspace_recognizes_mesh_but_never_forwarded_links(self):
        _source, part, mesh = self.mesh()
        flat = self.doc.getObject(part.FlatPattern)
        self.assertTrue(_isFlatPatternObject(flat))
        self.assertFalse(_isUnfoldObject(flat))
        self.assertFalse(_isFlatPatternObject(mesh))
        link = self.doc.addObject("App::Link", "FlatPresentation")
        link.LinkedObject = flat
        self.assertFalse(_isFlatPatternObject(link))

    def test_flat_workspace_packs_wires_in_xy_for_solids_and_centrelines(self):
        _source, part, mesh = self.mesh()
        flat = self.doc.getObject(part.FlatPattern)
        link = self.doc.addObject("App::Link", "FlatPresentation")
        link.LinkedObject = flat
        for representation in ("Solid wires", "Centrelines"):
            mesh.Definition.Representation = representation
            self.doc.recompute()
            self.assertEqual([link], arrangeFlatPatternLinks([link]))
            self.assertAlmostEqual(60, link.Shape.BoundBox.XLength)
            self.assertAlmostEqual(40, link.Shape.BoundBox.YLength)
            self.assertAlmostEqual(0, link.Shape.BoundBox.ZMin, places=5)
            self.assertLess(link.Shape.BoundBox.ZLength, 4)

    def test_nested_carrier_placement_change_updates_mesh_dependency(self):
        source, face_name = self.carrier()
        parent = self.doc.addObject("App::Part", "CarrierAssembly")
        parent.addObject(source)
        _part, mesh = create_welded_mesh(self.doc, source, face_name)
        self.assertIs(source.getParentGeoFeatureGroup(), parent)
        self.assertIs(_part, parent)
        self.assertEqual(1, len([o for o in parent.Group if o.TypeId == "PartDesign::Body"]))
        self.assertNotIn(parent, mesh.Definition.OutList)
        before = mesh.getGlobalPlacement().Base.x
        parent.Placement.Base = App.Vector(100, 0, 0)
        self.doc.recompute()
        self.assertAlmostEqual(before + 100, mesh.getGlobalPlacement().Base.x, places=5)

    def test_carrier_face_selected_through_part_container(self):
        source, face_name = self.carrier()
        parent = self.doc.addObject("App::Part", "CarrierAssembly")
        parent.addObject(source)
        _part, mesh = create_welded_mesh(self.doc, parent, source.Name + "." + face_name)
        body = self.doc.getObject(_part.SheetMetalBody)
        self.assertIs(mesh.Definition.Source[0], body)
        self.assertIs(body.Tip.Source, source)
        self.assertIs(source.getParentGeoFeatureGroup(), parent)
        self.assert_valid_mesh(mesh)

    def test_unfolder_default_contract_stays_compatible(self):
        first, _second = _two_bend_part(self.doc)
        index = int(self.top_face(first.Shape)[4:]) - 1
        bac = BendAllowanceCalculator.from_single_value(0.5, "ansi")
        edges, bends = unfold(first.Shape, index, bac)
        patches = []
        mapped_edges, mapped_bends = unfold(first.Shape, index, bac, face_map=patches)
        self.assertEqual(len(edges), len(mapped_edges))
        self.assertEqual(len(bends), len(mapped_bends))
        self.assertEqual(3, len(patches))
        self.assertAlmostEqual(sum(e.Length for e in edges), sum(e.Length for e in mapped_edges), places=7)

    def test_centreline_preview_retains_lengths_and_weight(self):
        _source, _part, mesh = self.mesh()
        mass, length = mesh.Weight.Value, mesh.FlatWireLength.Value
        mesh.Definition.Representation = "Centrelines"
        self.doc.recompute()
        self.assertEqual("", mesh.LastError)
        self.assertEqual(0, len(mesh.Shape.Solids))
        self.assertEqual(9, len(mesh.Shape.Edges))
        self.assertAlmostEqual(mass, mesh.Weight.Value)
        self.assertAlmostEqual(length, mesh.FlatWireLength.Value)


if __name__ == "__main__":
    unittest.main()
