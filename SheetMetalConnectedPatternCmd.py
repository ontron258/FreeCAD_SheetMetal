########################################################################
#
#  SheetMetalConnectedPatternCmd.py
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public License
#  as published by the Free Software Foundation; either version 2 of
#  the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Lesser General Public License for more details.
#
########################################################################

"""Pattern sheet-metal occurrences consumed by cross-part connections.

The source Sheet Metal Part remains the manufactured definition. An App::Link
array presents additional occurrences, while the existing participant-specific
bolted cuts consume a deduplicated union of terminal pattern branches. No
separate patterned-hole feature is created for new patterns.
"""

import os

import FreeCAD
import Part

import SheetMetalTools
from SheetMetalBoltConnectionCmd import (
    _find_sheet_metal_part,
    _scaled,
    _target_tip,
    _unit,
    SMBoltConnectionCut,
    connection_for_cut,
    connection_cuts,
    definition_locator_records,
    execute_aggregate_connection_cut,
    set_connection_occurrence_providers,
)


translate = FreeCAD.Qt.translate
icons_path = SheetMetalTools.icons_path

PATTERN_TYPES = ["Polar", "Linear"]


def _set_enumeration(obj, name, values, default, group, description):
    if name not in obj.PropertiesList:
        obj.addProperty("App::PropertyEnumeration", name, group, description)
        setattr(obj, name, values)
        setattr(obj, name, default)
        return
    current = str(getattr(obj, name))
    if obj.getEnumerationsOfProperty(name) != values:
        setattr(obj, name, values)
        setattr(obj, name, current if current in values else default)


def _rotation_about_center(center, axis, angle_degrees):
    rotation = FreeCAD.Rotation(axis, angle_degrees)
    return FreeCAD.Placement(FreeCAD.Vector(), rotation, center)


def _is_datum_line(obj):
    """Return whether *obj* is a supported rotary-axis datum."""
    if obj is None:
        return False
    type_id = getattr(obj, "TypeId", "")
    return type_id in ("Part::DatumLine", "PartDesign::Line") or (
        hasattr(obj, "isDerivedFrom")
        and (
            obj.isDerivedFrom("Part::DatumLine")
            or obj.isDerivedFrom("PartDesign::Line")
        )
    )


def _is_direction_reference(obj):
    """Return whether *obj* provides a usable local-Z axis direction."""
    if _is_datum_line(obj):
        return True
    if obj is None:
        return False
    return getattr(obj, "TypeId", "") == "App::Line" or (
        hasattr(obj, "isDerivedFrom") and obj.isDerivedFrom("App::Line")
    )


def _axis_reference_object(pattern):
    reference = getattr(pattern, "AxisReference", None)
    if isinstance(reference, tuple):
        return reference[0]
    return reference


def _set_axis_reference(pattern, reference):
    pattern.AxisReference = None if reference is None else (reference, [""])


def _reference_direction(reference):
    placement = reference.getGlobalPlacement()
    local_direction = (
        FreeCAD.Vector(1.0, 0.0, 0.0)
        if getattr(reference, "TypeId", "") == "App::Line"
        else FreeCAD.Vector(0.0, 0.0, 1.0)
    )
    return placement.Rotation.multVec(local_direction)


def polar_axis_definition(pattern):
    """Return the effective world-space polar center and axis."""
    reference = _axis_reference_object(pattern)
    if reference is None:
        return FreeCAD.Vector(pattern.PolarCenter), _unit(
            pattern.PolarAxis,
            "The connected pattern has no usable axis.",
        )
    if not _is_direction_reference(reference):
        raise ValueError("The polar axis reference is not an axis or datum line.")
    placement = reference.getGlobalPlacement()
    return FreeCAD.Vector(placement.Base), _unit(
        _reference_direction(reference),
        "The selected datum line has no usable direction.",
    )


def linear_direction_definition(pattern):
    """Return the effective world-space direction for a linear pattern."""
    reference = getattr(pattern, "LinearDirectionReference", None)
    if isinstance(reference, tuple):
        reference = reference[0]
    if reference is None:
        direction = _unit(
            pattern.LinearDirection,
            "The connected pattern has no usable linear direction.",
        )
    else:
        if not _is_direction_reference(reference):
            raise ValueError(
                "The linear direction reference is not an axis or datum line."
            )
        direction = _unit(
            _reference_direction(reference),
            "The selected linear axis has no usable direction.",
        )
    return (
        _scaled(direction, -1.0)
        if getattr(pattern, "ReverseLinearDirection", False)
        else direction
    )


def _linear_transforms(count, direction, spacing):
    if count < 1:
        raise ValueError("Pattern occurrences must be at least one.")
    if spacing <= SheetMetalTools.smEpsilon and count > 1:
        raise ValueError("Linear pattern spacing must be greater than zero.")
    return [
        FreeCAD.Placement(_scaled(direction, spacing * index), FreeCAD.Rotation())
        for index in range(count)
    ]


def _parent_pattern(pattern):
    reference = getattr(pattern, "ParentPatternReference", None)
    if isinstance(reference, tuple):
        return reference[0]
    return reference


def set_parent_pattern(pattern, parent):
    """Use *parent*'s complete occurrence set as *pattern*'s seed set.

    Follow-on patterns intentionally share one manufactured source part.  The
    parent contributes the seed part and every occurrence it already created;
    the child then applies its own transform to that complete set.
    """
    if parent is None:
        pattern.ParentPatternReference = None
        pattern.touch()
        return
    if getattr(parent, "SheetMetalType", "") != "ConnectedPartPattern":
        raise ValueError("Select a connected part pattern as the seed set.")
    if parent.Document is not pattern.Document:
        raise ValueError("The seed pattern must be in the same document.")
    if parent.SourcePartName != pattern.SourcePartName:
        raise ValueError(
            "The seed pattern must repeat the same source part ({}).".format(
                pattern.SourcePartLabel
            )
        )
    current = parent
    visited = set()
    while current is not None:
        if current is pattern:
            raise ValueError("Connected part patterns cannot form a dependency cycle.")
        if current.Name in visited:
            raise ValueError("The selected seed pattern already contains a cycle.")
        visited.add(current.Name)
        current = _parent_pattern(current)
    pattern.ParentPatternReference = (parent, [""])
    pattern.touch()


