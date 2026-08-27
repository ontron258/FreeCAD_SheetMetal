########################################################################
#
#  SheetMetalShapedFlangeCmd.py
#
#  Copyright 2026 SheetMetal Workbench contributors
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
########################################################################

"""Create a folded sheet from closed sketches on intersecting planes.

Each sketch describes the reference surface of a panel.  Two straight,
coincident boundary edges are replaced by a constant-thickness cylindrical
bend.  The reference surface is the sheet mid-plane, which makes the result
independent of sketch winding and normal direction.
"""

import math
import os

import FreeCAD
import Part

import SheetMetalMaterial
import SheetMetalTools


translate = FreeCAD.Qt.translate
icons_path = SheetMetalTools.icons_path
smEpsilon = SheetMetalTools.smEpsilon

smShapedFlangeDefaultVars = ["BendRadius"]
RELIEF_TYPES = ["None", "Tear", "Rectangle", "Round"]


def _ensure_enumeration_options(obj, property_name, options, default):
    """Add new enum choices to saved objects without changing their selection."""
    current = str(getattr(obj, property_name))
    if obj.getEnumerationsOfProperty(property_name) == options:
        return
    setattr(obj, property_name, options)
    setattr(obj, property_name, current if current in options else default)


def _unit(vector, message):
    """Return a normalized copy or report a useful modeling error."""
    result = FreeCAD.Vector(vector)
    if result.Length <= smEpsilon:
        raise ValueError(message)
    result.normalize()
    return result


def _scaled(vector, factor):
    """Scale a copy (FreeCAD.Vector.multiply mutates its receiver)."""
    result = FreeCAD.Vector(vector)
    result.multiply(factor)
    return result


def _region_id(sketch, wire_index):
    """Return a compact, document-stable identifier for a sketch loop."""
    sketch_name = getattr(sketch, "Name", sketch.Label)
    return "{}:Wire{}".format(sketch_name, wire_index + 1)


def _profile_regions(sketch):
    """Detect independently selectable closed-loop regions in a sketch."""
    shape = sketch.Shape.copy()
    if shape.isNull() or not shape.Wires:
        raise ValueError(
            translate("SheetMetal", "%1 does not contain a closed wire.").replace(
                "%1", sketch.Label
            )
        )
    regions = []
    for wire_index, wire in enumerate(shape.Wires):
        if not wire.isClosed():
            continue
        face = Part.Face(wire)
        if face.isNull() or not face.isValid():
            raise ValueError(
                translate("SheetMetal", "%1 contains an invalid closed loop.").replace(
                    "%1", sketch.Label
                )
            )
        normal = _unit(face.normalAt(0.0, 0.0), "Invalid panel normal.")
        if face.Vertexes:
            point = face.Vertexes[-1].Point
            if abs(point.sub(face.Vertexes[0].Point).dot(normal)) > smEpsilon * 10.0:
                raise ValueError(
                    translate("SheetMetal", "%1 is not planar.").replace(
                        "%1", sketch.Label
                    )
                )
        regions.append(
            {
                "id": _region_id(sketch, wire_index),
                "sketch": sketch,
                "wire_index": wire_index,
                "face": face,
                "depth": 0,
                "default": "Add",
            }
        )

    if not regions:
        raise ValueError(
            translate("SheetMetal", "%1 does not define an enclosed face.").replace(
                "%1", sketch.Label
            )
        )

    # Strict containment creates Inventor-like automatic hole/island parity.
    # Partial overlaps are not containment, so both loops default to Add.
    area_tolerance = max(smEpsilon, sum(region["face"].Area for region in regions) * 1.0e-9)
    for region in regions:
        for possible_parent in regions:
            if possible_parent is region:
                continue
            if possible_parent["face"].Area <= region["face"].Area + area_tolerance:
                continue
            common = region["face"].common(possible_parent["face"])
            if abs(common.Area - region["face"].Area) <= area_tolerance:
                region["depth"] += 1
        region["default"] = "Subtract" if region["depth"] % 2 else "Add"
    return regions


def _operation_map(entries):
    operations = {}
    for entry in entries or []:
        region_id, separator, operation = entry.partition("=")
        if separator and operation in ("Add", "Subtract", "Ignore"):
            operations[region_id] = operation
    return operations


def _closed_planar_faces(sketch, region_operations=None):
    """Build selected panel faces by combining detected sketch regions."""
    regions = _profile_regions(sketch)
    operations = _operation_map(region_operations)
    result = None
    for depth in sorted(set(region["depth"] for region in regions)):
        level = [region for region in regions if region["depth"] == depth]
        additions = [
            region["face"] for region in level
            if operations.get(region["id"], region["default"]) == "Add"
        ]
        subtractions = [
            region["face"] for region in level
            if operations.get(region["id"], region["default"]) == "Subtract"
        ]
        if additions:
            added = additions[0]
            if len(additions) > 1:
                added = added.multiFuse(additions[1:])
            result = added if result is None else result.fuse(added)
        if result is not None:
            for subtraction in subtractions:
                result = result.cut(subtraction)

    if result is None or result.isNull() or not result.Faces:
        raise ValueError(
            translate("SheetMetal", "%1 has no additive profile regions.").replace(
                "%1", sketch.Label
            )
        )
    return list(result.removeSplitter().Faces)


def _straight_edge_overlap(first, second, tolerance):
    """Return the common collinear segment of two straight edges, if any."""
    if not isinstance(first.Curve, Part.Line) or not isinstance(second.Curve, Part.Line):
        return None
    a0, a1 = first.Vertexes[0].Point, first.Vertexes[-1].Point
    b0, b1 = second.Vertexes[0].Point, second.Vertexes[-1].Point
    length = a0.distanceToPoint(a1)
    if length <= tolerance:
        return None
    direction = _unit(a1.sub(a0), "A panel boundary edge has zero length.")

    # Distance from each endpoint to the first edge's infinite line.
    if direction.cross(b0.sub(a0)).Length > tolerance:
        return None
    if direction.cross(b1.sub(a0)).Length > tolerance:
        return None

    second_parameters = [b0.sub(a0).dot(direction), b1.sub(a0).dot(direction)]
    overlap_start = max(0.0, min(second_parameters))
    overlap_end = min(length, max(second_parameters))
    if overlap_end - overlap_start <= tolerance:
        return None
    return (
        a0.add(_scaled(direction, overlap_start)),
        a0.add(_scaled(direction, overlap_end)),
    )


def _inward_direction(face, edge, axis):
    """Find the direction from a boundary edge into its panel face."""
    normal = _unit(face.normalAt(0.0, 0.0), "Invalid panel normal.")
    inward = _unit(normal.cross(axis), "Panel edge and normal are parallel.")
    midpoint = edge.valueAt((edge.FirstParameter + edge.LastParameter) * 0.5)
    toward_face = face.CenterOfMass.sub(midpoint)
    if toward_face.dot(inward) < 0.0:
        inward = inward.negative()
    return inward


