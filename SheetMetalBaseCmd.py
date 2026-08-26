########################################################################
#
#  SheetMetalBaseCmd.py
#
#  Copyright 2015 Shai Seger <shaise at gmail dot com>
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
#  You should have received a copy of the GNU Lesser General Public
#  License along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
#  MA 02110-1301, USA.
#
#
########################################################################

import os

import FreeCAD
import Part

import SheetMetalMaterial
import SheetMetalTools

translate = FreeCAD.Qt.translate
icons_path = SheetMetalTools.icons_path
panels_path = SheetMetalTools.panels_path

# List of properties to be saved as defaults.
smBaseDefaultVars = ["Radius", "Thickness"]


def _containing_app_part(obj):
    """Return the nearest App::Part containing an object."""
    current = obj
    while current is not None:
        if current.TypeId == "App::Part":
            return current
        current = current.getParentGeoFeatureGroup() or current.getParentGroup()
    return None


def _is_base_bend(obj):
    proxy = getattr(obj, "Proxy", None)
    return (
        proxy is not None
        and proxy.__class__.__name__ == "SMBaseBend"
    ) or all(
        name in obj.PropertiesList
        for name in ("BendSketch", "BendSide", "Thickness", "Radius")
    )


def _base_bends_in_part(part):
    return [
        obj for obj in part.Document.Objects
        if _is_base_bend(obj) and _containing_app_part(obj) is part
    ]


def _add_sheet_metal_part_properties(part):
    # Kept lazy so saved SMBaseBend proxies can restore while the workbench's
    # other command modules are still being imported.
    from SheetMetalShapedFlangeCmd import addSheetMetalPartProperties
    addSheetMetalPartProperties(part)


def _verify_base_bend_properties(obj):
    proxy = getattr(obj, "Proxy", None)
    if proxy is not None and hasattr(proxy, "addVerifyProperties"):
        proxy.addVerifyProperties(obj)
    else:
        SMBaseBend.addVerifyProperties(None, obj)


def _sync_base_bend_part_defaults(obj):
    """Copy owning-part values into compatibility properties used by smBase."""
    part = SheetMetalMaterial.findSheetMetalPart(obj)
    controlled = part is not None and bool(obj.UsePartThickness)
    obj.setEditorMode("UsePartThickness", 1)
    obj.setEditorMode("Thickness", 1 if controlled else 0)
    obj.setEditorMode(
        "Radius",
        1 if controlled and obj.UseDefaultBendRadius else 0,
    )
    if not controlled:
        return part
    if abs(obj.Thickness.Value - part.Thickness.Value) > 1.0e-9:
        obj.Thickness = part.Thickness.Value
    if (
        obj.UseDefaultBendRadius
        and abs(obj.Radius.Value - part.DefaultBendRadius.Value) > 1.0e-9
    ):
        obj.Radius = part.DefaultBendRadius.Value
    return part


def _bind_base_bends_to_part(part, base_bends, preserve_geometry):
    """Make existing BaseBends consume one owning part's sheet defaults."""
    if not base_bends:
        return part
    _add_sheet_metal_part_properties(part)
    if preserve_geometry and not part.UseMaterialCatalog:
        source = base_bends[0]
        part.Thickness = source.Thickness.Value
        part.DefaultBendRadius = source.Radius.Value
    for base_bend in base_bends:
        _verify_base_bend_properties(base_bend)
        base_bend.UsePartThickness = True
        base_bend.UseDefaultBendRadius = True
        base_bend.PartDefaultsVersion = 1
        _sync_base_bend_part_defaults(base_bend)
        base_bend.touch()
    return part


def migrateDocumentBaseBends(doc):
    """Promote App::Parts containing legacy BaseBends without changing geometry."""
    parts = {}
    for obj in doc.Objects:
        if not _is_base_bend(obj):
            continue
        part = _containing_app_part(obj)
        if part is not None:
            parts.setdefault(part.Name, (part, []))[1].append(obj)
    for part, base_bends in parts.values():
        for base_bend in base_bends:
            _verify_base_bend_properties(base_bend)
        if any(base_bend.PartDefaultsVersion < 1 for base_bend in base_bends):
            _bind_base_bends_to_part(part, base_bends, preserve_geometry=True)
        if (
            hasattr(part, "Tip")
            and isinstance(part.Tip, str)
            and part.Tip
            and part.Document.getObject(part.Tip) is None
        ):
            part.Tip = ""
    return [part for part, _base_bends in parts.values()]


