########################################################################
#
#  SheetMetalBoltConnectionCmd.py
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public License
#  as published by the Free Software Foundation; either version 2 of
#  the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
########################################################################

"""Multi-part bolted connections with independent per-part hole profiles.

The connection object stores design intent and live sketch locator links.  It
does not own a Shape.  Each connected SheetMetalPart receives a downstream
cut feature in its own history, so every participant remains independently
valid and unfoldable.
"""

import math
import os

import FreeCAD
import Part

import SheetMetalTools
from SheetMetalShapedFlangeCmd import _part_tip_feature


translate = FreeCAD.Qt.translate
icons_path = SheetMetalTools.icons_path

CONNECTION_TYPES = ["Hex Bolt", "Carriage Bolt", "Custom"]
BOLT_SIZES = ["3/16 in", "1/4 in", "5/16 in", "3/8 in", "1/2 in", "3/4 in"]
FITS = ["Close", "Normal", "Oversize"]
HOLE_TYPES = ["Round", "Square", "Slotted Round", "Slotted Square"]
PARTICIPANT_ROLES = ["Head Side", "Intermediate", "Nut Side"]
CUT_EXTENTS = ["Nearest Sheet Layer", "Through Entire Part"]

INCH = 25.4
DEFAULT_SLOT_LENGTH = 0.0
CUTTER_MARGIN = 2.0
ASSIGNMENT_PROBE_RADIUS = 0.25


ROUND_CLEARANCE_IN = {
    "3/16 in": {"Close": 0.196, "Normal": 0.213, "Oversize": 0.250},
    "1/4 in": {"Close": 0.257, "Normal": 0.281, "Oversize": 0.3125},
    "5/16 in": {"Close": 0.323, "Normal": 0.344, "Oversize": 0.390},
    "3/8 in": {"Close": 0.386, "Normal": 0.406, "Oversize": 0.469},
    "1/2 in": {"Close": 0.515, "Normal": 0.531, "Oversize": 0.625},
    "3/4 in": {"Close": 0.781, "Normal": 0.813, "Oversize": 0.875},
}

SQUARE_CLEARANCE_IN = {
    "3/16 in": {"Close": 0.196, "Normal": 0.213, "Oversize": 0.250},
    "1/4 in": {"Close": 0.281, "Normal": 0.281, "Oversize": 0.3125},
    "5/16 in": {"Close": 0.354, "Normal": 0.354, "Oversize": 0.390},
    "3/8 in": {"Close": 0.406, "Normal": 0.406, "Oversize": 0.469},
    "1/2 in": {"Close": 0.531, "Normal": 0.531, "Oversize": 0.625},
    "3/4 in": {"Close": 0.781, "Normal": 0.813, "Oversize": 0.875},
}


def _set_enumeration(obj, name, values, default, group, description):
    """Add or upgrade an enumeration while preserving a saved selection."""
    if name not in obj.PropertiesList:
        obj.addProperty("App::PropertyEnumeration", name, group, description)
        setattr(obj, name, values)
        setattr(obj, name, default)
        return
    current = str(getattr(obj, name))
    if obj.getEnumerationsOfProperty(name) != values:
        setattr(obj, name, values)
        setattr(obj, name, current if current in values else default)


def _unit(vector, message):
    result = FreeCAD.Vector(vector)
    if result.Length <= SheetMetalTools.smEpsilon:
        raise ValueError(message)
    result.normalize()
    return result


def _scaled(vector, factor):
    result = FreeCAD.Vector(vector)
    result.multiply(factor)
    return result


def _frame(point, x_axis, z_axis):
    z_axis = _unit(z_axis, "A bolt locator has no usable normal.")
    x_axis = FreeCAD.Vector(x_axis)
    x_axis = x_axis.sub(_scaled(z_axis, x_axis.dot(z_axis)))
    if x_axis.Length <= SheetMetalTools.smEpsilon:
        trial = FreeCAD.Vector(1.0, 0.0, 0.0)
        if abs(trial.dot(z_axis)) > 0.9:
            trial = FreeCAD.Vector(0.0, 1.0, 0.0)
        x_axis = trial.sub(_scaled(z_axis, trial.dot(z_axis)))
    x_axis = _unit(x_axis, "A bolt locator has no usable X direction.")
    return {
        "point": FreeCAD.Vector(point),
        "x_axis": x_axis,
        "z_axis": z_axis,
    }


def round_clearance_diameter(size, fit):
    """Return the standard round clearance diameter in millimetres."""
    return ROUND_CLEARANCE_IN[str(size)][str(fit)] * INCH


def square_clearance_width(size, fit):
    """Return the standard square clearance width in millimetres."""
    return SQUARE_CLEARANCE_IN[str(size)][str(fit)] * INCH


def _geometry_key(sketch, geometry_index):
    return "{}:{}".format(sketch.Name, geometry_index)


def _point_subelement_name(sketch, geometry_index):
    """Return the displayed Sketcher vertex name for one point geometry."""
    for vertex_index in range(len(sketch.Shape.Vertexes)):
        try:
            geo_id, _position_id = sketch.getGeoVertexIndex(vertex_index)
        except (AttributeError, IndexError, RuntimeError):
            return ""
        if geo_id == geometry_index:
            return "Vertex{}".format(vertex_index + 1)
    return ""


def _geometry_candidates(sketch):
    """Return selectable point/circle locator records from one sketch."""
    placement = sketch.getGlobalPlacement()
    x_axis = placement.Rotation.multVec(FreeCAD.Vector(1.0, 0.0, 0.0))
    z_axis = placement.Rotation.multVec(FreeCAD.Vector(0.0, 0.0, 1.0))
    candidates = []
    locator_number = 0
    for geometry_index, geometry in enumerate(getattr(sketch, "Geometry", [])):
        if hasattr(sketch, "getConstruction") and sketch.getConstruction(geometry_index):
            continue
        if isinstance(geometry, Part.Point):
            point = FreeCAD.Vector(geometry.X, geometry.Y, geometry.Z)
            kind = "Point"
            subelement = _point_subelement_name(sketch, geometry_index)
        elif isinstance(geometry, Part.Circle):
            point = geometry.Center
            kind = "Circle centre"
            subelement = ""
        else:
            continue
        locator_number += 1
        reference = (
            "{}.{}".format(sketch.Name, subelement)
            if subelement
            else "{} geometry {}".format(sketch.Name, geometry_index + 1)
        )
        candidates.append(
            {
                "key": _geometry_key(sketch, geometry_index),
                "label": "{} — {} {} (geometry {})".format(
                    reference, kind, locator_number, geometry_index + 1
                ),
                "frame": _frame(placement.multVec(point), x_axis, z_axis),
                "sketch": sketch,
                "subelement": subelement,
                "filterable": True,
            }
        )
    return candidates


def _geometry_frames(sketch, allowed_keys=None):
    """Return explicit non-construction point/circle frames for one sketch."""
    return [
        candidate["frame"]
        for candidate in _geometry_candidates(sketch)
        if allowed_keys is None or candidate["key"] in allowed_keys
    ]


def _subelement_frame(sketch, sub_name):
    placement = sketch.getGlobalPlacement()
    x_axis = placement.Rotation.multVec(FreeCAD.Vector(1.0, 0.0, 0.0))
    z_axis = placement.Rotation.multVec(FreeCAD.Vector(0.0, 0.0, 1.0))
    sub_object = sketch.getSubObject(sub_name)
    if sub_object is None:
        raise ValueError(
            translate("SheetMetal", "Bolt locator %1 no longer exists.").replace(
                "%1", "{}.{}".format(sketch.Label, sub_name)
            )
        )
    if isinstance(sub_object, Part.Vertex):
        point = sub_object.Point
    elif isinstance(sub_object, Part.Edge) and isinstance(sub_object.Curve, Part.Circle):
        point = sub_object.Curve.Center
    else:
        raise ValueError(
            translate(
                "SheetMetal",
                "Bolt locators must be sketch vertices or circular sketch edges.",
            )
        )
    # Sketch subelements include the sketch's own Placement, but not placements
    # inherited from parent Parts. Return to sketch-local coordinates before
    # applying its complete global placement.
    local_point = sketch.Placement.inverse().multVec(point)
    return _frame(placement.multVec(local_point), x_axis, z_axis)