def _own_pattern_transforms(pattern):
    """Return this feature's transforms, including its seed occurrence."""
    count = int(pattern.Occurrences)
    if count < 1:
        raise ValueError("Pattern occurrences must be at least one.")

    if str(pattern.PatternType) == "Linear":
        direction = linear_direction_definition(pattern)
        spacing = pattern.Spacing.Value
        return _linear_transforms(count, direction, spacing)

    center, axis = polar_axis_definition(pattern)
    total_angle = pattern.TotalAngle.Value
    if count <= 1:
        step = 0.0
    elif pattern.Closed:
        step = total_angle / count
    else:
        step = total_angle / (count - 1)
    return [
        _rotation_about_center(center, axis, step * index)
        for index in range(count)
    ]


def _pattern_transforms(pattern, include_seed, visited):
    if pattern.Name in visited:
        raise ValueError("Connected part patterns cannot form a dependency cycle.")
    visited = set(visited)
    visited.add(pattern.Name)
    parent = _parent_pattern(pattern)
    parent_transforms = (
        _pattern_transforms(parent, True, visited)
        if parent is not None
        else [FreeCAD.Placement()]
    )
    own_transforms = _own_pattern_transforms(pattern)
    combined = [
        own.multiply(parent_transform)
        for own in own_transforms
        for parent_transform in parent_transforms
    ]
    return combined if include_seed else combined[len(parent_transforms):]


def pattern_transforms(pattern, include_seed=False):
    """Return world-space deltas generated by this pattern feature.

    A follow-on pattern treats every occurrence of its parent pattern as the
    seed set, and returns only the newly generated occurrences by default.
    """
    return _pattern_transforms(pattern, include_seed, set())


def _pattern_group(doc):
    group = doc.getObject("ConnectedPartPatterns")
    if group is None:
        group = doc.addObject("App::DocumentObjectGroup", "ConnectedPartPatterns")
        group.Label = translate("SheetMetal", "Connected Part Patterns")
    return group


def _pattern_source_part(pattern):
    return pattern.Document.getObject(pattern.SourcePartName)


def _pattern_instance_link(pattern):
    return pattern.Document.getObject(pattern.InstanceLinkName)


def _pattern_connection_names(pattern):
    names = list(getattr(pattern, "ConnectionNames", []))
    if names:
        return names
    return [
        connection.Name
        for connection in list(getattr(pattern, "Connections", []))
        if connection is not None
    ]


def pattern_connections(pattern):
    """Resolve the connections repeated by a pattern without object back-links."""
    return [
        connection
        for connection in (
            pattern.Document.getObject(name)
            for name in _pattern_connection_names(pattern)
        )
        if connection is not None
        and getattr(connection, "SheetMetalType", "") == "BoltConnection"
    ]


def _pattern_depth(pattern, visited=None):
    visited = set() if visited is None else set(visited)
    if pattern.Name in visited:
        return 0
    visited.add(pattern.Name)
    parent = _parent_pattern(pattern)
    return 0 if parent is None else 1 + _pattern_depth(parent, visited)


def _leaf_patterns_for_connection(connection):
    """Return every terminal pattern branch containing one connection."""
    candidates = [
        obj
        for obj in connection.Document.Objects
        if getattr(obj, "SheetMetalType", "") == "ConnectedPartPattern"
        and connection.Name in _pattern_connection_names(obj)
    ]
    if not candidates:
        return []
    order = {obj.Name: index for index, obj in enumerate(connection.Document.Objects)}
    parents = {
        parent
        for parent in (_parent_pattern(candidate) for candidate in candidates)
        if parent in candidates
    }
    return sorted(
        [candidate for candidate in candidates if candidate not in parents],
        key=lambda item: (_pattern_depth(item), order[item.Name]),
    )


def _activate_pattern_connections(pattern):
    for connection in pattern_connections(pattern):
        providers = _leaf_patterns_for_connection(connection)
        set_connection_occurrence_providers(
            connection, providers, _pattern_source_part(pattern)
        )


def _pattern_for_cut(cut):
    reference = getattr(cut, "PatternReference", None)
    if isinstance(reference, tuple) and reference[0] is not None:
        return reference[0]
    name = str(getattr(cut, "PatternName", ""))
    if name:
        return cut.Document.getObject(name)
    return getattr(cut, "Pattern", None)


def _source_cuts_for_pattern_cut(cut):
    references = list(getattr(cut, "SourceCutReferences", []))
    if references:
        return [source_cut for source_cut, _sub_names in references]
    names = list(getattr(cut, "SourceCutNames", []))
    if names:
        return [
            source_cut
            for source_cut in (cut.Document.getObject(name) for name in names)
            if source_cut is not None
        ]
    return list(getattr(cut, "SourceCuts", []))


def _property_expression(obj, property_name):
    for name, expression in obj.ExpressionEngine:
        if name == property_name:
            return expression
    return None


