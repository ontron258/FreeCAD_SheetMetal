# -*- coding: utf-8 -*-

import unittest

import FreeCAD as App
import Part

from SheetMetalBoltConnectionCmd import (
    SMBoltConnection,
    SMBoltConnectionCut,
    repair_bolt_connection_proxies,
)
from SheetMetalCmd import SMBendWall, repairDocumentBendWalls
from SheetMetalShapedFlangeCmd import (
    SMShapedFlange,
    migrateDocumentFaceGeometry,
)


class TestProxyRecovery(unittest.TestCase):
    def test_unresolved_bend_reference_clears_stale_shape_and_requires_reselection(self):
        doc = App.newDocument("UnresolvedBendReference")
        try:
            base = doc.addObject("Part::Feature", "Base")
            base.Shape = Part.makeBox(40, 30, 2)
            edge = next("Edge%d" % (i + 1) for i, e in enumerate(base.Shape.Edges)
                        if abs(e.Length - 30) < 1.e-6
                        and all(abs(v.Point.z) < 1.e-6 for v in e.Vertexes))
            bend = doc.addObject("Part::FeaturePython", "Bend")
            SMBendWall(bend, base, [edge])
            bend.radius = 1
            bend.length = 10
            bend.angle = 90
            bend.Proxy.execute(bend)
            self.assertTrue(bend.Shape.isValid())
            volume = bend.Shape.Volume
            for names in (["?" + edge], ["Base.?" + edge], []):
                with self.subTest(names=names):
                    # Mimic a stale result left by a formerly valid reference.
                    bend.Shape = base.Shape
                    bend.baseObject = (base, names)
                    with self.assertRaisesRegex(ValueError, "Reselect the bend"):
                        bend.Proxy.execute(bend)
                    self.assertTrue(bend.Shape.isNull())
            bend.baseObject = (base, [edge])
            bend.Proxy.execute(bend)
            self.assertTrue(bend.Shape.isValid())
            self.assertAlmostEqual(bend.Shape.Volume, volume)
        finally:
            App.closeDocument(doc.Name)

    def test_face_proxy_is_reattached_from_saved_schema(self):
        doc = App.newDocument("RecoverFaceProxy")
        try:
            feature = doc.addObject("Part::FeaturePython", "Face")
            feature.addProperty("App::PropertyString", "SheetMetalType")
            feature.SheetMetalType = "Face"
            SMShapedFlange.addVerifyProperties(None, feature)
            feature.Proxy = None

            migrateDocumentFaceGeometry(doc)

            self.assertIsInstance(feature.Proxy, SMShapedFlange)
        finally:
            App.closeDocument(doc.Name)

    def test_bolt_connection_and_cut_proxies_are_reattached(self):
        doc = App.newDocument("RecoverBoltProxies")
        try:
            connection = doc.addObject("App::FeaturePython", "Connection")
            SMBoltConnection(connection)
            cut = doc.addObject("Part::FeaturePython", "ConnectionCut")
            SMBoltConnectionCut(cut)
            connection.Proxy = None
            cut.Proxy = None

            repaired = repair_bolt_connection_proxies(doc)

            self.assertEqual(repaired, [connection, cut])
            self.assertIsInstance(connection.Proxy, SMBoltConnection)
            self.assertIsInstance(cut.Proxy, SMBoltConnectionCut)
        finally:
            App.closeDocument(doc.Name)

    def test_bend_proxy_is_reattached_from_saved_schema(self):
        doc = App.newDocument("RecoverBendProxy")
        try:
            bend = doc.addObject("Part::FeaturePython", "Bend")
            SMBendWall.addVerifyProperties(None, bend)
            bend.addProperty("App::PropertyLinkSub", "baseObject", "Parameters")
            bend.Proxy = None

            repaired = repairDocumentBendWalls(doc)

            self.assertEqual(repaired, [bend])
            self.assertIsInstance(bend.Proxy, SMBendWall)
        finally:
            App.closeDocument(doc.Name)
