########################################################################
#
#  SheetMetalNaming.py
#
#  Copyright 2026 SheetMetal Workbench contributors
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
########################################################################

"""Computed drawing identity for sheet-metal parts in organizational groups."""

import FreeCAD


translate = FreeCAD.Qt.translate
DRAWING_NAME_SEPARATOR = " – "


def _is_sheet_metal_part(obj):
    return (
        obj is not None
        and hasattr(obj, "SheetMetalType")
        and obj.SheetMetalType == "Part"
    )


def _direct_document_groups(obj):
    groups = []
    for candidate in obj.InList:
        if candidate.TypeId not in (
            "App::DocumentObjectGroup",
            "App::DocumentObjectGroupPython",
        ):
            continue
        if hasattr(candidate, "Group") and obj in candidate.Group:
            groups.append(candidate)
    return sorted(groups, key=lambda item: item.Name)


def drawingGroupPath(part):
    """Return the outer-to-inner label path of organizational parent groups."""
    labels = []
    current = part
    visited = set()
    while True:
        parents = [
            group for group in _direct_document_groups(current)
            if group.Name not in visited
        ]
        if not parents:
            break
        current = parents[0]
        visited.add(current.Name)
        label = str(current.Label).strip()
        if label:
            labels.append(label)
    labels.reverse()
    return labels


def computedDrawingName(part):
    """Combine a part label with its organizational group path."""
    part_label = str(part.Label).strip() or part.Name
    if not getattr(part, "UseGroupInDrawingName", True):
        return part_label
    group_labels = drawingGroupPath(part)
    if not group_labels:
        return part_label

    prefix = DRAWING_NAME_SEPARATOR.join(group_labels)
    folded_label = part_label.casefold()
    folded_group = group_labels[-1].casefold()
    # Preserve existing documents whose part labels already contain their
    # immediate group name; users can shorten the label at their convenience.
    if folded_label == folded_group or folded_label.startswith(folded_group + " "):
        return part_label
    return prefix + DRAWING_NAME_SEPARATOR + part_label


def updateDrawingName(part):
    if not _is_sheet_metal_part(part):
        return
    value = computedDrawingName(part)
    if part.DrawingName != value:
        part.DrawingName = value


def addDrawingIdentityProperties(part):
    """Expose a drawing-safe computed name on one Sheet Metal Part."""
    if "UseGroupInDrawingName" not in part.PropertiesList:
        part.addProperty(
            "App::PropertyBool",
            "UseGroupInDrawingName",
            "Drawing",
            translate(
                "App::Property",
                "Prefix the part label with its organizational group path",
            ),
        ).UseGroupInDrawingName = True
    if "DrawingName" not in part.PropertiesList:
        part.addProperty(
            "App::PropertyString",
            "DrawingName",
            "Drawing",
            translate(
                "App::Property",
                "Computed name for drawings, flat patterns, and part lists",
            ),
        )
    part.setEditorMode("DrawingName", 1)
    updateDrawingName(part)


def upgradeDocumentDrawingNames(doc):
    for obj in doc.Objects:
        if _is_sheet_metal_part(obj):
            addDrawingIdentityProperties(obj)


class _DrawingNameObserver:
    def __init__(self):
        self.updating = False

    def _update_document(self, doc):
        if doc is None or self.updating:
            return
        try:
            self.updating = True
            upgradeDocumentDrawingNames(doc)
        finally:
            self.updating = False

    def slotChangedObject(self, obj, prop):
        if prop not in ("Label", "Group", "UseGroupInDrawingName"):
            return
        self._update_document(obj.Document)

    def slotActivateDocument(self, doc):
        self._update_document(doc)


if "_drawing_name_observer" not in globals():
    _drawing_name_observer = _DrawingNameObserver()
    FreeCAD.addDocumentObserver(_drawing_name_observer)
    for _open_document in FreeCAD.listDocuments().values():
        upgradeDocumentDrawingNames(_open_document)