class SMConnectedPartPattern:
    """Shared transformation definition and linked part occurrences."""

    def __init__(
        self,
        obj,
        source_part=None,
        connections=None,
        instance_link=None,
        center=None,
        parent_pattern=None,
    ):
        self.addVerifyProperties(obj)
        if source_part is not None:
            obj.SourcePartName = source_part.Name
            obj.SourcePartLabel = source_part.Label
        if connections is not None:
            obj.ConnectionNames = [connection.Name for connection in connections]
        if instance_link is not None:
            obj.InstanceLinkName = instance_link.Name
        if center is not None:
            obj.PolarCenter = center
        if parent_pattern is not None:
            obj.ParentPatternReference = (parent_pattern, [""])
        obj.Proxy = self

    def addVerifyProperties(self, obj):
        legacy_connections = list(getattr(obj, "Connections", []))
        if "SheetMetalType" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                "SheetMetalType",
                "Connected Pattern",
                translate("App::Property", "Sheet-metal object type"),
            ).SheetMetalType = "ConnectedPartPattern"
            obj.setEditorMode("SheetMetalType", 1)
        if "SourcePartName" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                "SourcePartName",
                "Connected Pattern",
                translate("App::Property", "Internal name of the repeated part"),
            )
            obj.setEditorMode("SourcePartName", 1)
        if "SourcePartLabel" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                "SourcePartLabel",
                "Connected Pattern",
                translate("App::Property", "Label of the repeated part"),
            )
            obj.setEditorMode("SourcePartLabel", 1)
        if "ConnectionNames" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyStringList",
                "ConnectionNames",
                "Connected Pattern",
                translate(
                    "App::Property",
                    "Internal names of bolted connections using this occurrence set",
                ),
            )
            obj.setEditorMode("ConnectionNames", 1)
        if legacy_connections:
            obj.ConnectionNames = [
                connection.Name
                for connection in legacy_connections
                if connection is not None
            ]
        if "Connections" in obj.PropertiesList:
            obj.Connections = []
            obj.removeProperty("Connections")
        if "InstanceLinkName" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                "InstanceLinkName",
                "Connected Pattern",
                translate("App::Property", "Generated App::Link array"),
            )
            obj.setEditorMode("InstanceLinkName", 1)
        if "ParentPatternReference" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyXLinkSub",
                "ParentPatternReference",
                "Connected Pattern",
                translate(
                    "App::Property",
                    "Optional earlier connected pattern used as this "
                    "feature's seed set",
                ),
            )
        _set_enumeration(
            obj,
            "PatternType",
            PATTERN_TYPES,
            "Polar",
            "Pattern Parameters",
            translate("App::Property", "Product pattern type"),
        )
        if "Occurrences" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyIntegerConstraint",
                "Occurrences",
                "Pattern Parameters",
                translate(
                    "App::Property",
                    "Total occurrences including the original source part",
                ),
            ).Occurrences = (4, 1, 10000, 1)
        if "LinearDirection" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyVector",
                "LinearDirection",
                "Linear Pattern",
                translate("App::Property", "World-space linear pattern direction"),
            ).LinearDirection = FreeCAD.Vector(1.0, 0.0, 0.0)
        if "LinearDirectionReference" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyXLinkSub",
                "LinearDirectionReference",
                "Linear Pattern",
                translate(
                    "App::Property",
                    "Coordinate-system axis or datum line supplying the live direction",
                ),
            )
        if "ReverseLinearDirection" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyBool",
                "ReverseLinearDirection",
                "Linear Pattern",
                translate(
                    "App::Property",
                    "Reverse the selected or manual linear pattern direction",
                ),
            ).ReverseLinearDirection = False
        if "Spacing" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyLength",
                "Spacing",
                "Linear Pattern",
                translate("App::Property", "Distance between consecutive occurrences"),
            ).Spacing = 100.0
        if "PolarCenter" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyVector",
                "PolarCenter",
                "Polar Pattern",
                translate("App::Property", "World-space center of rotation"),
            ).PolarCenter = FreeCAD.Vector()
        if "PolarAxis" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyVector",
                "PolarAxis",
                "Polar Pattern",
                translate("App::Property", "World-space axis of rotation"),
            ).PolarAxis = FreeCAD.Vector(0.0, 0.0, 1.0)
        if "AxisReference" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyXLinkSub",
                "AxisReference",
                "Polar Pattern",
                translate(
                    "App::Property",
                    "Datum line supplying the live world-space center and axis",
                ),
            )
        if "TotalAngle" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyAngle",
                "TotalAngle",
                "Polar Pattern",
                translate("App::Property", "Angular span of the pattern"),
            ).TotalAngle = 360.0
        if "Closed" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyBool",
                "Closed",
                "Polar Pattern",
                translate(
                    "App::Property",
                    "Distribute around a closed span without duplicating the endpoint",
                ),
            ).Closed = True
        if "AdditionalOccurrences" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyInteger",
                "AdditionalOccurrences",
                "Result",
                translate("App::Property", "Generated linked occurrences"),
            )
            obj.setEditorMode("AdditionalOccurrences", 1)
        if "ConnectionInstanceCount" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyInteger",
                "ConnectionInstanceCount",
                "Result",
                translate(
                    "App::Property",
                    "Total logical bolt connections including the seed occurrence",
                ),
            )
            obj.setEditorMode("ConnectionInstanceCount", 1)
        if "LastError" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                "LastError",
                "Result",
                translate("App::Property", "Pattern evaluation error"),
            )
            obj.setEditorMode("LastError", 1)

    def execute(self, fp):
        self.addVerifyProperties(fp)
        try:
            source_part = _pattern_source_part(fp)
            instance_link = _pattern_instance_link(fp)
            if source_part is None or instance_link is None:
                raise ValueError("The repeated part or its instance link is missing.")
            transforms = pattern_transforms(fp)
            additional_count = len(transforms)
            instance_link.LinkedObject = source_part
            instance_link.LinkTransform = True
            instance_link.ElementCount = additional_count
            instance_link.Visibility = additional_count > 0
            if additional_count:
                for occurrence, (element, transform) in enumerate(
                    zip(instance_link.ElementList, transforms), start=2
                ):
                    element.LinkPlacement = transform
                    element.Label = "{} {:03d}".format(fp.SourcePartLabel, occurrence)
            fp.AdditionalOccurrences = additional_count
            _activate_pattern_connections(fp)
            bolt_count = 0
            for connection in pattern_connections(fp):
                hole_only = set(connection.HoleOnlyLocatorKeys)
                bolt_count += sum(
                    record["key"] not in hole_only
                    for record in definition_locator_records(connection)
                )
            fp.ConnectionInstanceCount = (
                len(pattern_transforms(fp, include_seed=True)) * bolt_count
            )
            fp.LastError = ""
        except (ValueError, Part.OCCError) as error:
            fp.AdditionalOccurrences = 0
            fp.ConnectionInstanceCount = 0
            fp.LastError = str(error)
            FreeCAD.Console.PrintError(
                "Connected part pattern {}: {}\n".format(fp.Label, error)
            )

    def onDocumentRestored(self, fp):
        migrating = (
            "ConnectionNames" not in fp.PropertiesList
            or "Connections" in fp.PropertiesList
        )
        self.addVerifyProperties(fp)
        if migrating:
            fp.touch()
        if (
            SheetMetalTools.isGuiLoaded()
            and getattr(fp.ViewObject, "Proxy", None) is None
        ):
            SMConnectedPartPatternViewProvider(fp.ViewObject)

    def occurrenceTransforms(self, fp, include_seed=True):
        """Publish this feature's complete occurrence set to connections."""
        return pattern_transforms(fp, include_seed=include_seed)


