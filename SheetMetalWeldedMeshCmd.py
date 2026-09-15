########################################################################
#
#  Copyright 2026 Adrian
#  This program is free software under the GNU Lesser General Public
#  License, version 2 or (at your option) any later version.
#  This program is distributed WITHOUT ANY WARRANTY.
#
########################################################################

"""Welded mesh products driven by a carrier and two editable flat sketches."""

import math
import os

import FreeCAD as App
import Part
import Sketcher

import SheetMetalTools
import SheetMetalMaterial

translate = App.Qt.translate
ICON = os.path.join(SheetMetalTools.icons_path, "SheetMetal_WeldedMesh.svg")
FAMILIES = ("Longitude", "Latitude")


def _property(obj, kind, name, group, description, default=None, readonly=False):
    if name not in obj.PropertiesList:
        type_name = "Part::PropertyPartShape" if kind == "PartShape" else "App::Property" + kind
        obj.addProperty(type_name, name, group, translate("App::Property", description))
        if default is not None:
            setattr(obj, name, default)
    if readonly:
        obj.setEditorMode(name, 1)


def _enum(obj, name, choices, group, description):
    _property(obj, "Enumeration", name, group, description, choices)


class _TransientProxy:
    def dumps(self):
        return None

    def loads(self, state):
        self._mapping = None
        self._signature = None

    def __getstate__(self):
        return self.dumps()

    def __setstate__(self, state):
        self.loads(state)


def _source_shape(source):
    return source.Shape.transformed(source.Placement.toMatrix().inverse())


def _resolve_carrier_reference(source, face_name):
    """Resolve a face selected through an App::Part or PartDesign Body."""
    prefix, separator, face = face_name.rpartition(".")
    if separator:
        resolved = source.getSubObject(prefix + ".", 1)
        if resolved is None:
            raise ValueError(translate("SheetMetal", "The selected carrier feature is missing."))
        source, face_name = resolved, face
    return source, face_name