def _subelement_record(sketch, sub_name):
    return {
        "key": "{}:Sub:{}".format(sketch.Name, sub_name),
        "label": "{}.{}".format(sketch.Name, sub_name),
        "frame": _subelement_frame(sketch, sub_name),
        "sketch": sketch,
        "subelement": sub_name,
        "filterable": False,
    }


def locator_candidates(connection):
    """Return every referenced locator, including disabled whole-sketch ones."""
    records = []
    for sketch, sub_names in connection.LocatorReferences:
        if sketch is None or not sketch.isDerivedFrom("Sketcher::SketchObject"):
            raise ValueError(
                translate("SheetMetal", "Every bolt locator must belong to a sketch.")
            )
        if sub_names:
            records.extend(
                _subelement_record(sketch, name) for name in sub_names
            )
        else:
            records.extend(_geometry_candidates(sketch))
    return records


def locator_records(connection):
    """Resolve enabled locator keys, labels, and world-space frames."""
    filter_keys = set(getattr(connection, "LocatorGeometryFilter", []))
    records = [
        record
        for record in locator_candidates(connection)
        if not record["filterable"]
        or not filter_keys
        or record["key"] in filter_keys
    ]
    if not records:
        raise ValueError(
            translate(
                "SheetMetal",
                "No sketch points or circular sketch edges define this connection.",
            )
        )
    return records


def locator_frames(connection):
    """Resolve all enabled connection locators to world-space frames."""
    return [record["frame"] for record in locator_records(connection)]


def cut_locator_records(cut):
    """Return only the enabled locators assigned to one participant cut."""
    connection = connection_for_cut(cut)
    if connection is None:
        raise ValueError("The bolted connection reference is missing.")
    records = locator_records(connection)
    if cut.UseAllLocators:
        return records
    keys = set(cut.LocatorKeys)
    return [record for record in records if record["key"] in keys]


def _container_placement(feature):
    container = feature.getParentGeoFeatureGroup()
    if container is None or not hasattr(container, "getGlobalPlacement"):
        return FreeCAD.Placement()
    return container.getGlobalPlacement()


def _local_frame(feature, world_frame):
    inverse = _container_placement(feature).inverse()
    return _frame(
        inverse.multVec(world_frame["point"]),
        inverse.Rotation.multVec(world_frame["x_axis"]),
        inverse.Rotation.multVec(world_frame["z_axis"]),
    )


def _rectangle_face(center, x_axis, y_axis, length, width):
    half_length = length * 0.5
    half_width = width * 0.5
    points = [
        center.add(_scaled(x_axis, half_length)).add(_scaled(y_axis, half_width)),
        center.sub(_scaled(x_axis, half_length)).add(_scaled(y_axis, half_width)),
        center.sub(_scaled(x_axis, half_length)).sub(_scaled(y_axis, half_width)),
        center.add(_scaled(x_axis, half_length)).sub(_scaled(y_axis, half_width)),
    ]
    return Part.Face(Part.makePolygon(points + [points[0]]))


def make_hole_cutter(hole_type, center, x_axis, z_axis, width, slot_length, depth):
    """Build a centred through-cutter for one round, square, or slotted hole."""
    if width <= SheetMetalTools.smEpsilon:
        raise ValueError("Hole width must be greater than zero.")
    if depth <= SheetMetalTools.smEpsilon:
        raise ValueError("Cutter depth must be greater than zero.")

    frame = _frame(center, x_axis, z_axis)
    z_axis = frame["z_axis"]
    cutter_start = frame["point"].sub(_scaled(z_axis, depth * 0.5))
    profile = str(hole_type)
    x_axis = frame["x_axis"]
    y_axis = _unit(z_axis.cross(x_axis), "Cannot determine bolt-hole Y direction.")

    if profile == "Round":
        return Part.makeCylinder(width * 0.5, depth, cutter_start, z_axis)

    if profile == "Square":
        return _rectangle_face(cutter_start, x_axis, y_axis, width, width).extrude(
            _scaled(z_axis, depth)
        )

    if slot_length <= SheetMetalTools.smEpsilon:
        slot_length = width
    if slot_length + SheetMetalTools.smEpsilon < width:
        raise ValueError("Slot overall length cannot be smaller than its width.")

    if profile == "Slotted Square":
        return _rectangle_face(
            cutter_start, x_axis, y_axis, slot_length, width
        ).extrude(_scaled(z_axis, depth))

    if profile != "Slotted Round":
        raise ValueError("Unsupported hole profile: {}".format(profile))

    straight_length = max(0.0, slot_length - width)
    if straight_length <= SheetMetalTools.smEpsilon:
        return Part.makeCylinder(width * 0.5, depth, cutter_start, z_axis)

    first = cutter_start.sub(_scaled(x_axis, straight_length * 0.5))
    second = cutter_start.add(_scaled(x_axis, straight_length * 0.5))
    first_cap = Part.makeCylinder(width * 0.5, depth, first, z_axis)
    second_cap = Part.makeCylinder(width * 0.5, depth, second, z_axis)
    bridge = _rectangle_face(
        cutter_start, x_axis, y_axis, straight_length, width
    ).extrude(_scaled(z_axis, depth))
    return first_cap.fuse(bridge).fuse(second_cap).removeSplitter()


def connection_for_cut(cut):
    """Resolve a cut's owner without an invalid cross-Body PropertyLink."""
    name = str(getattr(cut, "ConnectionName", ""))
    if name:
        return cut.Document.getObject(name)
    return getattr(cut, "Connection", None)


def _effective_fit(cut):
    connection = connection_for_cut(cut)
    if cut.UseConnectionFit and connection is not None:
        return str(connection.Fit)
    return str(cut.Fit)


def _effective_width(cut):
    if cut.OverrideHoleSize:
        return cut.HoleWidth.Value
    connection = connection_for_cut(cut)
    if connection is None:
        raise ValueError("The bolted connection reference is missing.")
    if str(cut.HoleType) in ("Square", "Slotted Square"):
        return square_clearance_width(connection.BoltSize, _effective_fit(cut))
    return round_clearance_diameter(connection.BoltSize, _effective_fit(cut))


def _feature_depth(base_shape, center):
    diagonal = max(base_shape.BoundBox.DiagonalLength, 1.0)
    distance = center.distanceToPoint(base_shape.BoundBox.Center)
    return max(10.0, 2.0 * (distance + diagonal + CUTTER_MARGIN))


def axis_intersects_shape_bounds(shape, point, direction, margin=0.0):
    """Cheaply reject a locator axis that cannot reach a shape's bounding box."""
    if shape is None or shape.isNull():
        return False
    axis = _unit(direction, "A hole locator has no usable axis.")
    box = shape.BoundBox
    intervals = (
        (point.x, axis.x, box.XMin - margin, box.XMax + margin),
        (point.y, axis.y, box.YMin - margin, box.YMax + margin),
        (point.z, axis.z, box.ZMin - margin, box.ZMax + margin),
    )
    lower_t = -float("inf")
    upper_t = float("inf")
    for coordinate, component, lower, upper in intervals:
        if abs(component) <= SheetMetalTools.smEpsilon:
            if coordinate < lower or coordinate > upper:
                return False
            continue
        first = (lower - coordinate) / component
        second = (upper - coordinate) / component
        if first > second:
            first, second = second, first
        lower_t = max(lower_t, first)
        upper_t = min(upper_t, second)
        if lower_t > upper_t:
            return False
    return True


def _nearest_material(cutter, base_shape, center):
    """Return only the cutter/material intersection nearest the locator."""
    common = base_shape.common(cutter)
    solids = list(common.Solids)
    if not solids:
        raise ValueError("Bolt locator does not intersect this participant.")
    point = Part.Vertex(center)
    distances = [(solid.distToShape(point)[0], solid) for solid in solids]
    nearest_distance = min(distance for distance, _solid in distances)
    tolerance = max(SheetMetalTools.smEpsilon * 10.0, 1.0e-7)
    nearest = [
        solid for distance, solid in distances
        if distance <= nearest_distance + tolerance
    ]
    if len(nearest) == 1:
        return nearest[0]
    return Part.makeCompound(nearest)


def apply_hole_cutter(base_shape, cutter, center, cut_extent):
    """Apply a through-part or nearest-local-layer hole cutter."""
    if str(cut_extent) == "Through Entire Part":
        if not base_shape.common(cutter).Solids:
            raise ValueError("Bolt locator does not intersect this participant.")
        return base_shape.cut(cutter)
    return base_shape.cut(_nearest_material(cutter, base_shape, center))