def _bend_data(
    first_face, first_edge, second_face, second_edge,
    overlap_start, overlap_end, radius, thickness, bend_plane,
):
    """Calculate tangency geometry for a shared edge, or None if flat."""
    p0 = overlap_start
    p1 = overlap_end
    axis = _unit(p1.sub(p0), "A shared edge has zero length.")
    inward1 = _inward_direction(first_face, first_edge, axis)
    inward2 = _inward_direction(second_face, second_edge, axis)
    dot = max(-1.0, min(1.0, inward1.dot(inward2)))
    panel_angle = math.acos(dot)

    # Opposing inward directions are two halves of one flat panel.
    if abs(math.pi - panel_angle) <= 1.0e-7:
        return None
    if panel_angle <= 1.0e-7:
        raise ValueError(
            translate("SheetMetal", "A 180 degree fold cannot be built from coincident panels.")
        )

    if bend_plane == "Inside":
        reference_radius = radius
    elif bend_plane == "Outside":
        reference_radius = radius + thickness
    else:
        reference_radius = radius + thickness * 0.5
    half_angle = panel_angle * 0.5
    setback = reference_radius / math.tan(half_angle)
    bisector = _unit(inward1.add(inward2), "Cannot determine bend bisector.")
    center = p0.add(_scaled(bisector, reference_radius / math.sin(half_angle)))
    tangent1 = p0.add(_scaled(inward1, setback))
    tangent2 = p0.add(_scaled(inward2, setback))
    radial1 = _unit(tangent1.sub(center), "Cannot determine first bend tangent.")
    radial2 = _unit(tangent2.sub(center), "Cannot determine second bend tangent.")
    radial_mid = _unit(radial1.add(radial2), "Cannot determine bend arc.")

    return {
        "p0": p0,
        "p1": p1,
        "axis": axis,
        "length": p0.distanceToPoint(p1),
        "inward1": inward1,
        "inward2": inward2,
        "setback": setback,
        "center": center,
        "radial1": radial1,
        "radial2": radial2,
        "radial_mid": radial_mid,
    }


def _trim_strip(p0, p1, inward, setback):
    """Create the planar bend allowance strip removed from a panel."""
    q0 = p0.add(_scaled(inward, setback))
    q1 = p1.add(_scaled(inward, setback))
    wire = Part.makePolygon([p0, p1, q1, q0, p0])
    return Part.Face(wire)


def _bend_solid(data, radius, thickness, start_offset=0.0, end_offset=0.0):
    """Build an annular cylindrical sector along a shared edge."""
    length = data["length"] - start_offset - end_offset
    if length <= smEpsilon:
        raise ValueError(
            translate("SheetMetal", "Bend reliefs consume the complete bend length.")
        )
    center = data["center"].add(_scaled(data["axis"], start_offset))
    radial1 = data["radial1"]
    radial2 = data["radial2"]
    radial_mid = data["radial_mid"]
    outer_radius = radius + thickness

    inner1 = center.add(_scaled(radial1, radius))
    outer1 = center.add(_scaled(radial1, outer_radius))
    inner2 = center.add(_scaled(radial2, radius))
    outer2 = center.add(_scaled(radial2, outer_radius))
    outer_mid = center.add(_scaled(radial_mid, outer_radius))
    inner_mid = center.add(_scaled(radial_mid, radius))

    edges = [
        Part.makeLine(inner1, outer1),
        Part.Arc(outer1, outer_mid, outer2).toShape(),
        Part.makeLine(outer2, inner2),
        Part.Arc(inner2, inner_mid, inner1).toShape(),
    ]
    section = Part.Face(Part.Wire(edges))
    return section.extrude(_scaled(data["axis"], length))


def makeShapedFlange(
    sketches, thickness=1.0, radius=1.0, refine=True, bend_plane="Centered",
    region_operations=None,
):
    """Make a constant-thickness folded solid from linked closed sketches."""
    if not sketches:
        raise ValueError(translate("SheetMetal", "Select at least one closed sketch."))
    if thickness <= smEpsilon:
        raise ValueError(translate("SheetMetal", "Thickness must be greater than zero."))
    if radius <= smEpsilon:
        raise ValueError(translate("SheetMetal", "Bend radius must be greater than zero."))
    if bend_plane not in ("Outside", "Inside", "Centered"):
        raise ValueError(translate("SheetMetal", "Unknown bend plane setting."))

    panels = []
    for sketch in sketches:
        for face in _closed_planar_faces(sketch, region_operations):
            panels.append(
                {"sketch": sketch, "face": face, "cuts": [], "thickness_dirs": []}
            )

    bends = []
    links = [set() for _panel in panels]
    tolerance = max(smEpsilon * 10.0, thickness * 1.0e-6)
    for first_index in range(len(panels)):
        first = panels[first_index]
        for second_index in range(first_index + 1, len(panels)):
            second = panels[second_index]
            for first_edge in first["face"].Edges:
                for second_edge in second["face"].Edges:
                    overlap = _straight_edge_overlap(first_edge, second_edge, tolerance)
                    if overlap is None:
                        continue
                    data = _bend_data(
                        first["face"], first_edge, second["face"], second_edge,
                        overlap[0], overlap[1], radius, thickness, bend_plane,
                    )
                    links[first_index].add(second_index)
                    links[second_index].add(first_index)
                    if data is not None:
                        first["cuts"].append(
                            _trim_strip(data["p0"], data["p1"], data["inward1"], data["setback"])
                        )
                        second["cuts"].append(
                            _trim_strip(data["p0"], data["p1"], data["inward2"], data["setback"])
                        )
                        first["thickness_dirs"].append(data["radial1"])
                        second["thickness_dirs"].append(data["radial2"])
                        bends.append(_bend_solid(data, radius, thickness))

    if len(panels) > 1:
        reached = {0}
        pending = [0]
        while pending:
            current = pending.pop()
            for neighbor in links[current]:
                if neighbor not in reached:
                    reached.add(neighbor)
                    pending.append(neighbor)
        if len(reached) != len(panels):
            raise ValueError(
                translate(
                    "SheetMetal",
                    "All sketch panels must form one connected part by sharing "
                    "complete straight edges.",
                )
            )

    solids = []
    for panel in panels:
        face = panel["face"]
        for cut in panel["cuts"]:
            face = face.cut(cut)
        if face.isNull() or not face.Faces:
            raise ValueError(
                translate(
                    "SheetMetal",
                    "Bend radius is too large for one of the selected sketch panels.",
                )
            )
        for trimmed_face in face.Faces:
            normal = _unit(trimmed_face.normalAt(0.0, 0.0), "Invalid panel normal.")
            if bend_plane == "Centered":
                solid = trimmed_face.extrude(_scaled(normal, thickness))
                solid.translate(_scaled(normal, -0.5 * thickness))
            else:
                thickness_dirs = panel["thickness_dirs"]
                direction = thickness_dirs[0] if thickness_dirs else normal
                for other_direction in thickness_dirs[1:]:
                    if direction.dot(other_direction) < 1.0 - 1.0e-7:
                        raise ValueError(
                            translate(
                                "SheetMetal",
                                "Inside/Outside bend plane is inconsistent across one panel; "
                                "use Centered for opposing bends.",
                            )
                        )
                if bend_plane == "Outside":
                    direction = direction.negative()
                solid = trimmed_face.extrude(_scaled(direction, thickness))
            solids.append(solid)

    solids.extend(bends)
    if not solids:
        raise ValueError(translate("SheetMetal", "No sheet-metal solid could be created."))
    result = solids[0] if len(solids) == 1 else solids[0].multiFuse(solids[1:])
    if refine:
        result = result.removeSplitter()
    if result.isNull() or not result.Solids:
        raise ValueError(
            translate("SheetMetal", "The selected sketches do not make a valid solid.")
        )
    return result


