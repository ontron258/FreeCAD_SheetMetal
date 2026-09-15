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
        result = (shape.makeFillet(size, edges) if treatment == "Round"
                  else shape.makeChamfer(size, edges))
        if result.isNull() or not result.isValid() or len(result.Solids) != 1:
            raise ValueError("Invalid corner result")
    except Exception as error:
        raise ValueError(translate(
            "SheetMetal", "Cannot create these corners. Reduce the size or change the selection."
        )) from error
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
    if parent is not None:
        parent.addObject(obj)
    part = _owning_part(obj)
    if part is not None and "Tip" in part.PropertiesList:
        part.Tip = obj.Name


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

    def onChanged(self, obj, prop):
        if prop == "Treatment":
            for name, mode in (("Radius", "Round"), ("ChamferSize", "Chamfer")):
                if name in obj.PropertiesList:
                    obj.setEditorMode(name, 0 if obj.Treatment == mode else 2)

    def onDocumentRestored(self, obj):
        self.addVerifyProperties(obj)
        self.onChanged(obj, "Treatment")

    def getElementMapVersion(self, _fp, ver, _prop, restored):
        return None if restored else smElementMapVersion + ver

    def execute(self, obj):
        self.addVerifyProperties(obj)
        self.onChanged(obj, "Treatment")
        base, names = obj.baseObject
        if base is None:
            raise ValueError(translate("SheetMetal", "Select a base sheet-metal feature."))
        size = obj.Radius.Value if obj.Treatment == "Round" else obj.ChamferSize.Value
        obj.Shape = makeCornerTreatment(base.Shape, names, str(obj.Treatment), size)


if SheetMetalTools.isGuiLoaded():
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
            obj.Proxy.addVerifyProperties(obj)
            self.form = SheetMetalTools.taskLoadUI("CornerTreatmentPanel.ui")
            self.selParams = SheetMetalTools.taskConnectSelection(
                self.form.AddRemove, self.form.tree, obj, ["Vertex", "Edge"],
                self.form.pushClearSel)
            self.selParams.ConstrainToObject = obj.baseObject[0]
            self.selParams.verifySelection = self.verifySelection
            SheetMetalTools.taskConnectEnum(obj, self.form.Treatment, "Treatment",
                                           self.updateMode)
            SheetMetalTools.taskConnectSpin(obj, self.form.Radius, "Radius")
            SheetMetalTools.taskConnectSpin(obj, self.form.ChamferSize, "ChamferSize")
            self.updateMode()

        def verifySelection(self):
            if len(Gui.Selection.getSelectionEx()) > 1:
                SheetMetalTools.smWarnDialog(translate(
                    "SheetMetal", "Select corners from one sheet-metal feature."
                ))
                return None, None
            return SheetMetalTools.SMSelectionParameters.verifySelection(self.selParams)

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
                if not self.selParams.SelectState:
                    return False
            try:
                self.obj.Proxy.execute(self.obj)
            except Exception as error:
                SheetMetalTools.smWarnDialog(str(error))
                return False
            return SheetMetalTools.taskAccept(self)

        def reject(self):
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
            selection = Gui.Selection.getSelectionEx()
            return bool(FreeCAD.ActiveDocument and len(selection) == 1
                        and selection[0].SubElementNames
                        and all(isinstance(item, (Part.Vertex, Part.Edge))
                                for item in selection[0].SubObjects))

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
                SheetMetalTools.smAddNewObject(base, obj, body, SMCornerTreatmentTaskPanel)
            except Exception:
                base.Document.abortTransaction()
                raise

    Gui.addCommand("SheetMetal_CornerTreatment", AddCornerTreatmentCommand())
