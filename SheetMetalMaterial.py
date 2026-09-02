########################################################################
#
#  SheetMetalMaterial.py
#
#  Copyright 2026 SheetMetal Workbench contributors
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 2 of the License, or (at your option) any later version.
#
########################################################################

"""Material catalog and product-level defaults for sheet-metal parts."""

import FreeCAD

import SheetMetalTools


translate = FreeCAD.Qt.translate

MATERIALS = [
    "Hot Rolled Steel",
    "Galvanized Steel",
    "Stainless Steel",
    "Corten Steel",
]
SHEET_SIZES = [
    "10 ga", "11 ga", "12 ga", "14 ga", "16 ga",
    "18 ga", "20 ga", "22 ga", "24 ga",
    "1/8 in", "3/16 in", "1/4 in", "1/2 in",
]
MATERIAL_UPGRADES = ["Standard", "Stainless Steel", "Corten Steel"]
CONFIGURATION_OBJECT_NAME = "SheetMetalConfiguration"
CONFIGURATION_VARIABLE = CONFIGURATION_OBJECT_NAME + ".MaterialUpgrade"

_FRACTIONAL_THICKNESS_IN = {
    "1/8 in": 0.125,
    "3/16 in": 0.1875,
    "1/4 in": 0.25,
    "1/2 in": 0.5,
}
_GAUGE_THICKNESS_IN = {
    "Hot Rolled Steel": {
        "10 ga": 0.1345, "11 ga": 0.1196, "12 ga": 0.1046,
        "14 ga": 0.0747, "16 ga": 0.0598, "18 ga": 0.0478,
        "20 ga": 0.0359, "22 ga": 0.0299, "24 ga": 0.0239,
    },
    "Galvanized Steel": {
        "10 ga": 0.1382, "11 ga": 0.1233, "12 ga": 0.1084,
        "14 ga": 0.0785, "16 ga": 0.0635, "18 ga": 0.0516,
        "20 ga": 0.0396, "22 ga": 0.0336, "24 ga": 0.0276,
    },
    "Stainless Steel": {
        "10 ga": 0.1406, "11 ga": 0.1250, "12 ga": 0.1094,
        "14 ga": 0.0781, "16 ga": 0.0625, "18 ga": 0.0500,
        "20 ga": 0.0375, "22 ga": 0.0312, "24 ga": 0.0250,
    },
}
# Corten is commonly ordered to the same nominal gauge thicknesses as HRS.
# Keep it as an explicit alias so a future shop-specific table is one edit.
_GAUGE_THICKNESS_IN["Corten Steel"] = _GAUGE_THICKNESS_IN["Hot Rolled Steel"]

_K_FACTORS = {
    "Hot Rolled Steel": 0.44,
    "Galvanized Steel": 0.44,
    "Stainless Steel": 0.45,
    "Corten Steel": 0.44,
}

_DENSITIES_KG_M3 = {
    "Hot Rolled Steel": 7850.0,
    "Galvanized Steel": 7850.0,
    "Stainless Steel": 8000.0,
    "Corten Steel": 7850.0,
}

_MATERIAL_PROPERTY_NAMES = (
    "UseMaterialCatalog",
    "BaseMaterial",
    "SheetSize",
    "FollowMaterialUpgrade",
    "EffectiveMaterial",
    "UseStandardBendRadius",
    "UseStandardKFactor",
    "Density",
)


def _ensure_enumeration_options(obj, property_name, options, default):
    current = str(getattr(obj, property_name))
    if obj.getEnumerationsOfProperty(property_name) == options:
        return
    setattr(obj, property_name, options)
    setattr(obj, property_name, current if current in options else default)