def _sketch_normal(sketch):
    """Return sketch +Z in the coordinate system used by ``sketch.Shape``.

    Sketch geometry already includes the sketch's placement relative to its
    containing Body/Part, but it does not include the placement of that
    container.  Using ``getGlobalPlacement()`` therefore mixes global vectors
    with local face geometry and produces a zero-volume extrusion when the
    owning Part is rotated.
    """
    placement = getattr(sketch, "Placement", None)
    if placement is None and hasattr(sketch, "getGlobalPlacement"):
        # Keep lightweight/test profile objects without a Placement property
        # working; real FreeCAD sketches always take the local branch above.
        placement = sketch.getGlobalPlacement()
    if placement is None:
        return _unit(FreeCAD.Vector(0, 0, 1), "Invalid sketch normal.")
    return _unit(
        placement.Rotation.multVec(FreeCAD.Vector(0, 0, 1)),
        "Invalid sketch normal.",
    )


def _stage_mid_offset(sketch, thickness, thickness_side):
    normal = _sketch_normal(sketch)
    if thickness_side == "Normal":
        return _scaled(normal, thickness * 0.5)
    if thickness_side == "Reversed":
        return _scaled(normal, -thickness * 0.5)
    return FreeCAD.Vector(0, 0, 0)


def _midplane_intersection_point(p0, first_face, first_offset, second_face, second_offset):
    """Intersect two offset panel mid-planes at the bend cross-section."""
    normal1 = _unit(first_face.normalAt(0.0, 0.0), "Invalid first panel normal.")
    normal2 = _unit(second_face.normalAt(0.0, 0.0), "Invalid second panel normal.")
    cosine = normal1.dot(normal2)
    denominator = 1.0 - cosine * cosine
    if abs(denominator) <= 1.0e-10:
        raise ValueError(translate("SheetMetal", "Cannot intersect parallel panel planes."))
    distance1 = normal1.dot(first_offset)
    distance2 = normal2.dot(second_offset)
    coefficient1 = (distance1 - cosine * distance2) / denominator
    coefficient2 = (distance2 - cosine * distance1) / denominator
    return p0.add(
        _scaled(normal1, coefficient1).add(_scaled(normal2, coefficient2))
    )


def _staged_bend_data(
    first, first_edge, second, second_edge,
    overlap_start, overlap_end, radius, thickness,
):
    """Build bend tangency data between independently offset panel mid-planes."""
    p0 = overlap_start
    p1 = overlap_end
    axis = _unit(p1.sub(p0), "A shared edge has zero length.")
    inward1 = _inward_direction(first["face"], first_edge, axis)
    inward2 = _inward_direction(second["face"], second_edge, axis)
    dot = max(-1.0, min(1.0, inward1.dot(inward2)))
    panel_angle = math.acos(dot)
    if abs(math.pi - panel_angle) <= 1.0e-7:
        return None
    if panel_angle <= 1.0e-7:
        raise ValueError(
            translate("SheetMetal", "A 180 degree fold cannot be built from coincident panels.")
        )

    origin = _midplane_intersection_point(
        p0,
        first["face"], first["mid_offset"],
        second["face"], second["mid_offset"],
    )
    mid_radius = radius + thickness * 0.5
    half_angle = panel_angle * 0.5
    setback = mid_radius / math.tan(half_angle)
    bisector = _unit(inward1.add(inward2), "Cannot determine bend bisector.")
    center = origin.add(_scaled(bisector, mid_radius / math.sin(half_angle)))
    tangent_mid1 = origin.add(_scaled(inward1, setback))
    tangent_mid2 = origin.add(_scaled(inward2, setback))
    tangent_ref1 = tangent_mid1.sub(first["mid_offset"])
    tangent_ref2 = tangent_mid2.sub(second["mid_offset"])
    radial1 = _unit(tangent_mid1.sub(center), "Cannot determine first bend tangent.")
    radial2 = _unit(tangent_mid2.sub(center), "Cannot determine second bend tangent.")
    radial_mid = _unit(radial1.add(radial2), "Cannot determine bend arc.")

    tangent_end1 = tangent_ref1.add(_scaled(axis, p0.distanceToPoint(p1)))
    tangent_end2 = tangent_ref2.add(_scaled(axis, p0.distanceToPoint(p1)))
    cut1 = Part.Face(Part.makePolygon([p0, p1, tangent_end1, tangent_ref1, p0]))
    cut2 = Part.Face(Part.makePolygon([p0, p1, tangent_end2, tangent_ref2, p0]))
    return {
        "p0": p0,
        "p1": p1,
        "axis": axis,
        "length": p0.distanceToPoint(p1),
        "center": center,
        "radial1": radial1,
        "radial2": radial2,
        "radial_mid": radial_mid,
        "inward1": inward1,
        "inward2": inward2,
        "relief_base_depth1": max(
            0.0, tangent_ref1.sub(p0).dot(inward1)
        ),
        "relief_base_depth2": max(
            0.0, tangent_ref2.sub(p0).dot(inward2)
        ),
        "cut1": cut1,
        "cut2": cut2,
    }


def _bend_relief_face(endpoint, bend_direction, inward, width, depth, relief_type):
    """Create one planar bend-end slot in a panel reference plane.

    A tear is represented by a finite-width rectangular kerf because a
    zero-width cut cannot remove material from an OpenCASCADE solid.
    """
    side = _scaled(_unit(bend_direction, "Invalid relief direction."), width)
    inward = _unit(inward, "Invalid relief depth direction.")
    p0 = endpoint
    p1 = p0.add(side)

    if relief_type in ("Rectangle", "Tear"):
        p2 = p1.add(_scaled(inward, depth))
        p3 = p0.add(_scaled(inward, depth))
        return Part.Face(Part.makePolygon([p0, p1, p2, p3, p0]))

    if relief_type != "Round":
        raise ValueError(translate("SheetMetal", "Unknown bend relief type."))
    radius = width * 0.5
    if depth + smEpsilon < radius:
        raise ValueError(
            translate(
                "SheetMetal",
                "Round bend relief depth must be at least half its width.",
            )
        )
    straight_depth = max(0.0, depth - radius)
    p2 = p1.add(_scaled(inward, straight_depth))
    p3 = p0.add(_scaled(inward, straight_depth))
    cap = p0.add(_scaled(side, 0.5)).add(_scaled(inward, depth))
    edges = [Part.makeLine(p0, p1)]
    if straight_depth > smEpsilon:
        edges.append(Part.makeLine(p1, p2))
    edges.append(Part.Arc(p2, cap, p3).toShape())
    if straight_depth > smEpsilon:
        edges.append(Part.makeLine(p3, p0))
    return Part.Face(Part.Wire(edges))


