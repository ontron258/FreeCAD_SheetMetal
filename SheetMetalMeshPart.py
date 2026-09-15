########################################################################
# Copyright 2026 Adrian
# SPDX-License-Identifier: LGPL-2.0-or-later
########################################################################
"""Ownership and sheet representations of a welded mesh product."""

import FreeCAD as App
import Part
import SheetMetalTools

translate = App.Qt.translate


def is_mesh_part(obj):
    return obj is not None and getattr(obj, "MeshType", "") == "WeldedMesh"


def parent_part(obj):
    current = obj
    while current is not None:
        if current.TypeId == "App::Part":
            return current
        current = current.getParentGeoFeatureGroup() or current.getParentGroup()
    return None


def _body(obj):
    current = obj
    while current is not None:
        if current.TypeId == "PartDesign::Body":
            return current
        current = current.getParentGeoFeatureGroup()
    return None


def _property(obj, kind, name, text, default=None):
    if name not in obj.PropertiesList:
        obj.addProperty("App::Property" + kind, name, "Mesh", translate("App::Property", text))
        if default is not None:
            setattr(obj, name, default)
    obj.setEditorMode(name, 1)


def _move(obj, part):
    placement = obj.getGlobalPlacement() if hasattr(obj, "getGlobalPlacement") else None
    part.addObject(obj)
    if placement is not None:
        obj.Placement = part.getGlobalPlacement().inverse() * placement


def _history(source):
    result, seen = [], set()

    def visit(obj):
        if obj in seen or not obj.isDerivedFrom("Part::Feature"):
            return
        if obj.TypeId in ("App::Part", "PartDesign::Body"):
            return
        seen.add(obj)
        for dependency in obj.OutList:
            visit(dependency)
        result.append(obj)

    visit(source)
    return result


class SMMeshCarrier:
    """A Body base feature driven by an owned Part-workbench feature history."""

    def __init__(self, obj, source, face_name, thickness):
        _property(obj, "LinkGlobal", "Source", "Editable source feature for this sheet-metal Body", source)
        _property(obj, "String", "SourceFace", "Source side used for the envelope", face_name)
        _property(obj, "Length", "EnvelopeThickness", "Full welded envelope thickness", thickness)
        _property(obj, "String", "EnvelopeFace", "Current envelope reference face", "")
        _property(obj, "String", "LastError", "Envelope construction error", "")
        _property(obj, "LinkListGlobal", "SourceContainers", "Source placement dependencies", [])
        obj.setEditorMode("SourceContainers", 2)
        obj.Proxy = self

    def sync_containers(self, obj):
        own = set()
        current = obj.getParentGeoFeatureGroup()
        while current is not None:
            own.add(current)
            current = current.getParentGeoFeatureGroup()
        containers = []
        current = obj.Source.getParentGeoFeatureGroup() if obj.Source is not None else None
        while current is not None and current not in own:
            containers.append(current)
            current = current.getParentGeoFeatureGroup()
        if list(obj.SourceContainers) != containers:
            obj.SourceContainers = containers

    def execute(self, obj):
        from SheetMetalMeshGeometry import MeshSurfaceMap
        self.sync_containers(obj)
        source = obj.Source
        if source is None or source.Shape.isNull():
            obj.Shape = Part.Shape()
            return
        shape = source.Shape.transformed(source.Placement.toMatrix().inverse())
        try:
            mapping = MeshSurfaceMap(shape, obj.SourceFace, 0.5)
            delta = (obj.EnvelopeThickness.Value - mapping.thickness) / 2
            normal = mapping.root_face.normalAt(0, 0)
            point = mapping.root_face.CenterOfMass + normal * delta
            if abs(delta) > 1e-7:
                shell = Part.makeShell([patch["face"] for patch in mapping.patches])
                outer = shell.makeOffsetShape(delta, 1e-6)
                shape = outer.makeOffsetShape(-obj.EnvelopeThickness.Value, 1e-6, fill=True)
            if not shape.isValid() or len(shape.Solids) != 1:
                raise ValueError(translate("SheetMetal", "The full mesh envelope is not a valid sheet-metal solid."))
            faces = [(abs((face.CenterOfMass - point).dot(normal)), -face.Area, i)
                     for i, face in enumerate(shape.Faces, 1)
                     if isinstance(face.Surface, Part.Plane) and face.normalAt(0, 0).dot(normal) > 0.999]
            obj.EnvelopeFace = "Face" + str(min(faces)[2])
            obj.Shape = shape
            obj.LastError = ""
        except Exception as error:
            obj.Shape = Part.Shape()
            obj.LastError = str(error)
            return
        body = obj.getParentGeoFeatureGroup()
        obj.Placement = body.getGlobalPlacement().inverse() * source.getGlobalPlacement()

    def dumps(self):
        return None

    def loads(self, state):
        pass


