# -*- coding: utf-8 -*-

import os
import tempfile
import unittest

import FreeCAD as App
import Part

from SheetMetalMaterial import (
    CONFIGURATION_VARIABLE,
    materialConfiguration,
    standardSheetMetalParameters,
)
from SheetMetalShapedFlangeCmd import (
    SMShapedFlange,
    addSheetMetalPartProperties,
    createSheetMetalPart,
)
from SheetMetalUnfoldCmd import SMUnfold


class TestMaterialDefaults(unittest.TestCase):
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
            finally:
                App.closeDocument(reopened.Name)


if __name__ == "__main__":
    unittest.main()