class SMMeshDefinition(_TransientProxy):
    def __init__(self, obj):
        self.addVerifyProperties(obj)
        self._mapping = None
        self._signature = None
        obj.Proxy = self

    def addVerifyProperties(self, obj):
        _property(obj, "String", "SheetMetalType", "Mesh", "Mesh object type", "MeshDefinition", True)
        _property(obj, "Integer", "MeshSchemaVersion", "Mesh", "Saved mesh schema", 1, True)
        # Global links preserve the carrier's existing container. Local links
        # would make App::Part adopt the carrier into the new mesh product.
        _property(obj, "LinkSubGlobal", "Source", "Carrier", "Planar reference face on the forming carrier")
        _property(obj, "LinkListGlobal", "CarrierContainers", "Carrier", "Carrier placement dependencies", [], True)
        obj.setEditorMode("CarrierContainers", 2)
        _property(obj, "FloatConstraint", "KFactor", "Carrier", "Carrier development K-factor (ANSI)", (0.5, 0.0, 1.0, 0.01))
        _property(obj, "Distance", "ReferenceOffset", "Carrier", "Mesh midplane offset from carrier mid-thickness", 0.0)
        _property(obj, "Bool", "SameDiameter", "Mesh", "Use the longitude diameter for both families", True)
        for family in FAMILIES:
            _property(obj, "Length", family + "Diameter", family, "Wire diameter", 2.0)
            _enum(obj, family + "Mode", ["Pitch", "Count"], family, "Control wire spacing by pitch or count")
            _property(obj, "Length", family + "Pitch", family, "Centre-to-centre wire spacing", 25.0)
            _property(obj, "IntegerConstraint", family + "Count", family, "Number of wire rows including both ends", (5, 1, 2000, 1))
            _property(obj, "Length", family + "Margin", family, "Minimum transverse centreline distance from the bounding edges", 1.0)
            _property(obj, "Distance", family + "Offset", family, "Grid phase offset in pitch mode", 0.0)
        _property(obj, "Length", "WeldPenetration", "Mesh", "Geometric overlap at a crossing", 0.3)
        _enum(obj, "LayerOrder", ["Latitude above", "Longitude above"], "Mesh", "Above follows the selected carrier face normal")
        _enum(obj, "Representation", ["Solid wires", "Centrelines"], "Mesh", "Use centrelines for a faster preview")
        _property(obj, "String", "PatternMode", "Mesh", "Generated or editable wire layout", "Generated", True)
        _property(obj, "Length", "MeshThickness", "Mesh", "Overall welded mesh envelope thickness", 0.0, True)
        _property(obj, "Density", "Density", "Material", "Wire material density",
                  "{} kg/m^3".format(SheetMetalMaterial._DENSITIES_KG_M3["Hot Rolled Steel"]))
        _property(obj, "String", "Material", "Material", "Wire material description", "Steel wire")
        _property(obj, "String", "LastError", "Mesh", "Last carrier or parameter error", "", True)

    def onDocumentRestored(self, obj):
        self.loads(None)
        self.addVerifyProperties(obj)

    def onChanged(self, obj, prop):
        if "LatitudeDiameter" in obj.PropertiesList and "SameDiameter" in obj.PropertiesList:
            obj.setEditorMode("LatitudeDiameter", 1 if obj.SameDiameter else 0)
            if obj.SameDiameter and obj.LatitudeDiameter != obj.LongitudeDiameter:
                obj.LatitudeDiameter = obj.LongitudeDiameter
        for family in FAMILIES:
            mode = family + "Mode"
            if mode in obj.PropertiesList and family + "Offset" in obj.PropertiesList:
                count = str(getattr(obj, mode)) == "Count"
                obj.setEditorMode(family + "Count", 0 if count else 1)
                obj.setEditorMode(family + "Pitch", 1 if count else 0)
                obj.setEditorMode(family + "Offset", 1 if count else 0)

        if (prop in ("LongitudeDiameter", "LatitudeDiameter", "SameDiameter", "WeldPenetration")
                and all(name in obj.PropertiesList for name in
                        ("LongitudeDiameter", "LatitudeDiameter", "SameDiameter", "WeldPenetration"))):
            from SheetMetalMeshPart import sync_envelope
            sync_envelope(obj)

    def surface_map(self, obj):
        from SheetMetalMeshGeometry import MeshSurfaceMap
        source, names = obj.Source
        if source is None or len(names) != 1:
            raise ValueError(translate("SheetMetal", "The forming carrier or reference face is missing."))
        source, face_name = _resolve_carrier_reference(source, names[0])
        if source.TypeId == "PartDesign::Body" and source.Tip is not None:
            from SheetMetalMeshPart import SMMeshCarrier
            if isinstance(getattr(source.Tip, "Proxy", None), SMMeshCarrier):
                if source.Tip.LastError:
                    raise ValueError(source.Tip.LastError)
                face_name = source.Tip.EnvelopeFace
            elif "CarrierNormal" in obj.PropertiesList:
                local = _source_shape(source)
                faces = [(abs((f.CenterOfMass - obj.CarrierPoint).dot(obj.CarrierNormal)), -f.Area, i)
                         for i, f in enumerate(local.Faces, 1)
                         if isinstance(f.Surface, Part.Plane)
                         and f.normalAt(0, 0).dot(obj.CarrierNormal) > 0.999]
                if faces:
                    face_name = "Face" + str(min(faces)[2])
        if source is not obj.Source[0] or face_name != names[0]:
            obj.Source = (source, [face_name])
        signature = (source.Shape.hashCode(), face_name, float(obj.KFactor))
        if getattr(self, "_mapping", None) is None or signature != getattr(self, "_signature", None):
            self._mapping = MeshSurfaceMap(_source_shape(source), face_name, float(obj.KFactor))
            self._signature = signature
        return self._mapping

    def execute(self, obj):
        from SheetMetalMeshGeometry import mesh_layers
        try:
            diameters = wire_diameters(obj)
            thickness, _centres = mesh_layers(*diameters, obj.WeldPenetration.Value)
            if obj.Density.Value <= 0:
                raise ValueError(translate("SheetMetal", "Wire material density must be positive."))
            mapping = self.surface_map(obj)
            containers = []
            own_ancestors = set()
            current = obj.getParentGeoFeatureGroup()
            while current is not None:
                own_ancestors.add(current)
                current = current.getParentGeoFeatureGroup()
            current = obj.Source[0].getParentGeoFeatureGroup()
            while current is not None and current not in own_ancestors:
                containers.append(current)
                current = current.getParentGeoFeatureGroup()
            if list(obj.CarrierContainers) != containers:
                obj.CarrierContainers = containers
            obj.Shape = mapping.boundary
            obj.MeshThickness = thickness
            obj.LastError = ""
        except Exception as error:
            self._mapping = None
            self._signature = None
            obj.Shape = Part.Shape()
            obj.MeshThickness = 0
            obj.LastError = str(error)
            App.Console.PrintError("Welded mesh carrier: {}\n".format(error))