def prepare_carrier(doc, source, face_name, thickness):
    """Consume a carrier into one product while retaining its editable history."""
    from SheetMetalShapedFlangeCmd import addSheetMetalPartProperties

    owner, body = parent_part(source), _body(source)
    # A second mesh made from an existing mesh gets its own modelling history.
    # Do not move the first product's inputs into the second product.
    history = _history(source) if body is None else []
    if is_mesh_part(owner) or any(is_mesh_part(parent_part(o)) for o in history):
        source = doc.copyObject(body or source, True)
        body = source if source.TypeId == "PartDesign::Body" else None
        history = _history(source) if body is None else []
        owner = None
    inputs = set(history + ([body] if body is not None else []))
    can_reuse = owner is not None and all(
        child in inputs or not hasattr(child, "Shape")
        or hasattr(child, "UnfoldSketches")
        for child in owner.Group
    )
    part = owner if can_reuse else doc.addObject("App::Part", "WeldedMeshPart")
    if part is not owner:
        part.Label = translate("SheetMetal", "Welded Mesh Part")
    addSheetMetalPartProperties(part)
    if owner is not None and part is not owner:
        for name in ("Thickness", "DefaultBendRadius", "KFactor", "Density"):
            if name in owner.PropertiesList:
                setattr(part, name, getattr(owner, name))
    _property(part, "String", "MeshType", "Manufactured product type", "WeldedMesh")
    _property(part, "Integer", "MeshSchemaVersion", "Mesh product schema", 2)
    part.Thickness = thickness
    part.UseMaterialCatalog = False
    part.setEditorMode("Thickness", 1)
    for name, description in (
        ("SheetMetalBody", "Primary sheet-metal Body name"),
        ("FlatSheetMetal", "Flat sheet-metal representation name"),
        ("FormedWire", "Formed wire representation name"),
        ("FlatPattern", "Flat wire representation name"),
    ):
        _property(part, "String", name, description, "")

    if body is None:
        for item in history:
            _move(item, part)
            if "UsePartThickness" in item.PropertiesList:
                item.UsePartThickness = False
                item.UseDefaultBendRadius = False
                item.PartDefaultsVersion = 1
        body = doc.addObject("PartDesign::Body", "SheetMetalBody")
        part.addObject(body)
        feature = body.newObject("PartDesign::FeaturePython", "SheetMetalCarrier")
        feature.Label = translate("SheetMetal", "Sheet metal from carrier history")
        SMMeshCarrier(feature, source, face_name, thickness)
        body.Tip = feature
        if SheetMetalTools.isGuiLoaded():
            CarrierViewProvider(feature.ViewObject)
    elif body.getParentGeoFeatureGroup() is not part:
        _move(body, part)
    if not any("UsePartThickness" in o.PropertiesList
               or getattr(o, "SheetMetalType", "") == "Face"
               or isinstance(getattr(o, "Proxy", None), SMMeshCarrier)
               for o in body.Group):
        previous = body.Tip
        feature = body.newObject("PartDesign::FeaturePython", "SheetMetalEnvelope")
        feature.Label = translate("SheetMetal", "Full wire envelope")
        SMMeshCarrier(feature, previous, face_name, thickness)
        body.Tip = feature
    for feature in body.Group:
        if "UsePartThickness" in feature.PropertiesList:
            feature.UsePartThickness = True
            feature.PartDefaultsVersion = 1
    body.Label = translate("SheetMetal", "Sheet Metal Body")
    part.SheetMetalBody = body.Name
    part.Tip = body.Tip.Name if body.Tip is not None else ""
    if "Representation" not in part.PropertiesList:
        part.addProperty("App::PropertyEnumeration", "Representation", "Mesh",
                         translate("App::Property", "Visible representation of this mesh part"))
        part.Representation = ["Formed wires", "Sheet metal", "Flat wires", "Flat sheet metal"]
    doc.recompute()
    return part, body