def apply_hole_cutters(
    base_shape,
    cutter_records,
    cut_extent,
    skip_non_intersections=False,
    validate_through=True,
):
    """Apply many independent hole cutters in one final Boolean operation.

    ``cutter_records`` contains ``(cutter, center)`` pairs.  Pattern cuts can
    rely on their bounding-box broad phase for through cuts, avoiding one OCC
    common operation per possible hole.  Closest-layer cuts still resolve the
    local material for every locator before batching the final subtraction.
    """
    prepared, skipped = prepare_hole_cutters(
        base_shape,
        cutter_records,
        cut_extent,
        skip_non_intersections=skip_non_intersections,
        validate_through=validate_through,
    )
    if not prepared:
        return base_shape.copy(), skipped
    tool = prepared[0] if len(prepared) == 1 else Part.makeCompound(prepared)
    return base_shape.cut(tool), skipped


def prepare_hole_cutters(
    base_shape,
    cutter_records,
    cut_extent,
    skip_non_intersections=False,
    validate_through=True,
):
    """Resolve cutter/material intersections without performing subtraction."""
    prepared = []
    skipped = 0
    through_entire_part = str(cut_extent) == "Through Entire Part"
    for cutter, center in cutter_records:
        try:
            if through_entire_part:
                if validate_through and not base_shape.common(cutter).Solids:
                    raise ValueError(
                        "Bolt locator does not intersect this participant."
                    )
                prepared.append(cutter)
            else:
                prepared.append(_nearest_material(cutter, base_shape, center))
        except ValueError as error:
            if not skip_non_intersections or "does not intersect" not in str(error):
                raise
            skipped += 1
    return prepared, skipped


def _find_sheet_metal_part(obj):
    current = obj
    visited = set()
    while current is not None and current.Name not in visited:
        visited.add(current.Name)
        if (
            current.TypeId == "App::Part"
            and hasattr(current, "SheetMetalType")
            and current.SheetMetalType == "Part"
        ):
            return current
        current = (
            current.getParentGeoFeatureGroup()
            if hasattr(current, "getParentGeoFeatureGroup")
            else None
        )
    return None


def _target_tip(part):
    bodies = [obj for obj in part.Group if obj.TypeId == "PartDesign::Body"]
    if len(bodies) == 1:
        body_tip = bodies[0].Tip
        if (
            body_tip is not None
            and hasattr(body_tip, "Shape")
            and not body_tip.Shape.isNull()
            and body_tip.Shape.Solids
        ):
            return body_tip
    saved_tip = part.Tip if hasattr(part, "Tip") else None
    if isinstance(saved_tip, str) and saved_tip:
        resolved = part.Document.getObject(saved_tip)
        if (
            resolved is not None
            and hasattr(resolved, "Shape")
            and not resolved.Shape.isNull()
        ):
            return resolved
    tip = _part_tip_feature(part)
    if tip is not None:
        return tip
    return None


def _cut_base_feature(cut):
    """Prefer Part Design's live predecessor over a stale saved part Tip."""
    container = cut.getParentGeoFeatureGroup()
    base_feature = getattr(cut, "BaseFeature", None)
    if (
        container is not None
        and container.TypeId == "PartDesign::Body"
        and base_feature is not None
        and hasattr(base_feature, "Shape")
        and not base_feature.Shape.isNull()
    ):
        return base_feature
    return cut.PreviousFeature


CONNECTION_CUT_TYPES = {"BoltConnectionCut", "ConnectedPatternCut"}


def _is_connection_cut(obj):
    return getattr(obj, "SheetMetalType", "") in CONNECTION_CUT_TYPES


def _connection_cut_successor(cut):
    """Return the next connection cut in the same feature-history run."""
    for candidate in cut.Document.Objects:
        if (
            candidate is not cut
            and _is_connection_cut(candidate)
            and _cut_base_feature(candidate) is cut
        ):
            return candidate
    return None


def _connection_cut_chain(cut):
    """Return the untouched base and ordered contributors ending at *cut*."""
    contributors = []
    current = cut
    visited = set()
    while _is_connection_cut(current):
        if current.Name in visited:
            raise ValueError("Connection cut history contains a dependency cycle.")
        visited.add(current.Name)
        contributors.append(current)
        current = _cut_base_feature(current)
    contributors.reverse()
    return current, contributors


def _set_cut_passthrough(cut, base):
    cut.Shape = base.Shape.copy() if base is not None else Part.Shape()
    cut.RemovedVolume = 0.0
    if "SkippedLocationCount" in cut.PropertiesList:
        cut.SkippedLocationCount = 0
    if "AggregateContributorCount" in cut.PropertiesList:
        cut.AggregateContributorCount = 0
    cut.LastError = ""


def execute_aggregate_connection_cut(cut):
    """Evaluate one consecutive connection-cut run with one final Boolean.

    Intermediate cut features remain editable contributor records and pass the
    untouched base shape downstream.  Only the final feature in the run builds
    every direct and patterned cutter and subtracts their compound.
    """
    base = _cut_base_feature(cut)
    if base is None or base.Shape.isNull():
        cut.Shape = Part.Shape()
        cut.RemovedVolume = 0.0
        if "SkippedLocationCount" in cut.PropertiesList:
            cut.SkippedLocationCount = 0
        if "AggregateContributorCount" in cut.PropertiesList:
            cut.AggregateContributorCount = 0
        cut.LastError = "Previous feature is missing or has no shape."
        return
    if _connection_cut_successor(cut) is not None:
        _set_cut_passthrough(cut, base)
        return
    try:
        aggregate_base, contributors = _connection_cut_chain(cut)
        if (
            aggregate_base is None
            or not hasattr(aggregate_base, "Shape")
            or aggregate_base.Shape.isNull()
        ):
            raise ValueError("The aggregate connection base is missing or empty.")
        base_shape = aggregate_base.Shape.copy()
        initial_volume = base_shape.Volume
        prepared = []
        skipped = 0
        for contributor in contributors:
            collector = getattr(contributor.Proxy, "collect_cutter_batches", None)
            if collector is None:
                raise ValueError(
                    "{} cannot contribute cutters to the aggregate result.".format(
                        contributor.Label
                    )
                )
            try:
                batches, broad_phase_skipped = collector(contributor, base_shape)
                skipped += broad_phase_skipped
                for batch in batches:
                    tools, exact_skipped = prepare_hole_cutters(
                        base_shape,
                        batch["records"],
                        batch["cut_extent"],
                        skip_non_intersections=batch["skip_non_intersections"],
                        validate_through=batch["validate_through"],
                    )
                    prepared.extend(tools)
                    skipped += exact_skipped
            except (ValueError, Part.OCCError) as error:
                raise ValueError("{}: {}".format(contributor.Label, error))
        result = base_shape
        if prepared:
            tool = prepared[0] if len(prepared) == 1 else Part.makeCompound(prepared)
            result = base_shape.cut(tool)
        if any(bool(getattr(contributor, "Refine", False)) for contributor in contributors):
            result = result.removeSplitter()
        if result.isNull() or not result.isValid():
            raise ValueError("The aggregate connection cut produced an invalid shape.")
        cut.Shape = result
        cut.RemovedVolume = max(0.0, initial_volume - result.Volume)
        if "SkippedLocationCount" in cut.PropertiesList:
            cut.SkippedLocationCount = skipped
        if "AggregateContributorCount" in cut.PropertiesList:
            cut.AggregateContributorCount = len(contributors)
        cut.LastError = ""
    except (ValueError, Part.OCCError) as error:
        cut.Shape = base.Shape.copy()
        cut.RemovedVolume = 0.0
        if "SkippedLocationCount" in cut.PropertiesList:
            cut.SkippedLocationCount = 0
        if "AggregateContributorCount" in cut.PropertiesList:
            cut.AggregateContributorCount = 0
        cut.LastError = str(error)
        FreeCAD.Console.PrintError(
            "Aggregate connection cut {}: {}\n".format(cut.Label, error)
        )


def _locator_intersects_feature(feature, world_frame):
    """Return whether a narrow bolt-axis probe intersects a participant."""
    local = _local_frame(feature, world_frame)
    depth = _feature_depth(feature.Shape, local["point"])
    start = local["point"].sub(_scaled(local["z_axis"], depth * 0.5))
    probe = Part.makeCylinder(
        ASSIGNMENT_PROBE_RADIUS, depth, start, local["z_axis"]
    )
    return bool(feature.Shape.common(probe).Solids)


