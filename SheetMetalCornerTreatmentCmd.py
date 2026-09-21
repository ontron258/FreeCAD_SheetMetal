########################################################################
#
#  SheetMetalCornerTreatmentCmd.py
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#  GNU Lesser General Public License for more details.
#
########################################################################

"""Round or chamfer outline corners through the full sheet thickness."""

import math
import os

import FreeCAD
import Part
import SheetMetalTools

translate = FreeCAD.Qt.translate
smElementMapVersion = "sm1."
_TOL = 1.0e-6


def _sheet_thickness(shape):
    """Measure inward from an interior point of the largest planar sheet face.

    An interior sample avoids a bend or relief at a boundary vertex. The
    first material segment is used so another flange along the ray is ignored.
    No global axis or user-entered second thickness is needed.
    """
    planes = [face for face in shape.Faces if isinstance(face.Surface, Part.Plane)]
    if not planes:
        raise ValueError(translate("SheetMetal", "Select a sheet with planar faces."))
    face = max(planes, key=lambda item: item.Area)
    point = face.CenterOfMass
    if not face.isInside(point, _TOL, False):
        vertices, triangles = face.tessellate(0.1)
        if not triangles:
            raise ValueError(translate("SheetMetal", "Cannot measure the sheet thickness."))
        a, b, c = triangles[0]
        point = (vertices[a] + vertices[b] + vertices[c]) / 3.0
    ray = Part.makeLine(point, point - face.normalAt(0, 0) * shape.BoundBox.DiagonalLength)
    for edge in shape.common(ray).Edges:
        if any(vertex.Point.distanceToPoint(point) < _TOL for vertex in edge.Vertexes):
            if edge.Length > _TOL:
                return edge.Length
    raise ValueError(translate("SheetMetal", "Cannot measure the sheet thickness."))


def _is_corner_edge(shape, edge, thickness):
    if (not isinstance(edge.Curve, Part.Line) or len(edge.Vertexes) != 2
            or not math.isclose(edge.Length, thickness, rel_tol=_TOL, abs_tol=_TOL)):
        return False
    axis = (edge.Vertexes[1].Point - edge.Vertexes[0].Point).normalize()
    sides = shape.ancestorsOfType(edge, Part.Face)
    if len(sides) != 2 or any(not isinstance(face.Surface, Part.Plane) for face in sides):
        return False
    normals = [face.normalAt(0, 0) for face in sides]
    if (any(abs(normal.dot(axis)) > _TOL for normal in normals)
            or normals[0].cross(normals[1]).Length < _TOL):
        return False
    # The endpoints must lie on the two sheet skins, perpendicular to the
    # thickness edge. This excludes bend edges and already rounded corners.
    return all(any(
        isinstance(face.Surface, Part.Plane)
        and face.normalAt(0, 0).cross(axis).Length < _TOL
        for face in shape.ancestorsOfType(vertex, Part.Face)
    ) for vertex in edge.Vertexes)


def cornerEdges(shape, names):
    """Resolve vertices/edges to unique, straight thickness edges."""
    if shape.isNull() or len(shape.Solids) != 1 or not shape.isValid():
        raise ValueError(translate("SheetMetal", "Select one valid sheet-metal solid."))
    if not names:
        raise ValueError(translate("SheetMetal", "Select at least one corner."))
    thickness = _sheet_thickness(shape)
    result = []
    for name in names:
        if any(part.startswith("?") for part in name.split(".")):
            raise CornerTreatmentError(translate(
                "SheetMetal", "Corner reference no longer resolves: %1. Reselect this corner on the source sheet."
            ).replace("%1", name), [name])
        try:
            element = shape.getElement(SheetMetalTools.getElementFromTNP(name))
        except Exception as error:
            raise ValueError(translate("SheetMetal", "A selected corner no longer exists.")) from error
        if isinstance(element, Part.Vertex):
            candidates = shape.ancestorsOfType(element, Part.Edge)
        elif isinstance(element, Part.Edge):
            candidates = [element]
        else:
            candidates = []
        candidates = [edge for edge in candidates if _is_corner_edge(shape, edge, thickness)]
        if len(candidates) != 1:
            raise ValueError(translate(
                "SheetMetal",
                "Select sharp outline corner vertices or straight edges through the sheet thickness."
            ) + " (" + name + ")")
        edge = candidates[0]
        if not any(edge.isSame(previous) for previous in result):
            result.append(edge)
    return result