class SMConnectedPatternCut(SMBoltConnectionCut):
    """Compatibility adapter for retired pre-occurrence pattern cuts.

    Saved documents may still contain these features in participant history.
    They now contribute no cutters; the ordinary bolted-connection cut earlier
    in the same history consumes the connected pattern occurrence provider.
    """

    def __init__(
        self,
        obj,
        pattern=None,
        previous_feature=None,
        source_cuts=None,
        participant=None,
    ):
        self.addVerifyProperties(obj)
        if pattern is not None:
            obj.PatternReference = (pattern, [""])
            obj.PatternName = pattern.Name
        if previous_feature is not None:
            obj.PreviousFeature = previous_feature
        if source_cuts is not None:
            obj.SourceCutReferences = [
                (source_cut, []) for source_cut in source_cuts
            ]
            obj.SourceCutNames = [source_cut.Name for source_cut in source_cuts]
        if participant is not None:
            obj.ParticipantName = participant.Name
            obj.ParticipantLabel = participant.Label
        obj.Proxy = self

    def addVerifyProperties(self, obj):
        if "SheetMetalType" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                "SheetMetalType",
                "Connected Pattern Cut",
                translate("App::Property", "Sheet-metal object type"),
            ).SheetMetalType = "ConnectedPatternCut"
            obj.setEditorMode("SheetMetalType", 1)
        if "PatternName" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                "PatternName",
                "Connected Pattern Cut",
                translate("App::Property", "Internal name of the connected pattern"),
            )
            old_pattern = getattr(obj, "Pattern", None)
            if old_pattern is not None:
                obj.PatternName = old_pattern.Name
            obj.setEditorMode("PatternName", 1)
        if "PatternReference" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyXLinkSub",
                "PatternReference",
                "Connected Pattern Cut",
                translate("App::Property", "Owning connected part pattern"),
            )
            old_pattern = _pattern_for_cut(obj)
            if old_pattern is not None:
                obj.PatternReference = (old_pattern, [""])
        if "PreviousFeature" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyLink",
                "PreviousFeature",
                "Connected Pattern Cut",
                translate("App::Property", "Previous feature in this part's history"),
            )
        if "SourceCutNames" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyStringList",
                "SourceCutNames",
                "Connected Pattern Cut",
                translate(
                    "App::Property",
                    "Internal names of seed cuts whose profiles are repeated",
                ),
            )
            old_source_cuts = list(getattr(obj, "SourceCuts", []))
            if old_source_cuts:
                obj.SourceCutNames = [source_cut.Name for source_cut in old_source_cuts]
            obj.setEditorMode("SourceCutNames", 1)
        if "SourceCutReferences" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyXLinkSubList",
                "SourceCutReferences",
                "Connected Pattern Cut",
                translate(
                    "App::Property",
                    "Seed connection cuts whose participant profiles are repeated",
                ),
            )
            old_source_cuts = _source_cuts_for_pattern_cut(obj)
            if old_source_cuts:
                obj.SourceCutReferences = [
                    (source_cut, []) for source_cut in old_source_cuts
                ]
        if "ParticipantName" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                "ParticipantName",
                "Participant",
                translate("App::Property", "Internal connected-part name"),
            )
            obj.setEditorMode("ParticipantName", 1)
        if "ParticipantLabel" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                "ParticipantLabel",
                "Participant",
                translate("App::Property", "Connected-part label"),
            )
            obj.setEditorMode("ParticipantLabel", 1)
        if "Refine" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyBool",
                "Refine",
                "Result",
                translate("App::Property", "Remove residual splitter edges"),
            ).Refine = True
        if "RemovedVolume" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyVolume",
                "RemovedVolume",
                "Result",
                translate("App::Property", "Volume removed by repeated connections"),
            )
            obj.setEditorMode("RemovedVolume", 1)
        if "SkippedLocationCount" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyInteger",
                "SkippedLocationCount",
                "Result",
                translate(
                    "App::Property",
                    "Patterned locations rejected before an invalid cut was attempted",
                ),
            )
            obj.setEditorMode("SkippedLocationCount", 1)
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
                "App::PropertyString",
                "LastError",
                "Result",
                translate("App::Property", "Pattern-cut evaluation error"),
            )
            obj.setEditorMode("LastError", 1)
        SMBoltConnectionCut.addVerifyProperties(self, obj)
        source_cuts = _source_cuts_for_pattern_cut(obj)
        if source_cuts:
            connection = connection_for_cut(source_cuts[0])
            if connection is not None:
                obj.ConnectionName = connection.Name
        obj.CutMode = "No Cut"
        obj.ValidationState = "Retired; connection occurrence provider is active"

    def collect_cutter_batches(self, fp, base_shape):
        return SMBoltConnectionCut.collect_cutter_batches(self, fp, base_shape)

    def execute(self, fp):
        self.addVerifyProperties(fp)
        execute_aggregate_connection_cut(fp)

    def onDocumentRestored(self, fp):
        migrating = "AggregateContributorCount" not in fp.PropertiesList
        self.addVerifyProperties(fp)
        if migrating:
            fp.touch()
        if (
            SheetMetalTools.isGuiLoaded()
            and getattr(fp.ViewObject, "Proxy", None) is None
        ):
            SMConnectedPatternCutViewProvider(fp.ViewObject)


def _default_pattern_center(doc, source_part, connections):
    for connection in connections:
        for name in connection.ParticipantNames:
            if name == source_part.Name:
                continue
            participant = doc.getObject(name)
            if participant is not None and hasattr(participant, "getGlobalPlacement"):
                return participant.getGlobalPlacement().Base
    return FreeCAD.Vector()


