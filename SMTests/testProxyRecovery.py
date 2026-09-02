# -*- coding: utf-8 -*-

import unittest

import FreeCAD as App

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
