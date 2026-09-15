########################################################################
#
#  Copyright 2026 Adrian
#  This program is free software under the GNU Lesser General Public
#  License, version 2 or (at your option) any later version.
#  This program is distributed WITHOUT ANY WARRANTY.
#
########################################################################

"""Flat wire layouts and their geometric mapping onto a SheetMetal carrier.

The carrier's developed coordinates are a layout reference, not a physical
simulation of welded mesh forming. Each Sketcher line is one wire; cylindrical
patches support perpendicular, axial, and oblique crossings.
"""

import math

import FreeCAD as App
import Part

from SheetMetalNewUnfolder import (
    BendAllowanceCalculator, BendDirection, Edge2DCleanup, EstimateThickness,
    SketchExtraction, UVRef, unfold,
)

translate = App.Qt.translate
TOLERANCE = 1e-6
MAX_WIRES = 2000


def mesh_layers(longitude_diameter, latitude_diameter, penetration, reverse=False):
    """Return envelope thickness and family centres about its midplane."""
    dl, dt, p = map(float, (longitude_diameter, latitude_diameter, penetration))
    if not all(math.isfinite(v) for v in (dl, dt, p)) or min(dl, dt) <= 0:
        raise ValueError(translate("SheetMetal", "Wire diameters must be positive."))
    if p < 0 or p >= min(dl, dt):
        raise ValueError(translate(
            "SheetMetal", "Weld penetration must be nonnegative and less than either diameter."
        ))
    thickness = dl + dt - p
    centres = (-thickness / 2 + dl / 2, thickness / 2 - dt / 2)
    if reverse:
        centres = tuple(-c for c in centres)
    return thickness, centres


def wire_positions(low, high, mode, pitch, count, margin=0.0, offset=0.0):
    """Pitch is centred, with an optional phase offset; count includes ends."""
    if not all(math.isfinite(float(v)) for v in (low, high, pitch, margin, offset)):
        raise ValueError(translate("SheetMetal", "Wire spacing values must be finite."))
    if margin < 0 or high - low < 2 * margin - TOLERANCE:
        raise ValueError(translate("SheetMetal", "Wire margins exceed the flat pattern size."))
    start, end = low + margin, high - margin
    if mode == "Count":
        count = int(count)
        if count < 1 or count > MAX_WIRES:
            raise ValueError(translate("SheetMetal", "Wire count must be between 1 and 2000."))
        if count == 1:
            return [(start + end) / 2]
        if end - start < TOLERANCE:
            raise ValueError(translate("SheetMetal", "Multiple wires need a nonzero spacing span."))
        return [start + (end - start) * i / (count - 1) for i in range(count)]
    if mode != "Pitch" or pitch <= TOLERANCE:
        raise ValueError(translate("SheetMetal", "Wire pitch must be greater than zero."))
    count = int(math.floor((end - start) / pitch + TOLERANCE)) + 1
    if count > MAX_WIRES:
        raise ValueError(translate("SheetMetal", "Wire pitch produces more than 2000 wires."))
    first = (start + end - (count - 1) * pitch) / 2
    # Phase remains meaningful across a full pitch without changing the bounds.
    phase = offset % pitch
    candidates = [first + phase + i * pitch for i in range(-1, count + 1)]
    return [v for v in candidates if start - TOLERANCE <= v <= end + TOLERANCE]


def _face_from_edges(edges):
    wires = Edge2DCleanup.clean_and_structure_geometry(edges)
    face = Part.makeFace(wires, "Part::FaceMakerBullseye")
    if face.isNull() or not face.isValid():
        raise ValueError(translate("SheetMetal", "The developed carrier boundary is invalid."))
    return face