def create_connected_part_pattern(
    doc,
    source_part,
    connections,
    pattern_type="Polar",
    axis_reference=None,
    direction_reference=None,
    parent_pattern=None,
):
    """Create linked part occurrences and repeated cuts in fixed participants."""
    if parent_pattern is not None:
        if (
            getattr(parent_pattern, "SheetMetalType", "")
            != "ConnectedPartPattern"
        ):
            raise ValueError("The parent feature is not a Connected Part Pattern.")
        inherited_source = _pattern_source_part(parent_pattern)
        if inherited_source is None or inherited_source is not source_part:
            raise ValueError(
                "The follow-on pattern must use its parent pattern's source part."
            )
    if not (
        source_part is not None
        and source_part.TypeId == "App::Part"
        and hasattr(source_part, "SheetMetalType")
        and source_part.SheetMetalType == "Part"
    ):
        raise ValueError("Select one Sheet Metal Part as the repeated source part.")

    unique_connections = []
    for connection in connections:
        if connection not in unique_connections:
            unique_connections.append(connection)
    if not unique_connections:
        raise ValueError("Select at least one bolted connection to pattern.")

    has_fixed_participant = False
    for connection in unique_connections:
        if getattr(connection, "SheetMetalType", "") != "BoltConnection":
            raise ValueError("Every selected connection must be a Bolted Connection.")
        if source_part.Name not in connection.ParticipantNames:
            raise ValueError(
                "{} does not participate in {}.".format(
                    source_part.Label, connection.Label
                )
            )
        source_part_cut_found = False
        for cut in connection_cuts(connection):
            if cut.ParticipantName == source_part.Name:
                source_part_cut_found = True
                continue
            participant = doc.getObject(cut.ParticipantName)
            if participant is None:
                raise ValueError("A connected participant is missing.")
            has_fixed_participant = True
        if not source_part_cut_found:
            raise ValueError(
                "{} has no seed cut in {}.".format(
                    source_part.Label, connection.Label
                )
            )
    if not has_fixed_participant:
        raise ValueError("The selected connections have no fixed participants to cut.")

    instance_link = doc.addObject("App::Link", "ConnectedPartInstances")
    instance_link.Label = source_part.Label + " " + translate(
        "SheetMetal", "Pattern Instances"
    )
    instance_link.LinkedObject = source_part
    instance_link.LinkTransform = True

    pattern = doc.addObject("App::FeaturePython", "ConnectedPartPattern")
    pattern.Label = translate("SheetMetal", "Connected Part Pattern")
    center = _default_pattern_center(doc, source_part, unique_connections)
    SMConnectedPartPattern(
        pattern,
        source_part,
        unique_connections,
        instance_link,
        center,
        parent_pattern,
    )
    pattern.PatternType = pattern_type
    if axis_reference is not None:
        if not _is_direction_reference(axis_reference):
            raise ValueError(
                "Select a coordinate-system axis or datum line as the polar axis."
            )
        _set_axis_reference(pattern, axis_reference)
    if direction_reference is not None:
        if not _is_direction_reference(direction_reference):
            raise ValueError(
                "Select a coordinate-system axis or datum line as the direction."
            )
        pattern.LinearDirectionReference = (direction_reference, [""])
    _pattern_group(doc).addObject(pattern)
    _activate_pattern_connections(pattern)
    doc.recompute()
    return pattern, instance_link, []


def pattern_cuts(pattern):
    cuts = []
    for name in list(getattr(pattern, "PatternCutNames", [])):
        cut = pattern.Document.getObject(name)
        if cut is not None and _pattern_for_cut(cut) is pattern:
            cuts.append(cut)
    return cuts


def migrate_connected_part_patterns(doc, remove_legacy_cuts=True):
    """Upgrade saved connected patterns to unified occurrence-driven cuts.

    When requested, obsolete ConnectedPatternCut history nodes are bypassed and
    removed after all downstream feature and unfold links have been rewired.
    """
    patterns = [
        obj
        for obj in doc.Objects
        if getattr(obj, "SheetMetalType", "") == "ConnectedPartPattern"
    ]
    for pattern in patterns:
        SMConnectedPartPattern.addVerifyProperties(pattern.Proxy, pattern)
        pattern.touch()
    doc.recompute()
    if not remove_legacy_cuts:
        return 0

    legacy = [
        obj
        for obj in doc.Objects
        if getattr(obj, "SheetMetalType", "") == "ConnectedPatternCut"
    ]
    if not legacy:
        for pattern in patterns:
            if "PatternCutNames" in pattern.PropertiesList:
                pattern.removeProperty("PatternCutNames")
        return 0
    legacy_set = set(legacy)

    def replacement(feature):
        previous = getattr(feature, "PreviousFeature", None)
        visited = set()
        while previous in legacy_set:
            if previous in visited:
                raise ValueError("Legacy pattern-cut history contains a cycle.")
            visited.add(previous)
            previous = getattr(previous, "PreviousFeature", None)
        if previous is None:
            raise ValueError(
                "{} has no non-pattern predecessor.".format(feature.Label)
            )
        return previous

    for old in reversed(legacy):
        prior = replacement(old)
        for dependent in list(old.InList):
            if dependent in legacy_set or dependent.TypeId == "PartDesign::Body":
                continue
            for property_name in dependent.PropertiesList:
                if property_name == "Group":
                    continue
                property_type = dependent.getTypeIdOfProperty(property_name)
                try:
                    value = getattr(dependent, property_name)
                    if value is old and property_type in (
                        "App::PropertyLink",
                        "App::PropertyXLink",
                    ):
                        setattr(dependent, property_name, prior)
                    elif (
                        isinstance(value, tuple)
                        and value
                        and value[0] is old
                        and property_type
                        in ("App::PropertyLinkSub", "App::PropertyXLinkSub")
                    ):
                        setattr(dependent, property_name, (prior, value[1]))
                except (AttributeError, TypeError, RuntimeError) as error:
                    raise ValueError(
                        "Cannot rewire {}.{}: {}".format(
                            dependent.Label, property_name, error
                        )
                    )
        container = old.getParentGeoFeatureGroup()
        if container is not None and getattr(container, "Tip", None) is old:
            container.Tip = prior
        for part in doc.Objects:
            if str(getattr(part, "Tip", "")) == old.Name:
                part.Tip = prior.Name

    for pattern in patterns:
        if "PatternCutNames" in pattern.PropertiesList:
            pattern.PatternCutNames = []

    for old in reversed(legacy):
        prior = replacement(old)
        for dependent in list(old.InList):
            if dependent not in legacy_set:
                continue
            if getattr(dependent, "PreviousFeature", None) is old:
                dependent.PreviousFeature = prior
            if getattr(dependent, "BaseFeature", None) is old:
                dependent.BaseFeature = prior
        doc.removeObject(old.Name)

    for pattern in patterns:
        if "PatternCutNames" in pattern.PropertiesList:
            pattern.removeProperty("PatternCutNames")
    for connection in [
        obj
        for obj in doc.Objects
        if getattr(obj, "SheetMetalType", "") == "BoltConnection"
    ]:
        for cut in connection_cuts(connection):
            cut.touch()
    doc.recompute()

    if SheetMetalTools.isGuiLoaded():
        for obj in doc.Objects:
            if not (
                obj.TypeId == "App::Part"
                and getattr(obj, "SheetMetalType", "") == "Part"
            ):
                continue
            tip = _target_tip(obj)
            if getattr(obj, "ViewObject", None) is not None:
                obj.ViewObject.Visibility = True
            container = (
                tip.getParentGeoFeatureGroup() if tip is not None else None
            )
            if (
                container is not None
                and getattr(container, "ViewObject", None) is not None
            ):
                container.ViewObject.Visibility = True
            if tip is not None and getattr(tip, "ViewObject", None) is not None:
                tip.ViewObject.Visibility = True
    return len(legacy)


