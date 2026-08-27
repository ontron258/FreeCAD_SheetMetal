########################################################################
#
#  SheetMetalUnfoldCmd.py
#
#  Copyright 2014, 2018 Ulrich Brammer <ulrich@Pauline>
#  Copyright 2023 Ondsel Inc.
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

import math
import os
import sys

import FreeCAD
import Part

import SheetMetalBendCuts
import SheetMetalKfactor
import SheetMetalMaterial
import SheetMetalTools
import SheetMetalUnfolder
from engineering_mode import engineering_mode_enabled

translate = FreeCAD.Qt.translate
SMLogger = SheetMetalTools.SMLogger

if sys.version_info.major == 3 and sys.version_info.minor < 10:
    NewUnfolderAvailable = False
    FreeCAD.Console.PrintWarning(
        translate("SheetMetal",
            "Python version is too old for the new unfolder\n"
            "Reverting to the old one\n"
            )
        )
elif SheetMetalTools.smIsNetworkxAvailable():
    import SheetMetalNewUnfolder
    import networkx as nx

    if not hasattr(nx, "Graph"):
        NewUnfolderAvailable = False
    else:
        from SheetMetalNewUnfolder import BendAllowanceCalculator

        NewUnfolderAvailable = True
else:
    NewUnfolderAvailable = False
    FreeCAD.Console.PrintWarning(
        translate("SheetMetal",
            "Networkx dependency is missing and required for the new Unfolder\n"
            "Try uninstalling SheetMetal, refresh Addon Manager's cache, and reinstall\n"
            )
        )

# IMPORTANT: please remember to change the element map version in case
# of any changes in modeling logic.
smElementMapVersion = "sm1."

# List of properties to be saved as defaults.
smUnfoldDefaultVars = [
    "KFactorStandard",
    "GenerateSketch",
    "SeparateSketchLayers",
    "GenerateBendCuts",
    "BendCutMaxMaterial",
    "BendCutMaxCut",
    "BendCutEdgeOffset",
    "BendCutShapeMode",
]
smUnfoldNonSavedDefaultVars = [
    "UnfoldTransparency",
    "SketchColor",
    "InternalColor",
    "BendLineColor",
    "BendLabelColor",
    "CutSketchColor",
    "ExportType",
]

GENSKETCHCOLOR = "#000080"
OUTLINESKETCHCOLOR = "#c00000"
BENDLINESKETCHCOLOR = "#ff5733"
BENDLABELCOLOR = "#33ff33"
BENDCUTSKETCHCOLOR = "#00c0ff"
KFACTOR = 0.40


###################################################################################################
# Helper functions
###################################################################################################

def smUnfoldExportSketches(obj, useDialog=True):
    if len(obj.UnfoldSketches) == 0:
        return
    sketches = []
    if len(obj.UnfoldSketches) == 1:
        sketchNames = [obj.UnfoldSketches[0]]
    else:
        sketchNames = obj.UnfoldSketches
    for name in sketchNames:
        sketch = obj.Document.getObject(name)
        if sketch is None:
            return
        sketches.append(sketch)
    exptype = obj.Proxy.ExportType
    expname = obj.Label.removesuffix("_Unfold")
    filename = f"{FreeCAD.ActiveDocument.Name}-{expname}.{exptype}"
    if exptype == "dxf":
        smExportLayeredUnfoldDXF(sketches, filename, useDialog)
    else:
        SheetMetalTools.smGuiExportSketch(sketches, exptype, filename, useDialog)


def _unfoldDXFLayerName(sketch):
    """Map generated Unfold sketch roles to manufacturing DXF layers."""
    name = "{} {}".format(sketch.Name, sketch.Label).lower()
    if "bendcuts" in name or "bend_cuts" in name:
        return "BEND_CUT"
    if "bend_labels" in name or "bendlabels" in name:
        return "BEND_LABEL"
    if "_bends" in name or name.endswith(" bends"):
        return "BEND"
    if "internal" in name:
        return "INTERNAL"
    # The main/outline sketch and hole sketch are both cutting profiles.
    return "CUT"


def smExportLayeredUnfoldDXF(sketches, filename, useDialog=True):
    """Export Unfold geometry on explicit DXF layers without dirtying the model."""
    if not sketches:
        return
    source_doc = sketches[0].Document
    source_file = source_doc.FileName
    source_name = source_doc.Name
    export_doc = FreeCAD.newDocument("SheetMetalDXFExport")
    export_objects = []
    try:
        layer_shapes = {}
        for sketch in sketches:
            layer_name = _unfoldDXFLayerName(sketch)
            shape = sketch.Shape.copy()
            if hasattr(sketch, "getGlobalPlacement"):
                shape.Placement = sketch.getGlobalPlacement().multiply(shape.Placement)
            else:
                shape.Placement = sketch.Placement.multiply(shape.Placement)
            layer_shapes.setdefault(layer_name, []).append(shape)

        for layer_name, shapes in layer_shapes.items():
            layer = export_doc.addObject(
                "App::DocumentObjectGroup", "DXF_{}".format(layer_name)
            )
            layer.Label = layer_name
            # The current C++ DXF exporter uses the internal object name as
            # its layer; the legacy Python exporter uses the group label.
            geometry = export_doc.addObject("Part::Feature", layer_name)
            geometry.Label = layer_name
            geometry.Shape = Part.makeCompound(shapes)
            layer.addObject(geometry)
            export_objects.append(geometry)
        export_doc.recompute()
        if hasattr(SheetMetalTools, "smGuiExportSketch"):
            SheetMetalTools.smGuiExportSketch(
                export_objects,
                "dxf",
                filename,
                useDialog,
                sourceFile=source_file,
            )
        elif not useDialog:
            import importDXF

            importDXF.export(export_objects, filename)
    finally:
        FreeCAD.closeDocument(export_doc.Name)
        if FreeCAD.getDocument(source_name) is not None:
            FreeCAD.setActiveDocument(source_name)