def prepareNewBaseBendPart(base_bend, sketch, active_body=None):
    """Create or promote the owning Sheet Metal Part for a new BaseBend."""
    container = active_body if active_body is not None else sketch
    part = _containing_app_part(container)
    if part is None:
        from SheetMetalShapedFlangeCmd import createSheetMetalPart
        part = createSheetMetalPart(base_bend.Document)
        if active_body is not None:
            part.addObject(active_body)
        else:
            if _containing_app_part(sketch) is None:
                part.addObject(sketch)
            part.addObject(base_bend)
    else:
        was_sheet_metal = (
            hasattr(part, "SheetMetalType") and part.SheetMetalType == "Part"
        )
        _add_sheet_metal_part_properties(part)
        if not was_sheet_metal:
            SheetMetalMaterial.configureNewPart(part)
        if active_body is None:
            part.addObject(base_bend)
    _verify_base_bend_properties(base_bend)
    base_bend.UsePartThickness = True
    base_bend.UseDefaultBendRadius = True
    base_bend.PartDefaultsVersion = 1
    _sync_base_bend_part_defaults(base_bend)
    return part


def modifiedWire(WireList, radius, thk, length, normal, Side, sign):
    # If sketch is one type, make a face by extruding & offset it to
    # correct position.
    wire_extr = WireList.extrude(normal * sign * length)
    # Part.show(wire_extr, "wire_extr")
    if Side == "Inside":
        wire_extr = wire_extr.makeOffsetShape(thk / 2.0 * sign, 0.0, fill=False, join=2)
    elif Side == "Outside":
        wire_extr = wire_extr.makeOffsetShape(-thk / 2.0 * sign, 0.0, fill=False, join=2)
    # Part.show(wire_extr, "wire_extr")
    try:
        filleted_extr = wire_extr.makeFillet((radius + thk / 2.0), wire_extr.Edges)
    except:
        filleted_extr = wire_extr
    # Part.show(filleted_extr, "filleted_extr")
    filleted_extr = filleted_extr.makeOffsetShape(thk / 2.0 * sign, 0.0, fill=False, join=2)
    # Part.show(filleted_extr, "filleted_extr")
    return filleted_extr


def smBase(thk=2.0, length=10.0, radius=1.0, Side="Inside",
           midplane=False, reverse=False, MainObject=None):
    # To Get sketch normal.
    WireList = MainObject.Shape.Wires[0]
    mat = MainObject.getGlobalPlacement().Rotation
    normal = (mat.multVec(FreeCAD.Vector(0, 0, 1))).normalize()
    # print([mat, normal])
    if WireList.isClosed():
        # If Closed sketch is there, make a face & extrude it.
        sketch_face = Part.makeFace(MainObject.Shape.Wires, "Part::FaceMakerBullseye")
        thk = -1.0 * thk if reverse else thk
        wallSolid = sketch_face.extrude(sketch_face.normalAt(0, 0) * thk)
        if midplane:
            wallSolid = Part.Solid(wallSolid.translated(sketch_face.normalAt(0, 0) * thk * -0.5))
    else:
        filleted_extr = modifiedWire(WireList, radius, thk, length, normal, Side, 1.0)
        # Part.show(filleted_extr, "filleted_extr")
        dist = WireList.Vertexes[0].Point.distanceToPlane(FreeCAD.Vector(0, 0, 0), normal)
        # print(dist)
        slice_wire = filleted_extr.slice(normal, dist)
        # print(slice_wire)
        # Part.show(slice_wire[0], "slice_wire")
        traj = slice_wire[0]
        # Part.show(traj, "traj")
        if midplane:
            traj.translate(normal * -length / 2.0)
        elif reverse:
            traj.translate(normal * -length)
        traj_extr = traj.extrude(normal * length)
        # Part.show(traj_extr, "traj_extr")
        solidlist = []
        for face in traj_extr.Faces:
            solid = face.makeOffsetShape(thk, 0.0, fill=True)
            solidlist.append(solid)
        if len(solidlist) > 1:
            wallSolid = solidlist[0].multiFuse(solidlist[1:])
        else:
            wallSolid = solidlist[0]
        # Part.show(wallSolid, "wallSolid")
    # Part.show(wallSolid, "wallSolid")
    return wallSolid


