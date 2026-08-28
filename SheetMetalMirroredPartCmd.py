########################################################################
#
#  SheetMetalMirroredPartCmd.py
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

"""Create a separate manufactured Sheet Metal Part derived by reflection."""

import os

import FreeCAD
import Part

import SheetMetalMaterial
import SheetMetalTools
from SheetMetalBoltConnectionCmd import _find_sheet_metal_part, _target_tip, _unit
from SheetMetalShapedFlangeCmd import addSheetMetalPartProperties


translate = FreeCAD.Qt.translate
icons_path = SheetMetalTools.icons_path

MIRROR_PLANES = ["YZ plane", "XZ plane", "XY plane", "Custom normal"]


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


def mirror_plane_normal(mirror):
    plane = str(mirror.MirrorPlane)
    if plane == "YZ plane":
        return FreeCAD.Vector(1.0, 0.0, 0.0)
    if plane == "XZ plane":
        return FreeCAD.Vector(0.0, 1.0, 0.0)
    if plane == "XY plane":
        return FreeCAD.Vector(0.0, 0.0, 1.0)
    return _unit(mirror.CustomNormal, "The custom mirror plane has no usable normal.")


def reflection_matrix(normal, offset):
    """Return a homogeneous reflection across normal dot point = offset."""
    normal = _unit(normal, "The mirror plane has no usable normal.")
    nx, ny, nz = normal.x, normal.y, normal.z
    matrix = FreeCAD.Matrix()
    matrix.A11 = 1.0 - 2.0 * nx * nx
    matrix.A12 = -2.0 * nx * ny
    matrix.A13 = -2.0 * nx * nz
    matrix.A14 = 2.0 * offset * nx
    matrix.A21 = -2.0 * ny * nx
    matrix.A22 = 1.0 - 2.0 * ny * ny
    matrix.A23 = -2.0 * ny * nz
    matrix.A24 = 2.0 * offset * ny
    matrix.A31 = -2.0 * nz * nx
    matrix.A32 = -2.0 * nz * ny
    matrix.A33 = 1.0 - 2.0 * nz * nz
    matrix.A34 = 2.0 * offset * nz
    matrix.A41 = 0.0
    matrix.A42 = 0.0
    matrix.A43 = 0.0
    matrix.A44 = 1.0
    return matrix


def _world_shape(feature):
    """Copy a feature shape from its history-container frame into world space."""
    shape = feature.Shape.copy()
    container = feature.getParentGeoFeatureGroup()
    if container is not None and hasattr(container, "getGlobalPlacement"):
        shape.Placement = container.getGlobalPlacement().multiply(shape.Placement)
    return shape


def mirrored_shape(feature, normal, offset):
    shape = _world_shape(feature)
    result = shape.transformShape(reflection_matrix(normal, offset), True, True)
    if result is None:
        result = shape
    result = result.removeSplitter()
    if result.isNull() or not result.isValid():
        raise ValueError("The reflected sheet-metal shape is invalid.")
    if len(result.Solids) != 1:
        raise ValueError("A mirrored sheet-metal product must contain one solid.")
    return result


def _copy_part_defaults(source, target):
    material_values = SheetMetalMaterial.materialPropertyValues(source)
    SheetMetalMaterial.restoreMaterialPropertyValues(target, material_values)
    for name in (
        "Thickness",
        "DefaultBendRadius",
        "DefaultReliefType",
        "DefaultReliefWidth",
        "DefaultReliefDepth",
        "KFactor",
        "UseGroupInDrawingName",
    ):
        if name in source.PropertiesList and name in target.PropertiesList:
            value = getattr(source, name)
            if hasattr(value, "Value"):
                value = value.Value
            elif source.getTypeIdOfProperty(name) == "App::PropertyEnumeration":
                value = str(value)
            setattr(target, name, value)


def _add_mirror_identity(part, source):
    if "DerivedFrom" not in part.PropertiesList:
        part.addProperty(
            "App::PropertyLink",
            "DerivedFrom",
            "Manufacturing",
            translate("App::Property", "Source product for this manufactured variant"),
        )
    part.DerivedFrom = source
    part.setEditorMode("DerivedFrom", 1)
    if "VariantType" not in part.PropertiesList:
        part.addProperty(
            "App::PropertyString",
            "VariantType",
            "Manufacturing",
            translate("App::Property", "Relationship to the source product"),
        )
    part.VariantType = "Mirrored derivative"
    part.setEditorMode("VariantType", 1)