def arrangeFlatPatternLinks(links, spacing=10.0):
    """Align linked flat solids to XY and pack their bounding boxes in rows.

    Only App::Link placements are changed; the source Unfold objects retain
    their modeling placements. Every item rests at Z=0, so different sheet
    thicknesses naturally produce different top elevations.
    """
    spacing = max(0.0, float(spacing))
    items = []
    documents = set()
    z_axis = FreeCAD.Vector(0.0, 0.0, 1.0)

    for link in links:
        if link is None or link.LinkedObject is None:
            continue
        documents.add(link.Document)
        if hasattr(link, "LinkTransform"):
            link.LinkTransform = False
        link.Placement = FreeCAD.Placement()
        link.Document.recompute()
        planar_faces = [
            face for face in link.Shape.Faces
            if isinstance(face.Surface, Part.Plane)
        ]
        if not planar_faces:
            continue
        reference_face = max(planar_faces, key=lambda face: face.Area)
        normal = reference_face.normalAt(0.0, 0.0)
        if normal.Length <= SheetMetalTools.smEpsilon:
            continue
        normal.normalize()
        rotation = FreeCAD.Rotation(normal, z_axis)
        link.Placement = FreeCAD.Placement(FreeCAD.Vector(), rotation)
        link.Document.recompute()
        bounds = link.Shape.BoundBox
        items.append(
            {
                "link": link,
                "rotation": rotation,
                "bounds": bounds,
                "width": bounds.XLength,
                "height": bounds.YLength,
            }
        )

    if not items:
        return []

    # A compact deterministic shelf layout. Sorting tall items first avoids
    # many poor row breaks while keeping the algorithm predictable and fast.
    items.sort(key=lambda item: (-item["height"], -item["width"], item["link"].Name))
    packed_area = sum(
        (item["width"] + spacing) * (item["height"] + spacing)
        for item in items
    )
    row_limit = max(
        max(item["width"] for item in items),
        math.sqrt(packed_area) * 1.35,
    )
    cursor_x = 0.0
    cursor_y = 0.0
    row_height = 0.0
    placements = []
    for item in items:
        width = item["width"]
        height = item["height"]
        if cursor_x > 0.0 and cursor_x + width > row_limit:
            cursor_x = 0.0
            cursor_y += row_height + spacing
            row_height = 0.0
        bounds = item["bounds"]
        base = FreeCAD.Vector(
            cursor_x - bounds.XMin,
            cursor_y - bounds.YMin,
            -bounds.ZMin,
        )
        item["link"].Placement = FreeCAD.Placement(base, item["rotation"])
        placements.append(item["link"])
        cursor_x += width + spacing
        row_height = max(row_height, height)

    for document in documents:
        document.recompute()
    return placements


def _isUnfoldObject(obj):
    """Return True only for real Unfold features, never presentation links.

    App::Link forwards properties from its target.  Duck-typing only for
    ``baseObject`` and ``UnfoldSketches`` therefore makes a flat-pattern link
    look like another Unfold and causes a new link-to-link object on every
    workspace activation.
    """
    return (
        obj.TypeId != "App::Link"
        and hasattr(obj, "baseObject")
        and hasattr(obj, "UnfoldSketches")
    )


def _matching_planar_face_name(source_face, target_shape):
    """Find the descendant of a selected planar face in a later feature shape."""
    if source_face is None or not isinstance(source_face.Surface, Part.Plane):
        return None
    best_name = None
    best_overlap = 0.0
    for index, candidate in enumerate(target_shape.Faces, 1):
        if not isinstance(candidate.Surface, Part.Plane):
            continue
        try:
            overlap = source_face.common(candidate).Area
        except Part.OCCError:
            continue
        if overlap > best_overlap:
            best_overlap = overlap
            best_name = "Face{}".format(index)
    if best_overlap <= SheetMetalTools.smEpsilon:
        return None
    return best_name


def _largest_planar_face_name(shape):
    """Return a stable broad sheet face for recovery from an invalid source."""
    candidates = [
        (face.Area, "Face{}".format(index))
        for index, face in enumerate(shape.Faces, 1)
        if isinstance(face.Surface, Part.Plane)
    ]
    return max(candidates, default=(0.0, None))[1]


def retargetUnfoldsToPartTip(sheet_metal_part, new_tip, previous_tip=None):
    """Move part-following Unfold sources to a newly-created Face feature.

    An Unfold selected from a feature inside a PartDesign Body is stored as a
    LinkSub to the Body, qualified by the feature name (for example
    ``ShapedFlange.Face4``).  That deliberately stable link otherwise remains
    on the old cumulative feature when another Face is appended to the part.
    """
    if sheet_metal_part is None or new_tip is None or new_tip.Shape.isNull():
        return []
    history = {}
    current = previous_tip
    while current is not None and current.Name not in history:
        history[current.Name] = current
        current = getattr(current, "PreviousFeature", None)
    updated = []
    for unfold in sheet_metal_part.Document.Objects:
        if not _isUnfoldObject(unfold):
            continue
        owner = unfold
        while owner is not None and owner is not sheet_metal_part:
            owner = owner.getParentGeoFeatureGroup() or owner.getParentGroup()
        if owner is not sheet_metal_part:
            continue
        base_link, sub_names = unfold.baseObject
        if base_link is None or not sub_names:
            continue
        if "FollowPartTip" not in unfold.PropertiesList:
            SheetMetalTools.smAddBoolProperty(
                unfold,
                "FollowPartTip",
                translate(
                    "SheetMetal",
                    "Keep this flat pattern linked to the owning part's latest feature",
                ),
                True,
                "Parameters",
            )
        if not unfold.FollowPartTip:
            continue

        sub_name = sub_names[0]
        source_feature = base_link
        source_element = sub_name
        qualified = "." in sub_name
        invalid_body_prefix = False
        if qualified:
            feature_name, source_element = sub_name.split(".", 1)
            source_feature = unfold.Document.getObject(feature_name)
            source_body = (
                source_feature.getParentGeoFeatureGroup()
                if source_feature is not None
                else None
            )
            # Recover LinkSubs whose feature prefix came from another Body.
            # The element suffix still identifies the user's selected A-face
            # on the previous tip in that Body.
            if (
                base_link.TypeId == "PartDesign::Body"
                and source_body is not base_link
            ):
                invalid_body_prefix = True
                source_feature = previous_tip
        if source_feature is None or source_feature.Name not in history:
            continue
        if invalid_body_prefix:
            target_element = _largest_planar_face_name(new_tip.Shape)
        else:
            try:
                source_face = source_feature.Shape.getElement(source_element)
            except (Part.OCCError, RuntimeError):
                continue
            target_element = _matching_planar_face_name(source_face, new_tip.Shape)
        if target_element is None:
            continue
        if qualified:
            unfold.baseObject = (
                base_link,
                ["{}.{}".format(new_tip.Name, target_element)],
            )
        else:
            unfold.baseObject = (new_tip, [target_element])
        unfold.touch()
        updated.append(unfold)
    return updated


def migrateDocumentUnfoldPartTips(doc):
    """Retarget saved part-following Unfolds that predate the latest Face."""
    updated = []
    for candidate in doc.Objects:
        if not (
            candidate.TypeId == "App::Part"
            and getattr(candidate, "SheetMetalType", None) == "Part"
            and hasattr(candidate, "Tip")
        ):
            continue
        tip = doc.getObject(candidate.Tip) if candidate.Tip else None
        if tip is None:
            continue
        updated.extend(
            retargetUnfoldsToPartTip(
                candidate, tip, getattr(tip, "PreviousFeature", None)
            )
        )
    return updated


class _UnfoldPartTipObserver:
    """Update saved flat patterns when a document with newer Faces activates."""

    def slotActivateDocument(self, doc):
        updated = migrateDocumentUnfoldPartTips(doc)
        if any(not getattr(unfold, "ManualRecompute", False) for unfold in updated):
            doc.recompute()