def makeShapedFlangeStages(stages, thickness=1.0, refine=True):
    """Build cumulative sheet geometry from independently configured Face stages."""
    if not stages:
        raise ValueError(translate("SheetMetal", "A sheet-metal part has no Face features."))
    if thickness <= smEpsilon:
        raise ValueError(translate("SheetMetal", "Thickness must be greater than zero."))

    panels = []
    for stage_index, stage in enumerate(stages):
        radius = float(stage.get("radius", 1.0))
        if radius <= smEpsilon:
            raise ValueError(translate("SheetMetal", "Bend radius must be greater than zero."))
        thickness_side = stage.get("thickness_side", "Centered")
        if thickness_side not in ("Normal", "Reversed", "Centered"):
            raise ValueError(translate("SheetMetal", "Unknown thickness-side setting."))
        for sketch in stage.get("sketches", []):
            mid_offset = _stage_mid_offset(sketch, thickness, thickness_side)
            for face in _closed_planar_faces(sketch, stage.get("region_operations")):
                panels.append(
                    {
                        "sketch": sketch,
                        "face": face,
                        "cuts": [],
                        "stage_index": stage_index,
                        "stage": stage,
                        "mid_offset": mid_offset,
                        "thickness_side": thickness_side,
                    }
                )

    bends = []
    links = [set() for _panel in panels]
    tolerance = max(smEpsilon * 10.0, thickness * 1.0e-6)
    for first_index in range(len(panels)):
        first = panels[first_index]
        for second_index in range(first_index + 1, len(panels)):
            second = panels[second_index]
            for first_edge in first["face"].Edges:
                for second_edge in second["face"].Edges:
                    overlap = _straight_edge_overlap(first_edge, second_edge, tolerance)
                    if overlap is None:
                        continue
                    owner = first if first["stage_index"] >= second["stage_index"] else second
                    radius = float(owner["stage"].get("radius", 1.0))
                    data = _staged_bend_data(
                        first, first_edge, second, second_edge,
                        overlap[0], overlap[1], radius, thickness,
                    )
                    links[first_index].add(second_index)
                    links[second_index].add(first_index)
                    if data is not None:
                        first["cuts"].append(data["cut1"])
                        second["cuts"].append(data["cut2"])
                        relief_type = str(
                            owner["stage"].get("relief_type", "None")
                        )
                        if relief_type == "None":
                            bends.append(_bend_solid(data, radius, thickness))
                            continue

                        relief_width = float(
                            owner["stage"].get("relief_width", 0.0)
                        )
                        relief_depth = float(
                            owner["stage"].get("relief_depth", 0.0)
                        )
                        if relief_width <= smEpsilon or relief_depth <= smEpsilon:
                            raise ValueError(
                                translate(
                                    "SheetMetal",
                                    "Bend relief width and depth must be greater than zero.",
                                )
                            )
                        if 2.0 * relief_width >= data["length"] - tolerance:
                            raise ValueError(
                                translate(
                                    "SheetMetal",
                                    "Bend relief width is too large for the bend length.",
                                )
                            )

                        for endpoint, bend_direction in (
                            (data["p0"], data["axis"]),
                            (data["p1"], data["axis"].negative()),
                        ):
                            first["cuts"].append(
                                _bend_relief_face(
                                    endpoint,
                                    bend_direction,
                                    data["inward1"],
                                    relief_width,
                                    data["relief_base_depth1"] + relief_depth,
                                    relief_type,
                                )
                            )
                            second["cuts"].append(
                                _bend_relief_face(
                                    endpoint,
                                    bend_direction,
                                    data["inward2"],
                                    relief_width,
                                    data["relief_base_depth2"] + relief_depth,
                                    relief_type,
                                )
                            )
                        bends.append(
                            _bend_solid(
                                data,
                                radius,
                                thickness,
                                relief_width,
                                relief_width,
                            )
                        )

    if len(panels) > 1:
        reached = {0}
        pending = [0]
        while pending:
            current = pending.pop()
            for neighbor in links[current]:
                if neighbor not in reached:
                    reached.add(neighbor)
                    pending.append(neighbor)
        if len(reached) != len(panels):
            raise ValueError(
                translate(
                    "SheetMetal",
                    "Every Face stage must connect to the same sheet-metal part.",
                )
            )

    solids = []
    for panel in panels:
        face = panel["face"]
        for cut in panel["cuts"]:
            face = face.cut(cut)
        if face.isNull() or not face.Faces:
            raise ValueError(
                translate("SheetMetal", "A bend radius is too large for its panel.")
            )
        sketch_normal = _sketch_normal(panel["sketch"])
        for trimmed_face in face.Faces:
            if panel["thickness_side"] == "Normal":
                solid = trimmed_face.extrude(_scaled(sketch_normal, thickness))
            elif panel["thickness_side"] == "Reversed":
                solid = trimmed_face.extrude(_scaled(sketch_normal, -thickness))
            else:
                solid = trimmed_face.extrude(_scaled(sketch_normal, thickness))
                solid.translate(_scaled(sketch_normal, -0.5 * thickness))
            solids.append(solid)

    solids.extend(bends)
    if not solids:
        raise ValueError(translate("SheetMetal", "No sheet-metal solid could be created."))
    result = solids[0] if len(solids) == 1 else solids[0].multiFuse(solids[1:])
    if refine:
        result = result.removeSplitter()
    if result.isNull() or len(result.Solids) != 1 or not result.isValid():
        raise ValueError(
            translate(
                "SheetMetal",
                "Face stages do not form one valid constant-thickness solid. "
                "Check thickness directions and shared edges.",
            )
        )
    return result