def _patterns_using_connection(connection):
    """Return dependent patterns in parent-first, document creation order."""
    candidates = [
        obj
        for obj in connection.Document.Objects
        if getattr(obj, "SheetMetalType", "") == "ConnectedPartPattern"
        and connection.Name in _pattern_connection_names(obj)
    ]
    document_order = {
        obj.Name: index for index, obj in enumerate(connection.Document.Objects)
    }

    return sorted(
        candidates,
        key=lambda pattern: (_pattern_depth(pattern), document_order[pattern.Name]),
    )


def sync_connection_participant_patterns(connection, participant_cuts):
    """Upgrade new participant cuts to the connection's active occurrence set."""
    patterns = _leaf_patterns_for_connection(connection)
    if patterns:
        set_connection_occurrence_providers(
            connection, patterns, _pattern_source_part(patterns[0])
        )
    for cut in participant_cuts:
        cut.touch()
    connection.Document.recompute()
    return []


def _sync_pattern_cut_visibility(cut):
    """Show only the active patterned cut in a participant's history."""
    if not SheetMetalTools.isGuiLoaded():
        return
    participant = cut.Document.getObject(cut.ParticipantName)
    if participant is None or getattr(cut, "ViewObject", None) is None:
        return
    active_tip = _target_tip(participant)
    cut.ViewObject.Visibility = active_tip is cut