if "_unfold_part_tip_observer" not in globals():
    _unfold_part_tip_observer = _UnfoldPartTipObserver()
    FreeCAD.addDocumentObserver(_unfold_part_tip_observer)
    for _open_document in FreeCAD.listDocuments().values():
        if migrateDocumentUnfoldPartTips(_open_document):
            _open_document.recompute()


###################################################################################################
# Object class
###################################################################################################

class SMUnfold:
    """Class object for the unfold command."""

    def __init__(self, obj, selobj, sel_elements):
        """Add wall or Wall with radius bend."""
        selobj, sel_elements = SheetMetalTools.smUpdateLinks(obj, selobj, sel_elements)
        SheetMetalTools.smAddProperty(obj,
            "App::PropertyLinkSub",
            "baseObject",
            translate("App::Property", "Base Object"),
            (selobj, sel_elements),
        )
        self.addVerifyProperties(obj)
        SheetMetalTools.taskRestoreDefaults(obj, smUnfoldDefaultVars)
        sheet_metal_part = SheetMetalMaterial.findSheetMetalPart(selobj)
        if sheet_metal_part is not None and hasattr(sheet_metal_part, "KFactor"):
            obj.KFactor = float(sheet_metal_part.KFactor)
        # Setup transient properties.
        self.SketchColor = GENSKETCHCOLOR
        self.InternalColor = OUTLINESKETCHCOLOR
        self.BendLineColor = BENDLINESKETCHCOLOR
        self.BendLabelColor = BENDLABELCOLOR
        self.CutSketchColor = BENDCUTSKETCHCOLOR
        self.UnfoldTransparency = 0
        self.ExportType = "dxf"
        self.visibleSketches = []
        SheetMetalTools.taskRestoreDefaults(self, smUnfoldNonSavedDefaultVars)
        obj.Proxy = self
        self.UnfoldSketches = []

    def addVerifyProperties(self, obj):
        SheetMetalTools.smAddProperty(obj,
            "App::PropertyFloatConstraint",
            "KFactor",
            translate("SheetMetal", "Manual K-Factor value"),
            (0.4, 0.0, 2.0, 0.01),
        )
        SheetMetalTools.smAddEnumProperty(obj,
            "KFactorStandard",
            translate("SheetMetal", "K-Factor standard"),
            ["ansi", "din"],
            "ansi",
        )
        SheetMetalTools.smAddProperty(obj,
            "App::PropertyString",
            "MaterialSheet",
            translate("SheetMetal", "Material definition sheet"),
            "_manual",
            readOnly=True,
        )
        SheetMetalTools.smAddBoolProperty(obj,
            "ManualRecompute",
            translate("SheetMetal", "If set, object recomputation will be done on demand only"),
            False,
        )
        SheetMetalTools.smAddBoolProperty(
            obj,
            "FollowPartTip",
            translate(
                "SheetMetal",
                "Keep this flat pattern linked to the owning part's latest feature",
            ),
            True,
        )
        SheetMetalTools.smAddBoolProperty(obj,
            "GenerateSketch",
            translate("SheetMetal", "Generate unfold sketch"),
            False,
        )
        SheetMetalTools.smAddBoolProperty(obj,
            "SeparateSketchLayers",
            translate(
                "SheetMetal",
                "Generate separated unfold sketches for outline, inner lines and bend lines",
            ),
            False,
        )
        SheetMetalTools.smAddProperty(obj,
            "App::PropertyStringList",
            "UnfoldSketches",
            translate("SheetMetal", "Generated sketches"),
            None,
            "Hidden",
            attribs=8,  # Output only - no recompute if changed
        )
        SheetMetalTools.smAddBoolProperty(obj,
            "ShowBendAngles",
            translate("SheetMetal", "Show bend angles on the unfold sketch"),
            True,
        )
        SheetMetalTools.smAddLengthProperty(obj,
                "FontSize",
                translate("App::Property", "Font size for bend angle labels"),
                2.0)
        SheetMetalTools.smAddBoolProperty(obj,
            "GenerateBendCuts",
            translate("SheetMetal",
                "Generate laser bend-relief (hinge) cuts along the bend lines"),
            False,
        )
        SheetMetalTools.smAddLengthProperty(obj,
                "BendCutMaxMaterial",
                translate("SheetMetal",
                    "Maximum length of an uncut material bridge in the relief pattern"),
                8.0)
        SheetMetalTools.smAddLengthProperty(obj,
                "BendCutMaxCut",
                translate("SheetMetal",
                    "Maximum length of a single cut segment in the relief pattern"),
                3.0)
        SheetMetalTools.smAddLengthProperty(obj,
                "BendCutEdgeOffset",
                translate("SheetMetal",
                    "Margin left uncut at each end of the bend line"),
                5.0)
        SheetMetalTools.smAddEnumProperty(obj,
            "BendCutShapeMode",
            translate("SheetMetal", "Relief cut profile source"),
            ["straight", "sketch"],
            "straight",
        )
        SheetMetalTools.smAddProperty(obj,
            "App::PropertyLink",
            "BendCutProfileSketch",
            translate("SheetMetal",
                "Sketch defining a custom relief cut profile (e.g. dogbone, wave, "
                "chevron), tiled along each cut segment. Only used when "
                "'BendCutShapeMode' is 'sketch'"),
            None,
        )
        # SheetMetalTools.smAddProperty(
        #     obj,
        #     "App::PropertyBool",
        #     "DetachFromBody",
        #     translate
        #     ( "SheetMetal", "Make unfolded shape independent of the object's body"),
        #     False
        # )

    def getElementMapVersion(self, _fp, ver, _prop, restored):
        if not restored:
            return smElementMapVersion + ver
        return None

    def onChanged(self, obj, prop):
        if prop == "Visibility":
            isVisible = obj.Visibility
            visibleSketches = obj.Proxy.visibleSketches if isVisible else []
            for sketchName in obj.UnfoldSketches:
                sketch = obj.Document.getObject(sketchName)
                if sketch is not None:
                    if isVisible and sketchName in visibleSketches:
                        sketch.Visibility = True
                    elif not isVisible:
                        if sketch.Visibility:
                            visibleSketches.append(sketchName)
                            sketch.Visibility = False
            if not isVisible:
                obj.Proxy.visibleSketches = visibleSketches

    def getBendCutProfile(self, obj):
        """Return the normalized (tileable) relief profile wire to use for
        bend-relief cuts, or None to use plain straight cuts.

        Never raises: an invalid/missing profile sketch just falls back to
        straight cuts (with a logged warning), so a recompute never fails
        because of a bad selection here.
        """
        if obj.BendCutShapeMode != "sketch":
            return None
        sketch = obj.BendCutProfileSketch
        if sketch is None:
            SMLogger.warning(translate("SheetMetal",
                "Bend relief shape is set to 'From sketch' but no profile "
                "sketch is selected; using straight cuts instead.\n"))
            return None
        valid, msg = SheetMetalBendCuts.validate_profile_sketch(sketch)
        if not valid:
            SMLogger.warning(translate("SheetMetal",
                "Bend relief profile sketch is not usable ({}); using "
                "straight cuts instead.\n").format(msg))
            return None
        return SheetMetalBendCuts.normalize_profile_sketch(sketch)

    def newUnfolder(self, obj, baseObject, baseFace):
        """Use new unfolder system."""
        FreeCAD.Console.PrintMessage("Using V2 unfolding system\n")
        if obj.MaterialSheet in ["_manual", "_none"]:
            bac = BendAllowanceCalculator.from_single_value(obj.KFactor, obj.KFactorStandard)
        else:
            print("Using MDS:", obj.MaterialSheet)
            sheet = FreeCAD.ActiveDocument.getObject(obj.MaterialSheet)
            if sheet is None:
                sheet = FreeCAD.ActiveDocument.getObjectsByLabel(obj.MaterialSheet)[0]
            bac = BendAllowanceCalculator.from_spreadsheet(sheet)
        sel_face, unfolded_shape, bend_lines, root_normal, bend_infodata = SheetMetalNewUnfolder.getUnfold(
            bac, baseObject, baseFace
        )

        sketches = []
        if obj.GenerateSketch and unfolded_shape is not None:
            label_infodata = bend_infodata if obj.ShowBendAngles else []

            # Bend-relief cuts are a pure post-process of `bend_infodata`,
            # computed *before* it gets filtered above for label display
            # purposes - the two are independent switches. This never
            # touches `bend_lines`/`unfolded_shape`/fold geometry itself.
            #
            # IMPORTANT (coordinate frame): `bend_infodata[i].line` lives
            # in the "flattened, origin-aligned" frame that `getUnfold()`
            # aligns everything to internally (call it frame A). The
            # `bend_lines` *parameter* that `getUnfoldSketches()` expects,
            # on the other hand, is `bend_lines` as *returned* by
            # `getUnfold()`, which has already been transformed back into
            # the raw "in-place" frame (frame B) - `getUnfoldSketches()`
            # re-aligns frame B to a (newly, independently recomputed)
            # origin-aligned frame A' via its own `sketch_align_transform`
            # and applies it once, forward, to whatever it's given in
            # that parameter. Frame A and frame A' share the same
            # rotation (both derived from the same root/selected face)
            # but can differ in translation, since they're computed from
            # the bounding boxes of two different shapes (the full
            # unfold's sketch lines vs. just the outer wire re-extracted
            # from `unfolded_shape`).
            #
            # So: geometry already in frame A (like our relief cuts, or
            # like `bend_labels` below) must *not* be hop through another
            # forward transform meant for frame-B data, or it gets
            # transformed twice. `getUnfoldSketches()` itself sidesteps
            # this for bend labels by pre-applying the *inverse* of its
            # transform before merging them in, so the later forward pass
            # cancels back out. We do the same for the merged-sketch
            # substitution below.
            bend_lines_for_sketch = bend_lines
            extra_cut_layer_edges = None
            if obj.GenerateBendCuts and bend_infodata:
                profile = self.getBendCutProfile(obj)
                cut_compound, fallback_edges = SheetMetalBendCuts.build_relief_cuts(
                    bend_infodata,
                    obj.BendCutMaxCut.Value,
                    obj.BendCutMaxMaterial.Value,
                    obj.BendCutEdgeOffset.Value,
                    profile,
                )
                if fallback_edges:
                    # Bends too short for the requested pattern keep their
                    # plain solid bend line instead of being dropped.
                    cut_compound = Part.makeCompound([cut_compound, Part.makeCompound(fallback_edges)])

                if SheetMetalBendCuts.DEBUG:
                    FreeCAD.Console.PrintMessage(
                        f"[BendCuts] bend_infodata: {len(bend_infodata)} bend(s), "
                        f"lengths={[round(bi.line.Length, 3) for bi in bend_infodata]}\n")
                    FreeCAD.Console.PrintMessage(
                        f"[BendCuts] cut_compound (frame A, pre-fix) BoundBox={cut_compound.BoundBox}\n")

                if obj.SeparateSketchLayers:
                    # Keep the existing dashed "_Sketch_Bends" layer as-is
                    # and add the cut pattern as its own extra layer, built
                    # directly (bypassing getUnfoldSketches' internal
                    # merge/transform), exactly like the existing
                    # "_Sketch_Bend_Labels" layer already does with its
                    # own frame-A `bend_labels` data - no extra transform
                    # needed here.
                    extra_cut_layer_edges = cut_compound
                else:
                    # Single merged sketch: substitute the real cut
                    # geometry for the plain bend line so the exported
                    # sketch shows actual segmented cuts, not a solid
                    # line on top of them. `bend_lines_for_sketch` is
                    # about to receive one forward alignment transform
                    # from `getUnfoldSketches()` (meant for frame-B data),
                    # so pre-cancel it here since our data is already in
                    # frame A - mirrors how `bend_labels_transformed` is
                    # built inside `getUnfoldSketches()`.
                    cut_sketch_profile, _cut_inner, _cut_holes = SheetMetalNewUnfolder.SketchExtraction.extract_manually(
                        unfolded_shape, root_normal)
                    merge_align_transform = SheetMetalNewUnfolder.SketchExtraction.move_to_origin(
                        cut_sketch_profile, sel_face)
                    bend_lines_for_sketch = cut_compound.transformed(merge_align_transform.inverse())

                    if SheetMetalBendCuts.DEBUG:
                        FreeCAD.Console.PrintMessage(
                            f"[BendCuts] merge_align_transform (T)={merge_align_transform}\n")
                        FreeCAD.Console.PrintMessage(
                            f"[BendCuts] bend_lines (frame B, unmodified) BoundBox={bend_lines.BoundBox}\n")
                        FreeCAD.Console.PrintMessage(
                            f"[BendCuts] cut_compound after T^-1 correction BoundBox="
                            f"{bend_lines_for_sketch.BoundBox}\n")

            sketches = SheetMetalNewUnfolder.getUnfoldSketches(
                obj.Label,
                sel_face,
                unfolded_shape, 
                bend_lines_for_sketch,
                root_normal, 
                obj.UnfoldSketches,
                obj.SeparateSketchLayers,
                obj.Proxy.SketchColor,
                obj.Proxy.BendLineColor,
                obj.Proxy.InternalColor,
                bend_infodata=label_infodata,
                bend_label_color=obj.Proxy.BendLabelColor,
                bend_label_size=obj.FontSize,
            )
            if extra_cut_layer_edges is not None and extra_cut_layer_edges.Edges:
                cut_sketch = SheetMetalNewUnfolder.SketchExtraction.edges_to_sketch_object(
                    extra_cut_layer_edges.Edges,
                    f"{obj.Label}_Sketch_BendCuts",
                    obj.UnfoldSketches,
                    obj.Proxy.CutSketchColor,
                )
                sketches.append(cut_sketch)
        return unfolded_shape, sketches

    def oldUnfolder(self, obj, baseObject, baseFace):
        """Use old unfolder system.

        Note: Bend-relief cuts (`GenerateBendCuts`) are only supported
        through the new unfolder, which is the only one that provides
        per-bend `BendInfo` data. The task panel disables the bend-cuts
        controls when the old unfolder is active.
        """
        FreeCAD.Console.PrintMessage("Using V1 unfolding system\n")
        kFactorTable = {1: obj.KFactor}
        if obj.MaterialSheet != "_manual" and obj.MaterialSheet != "_none":
            lookupTable = SheetMetalKfactor.KFactorLookupTable(obj.MaterialSheet)
            kFactorTable = lookupTable.k_factor_lookup

        shape, foldComp, norm, _thename, _err_cd, _fSel, _obN = SheetMetalUnfolder.getUnfold(
                kFactorTable, baseObject, baseFace, obj.KFactorStandard)

        sketches = []
        if obj.GenerateSketch and shape is not None:
            sketches = SheetMetalUnfolder.getUnfoldSketches(
                obj.Label,
                shape,
                foldComp.Edges,
                norm,
                obj.UnfoldSketches,
                obj.SeparateSketchLayers,
                obj.Proxy.SketchColor,
                bendSketchColor=obj.Proxy.BendLineColor,
                internalSketchColor=obj.Proxy.InternalColor,
            )
        return shape, sketches

    def execute(self, fp):
        """Print a short message when doing a recomputation.

        Note:
            This method is mandatory.

        """
        self.addVerifyProperties(fp)
        baseObj, baseFace = SheetMetalTools.smGetSubElementName(fp.baseObject[1][0])
        if baseObj is None:
            baseObj = fp.baseObject[0]
        if not NewUnfolderAvailable or SheetMetalTools.use_old_unfolder():
            shape, sketches = self.oldUnfolder(fp, baseObj, baseFace)
        else:
            shape, sketches = self.newUnfolder(fp, baseObj, baseFace)

        fp.Shape = shape
        parent = SheetMetalTools.smGetParentBody(fp)
        sketchList = []
        for sketch in sketches:
            if sketch is not None:
                sketchList.append(sketch.Name)
                if parent is not None and SheetMetalTools.smGetParentBody(sketch) is None:
                    parent.addObject(sketch)

        # Remove non-used sketches.
        for prop in fp.UnfoldSketches:
            if not prop in sketchList:
                item = fp.Document.getObject(prop)
                if item is not None:
                    fp.Document.removeObject(item.Name)

        fp.UnfoldSketches = sketchList
        SheetMetalTools.smRemoveFromRecompute(fp)