def wire_diameters(definition):
    return (definition.LongitudeDiameter.Value,
            definition.LongitudeDiameter.Value if definition.SameDiameter else definition.LatitudeDiameter.Value)


class SMMeshPatternSketch(_TransientProxy):
    def __init__(self, obj, definition, family):
        _property(obj, "Link", "Definition", "Mesh", "Mesh pattern settings", definition, True)
        _property(obj, "String", "Family", "Mesh", "Wire family", family, True)
        _property(obj, "String", "SheetMetalType", "Mesh", "Mesh object type", "MeshPatternSketch", True)
        _property(obj, "String", "LastError", "Mesh", "Last pattern generation error", "", True)
        obj.Proxy = self

    def onChanged(self, obj, prop):
        if (prop == "Geometry" and not getattr(self, "_generating", False)
                and not getattr(obj.Document, "Restoring", False)
                and getattr(obj, "Definition", None) is not None):
            obj.Definition.PatternMode = "Editable"

    def execute(self, obj):
        from SheetMetalMeshGeometry import generate_lines
        if obj.Definition is not None and obj.Definition.PatternMode == "Editable":
            obj.LastError = ""
            return
        self._generating = True
        try:
            definition = obj.Definition
            if definition is None or definition.Shape.isNull() or definition.LastError:
                raise ValueError(translate("SheetMetal", "The mesh carrier is invalid."))
            family = obj.Family
            lines = generate_lines(
                definition.Shape, family, str(getattr(definition, family + "Mode")),
                getattr(definition, family + "Pitch").Value,
                getattr(definition, family + "Count"),
                getattr(definition, family + "Margin").Value,
                getattr(definition, family + "Offset").Value,
            )
            # The same sketches are rebuilt only in generated mode.
            if obj.GeometryCount:
                obj.delGeometries(list(range(obj.GeometryCount)))
            if lines:
                obj.addGeometry(lines, False)
            obj.LastError = ""
        except Exception as error:
            if obj.GeometryCount:
                obj.delGeometries(list(range(obj.GeometryCount)))
            obj.LastError = str(error)
            App.Console.PrintError("Welded mesh pattern: {}\n".format(error))
        finally:
            self._generating = False