def infer_participant_assignments(parts, tips, records):
    """Infer per-part locator keys without requiring one common participant."""
    assignments = {}
    for part, tip in zip(parts, tips):
        keys = [
            record["key"]
            for record in records
            if _locator_intersects_feature(tip, record["frame"])
        ]
        assignments[part.Name] = keys
    all_keys = {record["key"] for record in records}
    assigned_keys = {
        key for keys in assignments.values() for key in keys
    }
    missing = all_keys - assigned_keys
    if missing:
        raise ValueError(
            "{} selected location(s) do not intersect any participant.".format(
                len(missing)
            )
        )
    common_names = [
        part.Name
        for part in parts
        if set(assignments[part.Name]) == all_keys
    ]
    return assignments, common_names


class SMBoltConnection:
    """Metadata and locator definition shared by all participant cuts."""

    def __init__(self, obj, locator_references=None, participant_names=None):
        self.addVerifyProperties(obj)
        if locator_references is not None:
            obj.LocatorReferences = locator_references
        if participant_names is not None:
            obj.ParticipantNames = participant_names
        obj.Proxy = self

    def addVerifyProperties(self, obj):
        if "SheetMetalType" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString", "SheetMetalType", "Bolted Connection",
                translate("App::Property", "Sheet-metal object type"),
            ).SheetMetalType = "BoltConnection"
            obj.setEditorMode("SheetMetalType", 1)
        if "LocatorReferences" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyXLinkSubList", "LocatorReferences", "Bolted Connection",
                translate(
                    "App::Property",
                    "Sketch vertices, circular edges, or whole locator sketches",
                ),
            )
        if "LocatorGeometryFilter" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyStringList", "LocatorGeometryFilter", "Bolted Connection",
                translate(
                    "App::Property",
                    "Included whole-sketch geometry keys; empty includes every locator",
                ),
            )
        if "HoleOnlyLocatorKeys" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyStringList", "HoleOnlyLocatorKeys", "Bolted Connection",
                translate(
                    "App::Property",
                    "Enabled locator keys that cut holes without hardware",
                ),
            )
        _set_enumeration(
            obj, "ConnectionType", CONNECTION_TYPES, "Hex Bolt", "Hardware Metadata",
            translate("App::Property", "Common fastener type for this connection"),
        )
        _set_enumeration(
            obj, "BoltSize", BOLT_SIZES, "5/16 in", "Hardware Metadata",
            translate("App::Property", "Nominal bolt size"),
        )
        _set_enumeration(
            obj, "Fit", FITS, "Normal", "Hardware Metadata",
            translate("App::Property", "Common clearance fit"),
        )
        if "HardwareState" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString", "HardwareState", "Hardware Metadata",
                translate("App::Property", "Current hardware-generation state"),
            ).HardwareState = "Metadata only; bolt and nut geometry is not generated"
            obj.setEditorMode("HardwareState", 1)
        if "ParticipantNames" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyStringList", "ParticipantNames", "Bolted Connection",
                translate("App::Property", "Internal names of connected parts"),
            )
        if "CommonParticipantNames" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyStringList", "CommonParticipantNames", "Bolted Connection",
                translate(
                    "App::Property",
                    "Parts participating at every enabled bolt location",
                ),
            )
        if "RoleDefaultsApplied" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyBool", "RoleDefaultsApplied", "Bolted Connection",
                translate(
                    "App::Property",
                    "Participant roles have been initialized from common-part intent",
                ),
            ).RoleDefaultsApplied = False
            obj.setEditorMode("RoleDefaultsApplied", 2)
        if "CutFeatureNames" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyStringList", "CutFeatureNames", "Bolted Connection",
                translate("App::Property", "Generated per-part cut features"),
            )
        if "BoltCount" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyInteger", "BoltCount", "Hardware Metadata",
                translate("App::Property", "Number of locators with hardware"),
            )
            obj.setEditorMode("BoltCount", 1)
        if "LocatorCount" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyInteger", "LocatorCount", "Hardware Metadata",
                translate("App::Property", "Total number of enabled locations"),
            )
            obj.setEditorMode("LocatorCount", 1)
        if "AuxiliaryHoleCount" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyInteger", "AuxiliaryHoleCount", "Hardware Metadata",
                translate("App::Property", "Locations that cut without hardware"),
            )
            obj.setEditorMode("AuxiliaryHoleCount", 1)
        if "LastError" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString", "LastError", "Bolted Connection",
                translate("App::Property", "Locator evaluation error"),
            )
            obj.setEditorMode("LastError", 1)

    def execute(self, fp):
        self.addVerifyProperties(fp)
        try:
            records = locator_records(fp)
            hole_only = set(fp.HoleOnlyLocatorKeys)
            fp.LocatorCount = len(records)
            fp.AuxiliaryHoleCount = sum(
                record["key"] in hole_only for record in records
            )
            fp.BoltCount = fp.LocatorCount - fp.AuxiliaryHoleCount
            if fp.CutFeatureNames:
                sync_common_participants(fp)
            fp.LastError = ""
        except (ValueError, Part.OCCError) as error:
            fp.BoltCount = 0
            fp.LocatorCount = 0
            fp.AuxiliaryHoleCount = 0
            fp.LastError = str(error)
            FreeCAD.Console.PrintError("Bolted connection: {}\n".format(error))

    def onDocumentRestored(self, fp):
        self.addVerifyProperties(fp)