class MeshSurfaceMap:
    """Complete, transient map from a stable XY development to source space."""

    def __init__(self, shape, face_name, k_factor=0.5):
        if shape.isNull() or not shape.isValid() or len(shape.Solids) != 1:
            raise ValueError(translate("SheetMetal", "Select a valid single-solid sheet-metal carrier."))
        if not face_name.startswith("Face") or not face_name[4:].isdigit():
            raise ValueError(translate("SheetMetal", "Select one planar carrier face."))
        index = int(face_name[4:]) - 1
        if not 0 <= index < len(shape.Faces):
            raise ValueError(translate("SheetMetal", "The carrier reference face is missing."))
        self.root_face = shape.Faces[index]
        if self.root_face.Surface.TypeId != "Part::GeomPlane":
            raise ValueError(translate("SheetMetal", "The mesh reference face must be planar."))
        if not 0 <= k_factor <= 1:
            raise ValueError(translate("SheetMetal", "The carrier K-factor must be between zero and one."))
        self.thickness = EstimateThickness.using_best_method(shape, index)
        self.bac = BendAllowanceCalculator.from_single_value(k_factor, "ansi")
        entries = []
        edges, bends = unfold(shape, index, self.bac, face_map=entries)
        self.alignment = SketchExtraction.move_to_origin(Part.makeCompound(edges), self.root_face)
        self.boundary = _face_from_edges([e.transformed(self.alignment) for e in edges])
        self.bend_lines = Part.makeCompound([b.line.transformed(self.alignment) for b in bends])
        self.patches = []
        for entry in entries:
            entry["region"] = _face_from_edges(
                [e.transformed(self.alignment) for e in entry["boundary"]]
            )
            transform = self.alignment * entry["transform"]
            if "alignment" in entry:
                transform = transform * entry["alignment"]
                u0, u1, v0, v1 = entry["face"].ParameterRange
                entry["allowance"] = self.bac.get_bend_allowance(
                    BendDirection.from_face(entry["face"]),
                    entry["face"].Surface.Radius, self.thickness, u1 - u0,
                )
            entry["inverse"] = transform.inverse()
            self.patches.append(entry)
        # Overlapping developed panels have no unique inverse mapping.
        for i, first in enumerate(self.patches):
            for second in self.patches[i + 1:]:
                if first["region"].common(second["region"]).Area > 1e-5:
                    raise ValueError(translate("SheetMetal", "The carrier unfolds to overlapping panels."))

    def map_point(self, patch, point, offset):
        local = patch["inverse"].multVec(point)
        face = patch["face"]
        if "alignment" not in patch:
            return local + face.normalAt(0, 0) * offset
        u0, u1, v0, v1 = face.ParameterRange
        x, y = local.x, local.y
        if patch["uvref"] in (UVRef.BOTTOM_RIGHT, UVRef.TOP_RIGHT):
            x = v1 - v0 - x
        if patch["uvref"] in (UVRef.TOP_LEFT, UVRef.TOP_RIGHT):
            y = patch["allowance"] - y
        u, v = u0 + y / patch["allowance"] * (u1 - u0), v0 + x
        return face.valueAt(u, v) + face.normalAt(u, v) * offset

    def map_line(self, edge, offset):
        """Split a flat line at patch boundaries and join its formed segments."""
        start, end = edge.Vertexes[0].Point, edge.Vertexes[-1].Point
        vector = end - start
        length = vector.Length
        if length <= TOLERANCE:
            raise ValueError(translate("SheetMetal", "A mesh wire has zero length."))
        direction = vector * (1 / length)
        intervals = []
        for patch in self.patches:
            for segment in patch["region"].common(edge).Edges:
                bounds = sorted((p.Point - start).dot(direction) for p in segment.Vertexes)
                if len(bounds) >= 2 and bounds[-1] - bounds[0] > TOLERANCE:
                    intervals.append((max(0.0, bounds[0]), min(length, bounds[-1]), patch))
        cuts = sorted([0.0, length] + [t for a, b, _ in intervals for t in (a, b)])
        unique = []
        for t in cuts:
            if not unique or t - unique[-1] > TOLERANCE:
                unique.append(t)
        segments = []
        for a, b in zip(unique, unique[1:]):
            if b - a <= TOLERANCE:
                continue
            middle = (a + b) / 2
            patch = next((p for lo, hi, p in intervals if lo - TOLERANCE <= middle <= hi + TOLERANCE), None)
            if patch is None:
                raise ValueError(translate(
                    "SheetMetal", "An editable wire extends outside the carrier's flat boundary. Trim it or update the carrier."
                ))
            p0, pm, p1 = [self.map_point(patch, start + direction * t, offset) for t in (a, middle, b)]
            if "alignment" not in patch:
                segment = Part.makeLine(p0, p1)
            else:
                local_delta = patch["inverse"].multVec(start + direction * b) - patch["inverse"].multVec(start + direction * a)
                if abs(local_delta.y) < TOLERANCE:
                    segment = Part.makeLine(p0, p1)
                elif abs(local_delta.x) < TOLERANCE:
                    segment = Part.Arc(p0, pm, p1).toShape()
                else:
                    # An oblique straight line develops to a helix. Interpolate
                    # with small angular steps and exact endpoint tangents.
                    u0, u1, _v0, _v1 = patch["face"].ParameterRange
                    span = abs(local_delta.y / patch["allowance"] * (u1 - u0))
                    samples = max(8, int(math.ceil(span / math.radians(3))))
                    points = [self.map_point(patch, start + direction * (a + (b - a) * i / samples), offset) for i in range(samples + 1)]
                    h = min(1e-4, (b - a) / 1000)
                    tangent0 = self.map_point(patch, start + direction * (a + h), offset) - p0
                    tangent1 = p1 - self.map_point(patch, start + direction * (b - h), offset)
                    curve = Part.BSplineCurve()
                    curve.interpolate(Points=points, InitialTangent=tangent0, FinalTangent=tangent1)
                    segment = curve.toShape()
            if segments and segments[-1].Vertexes[-1].Point.distanceToPoint(segment.Vertexes[0].Point) > 1e-4:
                raise ValueError(translate("SheetMetal", "The carrier has a discontinuous bend mapping."))
            segments.append(segment)
        if not segments:
            raise ValueError(translate("SheetMetal", "The wire does not intersect the flat carrier."))
        result = Part.Wire(segments)
        if not result.isValid():
            raise ValueError(translate("SheetMetal", "The formed wire path is invalid."))
        return result