class SMWeldedMesh(_TransientProxy):
    def __init__(self, obj, definition, longitude, latitude):
        _property(obj, "String", "SheetMetalType", "Mesh", "Mesh object type", "WeldedMesh", True)
        _property(obj, "Link", "Definition", "Mesh", "Mesh settings and forming carrier", definition, True)
        for family, sketch in zip(FAMILIES, (longitude, latitude)):
            _property(obj, "Link", family + "Sketch", "Mesh", "Flat wire centreline input", sketch, True)
            _property(obj, "Integer", family + "WireCount", "Results", "Actual wire pieces after trimming", 0, True)
        _property(obj, "PartShape", "FlatShape", "Results", "Derived flat wire representation", readonly=True)
        obj.setEditorMode("FlatShape", 2)
        _property(obj, "Length", "FlatWireLength", "Results", "Total centreline length of the flat preparation", 0.0, True)
        _property(obj, "Length", "FormedWireLength", "Results", "Total geometric formed length; carrier allowance approximation", 0.0, True)
        _property(obj, "Mass", "Weight", "Results", "Mass from flat wire stock lengths and diameters", 0.0, True)
        _property(obj, "StringList", "Warnings", "Results", "Mesh layout and forming observations", [], True)
        _property(obj, "String", "LastError", "Results", "Last wire generation error", "", True)
        obj.setEditorMode("Placement", 1)
        obj.Proxy = self

    def execute(self, obj):
        from SheetMetalMeshGeometry import (
            MAX_WIRES, family_collisions, mesh_layers, sketch_lines, sweep_wire,
        )
        try:
            definition = obj.Definition
            if definition is None or definition.LastError or definition.Shape.isNull():
                raise ValueError(translate("SheetMetal", "The mesh carrier or its parameters are invalid."))
            mapping = definition.Proxy.surface_map(definition)
            diameters = wire_diameters(definition)
            _thickness, centres = mesh_layers(*diameters, definition.WeldPenetration.Value,
                                              reverse=str(definition.LayerOrder) == "Longitude above")
            families = [sketch_lines(getattr(obj, family + "Sketch")) for family in FAMILIES]
            if sum(map(len, families)) > MAX_WIRES:
                raise ValueError(translate("SheetMetal", "The complete mesh exceeds 2000 wire pieces."))
            formed, flat = [], []
            flat_length = formed_length = stock_volume = 0.0
            warnings = []
            for family, lines, diameter, centre in zip(FAMILIES, families, diameters, centres):
                if family_collisions(lines, diameter):
                    warnings.append(translate("SheetMetal", "%1 wires overlap within the same layer.").replace("%1", family))
                for line in lines:
                    path = mapping.map_line(line, centre - mapping.thickness / 2 + definition.ReferenceOffset.Value)
                    flat_path = Part.Wire([line.translated(App.Vector(0, 0, centre))])
                    if str(definition.Representation) == "Centrelines":
                        formed.append(path)
                        flat.append(flat_path)
                    else:
                        formed.append(sweep_wire(path, diameter))
                        flat.append(sweep_wire(flat_path, diameter))
                    flat_length += line.Length
                    formed_length += path.Length
                    stock_volume += math.pi * diameter ** 2 / 4 * line.Length
            if len(mapping.patches) > 1:
                warnings.append(translate("SheetMetal", "Formed wire lengths use the carrier bend allowance; validate preparation dimensions for the forming process."))
            result = Part.makeCompound(formed) if formed else Part.Shape()
            source = definition.Source[0]
            # Feature.Shape assignment retains the feature placement, so put
            # the source transform on the feature rather than the compound.
            obj.Shape = result
            placement = source.getGlobalPlacement()
            parent = obj.getParentGeoFeatureGroup()
            if parent is not None:
                placement = parent.getGlobalPlacement().inverse() * placement
            if obj.Placement != placement:
                obj.Placement = placement
            obj.FlatShape = Part.makeCompound(flat) if flat else Part.Shape()
            obj.FlatWireLength = flat_length
            obj.FormedWireLength = formed_length
            obj.Weight = stock_volume * definition.Density.Value
            obj.LongitudeWireCount, obj.LatitudeWireCount = map(len, families)
            obj.Warnings = warnings
            obj.LastError = ""
        except Exception as error:
            obj.Shape = Part.Shape()
            obj.FlatShape = Part.Shape()
            obj.FlatWireLength = obj.FormedWireLength = obj.Weight = 0
            obj.LongitudeWireCount = obj.LatitudeWireCount = 0
            obj.Warnings = []
            obj.LastError = str(error)
            App.Console.PrintError("Welded mesh: {}\n".format(error))


class SMMeshFlat(_TransientProxy):
    def __init__(self, obj, mesh):
        _property(obj, "String", "SheetMetalType", "Mesh", "Mesh object type", "WeldedMeshFlat", True)
        _property(obj, "Link", "Mesh", "Mesh", "Formed welded mesh", mesh, True)
        obj.Proxy = self

    def execute(self, obj):
        obj.Shape = obj.Mesh.FlatShape if obj.Mesh is not None else Part.Shape()