def standardSheetMetalParameters(material, sheet_size):
    """Return catalog defaults in FreeCAD internal units (millimetres)."""
    if material not in MATERIALS:
        raise ValueError("Unknown sheet material: {}".format(material))
    if sheet_size not in SHEET_SIZES:
        raise ValueError("Unknown sheet size: {}".format(sheet_size))
    thickness_in = _FRACTIONAL_THICKNESS_IN.get(sheet_size)
    if thickness_in is None:
        thickness_in = _GAUGE_THICKNESS_IN[material][sheet_size]
    thickness = thickness_in * 25.4
    return {
        "material": material,
        "thickness": thickness,
        "bend_radius": thickness,
        "k_factor": _K_FACTORS[material],
        "density": _DENSITIES_KG_M3[material],
    }


def _is_sheet_metal_part(obj):
    return (
        obj is not None
        and hasattr(obj, "SheetMetalType")
        and obj.SheetMetalType == "Part"
    )


def findSheetMetalPart(obj):
    """Return the Sheet Metal Part containing *obj*, if there is one."""
    current = obj
    while current is not None:
        if _is_sheet_metal_part(current):
            return current
        current = current.getParentGeoFeatureGroup() or current.getParentGroup()
    return None


def materialConfiguration(doc, create=False):
    """Return the document's material defaults and upgrade variable set."""
    config = doc.getObject(CONFIGURATION_OBJECT_NAME)
    if config is None and create:
        config = doc.addObject("App::VarSet", CONFIGURATION_OBJECT_NAME)
        config.Label = translate("SheetMetal", "Sheet Metal Configuration")
    if config is None:
        return None
    if "SheetMetalConfigurationType" not in config.PropertiesList:
        config.addProperty(
            "App::PropertyString",
            "SheetMetalConfigurationType",
            "Sheet Metal",
            translate("App::Property", "Sheet-metal configuration object type"),
        ).SheetMetalConfigurationType = "MaterialDefaults"
        config.setEditorMode("SheetMetalConfigurationType", 1)
    if "MaterialUpgrade" not in config.PropertiesList:
        config.addProperty(
            "App::PropertyEnumeration",
            "MaterialUpgrade",
            "Sheet Metal",
            translate(
                "App::Property",
                "Product-level material upgrade followed by opted-in parts",
            ),
        )
        config.MaterialUpgrade = MATERIAL_UPGRADES
        config.MaterialUpgrade = "Standard"
    else:
        _ensure_enumeration_options(
            config, "MaterialUpgrade", MATERIAL_UPGRADES, "Standard"
        )
    if "DefaultBaseMaterial" not in config.PropertiesList:
        config.addProperty(
            "App::PropertyEnumeration",
            "DefaultBaseMaterial",
            "New Part Defaults",
            translate(
                "App::Property",
                "Base material assigned to newly created sheet-metal parts",
            ),
        )
        config.DefaultBaseMaterial = MATERIALS
        config.DefaultBaseMaterial = "Hot Rolled Steel"
    else:
        _ensure_enumeration_options(
            config, "DefaultBaseMaterial", MATERIALS, "Hot Rolled Steel"
        )
    if "DefaultSheetSize" not in config.PropertiesList:
        config.addProperty(
            "App::PropertyEnumeration",
            "DefaultSheetSize",
            "New Part Defaults",
            translate(
                "App::Property",
                "Sheet size assigned to newly created sheet-metal parts",
            ),
        )
        config.DefaultSheetSize = SHEET_SIZES
        config.DefaultSheetSize = "14 ga"
    else:
        _ensure_enumeration_options(
            config, "DefaultSheetSize", SHEET_SIZES, "14 ga"
        )
    return config