class SMBaseBend:
    def __init__(self, obj, sketch):
        """Add wall or Wall with radius bend."""
        _tip_ = translate("App::Property", "Bend Plane")
        obj.addProperty("App::PropertyEnumeration", "BendSide", "Parameters", _tip_).BendSide = [
                "Outside", "Inside", "Middle"]
        _tip_ = translate("App::Property", "Wall Sketch object")
        obj.addProperty("App::PropertyLink", "BendSketch", "Parameters", _tip_).BendSketch = sketch
        _tip_ = translate("App::Property", "Extrude Symmetric to Plane")
        self.addVerifyProperties(obj)
        SheetMetalTools.taskRestoreDefaults(obj, smBaseDefaultVars)
        obj.Proxy = self

    def addVerifyProperties(self, obj):
        SheetMetalTools.smAddLengthProperty(obj,
            "Radius",
            translate("App::Property", "Bend Radius"),
            1.0)
        SheetMetalTools.smAddLengthProperty(obj,
            "Thickness",
            translate("App::Property", "Thickness of sheetmetal"),
            1.0)
        SheetMetalTools.smAddLengthProperty(obj,
            "Length",
            translate("App::Property", "Length of wall"),
            100.0)
        SheetMetalTools.smAddBoolProperty(obj,
            "MidPlane",
            FreeCAD.Qt.translate("App::Property", "Extrude Symmetric to Plane"),
            False)
        SheetMetalTools.smAddBoolProperty(obj,
            "Reverse",
            FreeCAD.Qt.translate("App::Property", "Reverse Extrusion Direction"),
            False)
        SheetMetalTools.smAddBoolProperty(
            obj,
            "UsePartThickness",
            translate("App::Property", "Use the owning Sheet Metal Part thickness"),
            False,
            "Part Defaults",
        )
        obj.setEditorMode("UsePartThickness", 1)
        SheetMetalTools.smAddBoolProperty(
            obj,
            "UseDefaultBendRadius",
            translate(
                "App::Property",
                "Use the owning Sheet Metal Part default bend radius",
            ),
            False,
            "Part Defaults",
        )
        SheetMetalTools.smAddIntProperty(
            obj,
            "PartDefaultsVersion",
            translate("App::Property", "Sheet Metal Part defaults migration version"),
            0,
            "Hidden",
        )

    def execute(self, fp):
        """Print a short message when doing a recomputation.

        Note:
            This method is mandatory.

        """
        self.addVerifyProperties(fp)
        part = _containing_app_part(fp)
        if part is not None and fp.PartDefaultsVersion < 1:
            _bind_base_bends_to_part(
                part,
                _base_bends_in_part(part),
                preserve_geometry=True,
            )
        _sync_base_bend_part_defaults(fp)
        fp.Shape = smBase(thk=fp.Thickness.Value,
                          length=fp.Length.Value,
                          radius=fp.Radius.Value,
                          Side=fp.BendSide,
                          midplane=fp.MidPlane,
                          reverse=fp.Reverse,
                          MainObject=fp.BendSketch)


class _BaseBendPartDefaultsObserver:
    """Migrate BaseBend containers and propagate owning-part defaults."""

    def __init__(self):
        self.updating = False

    def slotChangedObject(self, obj, prop):
        if self.updating or prop not in {"Thickness", "DefaultBendRadius"}:
            return
        if not (
            hasattr(obj, "SheetMetalType") and obj.SheetMetalType == "Part"
        ):
            return
        for base_bend in _base_bends_in_part(obj):
            if (
                prop == "Thickness"
                or getattr(base_bend, "UseDefaultBendRadius", False)
            ):
                base_bend.touch()

    def _migrate(self, doc):
        if self.updating:
            return
        try:
            self.updating = True
            migrateDocumentBaseBends(doc)
        finally:
            self.updating = False

    def slotActivateDocument(self, doc):
        self._migrate(doc)

    def slotRecomputedDocument(self, doc):
        if any(
            _is_base_bend(obj)
            and (
                "PartDefaultsVersion" not in obj.PropertiesList
                or obj.PartDefaultsVersion < 1
            )
            for obj in doc.Objects
        ):
            self._migrate(doc)


if "_base_bend_part_defaults_observer" not in globals():
    _base_bend_part_defaults_observer = _BaseBendPartDefaultsObserver()
    FreeCAD.addDocumentObserver(_base_bend_part_defaults_observer)
    for _open_document in FreeCAD.listDocuments().values():
        migrateDocumentBaseBends(_open_document)


###################################################################################################
# Gui code
###################################################################################################