def addSheetMetalPartProperties(part):
    """Add part-level sheet defaults to an App::Part container."""
    if "SheetMetalType" not in part.PropertiesList:
        part.addProperty(
            "App::PropertyString", "SheetMetalType", "Sheet Metal",
            translate("App::Property", "Sheet-metal object type"),
        ).SheetMetalType = "Part"
        part.setEditorMode("SheetMetalType", 1)
    SheetMetalTools.smAddLengthProperty(
        part, "Thickness", translate("App::Property", "Part sheet thickness"), 1.0,
    )
    SheetMetalTools.smAddLengthProperty(
        part,
        "DefaultBendRadius",
        translate("App::Property", "Default inside bend radius for new Face features"),
        1.0,
    )
    if "DefaultReliefType" not in part.PropertiesList:
        part.addProperty(
            "App::PropertyEnumeration", "DefaultReliefType", "Sheet Metal",
            translate(
                "App::Property",
                "Default bend relief type for bends introduced by Face features",
            ),
        )
        part.DefaultReliefType = RELIEF_TYPES
        part.DefaultReliefType = "None"
    else:
        _ensure_enumeration_options(
            part, "DefaultReliefType", RELIEF_TYPES, "None"
        )
    SheetMetalTools.smAddLengthProperty(
        part, "DefaultReliefWidth", translate("App::Property", "Default relief width"), 1.0,
    )
    SheetMetalTools.smAddLengthProperty(
        part, "DefaultReliefDepth", translate("App::Property", "Default relief depth"), 1.0,
    )
    if "KFactor" not in part.PropertiesList:
        part.addProperty(
            "App::PropertyFloatConstraint", "KFactor", "Sheet Metal",
            translate("App::Property", "Default neutral-axis K-factor for unfolding"),
        )
        part.KFactor = (0.4, 0.0, 1.0, 0.01)
    if "Tip" not in part.PropertiesList:
        part.addProperty(
            "App::PropertyString", "Tip", "Sheet Metal",
            translate(
                "App::Property",
                "Internal name of the latest cumulative Face feature",
            ),
        )
    part.setEditorMode("Tip", 1)
    SheetMetalMaterial.addMaterialProperties(part)
    SheetMetalMaterial.applyMaterialDefaults(part)


def createSheetMetalPart(doc):
    part = doc.addObject("App::Part", "SheetMetalPart")
    part.Label = translate("SheetMetal", "Sheet Metal Part")
    addSheetMetalPartProperties(part)
    SheetMetalTools.taskRestoreDefaults(
        part,
        ["Thickness", ("DefaultBendRadius", "defaultBendRadius")],
    )
    SheetMetalMaterial.configureNewPart(part)
    return part


def _parent_container(obj):
    """Return the nearest tree/geometry container of an object."""
    return obj.getParentGeoFeatureGroup() or obj.getParentGroup()


def _containing_body(obj):
    container = _parent_container(obj)
    while container is not None:
        if container.TypeId == "PartDesign::Body":
            return container
        container = _parent_container(container)
    return None


def _profile_scope(sketch):
    """Return the movable root and any containing App::Part/SheetMetalPart."""
    root = sketch
    parent = _parent_container(root)
    while parent is not None:
        if parent.TypeId == "App::Part" or (
            hasattr(parent, "SheetMetalType")
            and parent.SheetMetalType == "Part"
        ):
            return root, parent
        root = parent
        parent = _parent_container(root)
    return root, None


def _adopt_profiles(sheet_part, sketches):
    """Keep selected profiles within their sheet-metal App::Part scope."""
    for sketch in sketches:
        root, owner = _profile_scope(sketch)
        if owner is sheet_part:
            continue
        if owner is not None:
            raise ValueError(
                translate(
                    "SheetMetal",
                    "A selected profile belongs to a different Part. Select that Part "
                    "as the target, or move the profile before creating the Face feature.",
                )
            )
        sheet_part.addObject(root)


def upgradeSheetMetalPart(part):
    """Replace an experimental generic SheetMetalPart group with an App::Part."""
    if part.TypeId == "App::Part":
        addSheetMetalPartProperties(part)
        return part
    if not (
        hasattr(part, "SheetMetalType") and part.SheetMetalType == "Part"
    ):
        raise ValueError(translate("SheetMetal", "Object is not a SheetMetalPart."))
    addSheetMetalPartProperties(part)

    doc = part.Document
    name = part.Name
    label = part.Label
    children = list(part.Group) if hasattr(part, "Group") else []
    tip = part.Tip if hasattr(part, "Tip") else None
    tip_object = doc.getObject(tip) if isinstance(tip, str) and tip else tip
    history = (
        _feature_chain(tip_object)
        if tip_object is not None and hasattr(tip_object, "PreviousFeature")
        else []
    )
    ordered_children = [child for child in children if child not in history] + history
    values = {
        "Thickness": part.Thickness.Value,
        "DefaultBendRadius": part.DefaultBendRadius.Value,
        "DefaultReliefType": str(part.DefaultReliefType),
        "DefaultReliefWidth": part.DefaultReliefWidth.Value,
        "DefaultReliefDepth": part.DefaultReliefDepth.Value,
        "KFactor": float(part.KFactor),
    }
    material_values = SheetMetalMaterial.materialPropertyValues(part)

    profile_roots = []
    for child in ordered_children:
        if not hasattr(child, "Sketches"):
            continue
        for sketch in child.Sketches:
            root, owner = _profile_scope(sketch)
            if owner is None and root not in profile_roots:
                profile_roots.append(root)

    if any(root.TypeId == "PartDesign::Body" for root in profile_roots):
        raise ValueError(
            translate(
                "SheetMetal",
                "This experimental SheetMetalPart references sketches inside a Body. "
                "Recreate its Face history so the features can be placed inside "
                "that Body without cross-scope links.",
            )
        )

    doc.removeObject(name)
    replacement = doc.addObject("App::Part", name)
    replacement.Label = label
    addSheetMetalPartProperties(replacement)
    for prop, value in values.items():
        setattr(replacement, prop, value)
    SheetMetalMaterial.restoreMaterialPropertyValues(replacement, material_values)
    for root in profile_roots:
        replacement.addObject(root)
    for child in ordered_children:
        replacement.addObject(child)
    for index, feature in enumerate(history):
        feature.Label = translate("SheetMetal", "Face") + (
            "" if index == 0 else "{:03d}".format(index)
        )
    replacement.Tip = tip_object.Name if tip_object is not None else ""
    return replacement


def _feature_chain(feature):
    chain = []
    visited = set()
    current = feature
    while current is not None:
        if current.Name in visited:
            raise ValueError(translate("SheetMetal", "Cyclic Face feature history."))
        visited.add(current.Name)
        chain.append(current)
        current = current.PreviousFeature if hasattr(current, "PreviousFeature") else None
    chain.reverse()
    return chain


def _part_tip_feature(part):
    tip = part.Tip if hasattr(part, "Tip") else None
    if isinstance(tip, str):
        return part.Document.getObject(tip) if tip else None
    # Compatibility with the experimental group, whose Tip was a Link.
    return tip


def _sheet_metal_part(feature):
    part = _parent_container(feature)
    while part is not None:
        if hasattr(part, "SheetMetalType") and part.SheetMetalType == "Part":
            return part
        part = _parent_container(part)
    raise ValueError(
        translate("SheetMetal", "Face feature is not inside a SheetMetalPart.")
    )


