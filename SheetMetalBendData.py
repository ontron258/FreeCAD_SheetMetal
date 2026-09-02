########################################################################
#
#  SheetMetalBendData.py
#
#  Copyright 2026 Adrian
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
########################################################################

"""Persistent semantic bend data emitted by the V2 Unfold feature.

The serialized contract deliberately contains only physical bend occurrences.
Drawing concerns such as bend-type IDs, quantities, backstops, and table layout
belong to the drawing consumer rather than the Sheet Metal model.
"""

from __future__ import annotations

import json


BEND_DATA_SCHEMA = "freecad-sheetmetal-bends"
BEND_DATA_VERSION = 1


def _number(value, digits=9):
    """Return a stable JSON number while normalizing negative zero."""

    result = round(float(value), digits)
    return 0.0 if result == 0.0 else result


def _vector(value):
    return [_number(value.x), _number(value.y), _number(value.z)]


def _line_endpoints(edge):
    """Return canonically oriented endpoints for one straight bend edge."""

    if len(edge.Vertexes) >= 2:
        points = [edge.Vertexes[0].Point, edge.Vertexes[-1].Point]
    else:
        points = [edge.valueAt(edge.FirstParameter), edge.valueAt(edge.LastParameter)]
    endpoints = sorted((_vector(point) for point in points), key=tuple)
    return endpoints


def _source_name(index, prefix):
    if index is None:
        return ""
    return "{}{}".format(prefix, int(index))


def _source_index(info, attribute):
    value = getattr(info, attribute, None)
    return int(value) if value is not None else 0


def _occurrence_key(info, endpoints):
    source_face = _source_name(getattr(info, "source_face_index", None), "Face")
    if source_face:
        return source_face
    flat = ",".join("{:.9g}".format(value) for point in endpoints for value in point)
    return "line:" + flat


def occurrence_from_bend_info(info, default_thickness=0.0):
    """Convert one transient ``BendInfo`` into the persisted v1 contract."""

    edge = getattr(info, "unfold_line", None)
    if edge is None:
        raise ValueError("BendInfo has no unfold-local centerline")

    signed_angle = float(info.angle)
    direction = "UP" if signed_angle >= 0.0 else "DOWN"
    thickness = float(getattr(info, "thickness", default_thickness))
    analyzed_radius = float(info.radius)
    inside_radius = (
        analyzed_radius if direction == "UP" else analyzed_radius - thickness
    )
    endpoints = _line_endpoints(edge)

    return {
        "key": _occurrence_key(info, endpoints),
        "sourceFace": _source_name(
            getattr(info, "source_face_index", None), "Face"
        ),
        "sourceFaceIndex": _source_index(info, "source_face_index"),
        "sourceEdge": _source_name(
            getattr(info, "source_edge_index", None), "Edge"
        ),
        "sourceEdgeIndex": _source_index(info, "source_edge_index"),
        "direction": direction,
        "angleDegrees": _number(abs(signed_angle)),
        "signedAngleDegrees": _number(signed_angle),
        "analyzedRadiusMm": _number(analyzed_radius),
        "insideRadiusMm": _number(inside_radius),
        "outsideRadiusMm": _number(inside_radius + thickness),
        "thicknessMm": _number(thickness),
        "bendLengthMm": _number(edge.Length),
        "line": {"start": endpoints[0], "end": endpoints[1]},
    }


def build_bend_data(base_object, base_face, root_normal, bend_infos):
    """Build the versioned document stored on an ``SMUnfold`` object."""

    occurrences = [occurrence_from_bend_info(info) for info in bend_infos]
    occurrences.sort(
        key=lambda item: (
            item["sourceFaceIndex"],
            tuple(item["line"]["start"]),
            tuple(item["line"]["end"]),
        )
    )
    return {
        "schema": BEND_DATA_SCHEMA,
        "schemaVersion": BEND_DATA_VERSION,
        "coordinateFrame": "unfold-object-local",
        "provenance": "SheetMetalNewUnfolder.getUnfold",
        "baseObject": getattr(base_object, "Name", ""),
        "baseFace": str(base_face),
        "referenceNormal": _vector(root_normal),
        "occurrences": occurrences,
    }


def dumps_bend_data(data):
    """Serialize bend data deterministically for saved-document comparisons."""

    return json.dumps(data, sort_keys=True, separators=(",", ":"))