class SMBoltConnectionCut:
    """One participant-specific cut driven by a shared connection."""

    def __init__(
        self, obj, connection=None, previous_feature=None, participant=None,
        role="Intermediate", hole_type="Round",
    ):
        self.addVerifyProperties(obj)
        if connection is not None:
            obj.ConnectionName = connection.Name
        if previous_feature is not None:
            obj.PreviousFeature = previous_feature
        if participant is not None:
            obj.ParticipantName = participant.Name
            obj.ParticipantLabel = participant.Label
        obj.Role = role
        obj.HoleType = hole_type
        obj.Proxy = self

    def addVerifyProperties(self, obj):
        if "SheetMetalType" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString", "SheetMetalType", "Bolted Connection Cut",
                translate("App::Property", "Sheet-metal object type"),
            ).SheetMetalType = "BoltConnectionCut"
            obj.setEditorMode("SheetMetalType", 1)
        legacy_connection = None
        if "Connection" in obj.PropertiesList:
            legacy_connection = getattr(obj, "Connection", None)
            obj.Connection = None
            obj.removeProperty("Connection")
        if "ConnectionName" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString", "ConnectionName", "Bolted Connection Cut",
                translate("App::Property", "Internal name of the bolted connection"),
            )
            obj.setEditorMode("ConnectionName", 1)
        if legacy_connection is not None:
            obj.ConnectionName = legacy_connection.Name
        if "PreviousFeature" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyLink", "PreviousFeature", "Bolted Connection Cut",
                translate("App::Property", "Previous feature in this part's history"),
            )
        if "ParticipantName" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString", "ParticipantName", "Participant",
                translate("App::Property", "Internal connected-part name"),
            )
            obj.setEditorMode("ParticipantName", 1)
        if "ParticipantLabel" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString", "ParticipantLabel", "Participant",
                translate("App::Property", "Connected-part label"),
            )
            obj.setEditorMode("ParticipantLabel", 1)
        if "UseAllLocators" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyBool", "UseAllLocators", "Participant",
                translate(
                    "App::Property",
                    "This common participant uses every enabled bolt locator",
                ),
            ).UseAllLocators = True
        if "LocatorKeys" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyStringList", "LocatorKeys", "Participant",
                translate(
                    "App::Property",
                    "Enabled locator keys assigned to this non-common participant",
                ),
            )
        _set_enumeration(
            obj, "Role", PARTICIPANT_ROLES, "Intermediate", "Participant",
            translate("App::Property", "Part's role along the bolt stack"),
        )
        _set_enumeration(
            obj, "HoleType", HOLE_TYPES, "Round", "Hole Profile",
            translate("App::Property", "Hole profile used in this participant"),
        )
        if "UseConnectionFit" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyBool", "UseConnectionFit", "Hole Profile",
                translate("App::Property", "Use the connection's common fit"),
            ).UseConnectionFit = True
        _set_enumeration(
            obj, "Fit", FITS, "Normal", "Hole Profile",
            translate("App::Property", "Participant-specific clearance fit"),
        )
        if "OverrideHoleSize" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyBool", "OverrideHoleSize", "Hole Profile",
                translate("App::Property", "Override the standard hole width"),
            ).OverrideHoleSize = False
        if "HoleWidth" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyLength", "HoleWidth", "Hole Profile",
                translate("App::Property", "Round diameter or square/slot width"),
            ).HoleWidth = 0.354 * INCH
        if "SlotLength" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyLength", "SlotLength", "Hole Profile",
                translate(
                    "App::Property",
                    "Overall slot length; zero uses the hole width",
                ),
            ).SlotLength = DEFAULT_SLOT_LENGTH
        if "Rotation" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyAngle", "Rotation", "Hole Profile",
                translate("App::Property", "Square or slot rotation from sketch X"),
            ).Rotation = 0.0
        if "Refine" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyBool", "Refine", "Hole Profile",
                translate("App::Property", "Remove residual splitter edges"),
            ).Refine = True
        _set_enumeration(
            obj, "CutExtent", CUT_EXTENTS, "Nearest Sheet Layer", "Hole Profile",
            translate(
                "App::Property",
                "Cut only the closest local sheet layer or every aligned region",
            ),
        )
        if "RemovedVolume" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyVolume", "RemovedVolume", "Result",
                translate("App::Property", "Volume removed by this connection"),
            )
            obj.setEditorMode("RemovedVolume", 1)
        if "AggregateContributorCount" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyInteger",
                "AggregateContributorCount",
                "Result",
                translate(
                    "App::Property",
                    "Connection contributors evaluated by this terminal result",
                ),
            )
            obj.setEditorMode("AggregateContributorCount", 1)
        if "LastError" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString", "LastError", "Result",
                translate("App::Property", "Cut evaluation error"),
            )
            obj.setEditorMode("LastError", 1)

    def collect_cutter_batches(self, fp, base_shape):
        connection = connection_for_cut(fp)
        if connection is None:
            raise ValueError("The bolted connection reference is missing.")
        width = _effective_width(fp)
        slot_length = fp.SlotLength.Value
        rotation = math.radians(fp.Rotation.Value)
        cutter_records = []
        for record in cut_locator_records(fp):
            world_frame = record["frame"]
            local = _local_frame(fp, world_frame)
            x_axis = local["x_axis"]
            if str(fp.HoleType) != "Round" and abs(rotation) > 1.0e-14:
                y_axis = _unit(
                    local["z_axis"].cross(x_axis),
                    "Cannot determine slot rotation axis.",
                )
                x_axis = _scaled(x_axis, math.cos(rotation)).add(
                    _scaled(y_axis, math.sin(rotation))
                )
            depth = _feature_depth(base_shape, local["point"])
            cutter = make_hole_cutter(
                fp.HoleType,
                local["point"],
                x_axis,
                local["z_axis"],
                width,
                slot_length,
                depth,
            )
            cutter_records.append((cutter, local["point"]))
        return [
            {
                "cut_extent": str(fp.CutExtent),
                "records": cutter_records,
                "skip_non_intersections": False,
                "validate_through": True,
            }
        ], 0

    def execute(self, fp):
        self.addVerifyProperties(fp)
        execute_aggregate_connection_cut(fp)

    def onDocumentRestored(self, fp):
        migrating = "AggregateContributorCount" not in fp.PropertiesList
        self.addVerifyProperties(fp)
        if migrating:
            fp.touch()


def _connection_group(doc):
    group = doc.getObject("BoltedConnections")
    if group is None:
        group = doc.addObject("App::DocumentObjectGroup", "BoltedConnections")
        group.Label = translate("SheetMetal", "Bolted Connections")
    return group


def _default_profile(connection_type, index):
    if connection_type == "Carriage Bolt" and index == 0:
        return "Square"
    return "Round"


def create_bolted_connection(
    doc, locator_references, participants, connection_type="Hex Bolt",
):
    """Create one connection and one participant-specific cut per part."""
    unique_parts = []
    for participant in participants:
        if participant not in unique_parts:
            unique_parts.append(participant)
    if not unique_parts:
        raise ValueError("Select at least one sheet-metal part to cut.")
    if not locator_references:
        raise ValueError("Select at least one sketch locator.")

    tips = []
    for part in unique_parts:
        if not (
            part.TypeId == "App::Part"
            and hasattr(part, "SheetMetalType")
            and part.SheetMetalType == "Part"
        ):
            raise ValueError("Every participant must be a Sheet Metal Part.")
        tip = _target_tip(part)
        if tip is None or not hasattr(tip, "Shape") or tip.Shape.isNull():
            raise ValueError("{} has no finished shape to cut.".format(part.Label))
        tips.append(tip)

    connection = doc.addObject("App::FeaturePython", "BoltConnection")
    connection.Label = translate("SheetMetal", "Bolted Connection")
    SMBoltConnection(
        connection,
        locator_references,
        [part.Name for part in unique_parts],
    )
    connection.ConnectionType = connection_type
    _connection_group(doc).addObject(connection)
    records = locator_records(connection)
    assignments, common_names = infer_participant_assignments(
        unique_parts, tips, records
    )
    connection.CommonParticipantNames = common_names
    head_part_name = common_names[0] if common_names else unique_parts[0].Name

    cuts = []
    for index, (part, previous) in enumerate(zip(unique_parts, tips)):
        container = previous.getParentGeoFeatureGroup()
        if container is None or container.TypeId not in ("App::Part", "PartDesign::Body"):
            raise ValueError(
                "{} has no supported feature-history container.".format(part.Label)
            )
        feature_type = (
            "PartDesign::FeaturePython"
            if container.TypeId == "PartDesign::Body"
            else "Part::FeaturePython"
        )
        cut = doc.addObject(feature_type, "BoltConnectionCut")
        cut.Label = translate("SheetMetal", "Connection Cut") + " - " + part.Label
        container.addObject(cut)
        role = "Head Side" if part.Name == head_part_name else "Nut Side"
        SMBoltConnectionCut(
            cut,
            connection,
            previous,
            part,
            role,
            "Square" if connection_type == "Carriage Bolt" else "Round",
        )
        if _is_connection_cut(previous):
            previous.touch()
        cut.UseAllLocators = part.Name in common_names
        cut.LocatorKeys = [] if cut.UseAllLocators else assignments[part.Name]
        if container.TypeId == "PartDesign::Body":
            container.Tip = cut
        part.Tip = cut.Name
        if getattr(previous, "ViewObject", None) is not None:
            previous.ViewObject.Visibility = False
        if getattr(cut, "ViewObject", None) is not None:
            cut.ViewObject.Visibility = True
        cuts.append(cut)

    connection.CutFeatureNames = [cut.Name for cut in cuts]
    set_default_participant_roles(connection)
    doc.recompute()
    return connection, cuts


def add_connection_participants(connection, participants):
    """Append selected parts without requiring them to intersect seed locators."""
    doc = connection.Document
    existing_names = set(connection.ParticipantNames)
    new_parts = []
    for participant in participants:
        if participant.Name in existing_names or participant in new_parts:
            continue
        if not (
            participant.TypeId == "App::Part"
            and getattr(participant, "SheetMetalType", "") == "Part"
        ):
            raise ValueError("Every participant must be a Sheet Metal Part.")
        new_parts.append(participant)
    if not new_parts:
        return []

    records = locator_records(connection)
    all_keys = {record["key"] for record in records}
    new_cuts = []
    participant_names = list(connection.ParticipantNames)
    cut_names = list(connection.CutFeatureNames)
    for part in new_parts:
        previous = _target_tip(part)
        if previous is None or not hasattr(previous, "Shape") or previous.Shape.isNull():
            raise ValueError("{} has no finished shape to cut.".format(part.Label))
        locator_keys = [
            record["key"]
            for record in records
            if _locator_intersects_feature(previous, record["frame"])
        ]
        container = previous.getParentGeoFeatureGroup()
        if container is None or container.TypeId not in (
            "App::Part",
            "PartDesign::Body",
        ):
            raise ValueError(
                "{} has no supported feature-history container.".format(part.Label)
            )
        feature_type = (
            "PartDesign::FeaturePython"
            if container.TypeId == "PartDesign::Body"
            else "Part::FeaturePython"
        )
        cut = doc.addObject(feature_type, "BoltConnectionCut")
        cut.Label = translate("SheetMetal", "Connection Cut") + " - " + part.Label
        container.addObject(cut)
        SMBoltConnectionCut(
            cut,
            connection,
            previous,
            part,
            "Nut Side",
            "Square" if str(connection.ConnectionType) == "Carriage Bolt" else "Round",
        )
        if _is_connection_cut(previous):
            previous.touch()
        cut.UseAllLocators = bool(all_keys) and set(locator_keys) == all_keys
        cut.LocatorKeys = [] if cut.UseAllLocators else locator_keys
        if container.TypeId == "PartDesign::Body":
            container.Tip = cut
        part.Tip = cut.Name
        if getattr(previous, "ViewObject", None) is not None:
            previous.ViewObject.Visibility = False
        if getattr(cut, "ViewObject", None) is not None:
            cut.ViewObject.Visibility = True
        participant_names.append(part.Name)
        cut_names.append(cut.Name)
        new_cuts.append(cut)

    connection.ParticipantNames = participant_names
    connection.CutFeatureNames = cut_names
    sync_common_participants(connection)
    set_default_participant_roles(connection)
    doc.recompute()
    return new_cuts