if SheetMetalTools.isGuiLoaded():
    Gui = FreeCAD.Gui
    from PySide import QtCore, QtGui

    class SMConnectedPartPatternViewProvider(SheetMetalTools.SMViewProvider):
        def getIcon(self):
            return os.path.join(icons_path, "SheetMetal_ConnectedPartPattern.svg")

        def getTaskPanel(self, obj):
            return SMConnectedPartPatternTaskPanel(obj)

        def claimChildren(self):
            link = _pattern_instance_link(self.Object)
            return [link] if link is not None else []


    class SMConnectedPatternCutViewProvider(SheetMetalTools.SMViewProvider):
        def getIcon(self):
            return os.path.join(icons_path, "SheetMetal_ConnectedPartPattern.svg")

        def onDocumentRestored(self, view_object):
            QtCore.QTimer.singleShot(
                0,
                lambda obj=view_object.Object: _sync_pattern_cut_visibility(obj),
            )


    class SMConnectedPartPatternTaskPanel:
        def __init__(self, obj):
            self.obj = obj
            obj.Proxy.addVerifyProperties(obj)
            self.form = QtGui.QWidget()
            self.form.setWindowTitle(translate("SheetMetal", "Connected part pattern"))
            layout = QtGui.QVBoxLayout(self.form)

            summary = QtGui.QLabel(
                translate("SheetMetal", "Repeat %1 with %2 bolted connection(s)")
                .replace("%1", obj.SourcePartLabel)
                .replace("%2", str(len(pattern_connections(obj))))
            )
            layout.addWidget(summary)

            seed_set = QtGui.QFormLayout()
            seed_set_widget = QtGui.QWidget()
            seed_set_layout = QtGui.QHBoxLayout(seed_set_widget)
            seed_set_layout.setContentsMargins(0, 0, 0, 0)
            self.seed_set_label = QtGui.QLabel()
            self.seed_set_button = QtGui.QPushButton(
                translate("SheetMetal", "Use selected pattern")
            )
            self.seed_set_clear = QtGui.QPushButton(
                translate("SheetMetal", "Use source part only")
            )
            seed_set_layout.addWidget(self.seed_set_label, 1)
            seed_set_layout.addWidget(self.seed_set_button)
            seed_set_layout.addWidget(self.seed_set_clear)
            seed_set.addRow(
                translate("SheetMetal", "Seed occurrence set"), seed_set_widget
            )
            layout.addLayout(seed_set)

            common = QtGui.QFormLayout()
            self.pattern_type = QtGui.QComboBox()
            self.pattern_type.addItems(PATTERN_TYPES)
            self.pattern_type.setCurrentText(str(obj.PatternType))
            self.occurrences = QtGui.QSpinBox()
            self.occurrences.setRange(1, 10000)
            self.occurrences.setValue(int(obj.Occurrences))
            expression = _property_expression(obj, "Occurrences")
            if expression:
                self.occurrences.setEnabled(False)
                self.occurrences.setToolTip(
                    translate("SheetMetal", "Driven by expression: ") + str(expression)
                )
            common.addRow(translate("SheetMetal", "Pattern type"), self.pattern_type)
            common.addRow(
                translate("SheetMetal", "Total occurrences"), self.occurrences
            )
            layout.addLayout(common)

            self.polar_group = QtGui.QGroupBox(translate("SheetMetal", "Polar pattern"))
            polar = QtGui.QFormLayout(self.polar_group)
            axis_reference_widget = QtGui.QWidget()
            axis_reference_layout = QtGui.QHBoxLayout(axis_reference_widget)
            axis_reference_layout.setContentsMargins(0, 0, 0, 0)
            self.axis_reference_label = QtGui.QLabel()
            self.axis_reference_button = QtGui.QPushButton(
                translate("SheetMetal", "Use selected line")
            )
            self.axis_reference_clear = QtGui.QPushButton(
                translate("SheetMetal", "Clear")
            )
            axis_reference_layout.addWidget(self.axis_reference_label, 1)
            axis_reference_layout.addWidget(self.axis_reference_button)
            axis_reference_layout.addWidget(self.axis_reference_clear)
            self.center = self._vector_editor(obj.PolarCenter, -1e9, 1e9)
            self.axis = self._vector_editor(obj.PolarAxis, -1e6, 1e6)
            self.total_angle = self._spin(-360000.0, 360000.0, obj.TotalAngle.Value)
            self.total_angle.setSuffix(" deg")
            self.closed = QtGui.QCheckBox()
            self.closed.setChecked(obj.Closed)
            polar.addRow(
                translate("SheetMetal", "Axis reference"), axis_reference_widget
            )
            polar.addRow(translate("SheetMetal", "Center XYZ (mm)"), self.center[0])
            polar.addRow(translate("SheetMetal", "Axis XYZ"), self.axis[0])
            polar.addRow(translate("SheetMetal", "Angular span"), self.total_angle)
            polar.addRow(translate("SheetMetal", "Closed distribution"), self.closed)
            layout.addWidget(self.polar_group)

            self.linear_group = QtGui.QGroupBox(
                translate("SheetMetal", "Linear pattern")
            )
            linear = QtGui.QFormLayout(self.linear_group)
            direction_reference_widget = QtGui.QWidget()
            direction_reference_layout = QtGui.QHBoxLayout(
                direction_reference_widget
            )
            direction_reference_layout.setContentsMargins(0, 0, 0, 0)
            self.direction_reference_label = QtGui.QLabel()
            self.direction_reference_button = QtGui.QPushButton(
                translate("SheetMetal", "Use selected axis")
            )
            self.direction_reference_clear = QtGui.QPushButton(
                translate("SheetMetal", "Clear")
            )
            direction_reference_layout.addWidget(
                self.direction_reference_label, 1
            )
            direction_reference_layout.addWidget(self.direction_reference_button)
            direction_reference_layout.addWidget(self.direction_reference_clear)
            self.direction = self._vector_editor(obj.LinearDirection, -1e6, 1e6)
            self.reverse_direction = QtGui.QCheckBox()
            self.reverse_direction.setChecked(obj.ReverseLinearDirection)
            self.spacing = self._quantity_spin(
                "Spacing", 0.0, 1e9, obj.Spacing
            )
            linear.addRow(
                translate("SheetMetal", "Direction reference"),
                direction_reference_widget,
            )
            linear.addRow(
                translate("SheetMetal", "Reverse direction"),
                self.reverse_direction,
            )
            linear.addRow(
                translate("SheetMetal", "Direction XYZ"), self.direction[0]
            )
            linear.addRow(translate("SheetMetal", "Spacing"), self.spacing)
            layout.addWidget(self.linear_group)

            self.pattern_type.currentTextChanged.connect(self._set_pattern_type)
            self.axis_reference_button.clicked.connect(
                self._use_selected_axis_reference
            )
            self.axis_reference_clear.clicked.connect(self._clear_axis_reference)
            self.direction_reference_button.clicked.connect(
                self._use_selected_direction_reference
            )
            self.direction_reference_clear.clicked.connect(
                self._clear_direction_reference
            )
            self.seed_set_button.clicked.connect(self._use_selected_seed_pattern)
            self.seed_set_clear.clicked.connect(self._clear_seed_pattern)
            self.occurrences.valueChanged.connect(
                lambda value: self._set_property("Occurrences", value)
            )
            self._connect_vector(self.center[1], "PolarCenter")
            self._connect_vector(self.axis[1], "PolarAxis")
            self._connect_vector(self.direction[1], "LinearDirection")
            self.total_angle.valueChanged.connect(
                lambda value: self._set_property("TotalAngle", value)
            )
            self.closed.toggled.connect(
                lambda value: self._set_property("Closed", value)
            )
            self.reverse_direction.toggled.connect(
                self._set_reverse_direction
            )
            self.spacing.valueChanged.connect(
                lambda value: self._set_property("Spacing", value)
            )
            self._refresh_axis_reference()
            self._refresh_direction_reference()
            self._refresh_seed_pattern()
            self._update_groups()

        def _spin(self, minimum, maximum, value):
            spin = QtGui.QDoubleSpinBox()
            spin.setDecimals(4)
            spin.setRange(minimum, maximum)
            spin.setValue(value)
            return spin

        def _quantity_spin(self, property_name, minimum, maximum, value):
            try:
                spin = Gui.UiLoader().createWidget("Gui::QuantitySpinBox")
            except (AttributeError, RuntimeError):
                spin = self._spin(minimum, maximum, value.Value)
                spin.setSuffix(" mm")
            spin.setProperty("minimum", minimum)
            spin.setProperty("maximum", maximum)
            spin.setProperty("value", value)
            spin.setProperty("keyboardTracking", False)
            Gui.ExpressionBinding(spin).bind(self.obj, property_name)
            return spin

        def _vector_editor(self, value, minimum, maximum):
            widget = QtGui.QWidget()
            layout = QtGui.QHBoxLayout(widget)
            layout.setContentsMargins(0, 0, 0, 0)
            spins = []
            for component in (value.x, value.y, value.z):
                spin = self._spin(minimum, maximum, component)
                layout.addWidget(spin)
                spins.append(spin)
            return widget, spins

        def _connect_vector(self, spins, property_name):
            for spin in spins:
                spin.valueChanged.connect(
                    lambda _value, controls=spins, name=property_name: (
                        self._set_property(
                            name,
                            FreeCAD.Vector(*(control.value() for control in controls)),
                        )
                    )
                )

        def _set_property(self, name, value):
            setattr(self.obj, name, value)
            self.obj.Document.recompute()

        def _set_vector_controls(self, spins, value):
            for spin, component in zip(spins, (value.x, value.y, value.z)):
                blocked = spin.blockSignals(True)
                spin.setValue(component)
                spin.blockSignals(blocked)

        def _refresh_axis_reference(self):
            reference = _axis_reference_object(self.obj)
            has_reference = reference is not None
            self.axis_reference_label.setText(
                reference.Label
                if has_reference
                else translate("SheetMetal", "Manual XYZ")
            )
            self.axis_reference_clear.setEnabled(has_reference)
            self.center[0].setEnabled(not has_reference)
            self.axis[0].setEnabled(not has_reference)
            if has_reference:
                try:
                    center, axis = polar_axis_definition(self.obj)
                    self._set_vector_controls(self.center[1], center)
                    self._set_vector_controls(self.axis[1], axis)
                except ValueError as error:
                    SheetMetalTools.smWarnDialog(str(error))

        def _use_selected_axis_reference(self):
            references = [
                selected
                for selected in Gui.Selection.getSelection()
                if _is_direction_reference(selected)
            ]
            if len(references) != 1:
                SheetMetalTools.smWarnDialog(
                    translate(
                        "SheetMetal",
                        "Select exactly one coordinate-system axis or datum line.",
                    )
                )
                return
            _set_axis_reference(self.obj, references[0])
            self.obj.Document.recompute()
            self._refresh_axis_reference()

        def _clear_axis_reference(self):
            try:
                center, axis = polar_axis_definition(self.obj)
            except ValueError:
                center = FreeCAD.Vector(self.obj.PolarCenter)
                axis = FreeCAD.Vector(self.obj.PolarAxis)
            _set_axis_reference(self.obj, None)
            self.obj.PolarCenter = center
            self.obj.PolarAxis = axis
            self.obj.Document.recompute()
            self._set_vector_controls(self.center[1], center)
            self._set_vector_controls(self.axis[1], axis)
            self._refresh_axis_reference()

        def _refresh_direction_reference(self):
            reference = getattr(self.obj, "LinearDirectionReference", None)
            if isinstance(reference, tuple):
                reference = reference[0]
            has_reference = reference is not None
            self.direction_reference_label.setText(
                reference.Label
                if has_reference
                else translate("SheetMetal", "Manual XYZ")
            )
            self.direction_reference_clear.setEnabled(has_reference)
            self.direction[0].setEnabled(not has_reference)
            if has_reference:
                try:
                    direction = linear_direction_definition(self.obj)
                    self._set_vector_controls(self.direction[1], direction)
                except ValueError as error:
                    SheetMetalTools.smWarnDialog(str(error))

        def _use_selected_direction_reference(self):
            references = [
                selected
                for selected in Gui.Selection.getSelection()
                if _is_direction_reference(selected)
            ]
            if len(references) != 1:
                SheetMetalTools.smWarnDialog(
                    translate(
                        "SheetMetal",
                        "Select exactly one coordinate-system axis or datum line.",
                    )
                )
                return
            self.obj.LinearDirectionReference = (references[0], [""])
            self.obj.Document.recompute()
            self._refresh_direction_reference()

        def _clear_direction_reference(self):
            try:
                direction = linear_direction_definition(self.obj)
            except ValueError:
                direction = FreeCAD.Vector(self.obj.LinearDirection)
            self.obj.LinearDirectionReference = None
            self.obj.LinearDirection = direction
            self.obj.ReverseLinearDirection = False
            self.obj.Document.recompute()
            self._set_vector_controls(self.direction[1], direction)
            self.reverse_direction.setChecked(False)
            self._refresh_direction_reference()

        def _refresh_seed_pattern(self):
            parent = _parent_pattern(self.obj)
            self.seed_set_label.setText(
                parent.Label if parent is not None else self.obj.SourcePartLabel
            )
            self.seed_set_clear.setEnabled(parent is not None)

        def _use_selected_seed_pattern(self):
            patterns = [
                selected
                for selected in Gui.Selection.getSelection()
                if getattr(selected, "SheetMetalType", "")
                == "ConnectedPartPattern"
            ]
            if len(patterns) != 1:
                SheetMetalTools.smWarnDialog(
                    translate(
                        "SheetMetal",
                        "Select exactly one earlier connected part pattern.",
                    )
                )
                return
            try:
                set_parent_pattern(self.obj, patterns[0])
                self.obj.Document.recompute()
                self._refresh_seed_pattern()
            except ValueError as error:
                SheetMetalTools.smWarnDialog(str(error))

        def _clear_seed_pattern(self):
            set_parent_pattern(self.obj, None)
            self.obj.Document.recompute()
            self._refresh_seed_pattern()

        def _set_reverse_direction(self, value):
            self.obj.ReverseLinearDirection = value
            self.obj.Document.recompute()
            self._refresh_direction_reference()

        def _set_pattern_type(self, value):
            self._set_property("PatternType", value)
            self._update_groups()

        def _update_groups(self):
            polar = str(self.obj.PatternType) == "Polar"
            self.polar_group.setEnabled(polar)
            self.linear_group.setEnabled(not polar)

        def isAllowedAlterSelection(self):
            return True

        def isAllowedAlterView(self):
            return True

        def accept(self):
            return SheetMetalTools.taskAccept(self)

        def reject(self):
            SheetMetalTools.taskReject(self)


    def _selection_data():
        parts = []
        connections = []
        references = []
        patterns = []
        for obj in Gui.Selection.getSelection():
            sheet_metal_type = getattr(obj, "SheetMetalType", "")
            if sheet_metal_type == "ConnectedPartPattern":
                if obj not in patterns:
                    patterns.append(obj)
                continue
            if sheet_metal_type == "BoltConnection":
                if obj not in connections:
                    connections.append(obj)
                continue
            if sheet_metal_type == "BoltConnectionCut":
                connection = connection_for_cut(obj)
                if connection is not None and connection not in connections:
                    connections.append(connection)
                continue
            if _is_direction_reference(obj) and obj not in references:
                references.append(obj)
            part = _find_sheet_metal_part(obj)
            if part is not None and part not in parts:
                parts.append(part)
        return parts, connections, references, patterns


    class AddConnectedPartPatternCommandClass:
        def GetResources(self):
            return {
                "Pixmap": os.path.join(
                    icons_path, "SheetMetal_ConnectedPartPattern.svg"
                ),
                "MenuText": translate("SheetMetal", "Connected Part Pattern"),
                "ToolTip": translate(
                    "SheetMetal",
                    "Pattern one Sheet Metal Part and selected bolted connections, "
                    "or apply a follow-on transform to a selected connected pattern.",
                ),
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            parts, connections, references, patterns = _selection_data()
            doc.openTransaction("ConnectedPartPattern")
            try:
                parent_pattern = patterns[0] if patterns else None
                if parent_pattern is not None:
                    source_part = _pattern_source_part(parent_pattern)
                    selected_connections = pattern_connections(parent_pattern)
                    pattern_type = "Linear"
                else:
                    source_part = parts[0]
                    selected_connections = connections
                    pattern_type = "Polar"
                pattern, instance_link, cuts = create_connected_part_pattern(
                    doc,
                    source_part,
                    selected_connections,
                    pattern_type=pattern_type,
                    axis_reference=(
                        references[0]
                        if references and pattern_type == "Polar"
                        else None
                    ),
                    direction_reference=(
                        references[0]
                        if references and pattern_type == "Linear"
                        else None
                    ),
                    parent_pattern=parent_pattern,
                )
                SMConnectedPartPatternViewProvider(pattern.ViewObject)
                for cut in cuts:
                    SMConnectedPatternCutViewProvider(cut.ViewObject)
                Gui.Selection.clearSelection()
                Gui.Selection.addSelection(pattern)
                dialog = SMConnectedPartPatternTaskPanel(pattern)
                SheetMetalTools.updateTaskTitleIcon(dialog)
                Gui.Control.showDialog(dialog)
            except (IndexError, ValueError, Part.OCCError) as error:
                doc.abortTransaction()
                SheetMetalTools.smWarnDialog(str(error))

        def IsActive(self):
            if FreeCAD.ActiveDocument is None:
                return False
            parts, connections, references, patterns = _selection_data()
            direct = len(parts) == 1 and bool(connections) and not patterns
            follow_on = len(patterns) == 1 and not parts and not connections
            return (direct or follow_on) and len(references) <= 1


    Gui.addCommand(
        "SheetMetal_ConnectedPartPattern", AddConnectedPartPatternCommandClass()
    )