class CornerSelectionGate:
    """Filter picks against an immutable snapshot of the source sheet."""

    def __init__(self, base):
        self.base = base
        self.shape = base.Shape.copy()
        self.allowed = {}

    def allow(self, doc, obj, sub):
        if obj != self.base or doc != self.base.Document or not sub:
            return False
        if sub not in self.allowed:
            try:
                cornerEdges(self.shape, [sub])
                self.allowed[sub] = True
            except Exception:
                self.allowed[sub] = False
        return self.allowed[sub]


def cornerLengthUnit(doc):
    """Use the owning document's units, including inches for small US lengths."""
    schema = FreeCAD.Units.getSchema()
    if hasattr(doc, "UnitSystem"):
        schema = doc.getEnumerationsOfProperty("UnitSystem").index(doc.UnitSystem)
    # US customary otherwise normalizes a small corner radius to thou.
    if FreeCAD.Units.listSchemas()[schema].startswith("Imperial"):
        return "in"
    return FreeCAD.Units.schemaTranslate(FreeCAD.Units.Quantity("1 mm"), schema)[2]


class CornerTreatmentError(ValueError):
    """A failed operation with the source selections that need attention."""

    def __init__(self, message, corners):
        super().__init__(message)
        self.corners = list(corners)


def _apply_corners(shape, edges, treatment, size):
    result = (shape.makeFillet(size, edges) if treatment == "Round"
              else shape.makeChamfer(size, edges))
    if result.isNull() or not result.isValid() or len(result.Solids) != 1:
        raise ValueError("Invalid corner result")
    return result


def makeCornerTreatment(shape, names, treatment="Round", size=1.0):
    """Apply one radius or equal-distance chamfer to all selected corners.

    Kernel operations use the original edge set in one operation, preserving
    the sheet skins even for a radius much greater than the thickness.
    """
    if treatment not in ("Round", "Chamfer"):
        raise ValueError(translate("SheetMetal", "Choose Round or Chamfer."))
    if not math.isfinite(size) or size <= _TOL:
        raise ValueError(translate("SheetMetal", "Corner size must be greater than zero."))
    edges = cornerEdges(shape, names)
    try:
        result = _apply_corners(shape, edges, treatment, size)
    except Exception as error:
        # A single obstructed corner otherwise hides which of a large selection
        # failed. Diagnose only after failure, against the unchanged source.
        failed_edges = []
        for edge in edges:
            try:
                _apply_corners(shape, [edge], treatment, size)
            except Exception:
                failed_edges.append(edge)
        if failed_edges:
            failed_names = [name for name in names if any(
                edge.isSame(failed) for edge in cornerEdges(shape, [name])
                for failed in failed_edges)]
            message = translate("SheetMetal", "Cannot create corners at this size: %1.")
        else:
            # Individually valid corners can still collide with each other.
            failed_names = list(names)
            message = translate("SheetMetal", "These corners cannot be combined at this size: %1.")
        message = message.replace("%1", ", ".join(failed_names))
        message += " " + translate(
            "SheetMetal", "Reduce the size or change the selection. Check nearby edges, bends, and intersecting flanges.")
        raise CornerTreatmentError(message, failed_names) from error
    return result


def _owning_part(obj):
    parent = obj.getParentGeoFeatureGroup()
    while parent is not None:
        if getattr(parent, "SheetMetalType", None) == "Part":
            return parent
        parent = parent.getParentGeoFeatureGroup()
    return None


def adoptCornerFeature(obj, base):
    """Keep the finishing feature in the source's Body/Part and advance its Tip."""
    parent = base.getParentGeoFeatureGroup()
    if parent is not None and obj not in parent.Group:
        parent.addObject(obj)
    repairCornerMembership(obj)
    part = _owning_part(obj)
    if part is not None and "Tip" in part.PropertiesList:
        part.Tip = obj.Name