def addMaterialProperties(part):
    """Add material controls without changing legacy manual part defaults."""
    SheetMetalTools.smAddBoolProperty(
        part,
        "UseMaterialCatalog",
        translate(
            "App::Property",
            "Derive thickness and selected defaults from material and sheet size",
        ),
        False,
        "Material",
    )
    if "BaseMaterial" not in part.PropertiesList:
        part.addProperty(
            "App::PropertyEnumeration",
            "BaseMaterial",
            "Material",
            translate("App::Property", "Material used when the product upgrade is Standard"),
        )
        part.BaseMaterial = MATERIALS
        part.BaseMaterial = "Hot Rolled Steel"
    else:
        _ensure_enumeration_options(
            part, "BaseMaterial", MATERIALS, "Hot Rolled Steel"
        )
    if "SheetSize" not in part.PropertiesList:
        part.addProperty(
            "App::PropertyEnumeration",
            "SheetSize",
            "Material",
            translate("App::Property", "Nominal gauge or fractional sheet size"),
        )
        part.SheetSize = SHEET_SIZES
        part.SheetSize = "14 ga"
    else:
        _ensure_enumeration_options(part, "SheetSize", SHEET_SIZES, "14 ga")
    SheetMetalTools.smAddBoolProperty(
        part,
        "FollowMaterialUpgrade",
        translate(
            "App::Property",
            "Allow the product material variable to upgrade this part to stainless or Corten",
        ),
        False,
        "Material",
    )
    SheetMetalTools.smAddStringProperty(
        part,
        "EffectiveMaterial",
        translate("App::Property", "Resolved material used by this part"),
        "Manual",
        "Material",
    )
    part.setEditorMode("EffectiveMaterial", 1)
    SheetMetalTools.smAddStringProperty(
        part,
        "MaterialConfigurationVariable",
        translate("App::Property", "Predefined document configuration variable"),
        CONFIGURATION_VARIABLE,
        "Material",
    )
    part.MaterialConfigurationVariable = CONFIGURATION_VARIABLE
    part.setEditorMode("MaterialConfigurationVariable", 1)
    SheetMetalTools.smAddBoolProperty(
        part,
        "UseStandardBendRadius",
        translate("App::Property", "Use the material catalog's inside bend radius"),
        False,
        "Material",
    )
    SheetMetalTools.smAddBoolProperty(
        part,
        "UseStandardKFactor",
        translate("App::Property", "Use the material catalog's neutral-axis K-factor"),
        False,
        "Material",
    )
    if "Density" not in part.PropertiesList:
        part.addProperty(
            "App::PropertyDensity",
            "Density",
            "Material",
            translate(
                "App::Property",
                "Bulk material density; catalog controlled when material defaults are enabled",
            ),
        )
        part.Density = "{} kg/m^3".format(
            _DENSITIES_KG_M3["Hot Rolled Steel"]
        )
    if "Weight" not in part.PropertiesList:
        part.addProperty(
            "App::PropertyMass",
            "Weight",
            "Drawing",
            translate(
                "App::Property",
                "Calculated part mass from material density and the current sheet-metal solid",
            ),
        )
    part.setEditorMode("Weight", 1)
    _update_editor_modes(part)
    updatePartWeight(part)


def _effective_material(part, create_configuration=False):
    material = str(part.BaseMaterial)
    if not part.FollowMaterialUpgrade:
        return material
    config = materialConfiguration(part.Document, create_configuration)
    upgrade = str(config.MaterialUpgrade) if config is not None else "Standard"
    return material if upgrade == "Standard" else upgrade


def _update_editor_modes(part):
    if not all(name in part.PropertiesList for name in _MATERIAL_PROPERTY_NAMES):
        return
    catalog = bool(part.UseMaterialCatalog)
    for name in ("BaseMaterial", "SheetSize", "FollowMaterialUpgrade"):
        part.setEditorMode(name, 0 if catalog else 1)
    for name in ("UseStandardBendRadius", "UseStandardKFactor"):
        part.setEditorMode(name, 0 if catalog else 1)
    if "Thickness" in part.PropertiesList:
        part.setEditorMode("Thickness", 1 if catalog else 0)
    if "DefaultBendRadius" in part.PropertiesList:
        part.setEditorMode(
            "DefaultBendRadius",
            1 if catalog and part.UseStandardBendRadius else 0,
        )
    if "KFactor" in part.PropertiesList:
        part.setEditorMode(
            "KFactor", 1 if catalog and part.UseStandardKFactor else 0
        )
    if "Density" in part.PropertiesList:
        part.setEditorMode("Density", 1 if catalog else 0)