def sync_envelope(definition):
    """Synchronize input values before recompute, without a Body-to-output cycle."""
    from SheetMetalMeshGeometry import mesh_layers
    part = parent_part(definition)
    if not is_mesh_part(part):
        return
    try:
        latitude = definition.LongitudeDiameter if definition.SameDiameter else definition.LatitudeDiameter
        thickness, _ = mesh_layers(definition.LongitudeDiameter.Value, latitude.Value,
                                   definition.WeldPenetration.Value)
    except ValueError:
        return
    if abs(part.Thickness.Value - thickness) > 1e-9:
        part.Thickness = thickness
    part.setEditorMode("Thickness", 1)
    body = part.Document.getObject(part.SheetMetalBody)
    if body is not None:
        for obj in body.Group:
            if isinstance(getattr(obj, "Proxy", None), SMMeshCarrier):
                if abs(obj.EnvelopeThickness.Value - thickness) > 1e-9:
                    obj.EnvelopeThickness = thickness


class _MeshCarrierObserver:
    def slotChangedObject(self, obj, prop):
        if prop == "Tip" and obj.TypeId == "PartDesign::Body":
            owner = parent_part(obj)
            if is_mesh_part(owner) and getattr(owner, "SheetMetalBody", "") == obj.Name:
                owner.Tip = obj.Tip.Name if obj.Tip is not None else ""
        if prop == "Representation" and is_mesh_part(obj) and SheetMetalTools.isGuiLoaded():
            show_representation(obj)
        if prop in ("Group", "Placement") and obj.TypeId in ("App::Part", "PartDesign::Body"):
            self.slotBeforeRecomputeDocument(obj.Document)

    def slotBeforeRecomputeDocument(self, doc):
        for obj in doc.Objects:
            if isinstance(getattr(obj, "Proxy", None), SMMeshCarrier):
                obj.Proxy.sync_containers(obj)


def show_representation(part):
    """Show one representation; the solid Body remains the modelling authority."""
    if not SheetMetalTools.isGuiLoaded():
        return
    names = {"Formed wires": "FormedWire", "Sheet metal": "SheetMetalBody",
             "Flat wires": "FlatPattern", "Flat sheet metal": "FlatSheetMetal"}
    target = part.Document.getObject(getattr(part, names[str(part.Representation)], ""))
    if target is None:
        return
    for obj in part.Group:
        obj.ViewObject.Visibility = False
    part.ViewObject.Visibility = True
    target.ViewObject.Visibility = True
    if target.TypeId == "PartDesign::Body" and target.Tip is not None:
        target.Tip.ViewObject.Visibility = True


if "_mesh_carrier_observer" not in globals():
    _mesh_carrier_observer = _MeshCarrierObserver()
    App.addDocumentObserver(_mesh_carrier_observer)


class SMMeshSheetFlat:
    """Solid sheet flat sharing the exact layout used for the wire preparation."""

    def __init__(self, obj, definition):
        _property(obj, "Link", "Definition", "Sheet-metal layout and allowance", definition)
        _property(obj, "String", "SheetMetalType", "Representation type", "WeldedMeshSheetFlat")
        _property(obj, "String", "LastError", "Last sheet development error", "")
        obj.addProperty("Part::PropertyPartShape", "BendLines", "Mesh",
                        translate("App::Property", "Developed bend centre lines"))
        obj.setEditorMode("BendLines", 1)
        obj.Proxy = self

    def execute(self, obj):
        try:
            mapping = obj.Definition.Proxy.surface_map(obj.Definition)
            obj.Shape = mapping.boundary.extrude(App.Vector(0, 0, -mapping.thickness))
            obj.BendLines = mapping.bend_lines
            obj.LastError = ""
        except Exception as error:
            obj.Shape = Part.Shape()
            obj.BendLines = Part.Shape()
            obj.LastError = str(error)

    def dumps(self):
        return None

    def loads(self, state):
        pass


if SheetMetalTools.isGuiLoaded():
    class CarrierViewProvider(SheetMetalTools.SMViewProvider):
        def claimChildren(self):
            return [self.Object.Source] if self.Object.Source is not None else []