def create_welded_mesh(doc, source, face_name, **parameters):
    """Create a product. Call inside a transaction for interactive undo."""
    from SheetMetalMeshGeometry import MeshSurfaceMap, mesh_layers
    from SheetMetalMeshPart import prepare_carrier, SMMeshSheetFlat
    source, face_name = _resolve_carrier_reference(source, face_name)
    if source.Document is not doc:
        raise ValueError(translate("SheetMetal", "The carrier must belong to the active document."))
    carrier_part = SheetMetalMaterial.findSheetMetalPart(source)
    k_factor = float(getattr(carrier_part, "KFactor", 0.5))
    # Check the reference before adding document objects.
    mapping = MeshSurfaceMap(_source_shape(source), face_name, parameters.get("KFactor", k_factor))
    original_source = source
    dl = parameters.get("LongitudeDiameter", 2.0)
    dt = dl if parameters.get("SameDiameter", True) else parameters.get("LatitudeDiameter", 2.0)
    thickness, _ = mesh_layers(dl, dt, parameters.get("WeldPenetration", 0.3))
    reference_normal = mapping.root_face.normalAt(0, 0)
    reference_point = mapping.root_face.CenterOfMass
    part, source = prepare_carrier(doc, source, face_name, thickness)
    # A Body publishes its current Tip. Resolve the selected face on that
    # representation by geometry, since a selected earlier feature may differ.
    if source.Shape.isNull():
        raise ValueError(translate("SheetMetal", "The sheet-metal Body has no current solid."))
    if original_source is not source:
        selected = original_source.Shape.getElement(face_name)
        candidates = [(f.CenterOfMass.distanceToPoint(selected.CenterOfMass), i)
                      for i, f in enumerate(source.Shape.Faces, 1)
                      if isinstance(f.Surface, Part.Plane)
                      and abs(f.Area - selected.Area) < 1e-5]
        if candidates:
            face_name = "Face" + str(min(candidates)[1])
    if hasattr(source.Tip, "EnvelopeFace"):
        face_name = source.Tip.EnvelopeFace
    else:
        faces = [(abs((f.CenterOfMass - reference_point).dot(reference_normal)), -f.Area, i)
                 for i, f in enumerate(_source_shape(source).Faces, 1)
                 if isinstance(f.Surface, Part.Plane) and f.normalAt(0, 0).dot(reference_normal) > 0.999]
        face_name = "Face" + str(min(faces)[2])
    mapping = MeshSurfaceMap(_source_shape(source), face_name, parameters.get("KFactor", k_factor))
    definition = doc.addObject("Part::FeaturePython", "MeshDefinition")
    definition.Label = translate("SheetMetal", "Mesh Parameters and Boundary")
    SMMeshDefinition(definition)
    _property(definition, "Vector", "CarrierNormal", "Carrier", "Reference side normal in the Body", mapping.root_face.normalAt(0, 0), True)
    _property(definition, "Vector", "CarrierPoint", "Carrier", "Reference side point in the Body", mapping.root_face.CenterOfMass, True)
    for internal in ("CarrierNormal", "CarrierPoint"):
        definition.setEditorMode(internal, 2)
    definition.Source = (source, [face_name])
    definition.KFactor = k_factor
    definition.Proxy._mapping = mapping
    definition.Proxy._signature = (source.Shape.hashCode(), face_name, parameters.get("KFactor", k_factor))
    if carrier_part is not None:
        definition.Density = carrier_part.Density
        definition.Material = str(getattr(carrier_part, "BaseMaterial", "Steel wire"))
    definition.SameDiameter = parameters.get("SameDiameter", True)
    for name, value in parameters.items():
        if name not in definition.PropertiesList or name in ("Source", "Shape", "Proxy", "PatternMode"):
            raise ValueError("Unknown mesh parameter: " + name)
        setattr(definition, name, value)
    part.addObject(definition)
    sketches = []
    for family in FAMILIES:
        sketch = doc.addObject("Sketcher::SketchObjectPython", family + "Pattern")
        sketch.Label = translate("SheetMetal", "%1 Wires").replace("%1", family)
        SMMeshPatternSketch(sketch, definition, family)
        part.addObject(sketch)
        sketches.append(sketch)
    mesh = doc.addObject("Part::FeaturePython", "WeldedMesh")
    mesh.Label = translate("SheetMetal", "Formed Wires")
    SMWeldedMesh(mesh, definition, *sketches)
    part.addObject(mesh)
    flat = doc.addObject("Part::FeaturePython", "WeldedMeshFlat")
    flat.Label = translate("SheetMetal", "Flat Wires")
    SMMeshFlat(flat, mesh)
    part.addObject(flat)
    sheet_flat = doc.addObject("Part::FeaturePython", "MeshSheetFlat")
    sheet_flat.Label = translate("SheetMetal", "Flat Sheet Metal")
    SMMeshSheetFlat(sheet_flat, definition)
    part.addObject(sheet_flat)
    part.FormedWire, part.FlatPattern, part.FlatSheetMetal = mesh.Name, flat.Name, sheet_flat.Name
    _property(part, "Mass", "Weight", "Results", "Mass of the flat wire stock", 0.0, True)
    part.setExpression("Weight", mesh.Name + ".Weight")
    _property(part, "Length", "MeshThickness", "Results", "Welded envelope thickness", 0.0, True)
    part.setExpression("MeshThickness", definition.Name + ".MeshThickness")
    doc.recompute()
    if definition.LastError or mesh.LastError or any(s.LastError for s in sketches):
        raise ValueError(definition.LastError or mesh.LastError or next(s.LastError for s in sketches if s.LastError))
    if SheetMetalTools.isGuiLoaded():
        MeshViewProvider(mesh.ViewObject)
        MeshViewProvider(definition.ViewObject)
        MeshFlatViewProvider(flat.ViewObject)
        MeshFlatViewProvider(sheet_flat.ViewObject)
        for sketch in sketches:
            # Keep native Sketcher editing. The feature detects manual geometry
            # changes and switches the shared pattern mode before recomputing.
            sketch.ViewObject.Proxy = None
        definition.ViewObject.Visibility = False
        for sketch in sketches:
            sketch.ViewObject.Visibility = False
        flat.ViewObject.Visibility = False
        sheet_flat.ViewObject.Visibility = False
        source.ViewObject.Visibility = False
        for feature in source.Group:
            feature.ViewObject.Visibility = False
        original_source.ViewObject.Visibility = False
        mesh.ViewObject.ShapeColor = (0.72, 0.74, 0.77)
        from SheetMetalMeshPart import show_representation
        show_representation(part)
    return part, mesh