def connection_cuts(connection):
    """Return the extant cut features generated for a connection."""
    cuts = []
    for name in connection.CutFeatureNames:
        cut = connection.Document.getObject(name)
        if cut is not None and connection_for_cut(cut) is connection:
            cuts.append(cut)
    return cuts


def sync_common_participants(connection):
    """Synchronize connection metadata from participant assignment modes."""
    connection.CommonParticipantNames = [
        cut.ParticipantName
        for cut in connection_cuts(connection)
        if cut.UseAllLocators
    ]


def set_default_participant_roles(connection):
    """Assign Head/Intermediate/Nut defaults from common-part membership."""
    cuts = connection_cuts(connection)
    common_names = set(connection.CommonParticipantNames)
    common_cuts = [cut for cut in cuts if cut.ParticipantName in common_names]
    if common_cuts:
        for index, cut in enumerate(common_cuts):
            cut.Role = "Head Side" if index == 0 else "Intermediate"
        for cut in cuts:
            if cut.ParticipantName not in common_names:
                cut.Role = "Nut Side"
    else:
        assigned = [cut for cut in cuts if cut.UseAllLocators or cut.LocatorKeys]
        for cut in cuts:
            cut.Role = "Nut Side"
        if assigned:
            assigned[0].Role = "Head Side"
    connection.RoleDefaultsApplied = True


def set_connection_type(connection, value):
    """Set hardware metadata and apply non-destructive profile defaults."""
    previous = str(connection.ConnectionType)
    connection.ConnectionType = value
    for cut in connection_cuts(connection):
        if value == "Carriage Bolt" and str(cut.HoleType) != "Slotted Square":
            cut.HoleType = "Square"
        elif previous == "Carriage Bolt" and str(cut.HoleType) == "Square":
            cut.HoleType = "Round"