def _feature_container(sheet_part, sketches, previous_feature=None):
    """Choose a scope shared by the profiles and cumulative feature history."""
    bodies = {_containing_body(sketch) for sketch in sketches}
    bodies.discard(None)
    if len(bodies) > 1:
        raise ValueError(
            translate("SheetMetal", "Selected profiles belong to different Bodies.")
        )
    profile_body = next(iter(bodies)) if bodies else None

    if previous_feature is None:
        return profile_body or sheet_part

    previous_container = _parent_container(previous_feature)
    if previous_container is sheet_part and profile_body is None:
        return sheet_part
    if previous_container is profile_body and profile_body is not None:
        return profile_body
    raise ValueError(
        translate(
            "SheetMetal",
            "All Face features in one history must use profiles from the same Body, "
            "or profiles placed directly in the SheetMetalPart.",
        )
    )


def _stage_from_feature(feature):
    part = _sheet_metal_part(feature)
    use_default_radius = getattr(feature, "UseDefaultBendRadius", False)
    radius = part.DefaultBendRadius.Value if use_default_radius else feature.BendRadius.Value
    use_default_relief = getattr(feature, "UseDefaultRelief", False)
    return {
        "sketches": list(feature.Sketches),
        "region_operations": list(feature.RegionOperations),
        "radius": radius,
        "thickness_side": feature.ThicknessSide,
        "relief_type": part.DefaultReliefType if use_default_relief else feature.ReliefType,
        "relief_width": (
            part.DefaultReliefWidth.Value if use_default_relief else feature.ReliefWidth.Value
        ),
        "relief_depth": (
            part.DefaultReliefDepth.Value if use_default_relief else feature.ReliefDepth.Value
        ),
    }


class SMShapedFlange:
    """One cumulative Face operation in a SheetMetalPart history."""

    def __init__(self, obj, sketches, sheet_metal_part, previous_feature=None):
        if "SheetMetalType" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString", "SheetMetalType", "Part",
                translate("App::Property", "Sheet-metal feature type"),
            ).SheetMetalType = "Face"
            obj.setEditorMode("SheetMetalType", 1)
        addSheetMetalPartProperties(sheet_metal_part)
        obj.addProperty(
            "App::PropertyLink", "PreviousFeature", "Part",
            translate("App::Property", "Previous cumulative Face feature"),
        ).PreviousFeature = previous_feature
        obj.addProperty(
            "App::PropertyXLinkList",
            "Sketches",
            "Profiles",
            translate("App::Property", "Sketches used by this Face operation"),
        ).Sketches = sketches
        self.addVerifyProperties(obj)
        SheetMetalTools.taskRestoreDefaults(obj, ["BendRadius"])
        obj.Proxy = self

    def addVerifyProperties(self, obj):
        if "ThicknessSide" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyEnumeration",
                "ThicknessSide",
                "Face Parameters",
                translate("App::Property", "Direction of material from each sketch plane"),
            )
            obj.ThicknessSide = ["Normal", "Reversed", "Centered"]
            obj.ThicknessSide = "Centered"
        if "RegionOperations" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyStringList",
                "RegionOperations",
                "Profiles",
                translate("App::Property", "Add, subtract, or ignore detected sketch regions"),
            )
            obj.RegionOperations = []
        SheetMetalTools.smAddBoolProperty(
            obj, "UseDefaultBendRadius",
            translate("App::Property", "Use the owning part's default bend radius"),
            True,
            "Face Parameters",
        )
        SheetMetalTools.smAddLengthProperty(
            obj,
            "BendRadius",
            translate("App::Property", "Inside bend radius"),
            1.0,
            "Face Parameters",
        )
        SheetMetalTools.smAddBoolProperty(
            obj,
            "Refine",
            translate("App::Property", "Remove residual splitter edges"),
            True,
            "Face Parameters",
        )
        SheetMetalTools.smAddBoolProperty(
            obj, "UseDefaultRelief",
            translate("App::Property", "Use bend relief defaults from the owning part"),
            True,
            "Relief",
        )
        if "ReliefType" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyEnumeration", "ReliefType", "Relief",
                translate(
                    "App::Property",
                    "Bend relief shape cut at both ends of bends introduced by this feature",
                ),
            )
            obj.ReliefType = RELIEF_TYPES
            obj.ReliefType = "None"
        else:
            _ensure_enumeration_options(obj, "ReliefType", RELIEF_TYPES, "None")
        SheetMetalTools.smAddLengthProperty(
            obj, "ReliefWidth",
            translate("App::Property", "Bend relief width; kerf width for Tear"), 1.0,
            "Relief",
        )
        SheetMetalTools.smAddLengthProperty(
            obj, "ReliefDepth",
            translate("App::Property", "Bend relief depth; slit extension for Tear"), 1.0,
            "Relief",
        )

    def execute(self, fp):
        self.addVerifyProperties(fp)
        sheet_metal_part = _sheet_metal_part(fp)
        addSheetMetalPartProperties(sheet_metal_part)
        history = _feature_chain(fp)
        old_prefix = translate("SheetMetal", "Shaped Flange")
        for index, feature in enumerate(history):
            if feature.Label.startswith(old_prefix):
                feature.Label = translate("SheetMetal", "Face") + (
                    "" if index == 0 else "{:03d}".format(index)
                )
        stages = [_stage_from_feature(feature) for feature in history]
        fp.Shape = makeShapedFlangeStages(
            stages,
            thickness=sheet_metal_part.Thickness.Value,
            refine=fp.Refine,
        )


class _SheetMetalPartDefaultsObserver:
    """Invalidate Face history when a parent-level sheet default changes."""

    geometry_properties = {
        "Thickness",
        "DefaultBendRadius",
        "DefaultReliefType",
        "DefaultReliefWidth",
        "DefaultReliefDepth",
    }

    def slotChangedObject(self, obj, prop):
        if prop not in self.geometry_properties or not (
            hasattr(obj, "SheetMetalType") and obj.SheetMetalType == "Part"
        ):
            return
        for candidate in obj.Document.Objects:
            if not (
                hasattr(candidate, "SheetMetalType")
                and candidate.SheetMetalType == "Face"
            ):
                continue
            try:
                if _sheet_metal_part(candidate) is obj:
                    candidate.touch()
            except ValueError:
                pass


if "_sheet_metal_part_defaults_observer" not in globals():
    _sheet_metal_part_defaults_observer = _SheetMetalPartDefaultsObserver()
    FreeCAD.addDocumentObserver(_sheet_metal_part_defaults_observer)