class SMMirroredPart:
    """Live reflected BRep at the root of a distinct product Body."""

    def __init__(self, obj, source_part=None, target_part=None):
        self.addVerifyProperties(obj)
        if source_part is not None:
            obj.SourcePart = source_part
        if target_part is not None:
            obj.TargetPartName = target_part.Name
        obj.Proxy = self

    def addVerifyProperties(self, obj):
        if "SheetMetalType" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                "SheetMetalType",
                "Mirrored Part",
                translate("App::Property", "Sheet-metal object type"),
            ).SheetMetalType = "MirroredPart"
            obj.setEditorMode("SheetMetalType", 1)
        if "SourcePart" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyLink",
                "SourcePart",
                "Mirrored Part",
                translate("App::Property", "Source Sheet Metal Part"),
            )
        if "SourceFeature" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyLink",
                "SourceFeature",
                "Mirrored Part",
                translate("App::Property", "Current source-part Tip being reflected"),
            )
            obj.setEditorMode("SourceFeature", 1)
        if "TargetPartName" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                "TargetPartName",
                "Mirrored Part",
                translate("App::Property", "Internal name of the mirrored product"),
            )
            obj.setEditorMode("TargetPartName", 1)
        _set_enumeration(
            obj,
            "MirrorPlane",
            MIRROR_PLANES,
            "YZ plane",
            "Mirror Plane",
            translate("App::Property", "World-space plane orientation"),
        )
        if "PlaneOffset" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyDistance",
                "PlaneOffset",
                "Mirror Plane",
                translate(
                    "App::Property",
                    "Signed distance from the world origin along the plane normal",
                ),
            ).PlaneOffset = 0.0
        if "CustomNormal" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyVector",
                "CustomNormal",
                "Mirror Plane",
                translate("App::Property", "World-space custom plane normal"),
            ).CustomNormal = FreeCAD.Vector(1.0, 0.0, 0.0)
        if "Refine" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyBool",
                "Refine",
                "Result",
                translate("App::Property", "Remove residual splitter edges"),
            ).Refine = True
        if "LastError" not in obj.PropertiesList:
            obj.addProperty(
                "App::PropertyString",
                "LastError",
                "Result",
                translate("App::Property", "Mirror evaluation error"),
            )
            obj.setEditorMode("LastError", 1)

    def execute(self, fp):
        self.addVerifyProperties(fp)
        try:
            if fp.SourcePart is None:
                raise ValueError("The source Sheet Metal Part is missing.")
            source_feature = _target_tip(fp.SourcePart)
            if (
                source_feature is None
                or not hasattr(source_feature, "Shape")
                or source_feature.Shape.isNull()
            ):
                raise ValueError("The source Sheet Metal Part has no finished shape.")
            fp.SourceFeature = source_feature
            result = mirrored_shape(
                source_feature,
                mirror_plane_normal(fp),
                fp.PlaneOffset.Value,
            )
            if fp.Refine:
                result = result.removeSplitter()
            fp.Shape = result
            fp.LastError = ""
        except (ValueError, Part.OCCError) as error:
            fp.Shape = Part.Shape()
            fp.LastError = str(error)
            FreeCAD.Console.PrintError(
                "Mirrored sheet-metal part {}: {}\n".format(fp.Label, error)
            )


def create_mirrored_sheet_metal_part(
    doc,
    source_part,
    mirror_plane="YZ plane",
    plane_offset=0.0,
):
    if not (
        source_part is not None
        and source_part.TypeId == "App::Part"
        and hasattr(source_part, "SheetMetalType")
        and source_part.SheetMetalType == "Part"
    ):
        raise ValueError("Select one Sheet Metal Part to mirror.")
    source_feature = _target_tip(source_part)
    if (
        source_feature is None
        or not hasattr(source_feature, "Shape")
        or source_feature.Shape.isNull()
    ):
        raise ValueError("The selected Sheet Metal Part has no finished shape.")

    target_part = doc.addObject("App::Part", "MirroredSheetMetalPart")
    target_part.Label = source_part.Label + " " + translate("SheetMetal", "Mirrored")
    addSheetMetalPartProperties(target_part)
    _copy_part_defaults(source_part, target_part)
    _add_mirror_identity(target_part, source_part)

    body = doc.addObject("PartDesign::Body", "MirroredBody")
    body.Label = translate("SheetMetal", "Mirrored Body")
    target_part.addObject(body)
    feature = body.newObject("PartDesign::FeaturePython", "MirroredPart")
    feature.Label = translate("SheetMetal", "Mirrored Derived Part")
    SMMirroredPart(feature, source_part, target_part)
    feature.MirrorPlane = mirror_plane
    feature.PlaneOffset = plane_offset
    body.Tip = feature
    target_part.Tip = feature.Name
    doc.recompute()
    if feature.Shape.isNull() or not feature.Shape.isValid():
        raise ValueError(feature.LastError or "The mirrored part could not be created.")
    return target_part, body, feature