if SheetMetalTools.isGuiLoaded():
    Gui = FreeCAD.Gui
    from PySide import QtCore, QtGui

    class SMBoltConnectionViewProvider(SheetMetalTools.SMViewProvider):
        def getIcon(self):
            return os.path.join(icons_path, "SheetMetal_BoltConnection.svg")

        def getTaskPanel(self, obj):
            return SMBoltConnectionTaskPanel(obj)

        def claimChildren(self):
            return []


    class SMBoltConnectionCutViewProvider(SheetMetalTools.SMViewProvider):
        def getIcon(self):
            return os.path.join(icons_path, "SheetMetal_BoltConnection.svg")


    class SMBoltConnectionTaskPanel:
        """Edit common hardware metadata and participant-specific profiles."""

        def __init__(self, obj):
            self.obj = obj
            obj.Proxy.addVerifyProperties(obj)
            for cut in connection_cuts(obj):
                cut.Proxy.addVerifyProperties(cut)
            if not obj.RoleDefaultsApplied:
                set_default_participant_roles(obj)
            if str(obj.ConnectionType) == "Carriage Bolt":
                set_connection_type(obj, "Carriage Bolt")
                obj.Document.recompute()
            self.form = QtGui.QWidget()
            self.form.setWindowTitle(translate("SheetMetal", "Bolted connection"))
            layout = QtGui.QVBoxLayout(self.form)

            form_layout = QtGui.QFormLayout()
            self.connection_type = QtGui.QComboBox()
            self.connection_type.addItems(CONNECTION_TYPES)
            self.bolt_size = QtGui.QComboBox()
            self.bolt_size.addItems(BOLT_SIZES)
            self.fit = QtGui.QComboBox()
            self.fit.addItems(FITS)
            form_layout.addRow(translate("SheetMetal", "Connection type"), self.connection_type)
            form_layout.addRow(translate("SheetMetal", "Bolt size"), self.bolt_size)
            form_layout.addRow(translate("SheetMetal", "Common fit"), self.fit)
            layout.addLayout(form_layout)

            self.locator_label = QtGui.QLabel(
                self._locator_status_text()
            )
            layout.addWidget(self.locator_label)

            self.locator_tree = QtGui.QTreeWidget()
            self.locator_tree.setColumnCount(3)
            self.locator_tree.setHeaderLabels(
                [
                    translate("SheetMetal", "Use"),
                    translate("SheetMetal", "Bolt"),
                    translate("SheetMetal", "Location / Sketcher reference"),
                ]
            )
            self.locator_tree.setRootIsDecorated(False)
            self.locator_tree.setAlternatingRowColors(True)
            self.locator_tree.itemChanged.connect(self._locator_selection_changed)
            self.locator_tree.currentItemChanged.connect(
                self._highlight_locator_item
            )
            layout.addWidget(self.locator_tree)

            self.table = QtGui.QTableWidget()
            self.table.setColumnCount(9)
            self.table.setHorizontalHeaderLabels(
                [
                    translate("SheetMetal", "Part"),
                    translate("SheetMetal", "All locations"),
                    translate("SheetMetal", "Role"),
                    translate("SheetMetal", "Hole"),
                    translate("SheetMetal", "Common fit"),
                    translate("SheetMetal", "Fit"),
                    translate("SheetMetal", "Override width"),
                    translate("SheetMetal", "Slot length (in)"),
                    translate("SheetMetal", "Rotation (deg)"),
                ]
            )
            participant_buttons = QtGui.QHBoxLayout()
            self.add_participants_button = QtGui.QPushButton(
                translate("SheetMetal", "Add selected parts")
            )
            self.add_participants_button.setToolTip(
                translate(
                    "SheetMetal",
                    "Add selected Sheet Metal Parts and extend existing connected "
                    "patterns to cut them where transformed locators intersect.",
                )
            )
            participant_buttons.addStretch(1)
            participant_buttons.addWidget(self.add_participants_button)
            layout.addLayout(participant_buttons)
            layout.addWidget(self.table)

            assignment_label = QtGui.QLabel(
                translate("SheetMetal", "Bolt locations assigned to each part")
            )
            layout.addWidget(assignment_label)
            self.assignment_table = QtGui.QTableWidget()
            self.assignment_table.currentCellChanged.connect(
                self._highlight_assignment_row
            )
            layout.addWidget(self.assignment_table)

            self.error_label = QtGui.QLabel()
            self.error_label.setWordWrap(True)
            self.error_label.setStyleSheet("color: #d9534f;")
            layout.addWidget(self.error_label)

            self.connection_type.setCurrentText(str(obj.ConnectionType))
            self.bolt_size.setCurrentText(str(obj.BoltSize))
            self.fit.setCurrentText(str(obj.Fit))
            self.connection_type.currentTextChanged.connect(self._set_connection_type)
            self.bolt_size.currentTextChanged.connect(
                lambda value: self._set_connection_property("BoltSize", value)
            )
            self.fit.currentTextChanged.connect(
                lambda value: self._set_connection_property("Fit", value)
            )
            self.add_participants_button.clicked.connect(
                self._add_selected_participants
            )
            self._populate_locators()
            self._populate_participants()
            self._populate_assignment_matrix()
            self._update_status()

        def _recompute(self):
            self.obj.Document.recompute()
            self.locator_label.setText(self._locator_status_text())
            self._update_status()

        def _locator_status_text(self):
            return (
                translate(
                    "SheetMetal",
                    "%1 location(s): %2 bolt(s), %3 auxiliary hole(s)",
                )
                .replace("%1", str(self.obj.LocatorCount))
                .replace("%2", str(self.obj.BoltCount))
                .replace("%3", str(self.obj.AuxiliaryHoleCount))
            )

        def _cut_errors(self):
            return [
                "{}: {}".format(cut.ParticipantLabel, cut.LastError)
                for cut in connection_cuts(self.obj)
                if str(cut.LastError)
            ]

        def _update_status(self):
            errors = self._cut_errors()
            self.error_label.setText("\n".join(errors))
            self.error_label.setVisible(bool(errors))

        def _set_connection_property(self, name, value):
            setattr(self.obj, name, value)
            self._recompute()

        def _set_connection_type(self, value):
            set_connection_type(self.obj, value)
            self._recompute()
            self._populate_participants()

        def _populate_locators(self):
            self.locator_tree.blockSignals(True)
            self.locator_tree.clear()
            filter_keys = set(self.obj.LocatorGeometryFilter)
            has_filter = bool(filter_keys)
            hole_only = set(self.obj.HoleOnlyLocatorKeys)
            try:
                candidates = locator_candidates(self.obj)
            except ValueError:
                candidates = []
            for candidate in candidates:
                item = QtGui.QTreeWidgetItem(self.locator_tree)
                item.setData(0, QtCore.Qt.UserRole, candidate["key"])
                item.setData(
                    0, QtCore.Qt.UserRole + 1, candidate["filterable"]
                )
                item.setData(
                    0, QtCore.Qt.UserRole + 2, candidate["sketch"].Name
                )
                item.setData(
                    0, QtCore.Qt.UserRole + 3, candidate["subelement"]
                )
                enabled = (
                    not candidate["filterable"]
                    or not has_filter
                    or candidate["key"] in filter_keys
                )
                if candidate["filterable"]:
                    item.setCheckState(
                        0, QtCore.Qt.Checked if enabled else QtCore.Qt.Unchecked
                    )
                else:
                    item.setText(0, translate("SheetMetal", "Fixed"))
                item.setCheckState(
                    1,
                    QtCore.Qt.Unchecked
                    if candidate["key"] in hole_only
                    else QtCore.Qt.Checked,
                )
                item.setText(2, candidate["label"])
                item.setToolTip(
                    2,
                    translate(
                        "SheetMetal",
                        "Select this row to highlight the matching sketch locator.",
                    ),
                )
                flags = item.flags() | QtCore.Qt.ItemIsUserCheckable
                item.setFlags(flags)
            self.locator_tree.resizeColumnToContents(0)
            self.locator_tree.resizeColumnToContents(1)
            self.locator_tree.resizeColumnToContents(2)
            self.locator_tree.blockSignals(False)

        def _locator_selection_changed(self, _item, column):
            keys = []
            checked = []
            hole_only = []
            for index in range(self.locator_tree.topLevelItemCount()):
                item = self.locator_tree.topLevelItem(index)
                key = str(item.data(0, QtCore.Qt.UserRole))
                if bool(item.data(0, QtCore.Qt.UserRole + 1)):
                    keys.append(key)
                    if item.checkState(0) == QtCore.Qt.Checked:
                        checked.append(key)
                if item.checkState(1) != QtCore.Qt.Checked:
                    hole_only.append(key)
            if column == 0:
                if checked == keys:
                    self.obj.LocatorGeometryFilter = []
                elif checked:
                    self.obj.LocatorGeometryFilter = checked
                else:
                    self.obj.LocatorGeometryFilter = ["__none__"]
            self.obj.HoleOnlyLocatorKeys = hole_only
            self._recompute()
            if column == 0:
                self._populate_assignment_matrix()

        def _highlight_locator(self, sketch_name, subelement):
            sketch = self.obj.Document.getObject(str(sketch_name))
            if sketch is None:
                return
            Gui.Selection.clearSelection()
            if subelement:
                Gui.Selection.addSelection(sketch, str(subelement))
            else:
                Gui.Selection.addSelection(sketch)

        def _highlight_locator_item(self, item, _previous):
            if item is None:
                return
            self._highlight_locator(
                item.data(0, QtCore.Qt.UserRole + 2),
                item.data(0, QtCore.Qt.UserRole + 3),
            )

        def _combo(self, values, current, callback):
            combo = QtGui.QComboBox()
            combo.addItems(values)
            combo.setCurrentText(str(current))
            combo.currentTextChanged.connect(callback)
            return combo

        def _populate_participants(self):
            cuts = connection_cuts(self.obj)
            self.table.setRowCount(len(cuts))
            for row, cut in enumerate(cuts):
                item = QtGui.QTableWidgetItem(cut.ParticipantLabel)
                item.setFlags(item.flags() & ~QtCore.Qt.ItemIsEditable)
                self.table.setItem(row, 0, item)

                common = QtGui.QCheckBox()
                common.setChecked(cut.UseAllLocators)
                common.toggled.connect(
                    lambda value, feature=cut: self._set_common_participant(
                        feature, value
                    )
                )
                self.table.setCellWidget(row, 1, common)

                role = self._combo(
                    PARTICIPANT_ROLES,
                    cut.Role,
                    lambda value, feature=cut: self._set_cut_property(feature, "Role", value),
                )
                self.table.setCellWidget(row, 2, role)
                profile = self._combo(
                    HOLE_TYPES,
                    cut.HoleType,
                    lambda value, feature=cut: self._set_cut_property(
                        feature, "HoleType", value
                    ),
                )
                self.table.setCellWidget(row, 3, profile)

                common_fit = QtGui.QCheckBox()
                common_fit.setChecked(cut.UseConnectionFit)
                common_fit.toggled.connect(
                    lambda value, feature=cut: self._set_cut_property(
                        feature, "UseConnectionFit", value
                    )
                )
                self.table.setCellWidget(row, 4, common_fit)
                fit = self._combo(
                    FITS,
                    cut.Fit,
                    lambda value, feature=cut: self._set_cut_property(feature, "Fit", value),
                )
                self.table.setCellWidget(row, 5, fit)

                override = QtGui.QDoubleSpinBox()
                override.setDecimals(4)
                override.setRange(0.0, 1000.0)
                override.setValue(cut.HoleWidth.Value if cut.OverrideHoleSize else 0.0)
                override.setSpecialValueText(translate("SheetMetal", "Standard"))
                override.valueChanged.connect(
                    lambda value, feature=cut: self._set_override_width(feature, value)
                )
                self.table.setCellWidget(row, 6, override)

                slot_length = QtGui.QDoubleSpinBox()
                slot_length.setDecimals(4)
                slot_length.setRange(0.0, 1000.0)
                slot_length.setSuffix(" in")
                slot_length.setSpecialValueText(translate("SheetMetal", "Auto"))
                slot_length.setValue(cut.SlotLength.Value / INCH)
                slot_length.valueChanged.connect(
                    lambda value, feature=cut: self._set_slot_length_inches(
                        feature, value
                    )
                )
                self.table.setCellWidget(row, 7, slot_length)

                rotation = QtGui.QDoubleSpinBox()
                rotation.setDecimals(3)
                rotation.setRange(-360.0, 360.0)
                rotation.setValue(cut.Rotation.Value)
                rotation.valueChanged.connect(
                    lambda value, feature=cut: self._set_cut_property(
                        feature, "Rotation", value
                    )
                )
                self.table.setCellWidget(row, 8, rotation)

            self.table.resizeColumnsToContents()

        def _populate_assignment_matrix(self):
            self.assignment_table.blockSignals(True)
            cuts = connection_cuts(self.obj)
            try:
                records = locator_records(self.obj)
            except ValueError:
                records = []
            self.assignment_table.clear()
            self.assignment_table.setRowCount(len(records))
            self.assignment_table.setColumnCount(len(cuts) + 1)
            self.assignment_table.setHorizontalHeaderLabels(
                [translate("SheetMetal", "Bolt location")]
                + [cut.ParticipantLabel for cut in cuts]
            )
            for row, record in enumerate(records):
                label = QtGui.QTableWidgetItem(record["label"])
                label.setFlags(label.flags() & ~QtCore.Qt.ItemIsEditable)
                label.setData(QtCore.Qt.UserRole, record["sketch"].Name)
                label.setData(QtCore.Qt.UserRole + 1, record["subelement"])
                self.assignment_table.setItem(row, 0, label)
                for column, cut in enumerate(cuts, 1):
                    assigned = cut.UseAllLocators or record["key"] in cut.LocatorKeys
                    checkbox = QtGui.QCheckBox()
                    checkbox.setChecked(assigned)
                    checkbox.setEnabled(not cut.UseAllLocators)
                    checkbox.toggled.connect(
                        lambda value, feature=cut, key=record["key"]:
                        self._set_locator_assignment(feature, key, value)
                    )
                    self.assignment_table.setCellWidget(row, column, checkbox)
            self.assignment_table.resizeColumnsToContents()
            self.assignment_table.blockSignals(False)

        def _highlight_assignment_row(
            self, current_row, _current_column, _previous_row, _previous_column
        ):
            if current_row < 0:
                return
            item = self.assignment_table.item(current_row, 0)
            if item is None:
                return
            self._highlight_locator(
                item.data(QtCore.Qt.UserRole),
                item.data(QtCore.Qt.UserRole + 1),
            )

        def _set_common_participant(self, cut, value):
            cut.UseAllLocators = value
            if value:
                cut.LocatorKeys = []
            else:
                try:
                    cut.LocatorKeys = [
                        record["key"] for record in locator_records(self.obj)
                    ]
                except ValueError:
                    cut.LocatorKeys = []
            sync_common_participants(self.obj)
            set_default_participant_roles(self.obj)
            self._recompute()
            self._populate_participants()
            self._populate_assignment_matrix()

        def _set_locator_assignment(self, cut, key, value):
            if cut.UseAllLocators:
                return
            keys = set(cut.LocatorKeys)
            if value:
                keys.add(key)
            else:
                keys.discard(key)
            try:
                ordered = [
                    record["key"]
                    for record in locator_records(self.obj)
                    if record["key"] in keys
                ]
            except ValueError:
                ordered = []
            cut.LocatorKeys = ordered
            self._recompute()

        def _set_cut_property(self, cut, name, value):
            setattr(cut, name, value)
            self._recompute()

        def _set_slot_length_inches(self, cut, value):
            cut.SlotLength = value * INCH
            self._recompute()

        def _set_override_width(self, cut, value):
            cut.OverrideHoleSize = value > 0.0
            if value > 0.0:
                cut.HoleWidth = value
            self._recompute()

        def _add_selected_participants(self):
            parts = []
            for selected in Gui.Selection.getSelection():
                part = _find_sheet_metal_part(selected)
                if part is not None and part not in parts:
                    parts.append(part)
            parts = [
                part
                for part in parts
                if part.Name not in self.obj.ParticipantNames
            ]
            if not parts:
                SheetMetalTools.smWarnDialog(
                    translate(
                        "SheetMetal",
                        "Select one or more Sheet Metal Parts that are not already "
                        "participants.",
                    )
                )
                return
            try:
                added_cuts = add_connection_participants(self.obj, parts)
                from SheetMetalConnectedPatternCmd import (
                    sync_connection_participant_patterns,
                )

                pattern_cuts = sync_connection_participant_patterns(
                    self.obj, added_cuts
                )
                for cut in added_cuts:
                    if not isinstance(
                        getattr(cut.ViewObject, "Proxy", None),
                        SMBoltConnectionCutViewProvider,
                    ):
                        SMBoltConnectionCutViewProvider(cut.ViewObject)
                for cut in pattern_cuts:
                    if getattr(cut.ViewObject, "Proxy", None) is None:
                        from SheetMetalConnectedPatternCmd import (
                            SMConnectedPatternCutViewProvider,
                        )

                        SMConnectedPatternCutViewProvider(cut.ViewObject)
                self._recompute()
                self._populate_participants()
                self._populate_assignment_matrix()
            except (ValueError, Part.OCCError) as error:
                SheetMetalTools.smWarnDialog(str(error))

        def isAllowedAlterSelection(self):
            return True

        def isAllowedAlterView(self):
            return True

        def accept(self):
            if self.obj.LocatorCount <= 0:
                SheetMetalTools.smWarnDialog(
                    translate("SheetMetal", "Select at least one hole location.")
                )
                return False
            records = locator_records(self.obj)
            unassigned = [
                record["label"]
                for record in records
                if not any(
                    cut.UseAllLocators or record["key"] in cut.LocatorKeys
                    for cut in connection_cuts(self.obj)
                )
            ]
            if unassigned:
                SheetMetalTools.smWarnDialog(
                    translate(
                        "SheetMetal",
                        "Every enabled location must be assigned to at least one part.",
                    )
                )
                return False
            errors = self._cut_errors()
            if errors:
                SheetMetalTools.smWarnDialog("\n".join(errors))
                return False
            return SheetMetalTools.taskAccept(self)

        def reject(self):
            SheetMetalTools.taskReject(self)


    def _selection_data():
        locator_references = []
        parts = []
        for selection in Gui.Selection.getSelectionEx():
            obj = selection.Object
            if obj.isDerivedFrom("Sketcher::SketchObject"):
                sub_names = []
                for name in selection.SubElementNames:
                    sub_object = obj.getSubObject(name)
                    if isinstance(sub_object, Part.Vertex) or (
                        isinstance(sub_object, Part.Edge)
                        and isinstance(sub_object.Curve, Part.Circle)
                    ):
                        sub_names.append(name)
                if not selection.SubElementNames or sub_names:
                    locator_references.append((obj, sub_names))
                continue
            part = _find_sheet_metal_part(obj)
            if part is not None and part not in parts:
                parts.append(part)
        return locator_references, parts


    class AddBoltConnectionCommandClass:
        def GetResources(self):
            return {
                "Pixmap": os.path.join(icons_path, "SheetMetal_BoltConnection.svg"),
                "MenuText": translate("SheetMetal", "Bolted Connection"),
                "ToolTip": translate(
                    "SheetMetal",
                    "Create one connection/hole set from selected sketch points or "
                    "circles and one or more Sheet Metal Parts. Each location can be "
                    "assigned independently and may omit hardware.",
                ),
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            locator_references, parts = _selection_data()
            doc.openTransaction("BoltConnection")
            try:
                connection, cuts = create_bolted_connection(
                    doc, locator_references, parts
                )
                SMBoltConnectionViewProvider(connection.ViewObject)
                for cut in cuts:
                    SMBoltConnectionCutViewProvider(cut.ViewObject)
                Gui.Selection.clearSelection()
                Gui.Selection.addSelection(connection)
                dialog = SMBoltConnectionTaskPanel(connection)
                SheetMetalTools.updateTaskTitleIcon(dialog)
                Gui.Control.showDialog(dialog)
            except (ValueError, Part.OCCError) as error:
                doc.abortTransaction()
                SheetMetalTools.smWarnDialog(str(error))

        def IsActive(self):
            if FreeCAD.ActiveDocument is None:
                return False
            locator_references, parts = _selection_data()
            return bool(locator_references) and bool(parts)


    def repair_bolt_connection_view_providers(doc):
        """Restore editable GUI proxies in documents saved by older tooling."""
        repaired = []
        if doc is None:
            return repaired
        for obj in doc.Objects:
            sheet_metal_type = str(getattr(obj, "SheetMetalType", ""))
            view_object = getattr(obj, "ViewObject", None)
            if view_object is None:
                continue
            if sheet_metal_type == "BoltConnection":
                if not isinstance(
                    getattr(view_object, "Proxy", None),
                    SMBoltConnectionViewProvider,
                ):
                    SMBoltConnectionViewProvider(view_object)
                    repaired.append(obj)
            elif sheet_metal_type == "BoltConnectionCut":
                if not isinstance(
                    getattr(view_object, "Proxy", None),
                    SMBoltConnectionCutViewProvider,
                ):
                    SMBoltConnectionCutViewProvider(view_object)
                    repaired.append(obj)
        return repaired


    class _BoltConnectionViewProviderObserver:
        def slotActivateDocument(self, doc):
            repair_bolt_connection_view_providers(doc)


    if "_bolt_connection_view_provider_observer" not in globals():
        _bolt_connection_view_provider_observer = (
            _BoltConnectionViewProviderObserver()
        )
        FreeCAD.addDocumentObserver(_bolt_connection_view_provider_observer)
        for _open_document in FreeCAD.listDocuments().values():
            repair_bolt_connection_view_providers(_open_document)


    Gui.addCommand("SheetMetal_BoltConnection", AddBoltConnectionCommandClass())