def repairCornerMembership(obj):
    """Repair the original command's repeated Body reference without deleting a feature."""
    parent = obj.getParentGeoFeatureGroup()
    if parent is None or not hasattr(parent, "Group") or parent.Group.count(obj) < 2:
        return
    members = []
    for member in parent.Group:
        if member != obj or obj not in members:
            members.append(member)
    parent.Group = members


class SMCornerTreatment:
    def __init__(self, obj, base, names):
        self.addVerifyProperties(obj)
        obj.addProperty("App::PropertyLinkSub", "baseObject", "Parameters",
                        translate("App::Property", "Base object and selected corners"))
        obj.baseObject = (base, names)
        obj.Proxy = self

    def addVerifyProperties(self, obj):
        SheetMetalTools.smAddEnumProperty(obj, "Treatment",
            translate("App::Property", "Corner treatment"), ["Round", "Chamfer"], "Round")
        SheetMetalTools.smAddLengthProperty(obj, "Radius",
            translate("App::Property", "Outline corner radius"), 1.0)
        SheetMetalTools.smAddLengthProperty(obj, "ChamferSize",
            translate("App::Property", "Equal setback along both sides of the corner"), 1.0)
        SheetMetalTools.smAddStringProperty(obj, "LastError",
            translate("App::Property", "Reason the corner feature could not be built"), "", "Status")
        obj.setEditorMode("LastError", 1)
        if "FailedCorners" not in obj.PropertiesList:
            obj.addProperty("App::PropertyStringList", "FailedCorners", "Status",
                            translate("App::Property", "Corners that failed at the requested size"))
        obj.setEditorMode("FailedCorners", 1)

    def onChanged(self, obj, prop):
        if prop == "Treatment":
            for name, mode in (("Radius", "Round"), ("ChamferSize", "Chamfer")):
                if name in obj.PropertiesList:
                    obj.setEditorMode(name, 0 if obj.Treatment == mode else 2)

    def onDocumentRestored(self, obj):
        self.addVerifyProperties(obj)
        self.onChanged(obj, "Treatment")
        repairCornerMembership(obj)

    def getElementMapVersion(self, _fp, ver, _prop, restored):
        return None if restored else smElementMapVersion + ver

    def execute(self, obj):
        self.addVerifyProperties(obj)
        self.onChanged(obj, "Treatment")
        try:
            base, names = obj.baseObject
            if base is None:
                raise ValueError(translate("SheetMetal", "Select a base sheet-metal feature."))
            size = obj.Radius.Value if obj.Treatment == "Round" else obj.ChamferSize.Value
            result = makeCornerTreatment(base.Shape, names, str(obj.Treatment), size)
        except Exception as error:
            obj.LastError = str(error)
            obj.FailedCorners = getattr(error, "corners", [])
            # Do not present the last successful shape as the requested result.
            obj.Shape = Part.Shape()
            raise
        obj.Shape = result
        obj.LastError = ""
        obj.FailedCorners = []