###################################################################################################
# Gui code
###################################################################################################

if SheetMetalTools.isGuiLoaded():

    from PySide import QtGui, QtCore

    Gui = FreeCAD.Gui

    mds_help_url = "https://github.com/shaise/FreeCAD_SheetMetal#material-definition-sheet"
    last_selected_mds = "none"


    ###############################################################################################
    # View Provider
    ###############################################################################################

    class SMUnfoldViewProvider(SheetMetalTools.SMViewProvider):
        """Part / Part WB style ViewProvider."""

        def getIcon(self):
            return os.path.join(SheetMetalTools.icons_path, "SheetMetal_Unfold.svg")

        def claimChildren(self):
            objs = []
            for itemName in self.Object.UnfoldSketches:
                item = self.Object.Document.getObject(itemName)
                if item is not None:
                    objs.append(item)
            return objs

        def getTaskPanel(self, obj):
            return SMUnfoldTaskPanel(obj)


    ###############################################################################################
    # Task Panel
    ###############################################################################################

    class SMUnfoldTaskPanel:
        """Task Panel for the unfold function."""

        def __init__(self, obj):
            QtCore.QDir.addSearchPath("Icons", SheetMetalTools.icons_path)
            self.obj = obj
            self.form = SheetMetalTools.taskLoadUI("UnfoldOptions.ui")

            # Make sure all properties are added.
            obj.Proxy.addVerifyProperties(obj)

            self.setupUi(obj)

        def _boolToState(self, bool):
            return QtCore.Qt.Checked if bool else QtCore.Qt.Unchecked

        def _isManualKSelected(self):
            return self.form.availableMds.currentIndex() == (self.form.availableMds.count() - 1)

        def _isNoMdsSelected(self):
            return self.form.availableMds.currentIndex() == 0

        def _updateSelectedMds(self):
            count = self.form.availableMds.count()
            currentIndex = self.form.availableMds.currentIndex()
            if currentIndex == 0:
                newsheet = "_none"
            elif currentIndex == count - 1:
                newsheet = "_manual"
            else:
                newsheet = self.form.availableMds.currentText()
            if newsheet != self.obj.MaterialSheet:
                self.obj.MaterialSheet = newsheet
                self.recomputeObject()

        def _getLastSelectedMdsIndex(self):
            materialSheet = self.obj.MaterialSheet
            if materialSheet == "_none":
                return 0
            elif materialSheet == "_manual":
                return self.form.availableMds.count() - 1
            for i in range(self.form.availableMds.count()):
                if self.form.availableMds.itemText(i) == materialSheet:
                    return i
            return -1

        def checkKFactorValid(self):
            if self.obj.MaterialSheet == "_none":
                msg = translate("Logger",
                                "Unfold operation needs to know K-factor value(s) to be used.")
                SMLogger.warning(msg)
                msg += translate(
                    "QMessageBox",
                    "<ol>\n"
                    "<li>Either select 'Manual K-factor'</li>\n"
                    "<li>Or use a <a href='{}'>Material Definition Sheet</a></li>\n"
                    "</ol>",
                ).format(mds_help_url)
                SheetMetalTools.smWarnDialog(msg)
                return False
            return True

        def setupUi(self, obj):
            self.updateKFactor(True)
            if obj.Proxy.ExportType == "dxf":
                self.form.dxfExport.setChecked(True)
            else:
                self.form.svgExport.setChecked(True)
            self.SketchColor = GENSKETCHCOLOR
            self.InternalColor = OUTLINESKETCHCOLOR
            self.BendLineColor = BENDLINESKETCHCOLOR
            self.BendLabelColor = BENDLABELCOLOR
            self.CutSketchColor = BENDCUTSKETCHCOLOR
            # Bend-relief cuts need per-bend BendInfo data, only available
            # through the new unfolder. Computed early: `chkSketchChange`
            # (invoked below as a side effect of `taskConnectCheck`)
            # depends on it.
            self.bendCutsAvailable = NewUnfolderAvailable and not SheetMetalTools.use_old_unfolder()
            self.populateMdsList()
            SheetMetalTools.taskConnectSelectionSingle(self.form.pushFace, self.form.txtFace, obj,
                                                       "baseObject", ["Face"])
            SheetMetalTools.taskConnectColor(obj.Proxy, self.form.genColor, "SketchColor")
            SheetMetalTools.taskConnectColor(obj.Proxy, self.form.bendColor, "BendLineColor")
            SheetMetalTools.taskConnectColor(obj.Proxy, self.form.internalColor, "InternalColor")
            SheetMetalTools.taskConnectColor(obj.Proxy, self.form.bendLabelColor, "BendLabelColor")
            SheetMetalTools.taskConnectColor(obj.Proxy, self.form.cutColor, "CutSketchColor")
            SheetMetalTools.taskConnectCheck(obj, self.form.chkSketch, "GenerateSketch",
                                             self.chkSketchChange)
            SheetMetalTools.taskConnectCheck(obj, self.form.chkSeparate, "SeparateSketchLayers",
                                             self.chkSketchChange)
            SheetMetalTools.taskConnectCheck(obj, self.form.chkAngleLabels, "ShowBendAngles",
                                             self.chkSketchChange)
            SheetMetalTools.taskConnectCheck(obj, self.form.chkBendCuts, "GenerateBendCuts",
                                             self.chkBendCutsChange)
            SheetMetalTools.taskConnectSpin(obj, self.form.maxMaterialDist, "BendCutMaxMaterial")
            SheetMetalTools.taskConnectSpin(obj, self.form.maxCutDist, "BendCutMaxCut")
            SheetMetalTools.taskConnectSpin(obj, self.form.cutEdgeOffset, "BendCutEdgeOffset")
            SheetMetalTools.taskConnectSelectionSingle(self.form.pushCutSketch,
                                                       self.form.txtCutSketch, obj,
                                                       "BendCutProfileSketch",
                                                       ("Sketcher::SketchObject", []))
            SheetMetalTools.taskConnectCheck(obj, self.form.chkManualUpdate, "ManualRecompute",
                                             self.chkManualChanged)
            SheetMetalTools.taskConnectCheck(obj, self.form.chkManualUpdate, "ManualRecompute",
                                             self.chkManualChanged)
            SheetMetalTools.taskConnectSpin(obj, self.form.floatKFactor, "KFactor")
            SheetMetalTools.taskConnectSpin(obj.Proxy, self.form.transSpin, "UnfoldTransparency",
                                            bindFunction=False)
            SheetMetalTools.taskConnectSpin(obj, self.form.fontSize, "FontSize")
            self.form.pushUnfold.clicked.connect(self.unfoldPressed)
            self.form.pushExport.clicked.connect(self.doExport)
            self.form.availableMds.currentIndexChanged.connect(self.availableMdsChacnge)
            self.form.dxfExport.toggled.connect(self.exportTypeChanged)
            self.form.kfactorAnsi.toggled.connect(self.kfactorStdChanged)
            self.form.radioSketchCut.toggled.connect(self.cutShapeChanged)

            if obj.BendCutShapeMode == "sketch":
                self.form.radioSketchCut.setChecked(True)
            else:
                self.form.radioStraightCut.setChecked(True)

            if not self.bendCutsAvailable:
                self.form.chkBendCuts.setChecked(False)
                self.form.chkBendCuts.setToolTip(translate("SheetMetal",
                    "Bend relief cuts require the new unfolder (networkx), "
                    "which is not currently active."))

            self.availableMdsChacnge()
            self.chkSketchChange()
            # self.form.update()

        def updateKFactor(self, updateCheck):
            if self.obj.KFactorStandard == "ansi":
                if updateCheck:
                    self.form.kfactorAnsi.setChecked(True)
                self.form.floatKFactor.setProperty("value", self.obj.KFactor)
                self.form.floatKFactor.setProperty("maximum", 1.0)
            else:
                if updateCheck:
                    self.form.kfactorDin.setChecked(True)
                self.form.floatKFactor.setProperty("maximum", 2.0)
                self.form.floatKFactor.setProperty("value", self.obj.KFactor)

        def kfactorStdChanged(self):
            if self.form.kfactorAnsi.isChecked():
                self.obj.KFactorStandard = "ansi"
                self.obj.KFactor /= 2.0
            else:
                self.obj.KFactorStandard = "din"
                self.obj.KFactor *= 2.0
            self.updateKFactor(False)

        def recomputeObject(self, closeTask=False):
            SheetMetalTools.smForceRecompute = True
            if closeTask:
                SheetMetalTools.taskAccept(self)
            else:
                FreeCAD.ActiveDocument.recompute()
            SheetMetalTools.smForceRecompute = False
            # if len(self.obj.UnfoldSketches) > 0:
            #     FreeCAD.ActiveDocument.recompute()

        def checkBendCutsValid(self):
            if not (self.form.chkBendCuts.isChecked() and self.form.chkBendCuts.isEnabled()):
                return True
            if self.form.radioStraightCut.isChecked():
                return True
            sketch = self.obj.BendCutProfileSketch
            if sketch is None:
                SheetMetalTools.smWarnDialog(translate("SheetMetal",
                    "Bend relief shape is set to 'From sketch'.\n"
                    "Please select a profile sketch, or switch back to 'Straight'."))
                return False
            valid, msg = SheetMetalBendCuts.validate_profile_sketch(sketch)
            if not valid:
                SheetMetalTools.smWarnDialog(translate("SheetMetal",
                    "The selected bend relief profile sketch is not usable:\n{}"
                ).format(msg))
                return False
            return True

        def accept(self):
            if not self.checkKFactorValid():
                return False
            if not self.checkBendCutsValid():
                return False
            self.recomputeObject(True)
            self.obj.ViewObject.Transparency = self.obj.Proxy.UnfoldTransparency
            SheetMetalTools.taskSaveDefaults(self.obj, smUnfoldDefaultVars)
            SheetMetalTools.taskSaveDefaults(self.obj.Proxy, smUnfoldNonSavedDefaultVars)
            _show_flat_pattern_workspace(self.obj.Document, capture=False)
            # self._updateSelectedMds()
            # kFactorTable = self.getKFactorTable()
            return None

        def reject(self):
            _show_formed_workspace(self.obj.Document)
            FreeCAD.ActiveDocument.abortTransaction()
            Gui.Control.closeDialog()
            FreeCAD.ActiveDocument.recompute()

        def doExport(self):
            smUnfoldExportSketches(self.obj)

        def populateMdsList(self):
            sheetnames = SheetMetalKfactor.getSpreadSheetNames()
            self.form.availableMds.clear()

            self.form.availableMds.addItem(translate("SheetMetal", "Please select"))
            for mds in sheetnames:
                if mds.Label.startswith("material_"):
                    self.form.availableMds.addItem(mds.Label)
            self.form.availableMds.addItem(translate("SheetMetal", "Manual K-Factor"))

            selMdsIndex = self._getLastSelectedMdsIndex()
            if selMdsIndex > 0:
                self.form.availableMds.setCurrentIndex(selMdsIndex)
            elif len(sheetnames) == 1:
                self.form.availableMds.setCurrentIndex(1)
            elif engineering_mode_enabled() or len(sheetnames) > 1:
                self.form.availableMds.setCurrentIndex(0)
            else:
                self.form.availableMds.setCurrentIndex(1)

        def chkSketchChange(self, _value=None):
            genSketch = self.form.chkSketch.isChecked()
            self.form.chkSeparate.setEnabled(genSketch)
            self.form.genColor.setEnabled(genSketch)
            splitSketch = genSketch and self.form.chkSeparate.isChecked()
            self.form.bendColor.setEnabled(splitSketch)
            self.form.internalColor.setEnabled(splitSketch)
            unfoldUpdated = not self.obj in SheetMetalTools.smObjectsToRecompute
            exportEnabled = genSketch and len(self.obj.UnfoldSketches) > 0 and unfoldUpdated
            self.form.groupExport.setEnabled(exportEnabled)
            # Bend cuts only make sense when a sketch is actually being
            # generated at all - the whole sub-panel collapses with it.
            # (self.bendCutsAvailable is False when the old unfolder is
            # active; bend cuts stay disabled regardless of genSketch.)
            self.form.chkBendCuts.setEnabled(genSketch and self.bendCutsAvailable)
            self.chkBendCutsChange()

        def chkBendCutsChange(self, _value=None):
            genCuts = self.form.chkBendCuts.isChecked() and self.form.chkBendCuts.isEnabled()
            self.form.groupBendCutsBody.setVisible(genCuts)
            useSketch = genCuts and self.form.radioSketchCut.isChecked()
            self.form.groupCutShape.setVisible(useSketch)

        def cutShapeChanged(self, _value=None):
            self.obj.BendCutShapeMode = "sketch" if self.form.radioSketchCut.isChecked() else "straight"
            self.chkBendCutsChange()

        def exportTypeChanged(self):
            self.obj.Proxy.ExportType = "dxf" if self.form.dxfExport.isChecked() else "svg"

        def chkManualChanged(self, value):
            self.form.pushUnfold.setEnabled(value)

        def unfoldPressed(self):
            if not self.checkKFactorValid():
                return False
            if not self.checkBendCutsValid():
                return False
            self.recomputeObject()
            self.chkSketchChange()
            return None

        def availableMdsChacnge(self):
            self.form.groupManualFactor.setEnabled(self._isManualKSelected())
            self._updateSelectedMds()
            #self.form.kFactSpin.setEnabled(isManualK)


    ###############################################################################################
    # Commands
    ###############################################################################################

    def _containing_app_part(obj):
        """Return the App::Part that owns an object, including through a Body."""
        current = obj
        while current is not None:
            current = (
                current.getParentGeoFeatureGroup()
                or current.getParentGroup()
            )
            if current is not None and current.TypeId == "App::Part":
                return current
        return None


    def _place_unfold_in_source_part(new_obj, source_obj):
        owner_part = _containing_app_part(source_obj)
        if owner_part is not None:
            owner_part.addObject(new_obj)


    def _unfold_objects(doc):
        return [obj for obj in doc.Objects if _isUnfoldObject(obj)]


    def _flat_pattern_group(doc, create=False):
        group = doc.getObject("FlatPatterns")
        if group is None and create:
            group = doc.addObject("App::DocumentObjectGroup", "FlatPatterns")
            group.Label = translate("SheetMetal", "Flat Patterns")
            group.addProperty(
                "App::PropertyString", "WorkspaceType", "Flat Patterns",
                translate("App::Property", "Document view workspace type"),
            ).WorkspaceType = "FlatPatterns"
            group.setEditorMode("WorkspaceType", 1)
            group.addProperty(
                "App::PropertyStringList", "PreviousVisibility", "Flat Patterns",
                translate("App::Property", "Visibility state restored in formed view"),
            )
            group.setEditorMode("PreviousVisibility", 2)
            group.ViewObject.Visibility = False
        if group is not None:
            SheetMetalTools.smAddBoolProperty(
                group,
                "AutoArrange",
                translate(
                    "App::Property",
                    "Align flat patterns to XY and pack them without overlap",
                ),
                True,
                "Flat Pattern Layout",
            )
            SheetMetalTools.smAddLengthProperty(
                group,
                "LayoutSpacing",
                translate("App::Property", "Spacing between arranged flat patterns"),
                10.0,
                "Flat Pattern Layout",
            )
        return group


    def _flat_pattern_link(unfold_obj, create=False):
        if not _isUnfoldObject(unfold_obj):
            return None
        group = _flat_pattern_group(unfold_obj.Document, create)
        if group is None:
            return None
        for candidate in group.Group:
            if candidate.TypeId == "App::Link" and candidate.LinkedObject is unfold_obj:
                return candidate
        if not create:
            return None
        link = unfold_obj.Document.addObject("App::Link", "FlatPattern")
        link.LinkedObject = unfold_obj
        owner_part = _containing_app_part(unfold_obj)
        owner_label = owner_part.Label if owner_part is not None else unfold_obj.Label
        link.Label = translate("SheetMetal", "%1 Flat Pattern").replace(
            "%1", owner_label
        )
        group.addObject(link)
        link.ViewObject.Visibility = False
        return link


    def _capture_formed_visibility(doc, excluded=None):
        group = _flat_pattern_group(doc, True)
        excluded = set(excluded or [])
        flat_members = set(group.Group)
        entries = []
        for obj in doc.Objects:
            if obj is group or obj in flat_members or obj in excluded:
                continue
            if hasattr(obj, "ViewObject"):
                entries.append(
                    "{}={}".format(obj.Name, int(obj.ViewObject.Visibility))
                )
        group.PreviousVisibility = entries
        return group


    def _show_flat_pattern_workspace(doc, capture=True, excluded=None):
        group = _flat_pattern_group(doc, True)
        links = [
            _flat_pattern_link(unfold_obj, True)
            for unfold_obj in _unfold_objects(doc)
        ]
        if group.AutoArrange:
            arrangeFlatPatternLinks(links, group.LayoutSpacing.Value)
        if capture:
            _capture_formed_visibility(doc, excluded)
        flat_members = set(group.Group)
        for obj in doc.Objects:
            if obj is group or obj in flat_members:
                continue
            if hasattr(obj, "ViewObject"):
                obj.ViewObject.Visibility = False
        group.ViewObject.Visibility = True
        for link in links:
            link.ViewObject.Visibility = True
        Gui.activeDocument().activeView().fitAll()


    def _show_formed_workspace(doc):
        group = _flat_pattern_group(doc, False)
        if group is None:
            return
        group.ViewObject.Visibility = False
        for member in group.Group:
            member.ViewObject.Visibility = False
        for unfold_obj in _unfold_objects(doc):
            unfold_obj.ViewObject.Visibility = False
        for entry in group.PreviousVisibility:
            name, separator, value = entry.partition("=")
            obj = doc.getObject(name)
            if separator and obj is not None and hasattr(obj, "ViewObject"):
                obj.ViewObject.Visibility = value == "1"
        Gui.activeDocument().activeView().fitAll()

    class SMUnfoldCommandClass:
        """Unfold object."""

        def GetResources(self):
            __dir__ = os.path.dirname(__file__)
            iconPath = os.path.join(__dir__, "Resources", "icons")
            return {
                    # The name of a svg file available in the resources.
                    "Pixmap": os.path.join(iconPath, "SheetMetal_Unfold.svg"),
                    "MenuText": translate("SheetMetal", "Unfold"),
                    "Accel": "U",
                    "ToolTip": translate(
                        "SheetMetal",
                        "Flatten folded sheet metal object.\n"
                        "1. Select flat face on sheetmetal shape.\n"
                        "2. Change parameters from task Panel to create "
                        "unfold Shape & Flatten drawing.",
                        ),
                    }

        def Activated(self):
            sel = Gui.Selection.getSelectionEx()[0]
            selobj = sel.Object
            selparent = SheetMetalTools.smGetParentBody(selobj)
            name = "Unfold" if selparent is None else f"{selparent.Name}_Unfold"
            label = "Unfold" if selparent is None else f"{selparent.Label}_Unfold"
            newObj, activeBody = SheetMetalTools.smCreateNewObject(selobj, name, False)
            if newObj is None:
                return
            newObj.Label = label
            _capture_formed_visibility(newObj.Document, [newObj])
            _place_unfold_in_source_part(newObj, selobj)
            SMUnfold(newObj, selobj, sel.SubElementNames)
            _flat_pattern_link(newObj, True)
            SMUnfoldViewProvider(newObj.ViewObject)
            SheetMetalTools.smAddNewObject(selobj, newObj, activeBody, SMUnfoldTaskPanel)

        def IsActive(self):
            if (len(Gui.Selection.getSelection()) != 1
                    or len(Gui.Selection.getSelectionEx()[0].SubElementNames) != 1
            ):
                return False
            selFace = Gui.Selection.getSelectionEx()[0].SubObjects[0]
            return isinstance(selFace.Surface, Part.Plane)


    class SMRecomputeUnfoldsCommandClass:
        """Recompute all unfold objects marked for manual recompute."""

        def GetResources(self):
            __dir__ = os.path.dirname(__file__)
            iconPath = os.path.join(__dir__, "Resources", "icons")
            return {
                    # The name of a svg file available in the resources.
                    "Pixmap": os.path.join(iconPath, "SheetMetal_UnfoldUpdate.svg"),
                    "MenuText": translate("SheetMetal", "Unfold Update"),
                    "Accel": "UU",
                    "ToolTip": translate(
                        "SheetMetal",
                        "Update all unfold objects.\n"
                        ),
                    }

        def Activated(self):
            SheetMetalTools.smForceRecompute = True
            for obj in list(SheetMetalTools.smObjectsToRecompute):
                obj.touch()
            FreeCAD.ActiveDocument.recompute()
            SheetMetalTools.smForceRecompute = False

        def IsActive(self):
            return len(SheetMetalTools.smObjectsToRecompute) > 0


    class SMUnfoldUnattendedCommandClass:
        """Unfold object."""

        def GetResources(self):
            __dir__ = os.path.dirname(__file__)
            iconPath = os.path.join(__dir__, "Resources", "icons")
            return {
                    # The name of a svg file available in the resources.
                    "Pixmap": os.path.join(iconPath, "SheetMetal_UnfoldUnattended.svg"),
                    "MenuText": translate("SheetMetal", "Unattended Unfold"),
                    "Accel": "U",
                    "ToolTip": translate(
                        "SheetMetal",
                        "Flatten folded sheet metal object with default options\n"
                        "1. Select flat face on sheetmetal shape.\n"
                        "2. Click this command to unfold the object with last used parameters.",
                        ),
                    }

        def Activated(self):
            sel = Gui.Selection.getSelectionEx()[0]
            selobj = sel.Object
            selparent = SheetMetalTools.smGetParentBody(selobj)
            name = "Unfold" if selparent is None else f"{selparent.Name}_Unfold"
            label = "Unfold" if selparent is None else f"{selparent.Label}_Unfold"
            newObj, activeBody = SheetMetalTools.smCreateNewObject(selobj, name, False)
            if newObj is None:
                return
            newObj.Label = label
            _capture_formed_visibility(newObj.Document, [newObj])
            _place_unfold_in_source_part(newObj, selobj)
            SMUnfold(newObj, selobj, sel.SubElementNames)
            _flat_pattern_link(newObj, True)
            SMUnfoldViewProvider(newObj.ViewObject)
            SheetMetalTools.smAddNewObject(selobj, newObj, activeBody)
            _show_flat_pattern_workspace(newObj.Document, capture=False)
            return

        def IsActive(self):
            if (len(Gui.Selection.getSelection()) != 1
                    or len(Gui.Selection.getSelectionEx()[0].SubElementNames) != 1
            ):
                return False
            selFace = Gui.Selection.getSelectionEx()[0].SubObjects[0]
            return isinstance(selFace.Surface, Part.Plane)


    class SMToggleFlatPatternWorkspaceCommandClass:
        """Switch between the saved formed view and linked flat patterns."""

        def GetResources(self):
            return {
                "Pixmap": os.path.join(
                    SheetMetalTools.icons_path,
                    "SheetMetal_FlatPatternWorkspace.svg",
                ),
                "MenuText": translate("SheetMetal", "Toggle Flat Pattern Workspace"),
                "ToolTip": translate(
                    "SheetMetal",
                    "Show only linked flat patterns, or restore the previous formed view.",
                ),
                "Checkable": True,
            }

        def Activated(self, checked=False):
            # FreeCAD passes the requested checked state to checkable commands.
            # The current group visibility remains the source of truth because
            # the command can also be invoked from Python without that argument.
            doc = FreeCAD.ActiveDocument
            group = _flat_pattern_group(doc, False)
            doc.openTransaction("FlatPatternWorkspace")
            if group is not None and group.ViewObject.Visibility:
                _show_formed_workspace(doc)
            else:
                _show_flat_pattern_workspace(doc)
            doc.commitTransaction()

        def IsActive(self):
            return (
                FreeCAD.ActiveDocument is not None
                and bool(_unfold_objects(FreeCAD.ActiveDocument))
            )

        def IsChecked(self):
            if FreeCAD.ActiveDocument is None:
                return False
            group = _flat_pattern_group(FreeCAD.ActiveDocument, False)
            return group is not None and group.ViewObject.Visibility


    Gui.addCommand("SheetMetal_UnattendedUnfold", SMUnfoldUnattendedCommandClass())
    Gui.addCommand("SheetMetal_Unfold", SMUnfoldCommandClass())
    Gui.addCommand("SheetMetal_UnfoldUpdate", SMRecomputeUnfoldsCommandClass())
    Gui.addCommand(
        "SheetMetal_ToggleFlatPatternWorkspace",
        SMToggleFlatPatternWorkspaceCommandClass(),
    )
