# -*- coding: utf-8 -*-

import os
import tempfile
import unittest

import FreeCAD as App
import Part

from SheetMetalTools import ensureDocumentVarSetExpressionOutputs

from SheetMetalBaseCmd import (
    SMBaseBend,
    migrateDocumentBaseBends,
    prepareNewBaseBendPart,
)
from SheetMetalMaterial import (
    CONFIGURATION_VARIABLE,
    materialConfiguration,
    standardSheetMetalParameters,
    updatePartWeight,
)
from SheetMetalShapedFlangeCmd import (
    SMShapedFlange,
    addSheetMetalPartProperties,
    createSheetMetalPart,
    promoteNewSheetMetalPart,
)
from SheetMetalUnfoldCmd import SMUnfold


class TestMaterialDefaults(unittest.TestCase):
    @staticmethod
    def _legacy_base_bend(doc, part_name, feature_name):
        part = doc.addObject("App::Part", part_name)
        body = doc.addObject("PartDesign::Body", part_name + "Body")
        part.addObject(body)
        profile = body.newObject("PartDesign::Feature", part_name + "Profile")
        profile.Shape = Part.makePolygon(
            [App.Vector(0, 0, 0), App.Vector(20, 0, 0)]
        )
        base_bend = body.newObject("PartDesign::FeaturePython", feature_name)
        SMBaseBend(base_bend, profile)
        base_bend.Thickness = 3.175
        base_bend.Radius = 1.25
        base_bend.Length = 50.0
        return part, base_bend

    def test_catalog_matches_standard_gauge_tables(self):
        hrs = standardSheetMetalParameters("Hot Rolled Steel", "14 ga")
        galvanized = standardSheetMetalParameters("Galvanized Steel", "14 ga")
        stainless = standardSheetMetalParameters("Stainless Steel", "14 ga")
        fraction = standardSheetMetalParameters("Stainless Steel", "3/16 in")

        self.assertAlmostEqual(hrs["thickness"] / 25.4, 0.0747, places=7)
        self.assertAlmostEqual(galvanized["thickness"] / 25.4, 0.0785, places=7)
        self.assertAlmostEqual(stainless["thickness"] / 25.4, 0.0781, places=7)
        self.assertAlmostEqual(fraction["thickness"] / 25.4, 0.1875, places=7)
        self.assertEqual(hrs["bend_radius"], hrs["thickness"])
        self.assertAlmostEqual(hrs["k_factor"], 0.44)
        self.assertAlmostEqual(stainless["k_factor"], 0.45)
        self.assertAlmostEqual(hrs["density"], 7850.0)
        self.assertAlmostEqual(stainless["density"], 8000.0)

    def test_computed_varset_properties_propagate_to_consumers(self):
        doc = App.newDocument("SheetMetalVarSetOutputs")
        try:
            cage = doc.addObject("App::VarSet", "Cage")
            cage.addProperty("App::PropertyInteger", "Tiers")
            cage.addProperty("App::PropertyLength", "Step")
            cage.addProperty("App::PropertyLength", "RowHeight")
            cage.Tiers = 2
            cage.Step = 100.0
            cage.setExpression("RowHeight", "Step * Tiers")

            consumer = doc.addObject("PartDesign::Feature", "Panel")
            consumer.addProperty("App::PropertyLength", "Length")
            consumer.setExpression("Length", "Cage.RowHeight")

            ensureDocumentVarSetExpressionOutputs(doc)
            doc.recompute()
            self.assertIn("Output", cage.getPropertyStatus("RowHeight"))
            self.assertAlmostEqual(consumer.Length.Value, 200.0)

            cage.Tiers = 4
            doc.recompute()
            self.assertAlmostEqual(cage.RowHeight.Value, 400.0)
            self.assertAlmostEqual(consumer.Length.Value, 400.0)
        finally:
            App.closeDocument(doc.Name)

    def test_part_weight_uses_catalog_density_and_current_tip_volume(self):
        doc = App.newDocument("SheetMetalPartWeight")
        try:
            part = createSheetMetalPart(doc)
            solid = doc.addObject("Part::Feature", "CurrentSolid")
            solid.Shape = Part.makeBox(10.0, 20.0, 2.0)
            part.addObject(solid)
            part.Tip = solid.Name
            doc.recompute()

            self.assertEqual(
                part.getTypeIdOfProperty("Density"), "App::PropertyDensity"
            )
            self.assertEqual(
                part.getTypeIdOfProperty("Weight"), "App::PropertyMass"
            )
            self.assertAlmostEqual(
                part.Density.getValueAs("kg/m^3"), 7850.0
            )
            self.assertAlmostEqual(
                part.Weight.getValueAs("kg"), 400.0 * 7.85e-6
            )

            solid.Shape = Part.makeBox(10.0, 20.0, 1.0)
            doc.recompute()
            self.assertAlmostEqual(
                part.Weight.getValueAs("kg"), 200.0 * 7.85e-6
            )
        finally:
            App.closeDocument(doc.Name)

    def test_material_upgrade_changes_density_and_weight(self):
        doc = App.newDocument("SheetMetalMaterialWeightUpgrade")
        try:
            part = createSheetMetalPart(doc)
            solid = doc.addObject("Part::Feature", "CurrentSolid")
            solid.Shape = Part.makeBox(10.0, 10.0, 10.0)
            part.addObject(solid)
            part.Tip = solid.Name
            doc.recompute()

            materialConfiguration(doc).MaterialUpgrade = "Stainless Steel"
            doc.recompute()

            self.assertAlmostEqual(
                part.Density.getValueAs("kg/m^3"), 8000.0
            )
            self.assertAlmostEqual(part.Weight.getValueAs("kg"), 0.008)
        finally:
            App.closeDocument(doc.Name)

    def test_manual_density_controls_weight(self):
        doc = App.newDocument("SheetMetalManualWeight")
        try:
            part = doc.addObject("App::Part", "SheetMetalPart")
            addSheetMetalPartProperties(part)
            solid = doc.addObject("Part::Feature", "CurrentSolid")
            solid.Shape = Part.makeBox(10.0, 10.0, 10.0)
            part.addObject(solid)
            part.Tip = solid.Name
            part.Density = "2700 kg/m^3"

            self.assertAlmostEqual(updatePartWeight(part), 0.0027)
            self.assertAlmostEqual(part.Weight.getValueAs("kg"), 0.0027)
        finally:
            App.closeDocument(doc.Name)

    def test_product_upgrade_only_changes_opted_in_parts(self):
        doc = App.newDocument("SheetMetalMaterialUpgrade")
        try:
            upgradeable = createSheetMetalPart(doc)
            fixed_stainless = createSheetMetalPart(doc)
            fixed_stainless.BaseMaterial = "Stainless Steel"
            fixed_stainless.FollowMaterialUpgrade = False

            config = materialConfiguration(doc)
            self.assertEqual(config.TypeId, "App::VarSet")
            self.assertEqual(
                upgradeable.MaterialConfigurationVariable,
                CONFIGURATION_VARIABLE,
            )
            config.MaterialUpgrade = "Corten Steel"

            self.assertEqual(str(upgradeable.EffectiveMaterial), "Corten Steel")
            self.assertAlmostEqual(
                upgradeable.Thickness.Value / 25.4, 0.0747, places=7
            )
            self.assertEqual(
                str(fixed_stainless.EffectiveMaterial), "Stainless Steel"
            )
            self.assertAlmostEqual(
                fixed_stainless.Thickness.Value / 25.4, 0.0781, places=7
            )
            self.assertAlmostEqual(float(fixed_stainless.KFactor), 0.45)
        finally:
            App.closeDocument(doc.Name)

    def test_document_defaults_seed_new_sheet_metal_parts(self):
        doc = App.newDocument("SheetMetalDocumentDefaults")
        try:
            config = materialConfiguration(doc, True)
            config.DefaultBaseMaterial = "Galvanized Steel"
            config.DefaultSheetSize = "16 ga"

            part = createSheetMetalPart(doc)

            self.assertTrue(part.UseMaterialCatalog)
            self.assertEqual(str(part.BaseMaterial), "Galvanized Steel")
            self.assertEqual(str(part.EffectiveMaterial), "Galvanized Steel")
            self.assertEqual(str(part.SheetSize), "16 ga")
            self.assertAlmostEqual(
                part.Thickness.Value / 25.4, 0.0635, places=7
            )
        finally:
            App.closeDocument(doc.Name)

    def test_plain_part_promoted_by_first_face_uses_document_defaults(self):
        doc = App.newDocument("SheetMetalFacePromotionDefaults")
        try:
            config = materialConfiguration(doc, True)
            config.DefaultBaseMaterial = "Galvanized Steel"
            part = doc.addObject("App::Part", "Panel")

            promoteNewSheetMetalPart(part)

            self.assertEqual(part.SheetMetalType, "Part")
            self.assertTrue(part.UseMaterialCatalog)
            self.assertTrue(part.FollowMaterialUpgrade)
            self.assertEqual(str(part.BaseMaterial), "Galvanized Steel")
            self.assertAlmostEqual(
                part.Thickness.Value / 25.4, 0.0785, places=7
            )
        finally:
            App.closeDocument(doc.Name)

    def test_legacy_part_keeps_manual_defaults(self):
        doc = App.newDocument("SheetMetalManualMaterial")
        try:
            part = doc.addObject("App::Part", "SheetMetalPart")
            addSheetMetalPartProperties(part)
            part.Thickness = 2.345
            part.DefaultBendRadius = 4.567
            part.KFactor = 0.37

            addSheetMetalPartProperties(part)

            self.assertFalse(part.UseMaterialCatalog)
            self.assertEqual(part.EffectiveMaterial, "Manual")
            self.assertAlmostEqual(part.Thickness.Value, 2.345)
            self.assertAlmostEqual(part.DefaultBendRadius.Value, 4.567)
            self.assertAlmostEqual(float(part.KFactor), 0.37)
        finally:
            App.closeDocument(doc.Name)

    def test_new_unfold_uses_owning_part_k_factor(self):
        doc = App.newDocument("SheetMetalPartUnfoldDefaults")
        try:
            part = createSheetMetalPart(doc)
            part.UseStandardKFactor = False
            part.KFactor = 0.47
            source = doc.addObject("Part::Feature", "Source")
            source.Shape = Part.makeBox(10.0, 10.0, part.Thickness.Value)
            part.addObject(source)
            unfold = doc.addObject("Part::FeaturePython", "Unfold")
            part.addObject(unfold)

            SMUnfold(unfold, source, ["Face1"])

            self.assertAlmostEqual(float(unfold.KFactor), 0.47)
        finally:
            App.closeDocument(doc.Name)

    def test_material_upgrade_recomputes_face_thickness(self):
        doc = App.newDocument("SheetMetalMaterialGeometry")
        try:
            part = createSheetMetalPart(doc)
            profile = doc.addObject("Part::Feature", "Profile")
            profile.Shape = Part.makePolygon(
                [
                    App.Vector(0, 0, 0),
                    App.Vector(10, 0, 0),
                    App.Vector(10, 10, 0),
                    App.Vector(0, 10, 0),
                    App.Vector(0, 0, 0),
                ]
            )
            part.addObject(profile)
            face = doc.addObject("Part::FeaturePython", "ShapedFlange")
            SMShapedFlange(face, [profile], part)
            part.addObject(face)
            part.Tip = face.Name
            doc.recompute()
            hrs_volume = face.Shape.Volume

            materialConfiguration(doc).MaterialUpgrade = "Stainless Steel"
            doc.recompute()

            self.assertAlmostEqual(hrs_volume, 100.0 * 0.0747 * 25.4)
            self.assertAlmostEqual(face.Shape.Volume, 100.0 * 0.0781 * 25.4)
            self.assertTrue(face.Shape.isValid())
        finally:
            App.closeDocument(doc.Name)

    def test_material_policy_survives_save_and_reopen(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "SheetMetalMaterialDefaults.FCStd")
            doc = App.newDocument("SheetMetalMaterialPersistence")
            part = createSheetMetalPart(doc)
            part.SheetSize = "16 ga"
            doc.recompute()
            doc.saveAs(path)
            App.closeDocument(doc.Name)

            reopened = App.openDocument(path)
            try:
                reopened_part = reopened.getObject("SheetMetalPart")
                config = reopened.getObject("SheetMetalConfiguration")
                config.MaterialUpgrade = "Stainless Steel"
                reopened.recompute()

                self.assertTrue(reopened_part.UseMaterialCatalog)
                self.assertTrue(reopened_part.FollowMaterialUpgrade)
                self.assertEqual(
                    str(reopened_part.EffectiveMaterial), "Stainless Steel"
                )
                self.assertAlmostEqual(
                    reopened_part.Thickness.Value / 25.4, 0.0625, places=7
                )
                self.assertAlmostEqual(float(reopened_part.KFactor), 0.45)
                self.assertAlmostEqual(
                    reopened_part.Density.getValueAs("kg/m^3"), 8000.0
                )
                self.assertIn("Weight", reopened_part.PropertiesList)
            finally:
                App.closeDocument(reopened.Name)

    def test_legacy_saved_part_is_upgraded_when_document_opens(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "LegacySheetMetalPart.FCStd")
            doc = App.newDocument("LegacySheetMetalMaterial")
            part = doc.addObject("App::Part", "SheetMetalPart")
            part.addProperty("App::PropertyString", "SheetMetalType", "Sheet Metal")
            part.SheetMetalType = "Part"
            part.addProperty("App::PropertyLength", "Thickness", "Sheet Metal")
            part.Thickness = 3.175
            doc.saveAs(path)
            App.closeDocument(doc.Name)

            reopened = App.openDocument(path)
            try:
                reopened_part = reopened.getObject("SheetMetalPart")
                self.assertIn("UseMaterialCatalog", reopened_part.PropertiesList)
                self.assertFalse(reopened_part.UseMaterialCatalog)
                self.assertEqual(reopened_part.EffectiveMaterial, "Manual")
                self.assertAlmostEqual(reopened_part.Thickness.Value, 3.175)
                self.assertAlmostEqual(
                    reopened_part.Density.getValueAs("kg/m^3"), 7850.0
                )
                self.assertIn("Weight", reopened_part.PropertiesList)
            finally:
                App.closeDocument(reopened.Name)

    def test_legacy_base_bend_parts_adopt_existing_geometry_as_defaults(self):
        doc = App.newDocument("SheetMetalBaseBendMigration")
        try:
            pairs = [
                self._legacy_base_bend(doc, "Part{:03d}".format(index),
                                       "BaseBend{:03d}".format(index))
                for index in range(3)
            ]

            migrateDocumentBaseBends(doc)
            doc.recompute()

            for part, base_bend in pairs:
                self.assertEqual(part.SheetMetalType, "Part")
                self.assertFalse(part.UseMaterialCatalog)
                self.assertAlmostEqual(part.Thickness.Value, 3.175)
                self.assertAlmostEqual(part.DefaultBendRadius.Value, 1.25)
                self.assertTrue(base_bend.UsePartThickness)
                self.assertTrue(base_bend.UseDefaultBendRadius)
                self.assertEqual(base_bend.PartDefaultsVersion, 1)
                self.assertAlmostEqual(base_bend.Thickness.Value, 3.175)
                self.assertAlmostEqual(base_bend.Radius.Value, 1.25)
                self.assertTrue(base_bend.Shape.isValid())

            first_part, first_base = pairs[0]
            original_volume = first_base.Shape.Volume
            first_part.Thickness = 2.0
            first_part.DefaultBendRadius = 2.5
            doc.recompute()
            self.assertAlmostEqual(first_base.Thickness.Value, 2.0)
            self.assertAlmostEqual(first_base.Radius.Value, 2.5)
            self.assertNotAlmostEqual(first_base.Shape.Volume, original_volume)

            first_base.UseDefaultBendRadius = False
            first_base.Radius = 4.0
            first_part.DefaultBendRadius = 3.0
            doc.recompute()
            self.assertAlmostEqual(first_base.Radius.Value, 4.0)
        finally:
            App.closeDocument(doc.Name)

    def test_legacy_base_bend_with_null_proxy_is_restored(self):
        doc = App.newDocument("SheetMetalNullBaseBendProxy")
        try:
            _part, base_bend = self._legacy_base_bend(
                doc, "Panel", "BaseBend"
            )
            migrateDocumentBaseBends(doc)
            doc.recompute()
            original_min_x = base_bend.Shape.BoundBox.XMin

            base_bend.Proxy = None
            base_bend.BendSketch.Shape = Part.makePolygon(
                [App.Vector(10, 0, 0), App.Vector(30, 0, 0)]
            )
            migrateDocumentBaseBends(doc)
            doc.recompute()

            self.assertEqual(base_bend.Proxy.__class__.__name__, "SMBaseBend")
            self.assertTrue(base_bend.Shape.isValid())
            self.assertNotAlmostEqual(
                base_bend.Shape.BoundBox.XMin, original_min_x
            )
            self.assertAlmostEqual(base_bend.Shape.BoundBox.XMin, 10.0)
        finally:
            App.closeDocument(doc.Name)

    def test_new_base_bend_creates_catalog_driven_sheet_metal_part(self):
        doc = App.newDocument("SheetMetalNewBaseBendPart")
        try:
            profile = doc.addObject("Part::Feature", "Profile")
            profile.Shape = Part.makePolygon(
                [App.Vector(0, 0, 0), App.Vector(20, 0, 0)]
            )
            base_bend = doc.addObject("Part::FeaturePython", "BaseBend")
            SMBaseBend(base_bend, profile)

            part = prepareNewBaseBendPart(base_bend, profile)
            doc.recompute()

            self.assertEqual(part.SheetMetalType, "Part")
            self.assertTrue(part.UseMaterialCatalog)
            self.assertTrue(part.FollowMaterialUpgrade)
            self.assertIs(base_bend.getParentGeoFeatureGroup(), part)
            self.assertTrue(base_bend.UsePartThickness)
            self.assertAlmostEqual(
                base_bend.Thickness.Value / 25.4, 0.0747, places=7
            )
            self.assertAlmostEqual(
                base_bend.Radius.Value, part.DefaultBendRadius.Value
            )
            self.assertAlmostEqual(
                part.Weight.getValueAs("kg"),
                base_bend.Shape.Volume * part.Density.Value,
            )
        finally:
            App.closeDocument(doc.Name)

    def test_new_base_bend_promotes_the_active_body_app_part(self):
        doc = App.newDocument("SheetMetalNewBodyBaseBend")
        try:
            part = doc.addObject("App::Part", "Panel")
            body = doc.addObject("PartDesign::Body", "Body")
            part.addObject(body)
            profile = body.newObject("PartDesign::Feature", "Profile")
            profile.Shape = Part.makePolygon(
                [App.Vector(0, 0, 0), App.Vector(20, 0, 0)]
            )
            base_bend = doc.addObject("PartDesign::FeaturePython", "BaseBend")
            SMBaseBend(base_bend, profile)

            result = prepareNewBaseBendPart(base_bend, profile, body)
            body.addObject(base_bend)
            doc.recompute()

            self.assertIs(result, part)
            self.assertEqual(part.SheetMetalType, "Part")
            self.assertTrue(part.UseMaterialCatalog)
            self.assertIs(base_bend.getParentGeoFeatureGroup(), body)
            self.assertTrue(base_bend.UsePartThickness)
            self.assertAlmostEqual(
                base_bend.Thickness.Value, part.Thickness.Value
            )
            self.assertTrue(base_bend.Shape.isValid())
            self.assertAlmostEqual(
                part.Weight.getValueAs("kg"),
                base_bend.Shape.Volume * part.Density.Value,
            )
        finally:
            App.closeDocument(doc.Name)


if __name__ == "__main__":
    unittest.main()