if SheetMetalTools.isGuiLoaded():
    from PySide import QtCore, QtGui

    Gui = FreeCAD.Gui
    _ICON = os.path.join(SheetMetalTools.icons_path, "SheetMetal_CornerTreatment.svg")

    class SMCornerTreatmentViewProvider(SheetMetalTools.SMViewProvider):
        def getIcon(self):
            return _ICON

        def getTaskPanel(self, obj):
            return SMCornerTreatmentTaskPanel(obj)

        def onDelete(self, view, _subelements):
            obj = view.Object
            part = _owning_part(obj)
            if part is not None and getattr(part, "Tip", "") == obj.Name:
                base = obj.baseObject[0]
                part.Tip = base.Name if base is not None else ""
            return True

    class SMCornerTreatmentTaskPanel:
        def __init__(self, obj):
            self.obj = obj
            self._gateActive = False
            self._documentObserverActive = False
            self._closed = False
            self.selectionError = ""
            self.invalidNames = []
            obj.Proxy.addVerifyProperties(obj)
            self.form = SheetMetalTools.taskLoadUI("CornerTreatmentPanel.ui")
            self.selParams = SheetMetalTools.taskConnectSelection(
                self.form.AddRemove, self.form.tree, obj, ["Vertex", "Edge"],
                self.form.pushClearSel)
            self.selParams.ConstrainToObject = obj.baseObject[0]
            self.selParams.verifySelection = self.verifySelection
            self.form.AddRemove.clicked.connect(self.selectionModeChanged)
            self.form.destroyed.connect(self.cleanup)
            self.form.ErrorMessage.setTextFormat(QtCore.Qt.PlainText)
            self.form.ErrorMessage.setStyleSheet(
                "QLabel { color: #9f1239; background: #fff1f2; "
                "border: 1px solid #fda4af; padding: 8px; }")
            SheetMetalTools.taskConnectEnum(obj, self.form.Treatment, "Treatment",
                                           self.parameterChanged)
            for name in ("Radius", "ChamferSize"):
                spin = getattr(self.form, name)
                spin.setProperty("autoNormalize", False)
                spin.setProperty("unit", cornerLengthUnit(obj.Document))
                SheetMetalTools.taskConnectSpin(obj, spin, name, self.parameterChanged)
            FreeCAD.addDocumentObserver(self)
            self._documentObserverActive = True
            self.updateMode()
            self.updateFeedback()

        def slotChangedDocument(self, doc, prop):
            if not self._closed and doc == self.obj.Document and prop == "UnitSystem":
                # FreeCAD also updates its quantity widgets while processing
                # the schema change. Apply the document unit after that update.
                QtCore.QTimer.singleShot(0, self.updateUnits)

        def updateUnits(self):
            if self._closed:
                return
            for name in ("Radius", "ChamferSize"):
                spin = getattr(self.form, name)
                blocked = spin.blockSignals(True)
                try:
                    spin.setProperty("unit", cornerLengthUnit(self.obj.Document))
                    spin.setProperty("value", getattr(self.obj, name))
                finally:
                    spin.blockSignals(blocked)

        def cleanup(self, *_args):
            self._closed = True
            self.cleanupSelection()
            if self._documentObserverActive:
                FreeCAD.removeDocumentObserver(self)
                self._documentObserverActive = False

        def cleanupSelection(self, *_args):
            if self._gateActive:
                Gui.Selection.removeSelectionGate()
                self._gateActive = False
            observer = getattr(SheetMetalTools.SelectionObserver, "observer", None)
            if observer is not None and observer.sp is self.selParams:
                SheetMetalTools.SelectionObserver._delete_observer()

        def selectionModeChanged(self, *_args):
            if not self.selParams.SelectState and not self._gateActive:
                self.gate = CornerSelectionGate(self.obj.baseObject[0])
                Gui.Selection.addSelectionGate(self.gate)
                self._gateActive = True
            elif self.selParams.SelectState:
                self.cleanupSelection()
            self.updateFeedback()

        def parameterChanged(self, _value=None):
            self.updateMode()
            self.updateFeedback()

        def updateFeedback(self):
            if self._closed:
                return
            error = self.selectionError or self.obj.LastError
            base, names = self.obj.baseObject
            if self.selParams.SelectState and base is not None:
                SheetMetalTools.taskPopulateSelectionList(self.form.tree, (base, names))
                self.invalidNames = list(self.obj.FailedCorners)
                if error:
                    for name in names:
                        try:
                            cornerEdges(base.Shape, [name])
                        except Exception:
                            if name not in self.invalidNames:
                                self.invalidNames.append(name)
            self.form.ErrorMessage.setText(
                translate("SheetMetal", "Corner preview failed: ") + error if error else "")
            self.form.ErrorMessage.setVisible(bool(error))
            for i in range(self.form.tree.topLevelItemCount()):
                item = self.form.tree.topLevelItem(i)
                invalid = item.text(1) in self.invalidNames
                for column in (0, 1):
                    item.setBackground(column, QtGui.QBrush(QtGui.QColor("#fff1f2"))
                                       if invalid else QtGui.QBrush())
                    item.setForeground(column, QtGui.QBrush(QtGui.QColor("#9f1239"))
                                       if invalid else QtGui.QBrush())
                    item.setToolTip(column, error if invalid else "")
            if self.selParams.SelectState:
                self.obj.Visibility = not bool(error)
                if base is not None:
                    base.Visibility = bool(error)

        def verifySelection(self):
            self.selectionError = ""
            self.invalidNames = []
            selection = Gui.Selection.getSelectionEx()
            base = self.obj.baseObject[0]
            if len(selection) != 1 or selection[0].Object != base:
                self.selectionError = translate(
                    "SheetMetal", "Select at least one corner from the source sheet-metal feature.")
            else:
                names = list(selection[0].SubElementNames)
                for name in names:
                    try:
                        cornerEdges(base.Shape, [name])
                    except Exception:
                        self.invalidNames.append(name)
                if self.invalidNames:
                    # The shared live observer can display Body-qualified names.
                    # Rebuild from resolved source names so the offending rows
                    # match the error and can be removed with Clear Selected.
                    SheetMetalTools.taskPopulateSelectionList(
                        self.form.tree, (base, names), True)
                    self.selectionError = translate(
                        "SheetMetal", "Invalid corners: %1. Select sharp outline vertices or thickness edges."
                    ).replace("%1", ", ".join(self.invalidNames))
                elif not names:
                    self.selectionError = translate("SheetMetal", "Select at least one corner.")
                else:
                    return base, names
            self.updateFeedback()
            return None, None

        def updateMode(self, _value=None):
            rounded = self.obj.Treatment == "Round"
            self.form.Radius.setVisible(rounded)
            self.form.RadiusLabel.setVisible(rounded)
            self.form.ChamferSize.setVisible(not rounded)
            self.form.ChamferLabel.setVisible(not rounded)

        def isAllowedAlterSelection(self):
            return True

        def isAllowedAlterView(self):
            return True

        def accept(self):
            if not self.selParams.SelectState:
                SheetMetalTools._taskMultiSelectionModeClicked(self.selParams)
                self.selectionModeChanged()
                if not self.selParams.SelectState:
                    return False
            try:
                self.obj.Proxy.execute(self.obj)
            except Exception:
                self.updateFeedback()
                return False
            self.cleanup()
            return SheetMetalTools.taskAccept(self)

        def reject(self):
            self.cleanup()
            SheetMetalTools.taskReject(self)

    class AddCornerTreatmentCommand:
        def GetResources(self):
            return {
                "Pixmap": _ICON,
                "MenuText": translate("SheetMetal", "Corner Round / Chamfer"),
                "ToolTip": translate("SheetMetal",
                    "Round or chamfer selected outline corners through the sheet thickness.\n"
                    "Select corner vertices or thickness edges on one sheet-metal feature."),
            }

        def IsActive(self):
            if not FreeCAD.ActiveDocument or Gui.Control.activeDialog():
                return False
            selection = Gui.Selection.getSelectionEx()
            if len(selection) != 1 or not selection[0].SubElementNames:
                return False
            try:
                cornerEdges(selection[0].Object.Shape, selection[0].SubElementNames)
                return True
            except Exception:
                return False

        def Activated(self):
            if not self.IsActive():
                return
            selection = Gui.Selection.getSelectionEx()[0]
            base, names = selection.Object, selection.SubElementNames
            try:
                cornerEdges(base.Shape, names)
            except Exception as error:
                SheetMetalTools.smWarnDialog(str(error))
                return
            obj, body = SheetMetalTools.smCreateNewObject(base, "CornerTreatment")
            if obj is None:
                return
            try:
                SMCornerTreatment(obj, base, names)
                obj.Label = translate("SheetMetal", "Corner Round / Chamfer")
                adoptCornerFeature(obj, base)
                SMCornerTreatmentViewProvider(obj.ViewObject)
                # adoptCornerFeature already added it to its Body/Part. Calling
                # Body.addObject a second time creates a repeated tree entry.
                SheetMetalTools.smAddNewObject(base, obj, None, SMCornerTreatmentTaskPanel)
            except Exception:
                base.Document.abortTransaction()
                raise

    Gui.addCommand("SheetMetal_CornerTreatment", AddCornerTreatmentCommand())