def generate_lines(boundary, family, mode, pitch, count, margin, offset):
    bounds = boundary.BoundBox
    longitude = family == "Longitude"
    low, high = (bounds.YMin, bounds.YMax) if longitude else (bounds.XMin, bounds.XMax)
    positions = wire_positions(low, high, mode, pitch, count, margin, offset)
    result = []
    for position in positions:
        if longitude:
            line = Part.makeLine(App.Vector(bounds.XMin - 1, position, 0), App.Vector(bounds.XMax + 1, position, 0))
        else:
            line = Part.makeLine(App.Vector(position, bounds.YMin - 1, 0), App.Vector(position, bounds.YMax + 1, 0))
        for edge in boundary.common(line).Edges:
            if edge.Length > TOLERANCE:
                result.append(Part.LineSegment(edge.Vertexes[0].Point, edge.Vertexes[-1].Point))
    if len(result) > MAX_WIRES:
        raise ValueError(translate("SheetMetal", "The clipped pattern exceeds 2000 wires."))
    return result


def sketch_lines(sketch, reference_placement=None):
    if sketch is None:
        raise ValueError(translate("SheetMetal", "A wire sketch is missing."))
    if getattr(sketch, "LastError", ""):
        raise ValueError(sketch.LastError)
    placement = sketch.Placement
    if reference_placement is not None:
        placement = reference_placement.inverse() * placement
    lines = []
    for index, geometry in enumerate(sketch.Geometry):
        if sketch.getConstruction(index):
            continue
        if not isinstance(geometry, Part.LineSegment):
            raise ValueError(translate("SheetMetal", "Wire sketches currently support straight line segments only."))
        start = placement.multVec(geometry.StartPoint)
        end = placement.multVec(geometry.EndPoint)
        if abs(start.z) > TOLERANCE or abs(end.z) > TOLERANCE:
            raise ValueError(translate("SheetMetal", "Wire sketches must remain in the flat XY plane."))
        if start.distanceToPoint(end) <= TOLERANCE:
            raise ValueError(translate("SheetMetal", "A mesh wire has zero length."))
        lines.append(Part.makeLine(start, end))
    if len(lines) > MAX_WIRES:
        raise ValueError(translate("SheetMetal", "A wire sketch exceeds 2000 wires."))
    return lines


def sweep_wire(path, diameter):
    edge = path.Edges[0]
    start = edge.valueAt(edge.FirstParameter)
    tangent = edge.tangentAt(edge.FirstParameter)
    profile = Part.Wire([Part.makeCircle(diameter / 2, start, tangent)])
    solid = path.makePipeShell([profile], True, False)
    if solid.isNull() or not solid.isValid() or len(solid.Solids) != 1 or solid.Volume <= 0:
        raise ValueError(translate("SheetMetal", "A wire cannot be swept through this bend. Increase the bend radius or reduce its diameter."))
    return solid


def family_collisions(lines, diameter):
    """Detect same-layer overlaps in the flat preparation, including diagonals."""
    solids = {}

    def cylinder(index):
        if index not in solids:
            edge = lines[index]
            start = edge.valueAt(edge.FirstParameter)
            solids[index] = Part.makeCylinder(diameter / 2, edge.Length, start,
                                              edge.tangentAt(edge.FirstParameter))
        return solids[index]

    for i, first in enumerate(lines):
        box = first.BoundBox
        for j, second in enumerate(lines[i + 1:], i + 1):
            other = second.BoundBox
            if (box.XMax + diameter < other.XMin or other.XMax + diameter < box.XMin
                    or box.YMax + diameter < other.YMin or other.YMax + diameter < box.YMin):
                continue
            if first.distToShape(second)[0] < diameter - TOLERANCE:
                # Centreline distance treats ends as hemispheres. Our wires
                # have flat caps: adjoining pieces at an opening's boundary,
                # and collinear pieces separated by a gap, do not overlap.
                if cylinder(i).common(cylinder(j)).Volume > TOLERANCE ** 3:
                    return True
    return False