if SheetMetalTools.isGuiLoaded():
    Gui = FreeCAD.Gui
    from PySide import QtGui

    class SMMirroredPartViewProvider(SheetMetalTools.SMViewProvider):
        def getIcon(self):
            return os.path.join(icons_path, "SheetMetal_MirroredPart.svg")

        def getTaskPanel(self, obj):
            return SMMirroredPartTaskPanel(obj)


    class SMMirroredPartTaskPanel:
        def __init__(self, obj):
            self.obj = obj
            obj.Proxy.addVerifyProperties(obj)
            self.form = QtGui.QWidget()
            self.form.setWindowTitle(translate("SheetMetal", "Mirrored sheet-metal part"))
            layout = QtGui.QVBoxLayout(self.form)

            source_label = obj.SourcePart.Label if obj.SourcePart is not None else "?"
            layout.addWidget(
                QtGui.QLabel(
                    translate("SheetMetal", "Derived manufactured product from %1")
                    .replace("%1", source_label)
                )
            )

            form = QtGui.QFormLayout()
            self.plane = QtGui.QComboBox()
            self.plane.addItems(MIRROR_PLANES)
            self.plane.setCurrentText(str(obj.MirrorPlane))
            self.offset = QtGui.QDoubleSpinBox()
            self.offset.setDecimals(4)
            self.offset.setRange(-1e9, 1e9)
            self.offset.setValue(obj.PlaneOffset.Value)
            self.offset.setSuffix(" mm")
            self.normal_widget = QtGui.QWidget()
            normal_layout = QtGui.QHBoxLayout(self.normal_widget)
            normal_layout.setContentsMargins(0, 0, 0, 0)
            self.normal_spins = []
            for component in (
                obj.CustomNormal.x,
                obj.CustomNormal.y,
                obj.CustomNormal.z,
            ):
                spin = QtGui.QDoubleSpinBox()
                spin.setDecimals(5)
                spin.setRange(-1e6, 1e6)
                spin.setValue(component)
                normal_layout.addWidget(spin)
                self.normal_spins.append(spin)
            form.addRow(translate("SheetMetal", "Mirror plane"), self.plane)
            form.addRow(translate("SheetMetal", "Plane offset"), self.offset)
            form.addRow(translate("SheetMetal", "Custom normal XYZ"), self.normal_widget)
            layout.addLayout(form)

            self.plane.currentTextChanged.connect(self._set_plane)
            self.offset.valueChanged.connect(
                lambda value: self._set_property("PlaneOffset", value)
            )
            for spin in self.normal_spins:
                spin.valueChanged.connect(self._set_normal)
            self._update_normal_enabled()

        def _set_property(self, name, value):
            setattr(self.obj, name, value)
            self.obj.Document.recompute()

        def _set_plane(self, value):
            self._set_property("MirrorPlane", value)
            self._update_normal_enabled()

        def _set_normal(self, _value):
            self._set_property(
                "CustomNormal",
                FreeCAD.Vector(*(spin.value() for spin in self.normal_spins)),
            )

        def _update_normal_enabled(self):
            self.normal_widget.setEnabled(str(self.obj.MirrorPlane) == "Custom normal")

        def isAllowedAlterSelection(self):
            return True

        def isAllowedAlterView(self):
            return True

        def accept(self):
            return SheetMetalTools.taskAccept(self)

        def reject(self):
            SheetMetalTools.taskReject(self)


    def _selected_source_parts():
        parts = []
        for obj in Gui.Selection.getSelection():
            part = _find_sheet_metal_part(obj)
            if part is not None and part not in parts:
                parts.append(part)
        return parts


    class AddMirroredPartCommandClass:
        def GetResources(self):
            return {
                "Pixmap": os.path.join(icons_path, "SheetMetal_MirroredPart.svg"),
                "MenuText": translate("SheetMetal", "Create Mirrored Sheet Metal Part"),
                "ToolTip": translate(
                    "SheetMetal",
                    "Create a distinct manufactured Sheet Metal Part with a live "
                    "mirrored Body derived from the selected source product.",
                ),
            }

        def Activated(self):
            doc = FreeCAD.ActiveDocument
            sources = _selected_source_parts()
            doc.openTransaction("MirroredSheetMetalPart")
            try:
                target_part, _body, feature = create_mirrored_sheet_metal_part(
                    doc, sources[0]
                )
                SMMirroredPartViewProvider(feature.ViewObject)
                Gui.Selection.clearSelection()
                Gui.Selection.addSelection(feature)
                dialog = SMMirroredPartTaskPanel(feature)
                SheetMetalTools.updateTaskTitleIcon(dialog)
                Gui.Control.showDialog(dialog)
            except (IndexError, ValueError, Part.OCCError) as error:
                doc.abortTransaction()
                SheetMetalTools.smWarnDialog(str(error))

        def IsActive(self):
            return FreeCAD.ActiveDocument is not None and len(_selected_source_parts()) == 1


    Gui.addCommand("SheetMetal_MirroredPart", AddMirroredPartCommandClass())