if SheetMetalTools.isGuiLoaded():
    Gui = FreeCAD.Gui


    ###############################################################################################
    # View Provider
    ###############################################################################################

    class SMBaseViewProvider(SheetMetalTools.SMViewProvider):
        """Part / Part WB style ViewProvider."""

        def getIcon(self):
            return os.path.join(icons_path, "SheetMetal_AddBase.svg")

        def claimChildren(self):
            objs = []
            if hasattr(self, "Object") and hasattr(self.Object, "BendSketch"):
                objs.append(self.Object.BendSketch)
            return objs

        def getTaskPanel(self, obj):
            return SMBaseBendTaskPanel(obj)


    ###############################################################################################
    # Task Panel
    ###############################################################################################

    class SMBaseBendTaskPanel:
        """A TaskPanel for the SheetMetal base bend command."""

        def __init__(self, obj):
            self.obj = obj
            self.form = SheetMetalTools.taskLoadUI("CreateBaseShape.ui")
            # Make sure all properties are added.
            obj.Proxy.addVerifyProperties(obj)

            self.updateDisplay()

            SheetMetalTools.taskConnectSelectionSingle(self.form.pushSketch, self.form.txtSketch,
                                                       obj, "BendSketch",
                                                       ("Sketcher::SketchObject", []))
            SheetMetalTools.taskConnectSpin(obj, self.form.spinRadius, "Radius")
            SheetMetalTools.taskConnectSpin(obj, self.form.spinThickness, "Thickness")
            SheetMetalTools.taskConnectSpin(obj, self.form.spinLength, "Length")
            SheetMetalTools.taskConnectEnum(obj, self.form.comboBendPlane, "BendSide")
            SheetMetalTools.taskConnectCheck(obj, self.form.checkSymetric, "MidPlane",
                                             self.midplaneChanged)
            SheetMetalTools.taskConnectCheck(obj, self.form.checkRevDirection, "Reverse")
            obj.BendSketch.Visibility = True

        def isAllowedAlterSelection(self):
            return True

        def isAllowedAlterView(self):
            return True

        def updateDisplay(self):
            self.form.checkRevDirection.setVisible(self.obj.MidPlane is False)
            self.form.spinThickness.setEnabled(not self.obj.UsePartThickness)
            self.form.spinRadius.setEnabled(
                not (
                    self.obj.UsePartThickness
                    and self.obj.UseDefaultBendRadius
                )
            )

        def midplaneChanged(self, value):
            self.updateDisplay()

        def accept(self):
            SheetMetalTools.taskAccept(self)
            defaults = []
            if not self.obj.UseDefaultBendRadius:
                defaults.append("Radius")
            if not self.obj.UsePartThickness:
                defaults.append("Thickness")
            SheetMetalTools.taskSaveDefaults(self.obj, defaults)
            self.obj.BendSketch.Visibility = False
            return True

        def reject(self):
            SheetMetalTools.taskReject(self)

        # def retranslateUi(self, SMBendTaskPanel):


    ###############################################################################################
    # Command
    ###############################################################################################

    class AddBaseCommandClass:
        """Add Base Wall command."""

        def GetResources(self):
            return {
                    # The name of a svg file available in the resources.
                    "Pixmap": os.path.join(icons_path, "SheetMetal_AddBase.svg"),
                    "MenuText": translate("SheetMetal", "Make Base Wall"),
                    "Accel": "C, B",
                    "ToolTip": translate(
                        "SheetMetal",
                        "Create a sheetmetal wall from a sketch\n"
                        "1. Select a Sketch to create bends with walls.\n"
                        "2. Use Property editor to modify other parameters",
                        ),
                    }

        def Activated(self):
            selobj = Gui.Selection.getSelectionEx()[0].Object
            newObj, activeBody = SheetMetalTools.smCreateNewObject(selobj, "BaseBend")
            if newObj is None:
                return
            SMBaseBend(newObj, selobj)
            prepareNewBaseBendPart(newObj, selobj, activeBody)
            SMBaseViewProvider(newObj.ViewObject)
            SheetMetalTools.smAddNewObject(selobj, newObj, activeBody, SMBaseBendTaskPanel)
            return

        def IsActive(self):
            if len(Gui.Selection.getSelection()) != 1:
                return False
            selobj = Gui.Selection.getSelection()[0]
            if not (
                selobj.isDerivedFrom("Sketcher::SketchObject")
                or selobj.isDerivedFrom("PartDesign::ShapeBinder")
                or selobj.isDerivedFrom("PartDesign::SubShapeBinder")
                or selobj.isDerivedFrom("Part::Part2DObjectPython")
            ):
                return False
            return True

    Gui.addCommand("SheetMetal_AddBase", AddBaseCommandClass())