def convert_to_editable(mesh):
    """Freeze the active sketch pair without duplicating it."""
    mesh.Definition.PatternMode = "Editable"
    mesh.Document.recompute()
    return [getattr(mesh, family + "Sketch") for family in FAMILIES]


def regenerate_pattern(mesh):
    """Explicitly regenerate the active pair; document transactions provide undo."""
    mesh.Definition.PatternMode = "Generated"
    for family in FAMILIES:
        getattr(mesh, family + "Sketch").touch()
    mesh.Document.recompute()


def find_mesh(obj):
    from SheetMetalMeshPart import is_mesh_part
    if obj is None:
        return None
    if getattr(obj, "SheetMetalType", "") == "WeldedMesh":
        return obj
    if getattr(obj, "SheetMetalType", "") == "WeldedMeshFlat":
        return obj.Mesh
    if is_mesh_part(obj):
        return obj.Document.getObject(obj.FormedWire)
    parent = obj.getParentGeoFeatureGroup()
    return find_mesh(parent) if parent is not None else None


if SheetMetalTools.isGuiLoaded():
    Gui = App.Gui
    from PySide import QtGui

    class MeshViewProvider(SheetMetalTools.SMViewProvider):
        def getIcon(self):
            return ICON

        def claimChildren(self):
            return []

        def getTaskPanel(self, obj):
            return MeshTaskPanel(find_mesh(obj))

        def doubleClicked(self, vobj):
            self.startDefaultEditMode(vobj)
            return True

        def unsetEdit(self, vobj, mode):
            Gui.Control.closeDialog()
            mesh = find_mesh(vobj.Object)
            if mesh is not None:
                mesh.Definition.ViewObject.Visibility = False
                mesh.ViewObject.Visibility = True
            return False

        def setupContextMenu(self, vobj, menu):
            super().setupContextMenu(vobj, menu)
            mesh = find_mesh(vobj.Object)
            if mesh is not None:
                action = menu.addAction(translate("SheetMetal", "Make wire sketches editable"))
                action.triggered.connect(lambda: _edit_pattern(mesh, False))
                action = menu.addAction(translate("SheetMetal", "Regenerate wire pattern"))
                action.triggered.connect(lambda: _edit_pattern(mesh, True))

    class MeshFlatViewProvider(SheetMetalTools.SMViewProvider):
        def getIcon(self):
            return ICON

        def claimChildren(self):
            return []

    class MeshTaskPanel:
        def __init__(self, mesh):
            self.obj = mesh
            self.definition = mesh.Definition
            self.form = QtGui.QWidget()
            self.form.setWindowTitle(translate("SheetMetal", "Welded mesh"))
            layout = QtGui.QVBoxLayout(self.form)
            self.fields = {}
            form = QtGui.QFormLayout()
            self.same = QtGui.QCheckBox(translate("SheetMetal", "Same diameter for both directions"))
            self.same.setChecked(self.definition.SameDiameter)
            layout.addWidget(self.same)
            for family in FAMILIES:
                group = QtGui.QGroupBox(translate("SheetMetal", family))
                family_form = QtGui.QFormLayout(group)
                self._number(family_form, family + "Diameter", "Diameter", 0.01, 1000)
                self._choice(family_form, family + "Mode", "Spacing", ["Pitch", "Count"])
                self._number(family_form, family + "Pitch", "Pitch", 0.01, 1e6)
                self._number(family_form, family + "Count", "Count", 1, 2000, integer=True)
                self._number(family_form, family + "Margin", "Edge margin", 0, 1e6)
                self._number(family_form, family + "Offset", "Grid phase", -1e6, 1e6)
                layout.addWidget(group)
                self.fields[family + "Mode"].currentTextChanged.connect(self._enabled)
            self._number(form, "WeldPenetration", "Weld penetration", 0, 1000)
            self._choice(form, "LayerOrder", "Layer order", ["Latitude above", "Longitude above"])
            self._number(form, "ReferenceOffset", "Carrier midplane offset", -1e6, 1e6)
            self._number(form, "KFactor", "Carrier K-factor", 0, 1, unit=False)
            self._choice(form, "Representation", "Representation", ["Solid wires", "Centrelines"])
            layout.addLayout(form)
            self.status = QtGui.QLabel()
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            update = QtGui.QPushButton(translate("SheetMetal", "Update preview"))
            update.clicked.connect(self.apply)
            layout.addWidget(update)
            self.same.toggled.connect(self._enabled)
            self.fields["LongitudeDiameter"].valueChanged.connect(self._enabled)
            self._enabled()
            self._status()

        def _number(self, form, name, label, minimum, maximum, integer=False, unit=True):
            spin = QtGui.QSpinBox() if integer else QtGui.QDoubleSpinBox()
            if not integer:
                spin.setDecimals(3)
                if unit:
                    spin.setSuffix(" mm")
            spin.setRange(minimum, maximum)
            value = getattr(self.definition, name)
            spin.setValue(value.Value if hasattr(value, "Value") else value)
            self.fields[name] = spin
            form.addRow(translate("SheetMetal", label), spin)

        def _choice(self, form, name, label, values):
            combo = QtGui.QComboBox()
            combo.addItems(values)
            combo.setCurrentText(str(getattr(self.definition, name)))
            self.fields[name] = combo
            form.addRow(translate("SheetMetal", label), combo)

        def _enabled(self, _value=None):
            self.fields["LatitudeDiameter"].setEnabled(not self.same.isChecked())
            if self.same.isChecked():
                self.fields["LatitudeDiameter"].setValue(self.fields["LongitudeDiameter"].value())
            for family in FAMILIES:
                generated = self.definition.PatternMode == "Generated"
                count = self.fields[family + "Mode"].currentText() == "Count"
                self.fields[family + "Mode"].setEnabled(generated)
                self.fields[family + "Count"].setEnabled(generated and count)
                self.fields[family + "Pitch"].setEnabled(generated and not count)
                self.fields[family + "Offset"].setEnabled(generated and not count)
                self.fields[family + "Margin"].setEnabled(generated)

        def _status(self):
            error = self.definition.LastError or self.obj.LastError
            self.status.setText(error or translate("SheetMetal", "%1 layout; welded thickness %2 mm; %3 wire pieces.")
                                .replace("%1", self.definition.PatternMode)
                                .replace("%2", "{:.3f}".format(self.definition.MeshThickness.Value))
                                .replace("%3", str(self.obj.LongitudeWireCount + self.obj.LatitudeWireCount)))

        def apply(self):
            self.definition.SameDiameter = self.same.isChecked()
            for name, widget in self.fields.items():
                value = widget.currentText() if isinstance(widget, QtGui.QComboBox) else widget.value()
                setattr(self.definition, name, value)
            self.obj.Document.recompute()
            self._status()
            return not (self.definition.LastError or self.obj.LastError)

        def accept(self):
            if not self.apply():
                return False
            self.obj.Document.commitTransaction()
            Gui.activeDocument().resetEdit()
            Gui.Control.closeDialog()
            return True

        def reject(self):
            self.obj.Document.abortTransaction()
            Gui.activeDocument().resetEdit()
            Gui.Control.closeDialog()
            return True

        def isAllowedAlterSelection(self):
            return True

        def isAllowedAlterView(self):
            return True

    def _selected_mesh():
        selection = Gui.Selection.getSelection()
        return find_mesh(selection[0]) if len(selection) == 1 else None

    def _edit_pattern(mesh, regenerate):
        if mesh is None:
            return
        doc = mesh.Document
        doc.openTransaction(translate("SheetMetal", "Change mesh pattern"))
        try:
            if regenerate:
                regenerate_pattern(mesh)
                mesh.Definition.ViewObject.Visibility = False
                mesh.ViewObject.Visibility = True
                for family in FAMILIES:
                    getattr(mesh, family + "Sketch").ViewObject.Visibility = False
            else:
                sketches = convert_to_editable(mesh)
                Gui.Selection.clearSelection()
                Gui.Selection.addSelection(sketches[0])
                # Reveal the flat boundary beside the two editable inputs.
                mesh.ViewObject.Visibility = False
                mesh.Definition.ViewObject.Visibility = True
                mesh.Definition.ViewObject.DisplayMode = "Wireframe"
                for sketch in sketches:
                    sketch.ViewObject.Visibility = True
                Gui.activeDocument().activeView().viewTop()
                Gui.activeDocument().activeView().fitAll()
            doc.commitTransaction()
        except Exception as error:
            doc.abortTransaction()
            SheetMetalTools.smWarnDialog(str(error))

    class CreateWeldedMeshCommand:
        def GetResources(self):
            return {"Pixmap": ICON, "MenuText": translate("SheetMetal", "Create Welded Mesh Part"),
                    "ToolTip": translate("SheetMetal", "Select a planar face on a sheet-metal carrier to create flat and formed wire mesh.")}

        def IsActive(self):
            selection = Gui.Selection.getSelectionEx()
            return (App.ActiveDocument is not None and len(selection) == 1
                    and len(selection[0].SubObjects) == 1
                    and isinstance(selection[0].SubObjects[0], Part.Face)
                    and isinstance(selection[0].SubObjects[0].Surface, Part.Plane))

        def Activated(self):
            selection = Gui.Selection.getSelectionEx()[0]
            doc = App.ActiveDocument
            doc.openTransaction(translate("SheetMetal", "Create welded mesh"))
            try:
                _part, mesh = create_welded_mesh(doc, selection.Object, selection.SubElementNames[0])
                Gui.Selection.clearSelection()
                Gui.Selection.addSelection(mesh)
                Gui.Control.showDialog(MeshTaskPanel(mesh))
                Gui.activeDocument().activeView().fitAll()
            except Exception as error:
                doc.abortTransaction()
                SheetMetalTools.smWarnDialog(str(error))

    class EditableMeshCommand:
        def GetResources(self):
            return {"Pixmap": ICON, "MenuText": translate("SheetMetal", "Make Mesh Wires Editable"),
                    "ToolTip": translate("SheetMetal", "Create editable longitude and latitude sketches. Manual edits survive recomputes.")}

        def IsActive(self):
            return _selected_mesh() is not None

        def Activated(self):
            _edit_pattern(_selected_mesh(), False)

    Gui.addCommand("SheetMetal_WeldedMesh", CreateWeldedMeshCommand())
    Gui.addCommand("SheetMetal_EditableMesh", EditableMeshCommand())