def _shape_from_part_tip(part):
    """Return the current formed solid used to calculate a part's weight."""
    tip_name = str(getattr(part, "Tip", "")).strip()
    if tip_name:
        tip = part.Document.getObject(tip_name)
        if tip is not None and hasattr(tip, "Shape"):
            return tip.Shape

    # Make Base Wall may live in a PartDesign Body whose native Tip remains the
    # best final-feature reference, even in older documents with no part Tip.
    for child in reversed(list(getattr(part, "Group", []))):
        if child.TypeId != "PartDesign::Body":
            continue
        body_tip = getattr(child, "Tip", None)
        if body_tip is not None and hasattr(body_tip, "Shape"):
            return body_tip.Shape

    # Legacy Make Base Wall parts predate the string Tip.  Limit this fallback
    # to BaseBend-shaped objects so Unfold or presentation geometry can never
    # become the source of a formed-part weight.
    base_bends = []
    for candidate in part.Document.Objects:
        if candidate is part or findSheetMetalPart(candidate) is not part:
            continue
        if not all(
            name in candidate.PropertiesList
            for name in ("BendSketch", "BendSide", "Thickness", "Radius")
        ):
            continue
        if hasattr(candidate, "Shape"):
            base_bends.append(candidate)
    return base_bends[-1].Shape if base_bends else None


def updatePartWeight(part):
    """Update the unit-aware drawing weight for one Sheet Metal Part."""
    if not _is_sheet_metal_part(part) or not all(
        name in part.PropertiesList for name in ("Density", "Weight")
    ):
        return 0.0
    shape = _shape_from_part_tip(part)
    volume = 0.0
    if shape is not None and not shape.isNull():
        volume = max(0.0, float(shape.Volume))
    mass_kg = volume * float(part.Density.Value)
    if abs(float(part.Weight.Value) - mass_kg) > 1.0e-12:
        part.Weight = mass_kg
    return mass_kg


def applyMaterialDefaults(part, create_configuration=False):
    """Resolve a part's material policy and update its derived defaults."""
    required = set(_MATERIAL_PROPERTY_NAMES) | {
        "Thickness", "DefaultBendRadius", "KFactor"
    }
    if not _is_sheet_metal_part(part) or not required.issubset(part.PropertiesList):
        return None
    if not part.UseMaterialCatalog:
        if part.EffectiveMaterial != "Manual":
            part.EffectiveMaterial = "Manual"
        _update_editor_modes(part)
        return None
    material = _effective_material(part, create_configuration)
    defaults = standardSheetMetalParameters(material, str(part.SheetSize))
    if part.EffectiveMaterial != defaults["material"]:
        part.EffectiveMaterial = defaults["material"]
    if abs(part.Thickness.Value - defaults["thickness"]) > 1.0e-9:
        part.Thickness = defaults["thickness"]
    if (
        part.UseStandardBendRadius
        and abs(part.DefaultBendRadius.Value - defaults["bend_radius"]) > 1.0e-9
    ):
        part.DefaultBendRadius = defaults["bend_radius"]
    if (
        part.UseStandardKFactor
        and abs(float(part.KFactor) - defaults["k_factor"]) > 1.0e-12
    ):
        part.KFactor = defaults["k_factor"]
    density = defaults["density"]
    density_changed = False
    if abs(part.Density.getValueAs("kg/m^3") - density) > 1.0e-9:
        part.Density = "{} kg/m^3".format(density)
        density_changed = True
    _update_editor_modes(part)
    if density_changed:
        updatePartWeight(part)
    return defaults


def configureNewPart(part):
    """Enable catalog defaults and product upgrade behavior on a new part."""
    config = materialConfiguration(part.Document, True)
    part.UseMaterialCatalog = True
    part.BaseMaterial = str(config.DefaultBaseMaterial)
    part.SheetSize = str(config.DefaultSheetSize)
    part.FollowMaterialUpgrade = True
    part.UseStandardBendRadius = True
    part.UseStandardKFactor = True
    return applyMaterialDefaults(part, True)