if SheetMetalTools.isGuiLoaded():
    Gui = FreeCAD.Gui
    from PySide import QtCore, QtGui

    class SMShapedFlangeViewProvider(SheetMetalTools.SMViewProvider):
        def getIcon(self):
            return os.path.join(icons_path, "SheetMetal_ShapedFlange.svg")

        def claimChildren(self):
            # Profiles remain owned by their App::Part/Body. Claiming them here
            # would move their tree presentation across PartDesign scope.
            return []

        def getTaskPanel(self, obj):
            return SMShapedFlangeTaskPanel(obj)


    class SMShapedFlangeTaskPanel:
        """Edit detected closed-loop regions and their boolean operation."""

        def __init__(self, obj):
            self.obj = obj
            self.region_edges = {}
            obj.Proxy.addVerifyProperties(obj)
            self.form = QtGui.QWidget()
            self.form.setWindowTitle(translate("SheetMetal", "Sheet metal face profiles"))

            layout = QtGui.QVBoxLayout(self.form)
            help_label = QtGui.QLabel(
                translate(
                    "SheetMetal",
                    "Closed loops are detected as profile regions. Select one or more "
                    "rows, then choose how they contribute to the panel.",
                )
            )
            help_label.setWordWrap(True)
            layout.addWidget(help_label)

            self.tree = QtGui.QTreeWidget()
            self.tree.setColumnCount(4)
            self.tree.setHeaderLabels(
                [
                    translate("SheetMetal", "Sketch"),
                    translate("SheetMetal", "Region"),
                    translate("SheetMetal", "Operation"),
                    translate("SheetMetal", "Area"),
                ]
            )
            self.tree.setSelectionMode(QtGui.QAbstractItemView.ExtendedSelection)
            layout.addWidget(self.tree)

            button_layout = QtGui.QHBoxLayout()
            for label, operation in (
                (translate("SheetMetal", "Add"), "Add"),
                (translate("SheetMetal", "Subtract"), "Subtract"),
                (translate("SheetMetal", "Ignore"), "Ignore"),
            ):
                button = QtGui.QPushButton(label)
                button.clicked.connect(
                    lambda _checked=False, selected_operation=operation:
                    self.setSelectedOperation(selected_operation)
                )
                button_layout.addWidget(button)
            layout.addLayout(button_layout)

            reset_button = QtGui.QPushButton(
                translate("SheetMetal", "Reset automatic region detection")
            )
            reset_button.clicked.connect(self.resetAutomatic)
            layout.addWidget(reset_button)

            parameter_group = QtGui.QGroupBox(
                translate("SheetMetal", "Face parameters")
            )
            parameter_layout = QtGui.QFormLayout(parameter_group)

            self.thickness_side = QtGui.QComboBox()
            self.thickness_side.addItems(["Normal", "Reversed", "Centered"])
            parameter_layout.addRow(
                translate("SheetMetal", "Thickness side"), self.thickness_side
            )
            SheetMetalTools.taskConnectEnum(obj, self.thickness_side, "ThicknessSide")

            self.default_radius = QtGui.QCheckBox(
                translate("SheetMetal", "Use part default bend radius")
            )
            parameter_layout.addRow(self.default_radius)
            self.bend_radius = Gui.UiLoader().createWidget("Gui::QuantitySpinBox")
            self.bend_radius.setProperty("minimum", smEpsilon)
            parameter_layout.addRow(
                translate("SheetMetal", "Bend radius"), self.bend_radius
            )
            SheetMetalTools.taskConnectSpin(obj, self.bend_radius, "BendRadius")
            SheetMetalTools.taskConnectCheck(
                obj,
                self.default_radius,
                "UseDefaultBendRadius",
                lambda checked: self.bend_radius.setEnabled(not checked),
            )

            self.default_relief = QtGui.QCheckBox(
                translate("SheetMetal", "Use part default bend relief")
            )
            parameter_layout.addRow(self.default_relief)
            self.relief_type = QtGui.QComboBox()
            self.relief_type.addItems(["None", "Rectangle", "Round"])
            parameter_layout.addRow(
                translate("SheetMetal", "Relief type"), self.relief_type
            )
            SheetMetalTools.taskConnectEnum(obj, self.relief_type, "ReliefType")
            self.relief_width = Gui.UiLoader().createWidget("Gui::QuantitySpinBox")
            self.relief_width.setProperty("minimum", 0.0)
            parameter_layout.addRow(
                translate("SheetMetal", "Relief width"), self.relief_width
            )
            SheetMetalTools.taskConnectSpin(obj, self.relief_width, "ReliefWidth")
            self.relief_depth = Gui.UiLoader().createWidget("Gui::QuantitySpinBox")
            self.relief_depth.setProperty("minimum", 0.0)
            parameter_layout.addRow(
                translate("SheetMetal", "Relief depth"), self.relief_depth
            )
            SheetMetalTools.taskConnectSpin(obj, self.relief_depth, "ReliefDepth")

            def update_relief_controls(checked):
                enabled = not checked
                self.relief_type.setEnabled(enabled)
                self.relief_width.setEnabled(enabled)
                self.relief_depth.setEnabled(enabled)

            SheetMetalTools.taskConnectCheck(
                obj, self.default_relief, "UseDefaultRelief", update_relief_controls
            )
            layout.addWidget(parameter_group)

            self.populate()
            self.tree.itemSelectionChanged.connect(self.highlightSelectedRegions)
            for sketch in self.obj.Sketches:
                sketch.ViewObject.Visibility = True
            self.tree.resizeColumnToContents(0)
            self.tree.resizeColumnToContents(1)
            self.tree.resizeColumnToContents(2)

        def populate(self):
            self.tree.clear()
            self.region_edges = {}
            operations = _operation_map(self.obj.RegionOperations)
            for sketch in self.obj.Sketches:
                for region in _profile_regions(sketch):
                    item = QtGui.QTreeWidgetItem(self.tree)
                    item.setText(0, sketch.Label)
                    item.setText(1, "Wire {}".format(region["wire_index"] + 1))
                    item.setText(2, operations.get(region["id"], region["default"]))
                    item.setText(3, "{:.3f}".format(region["face"].Area))
                    item.setData(0, QtCore.Qt.UserRole, region["id"])
                    item.setToolTip(
                        1,
                        translate("SheetMetal", "Detected profile: %1").replace(
                            "%1", region["id"]
                        ),
                    )
                    edge_names = []
                    wire = sketch.Shape.Wires[region["wire_index"]]
                    for wire_edge in wire.Edges:
                        for edge_index, sketch_edge in enumerate(sketch.Shape.Edges):
                            if wire_edge.isSame(sketch_edge):
                                edge_names.append("Edge{}".format(edge_index + 1))
                                break
                    self.region_edges[region["id"]] = (sketch, edge_names)

        def highlightSelectedRegions(self):
            Gui.Selection.clearSelection()
            for item in self.tree.selectedItems():
                region_id = item.data(0, QtCore.Qt.UserRole)
                sketch, edge_names = self.region_edges[region_id]
                for edge_name in edge_names:
                    Gui.Selection.addSelection(
                        sketch.Document.Name, sketch.Name, edge_name
                    )

        def setSelectedOperation(self, operation):
            selected = self.tree.selectedItems()
            if not selected:
                return
            for item in selected:
                item.setText(2, operation)
            self.saveOperations()

        def saveOperations(self):
            entries = []
            for index in range(self.tree.topLevelItemCount()):
                item = self.tree.topLevelItem(index)
                region_id = item.data(0, QtCore.Qt.UserRole)
                entries.append("{}={}".format(region_id, item.text(2)))
            self.obj.RegionOperations = entries
            self.obj.Document.recompute()

        def resetAutomatic(self):
            self.obj.RegionOperations = []
            self.obj.Document.recompute()
            self.populate()

        def accept(self):
            Gui.Selection.clearSelection()
            for sketch in self.obj.Sketches:
                sketch.ViewObject.Visibility = False
            SheetMetalTools.taskSaveDefaults(self.obj, smShapedFlangeDefaultVars)
            return SheetMetalTools.taskAccept(self)

        def reject(self):
            Gui.Selection.clearSelection()
            for sketch in self.obj.Sketches:
                sketch.ViewObject.Visibility = False
            SheetMetalTools.taskReject(self)


    class AddShapedFlangeCommandClass:
        @staticmethod
        def _getSelectionData():
            sketches = []
            feature = None
            sheet_part = None
            for obj in Gui.Selection.getSelection():
                if obj.TypeId == "App::Part" or (
                    hasattr(obj, "SheetMetalType") and obj.SheetMetalType == "Part"
                ):
                    sheet_part = obj
                elif (
                    hasattr(obj, "SheetMetalType")
                    and obj.SheetMetalType == "Face"
                    and hasattr(obj, "PreviousFeature")
                ):
                    feature = obj
                    sheet_part = _sheet_metal_part(obj)
                else:
                    sketches.append(obj)
            return sketches, feature, sheet_part

        def GetResources(self):
            return {
                "Pixmap": os.path.join(icons_path, "SheetMetal_ShapedFlange.svg"),
                "MenuText": translate("SheetMetal", "Make Sheet Metal Face"),
                "ToolTip": translate(
                    "SheetMetal",
                    "Add a cumulative Face feature to a sheet-metal part.\n"
                    "Select one or more closed sketches, optionally with a target "
                    "SheetMetalPart or an earlier Face feature.",
                ),
            }

        def Activated(self):
            sketches, selected_feature, sheet_part = self._getSelectionData()
            doc = FreeCAD.ActiveDocument
            doc.openTransaction("ShapedFlange")

            if sheet_part is None:
                parts = [
                    candidate for candidate in doc.Objects
                    if hasattr(candidate, "SheetMetalType")
                    and candidate.SheetMetalType == "Part"
                ]
                if len(parts) == 1:
                    sheet_part = parts[0]
                elif len(parts) > 1:
                    compatible = []
                    for candidate in parts:
                        addSheetMetalPartProperties(candidate)
                        stages = []
                        candidate_tip = _part_tip_feature(candidate)
                        if candidate_tip is not None:
                            stages = [
                                _stage_from_feature(item)
                                for item in _feature_chain(candidate_tip)
                            ]
                        stages.append(
                            {
                                "sketches": sketches,
                                "region_operations": [],
                                "radius": candidate.DefaultBendRadius.Value,
                                "thickness_side": "Centered",
                            }
                        )
                        try:
                            makeShapedFlangeStages(
                                stages, candidate.Thickness.Value, True
                            )
                            compatible.append(candidate)
                        except (ValueError, Part.OCCError):
                            pass
                    if len(compatible) == 1:
                        sheet_part = compatible[0]
                    else:
                        doc.abortTransaction()
                        SheetMetalTools.smWarnDialog(
                            translate(
                                "SheetMetal",
                                "Select the target SheetMetalPart when more than one exists.",
                            )
                        )
                        return
                else:
                    profile_parts = {
                        owner
                        for sketch in sketches
                        for _root, owner in [_profile_scope(sketch)]
                        if owner is not None and owner.TypeId == "App::Part"
                    }
                    if len(profile_parts) == 1:
                        sheet_part = profile_parts.pop()
                        addSheetMetalPartProperties(sheet_part)
                    elif len(profile_parts) > 1:
                        doc.abortTransaction()
                        SheetMetalTools.smWarnDialog(
                            translate(
                                "SheetMetal",
                                "Selected profiles belong to more than one Part.",
                            )
                        )
                        return
                    else:
                        sheet_part = createSheetMetalPart(doc)
            else:
                addSheetMetalPartProperties(sheet_part)

            try:
                sheet_part = upgradeSheetMetalPart(sheet_part)
                _adopt_profiles(sheet_part, sketches)
            except ValueError as error:
                doc.abortTransaction()
                SheetMetalTools.smWarnDialog(str(error))
                return

            previous_feature = selected_feature or _part_tip_feature(sheet_part)
            old_tip = _part_tip_feature(sheet_part)
            try:
                feature_container = _feature_container(
                    sheet_part, sketches, previous_feature
                )
            except ValueError as error:
                doc.abortTransaction()
                SheetMetalTools.smWarnDialog(str(error))
                return
            feature_type = (
                "PartDesign::FeaturePython"
                if feature_container.TypeId == "PartDesign::Body"
                else "Part::FeaturePython"
            )
            obj = doc.addObject(feature_type, "ShapedFlange")
            history_index = (
                len(_feature_chain(previous_feature))
                if previous_feature is not None
                else 0
            )
            obj.Label = translate("SheetMetal", "Face") + (
                "" if history_index == 0 else "{:03d}".format(history_index)
            )
            feature_container.addObject(obj)
            SMShapedFlange(obj, sketches, sheet_part, previous_feature)
            SMShapedFlangeViewProvider(obj.ViewObject)
            sheet_part.Tip = obj.Name
            if old_tip is not None:
                old_tip.ViewObject.Visibility = False
            for sketch in sketches:
                sketch.ViewObject.Visibility = False
            obj.ViewObject.Visibility = True
            Gui.Selection.clearSelection()
            Gui.Selection.addSelection(obj)
            doc.recompute()
            dialog = SMShapedFlangeTaskPanel(obj)
            SheetMetalTools.updateTaskTitleIcon(dialog)
            Gui.Control.showDialog(dialog)

        def IsActive(self):
            selection = Gui.Selection.getSelection()
            if not selection:
                return False
            has_sketch = any(
                obj.isDerivedFrom("Sketcher::SketchObject")
                or obj.isDerivedFrom("PartDesign::ShapeBinder")
                or obj.isDerivedFrom("PartDesign::SubShapeBinder")
                or obj.isDerivedFrom("Part::Part2DObjectPython")
                for obj in selection
            )
            if not has_sketch:
                return False
            return all(
                obj.TypeId == "App::Part"
                or hasattr(obj, "SheetMetalType")
                or (
                    hasattr(obj, "SheetMetalType")
                    and obj.SheetMetalType == "Face"
                )
                or obj.isDerivedFrom("Sketcher::SketchObject")
                or obj.isDerivedFrom("PartDesign::ShapeBinder")
                or obj.isDerivedFrom("PartDesign::SubShapeBinder")
                or obj.isDerivedFrom("Part::Part2DObjectPython")
                for obj in selection
            )


    Gui.addCommand("SheetMetal_ShapedFlange", AddShapedFlangeCommandClass())