def materialPropertyValues(part):
    """Capture material controls while upgrading an experimental container."""
    values = {}
    for name in _MATERIAL_PROPERTY_NAMES:
        if name not in part.PropertiesList:
            continue
        if name in ("BaseMaterial", "SheetSize", "EffectiveMaterial"):
            values[name] = str(getattr(part, name))
        elif name == "Density":
            values[name] = part.Density.getValueAs("kg/m^3")
        else:
            values[name] = bool(getattr(part, name))
    return values


def restoreMaterialPropertyValues(part, values):
    """Restore captured controls, applying catalog values only at the end."""
    use_catalog = bool(values.get("UseMaterialCatalog", False))
    for name, value in values.items():
        if name != "UseMaterialCatalog" and name in part.PropertiesList:
            if name == "Density":
                part.Density = "{} kg/m^3".format(value)
            else:
                setattr(part, name, value)
    part.UseMaterialCatalog = use_catalog
    applyMaterialDefaults(part, use_catalog and part.FollowMaterialUpgrade)


def upgradeDocumentMaterialProperties(doc):
    """Expose material controls on Sheet Metal Parts from older documents."""
    for obj in doc.Objects:
        if not _is_sheet_metal_part(obj):
            continue
        addMaterialProperties(obj)
        applyMaterialDefaults(obj)


class _MaterialDefaultsObserver:
    """Propagate material selections and product upgrades into opted-in parts."""

    part_properties = {
        "UseMaterialCatalog",
        "BaseMaterial",
        "SheetSize",
        "FollowMaterialUpgrade",
        "UseStandardBendRadius",
        "UseStandardKFactor",
    }

    def __init__(self):
        self.updating = False
        self.pending_weight_parts = {}

    def _queue_weight_update(self, part):
        doc = getattr(part, "Document", None)
        if doc is None:
            return
        self.pending_weight_parts[(doc.Name, part.Name)] = part

    def slotChangedObject(self, obj, prop):
        if self.updating:
            return
        try:
            self.updating = True
            if _is_sheet_metal_part(obj) and prop in self.part_properties:
                applyMaterialDefaults(
                    obj,
                    prop == "FollowMaterialUpgrade" and obj.FollowMaterialUpgrade,
                )
                return
            if _is_sheet_metal_part(obj) and prop == "Density":
                updatePartWeight(obj)
                return
            if _is_sheet_metal_part(obj) and prop == "Tip":
                self._queue_weight_update(obj)
                return
            if prop == "Shape":
                part = findSheetMetalPart(obj)
                if part is not None and part is not obj:
                    self._queue_weight_update(part)
                return
            if (
                prop == "MaterialUpgrade"
                and hasattr(obj, "SheetMetalConfigurationType")
                and obj.SheetMetalConfigurationType == "MaterialDefaults"
            ):
                for candidate in obj.Document.Objects:
                    if (
                        _is_sheet_metal_part(candidate)
                        and hasattr(candidate, "FollowMaterialUpgrade")
                        and candidate.FollowMaterialUpgrade
                    ):
                        applyMaterialDefaults(candidate)
        finally:
            self.updating = False

    def slotActivateDocument(self, doc):
        if self.updating:
            return
        try:
            self.updating = True
            upgradeDocumentMaterialProperties(doc)
        finally:
            self.updating = False

    def slotRecomputedDocument(self, doc):
        if self.updating:
            return
        try:
            self.updating = True
            document_key = doc.Name
            pending = [
                (key, part)
                for key, part in self.pending_weight_parts.items()
                if key[0] == document_key
            ]
            for key, part in pending:
                self.pending_weight_parts.pop(key, None)
                if getattr(part, "Document", None) is doc:
                    updatePartWeight(part)
        finally:
            self.updating = False

if "_material_defaults_observer" not in globals():
    _material_defaults_observer = _MaterialDefaultsObserver()
    FreeCAD.addDocumentObserver(_material_defaults_observer)
    for _open_document in FreeCAD.listDocuments().values():
        upgradeDocumentMaterialProperties(_open_document)
