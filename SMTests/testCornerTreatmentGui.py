# -*- coding: utf-8 -*-
"""Run inside FreeCAD's GUI (also registered with the Test workbench)."""

import unittest
from unittest import mock

import FreeCAD as App
import Part
import SheetMetalCornerTreatmentCmd as C
from SMTests.testCornerTreatment import _edge_names

if App.GuiUp:
    import FreeCADGui as Gui
    from PySide import QtCore
    try:
        from PySide.QtTest import QTest
    except ImportError:
        from PySide6.QtTest import QTest


@unittest.skipUnless(App.GuiUp, "Requires FreeCAD GUI")
class TestCornerTreatmentGui(unittest.TestCase):
    def setUp(self):
        self.doc = App.newDocument("CornerGuiRegression")
        self.doc.UndoMode = 1
        self.schema = App.Units.getSchema()
        self.body = self.doc.addObject("PartDesign::Body", "Body")
        self.base = self.body.newObject("PartDesign::Feature", "Base")
        self.base.Shape = Part.makeBox(60, 40, 2)
        self.doc.recompute()
        Gui.activeDocument().activeView().setActiveObject("pdbody", self.body)
        self.edges = _edge_names(self.base.Shape)
        self.long_edge = next("Edge%d" % (i + 1) for i, e in enumerate(self.base.Shape.Edges)
                              if e.Length > 2)
        self.panels = []
        original = C.SMCornerTreatmentTaskPanel

        def capture(obj):
            panel = original(obj)
            self.panels.append(panel)
            return panel

        self.panel_patch = mock.patch.object(C, "SMCornerTreatmentTaskPanel", side_effect=capture)
        self.panel_patch.start()

    def tearDown(self):
        for panel in self.panels:
            if not panel._closed:
                panel.reject()
        self.panel_patch.stop()
        Gui.Selection.clearSelection()
        App.closeDocument(self.doc.Name)
        App.Units.setSchema(self.schema)

    def createPanel(self):
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(self.base, self.edges[0])
        command = C.AddCornerTreatmentCommand()
        self.assertTrue(command.IsActive())
        command.Activated()
        return self.panels[-1]

    def test_body_contains_one_entry_and_command_cannot_reenter(self):
        panel = self.createPanel()
        self.assertEqual(self.body.Group.count(panel.obj), 1)
        panel.form.AddRemove.click()
        command = C.AddCornerTreatmentCommand()
        self.assertFalse(command.IsActive())
        command.Activated()
        self.assertEqual(len(self.panels), 1)
        self.assertTrue(panel.accept())
        self.assertEqual(self.body.Group.count(panel.obj), 1)
        self.doc.undo()
        self.assertEqual(self.body.Group, [self.base])
        self.doc.redo()
        self.assertEqual([o.Name for o in self.body.Group], ["Base", "CornerTreatment"])

    def test_native_gate_rejects_invalid_picks_and_cleans_up(self):
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(self.base, self.long_edge)
        self.assertFalse(C.AddCornerTreatmentCommand().IsActive())
        panel = self.createPanel()
        panel.form.AddRemove.click()
        self.assertTrue(panel._gateActive)
        Gui.Selection.clearSelection()
        for name in ("Face1", self.long_edge):
            Gui.Selection.addSelection(self.base, name)
            self.assertFalse(Gui.Selection.getSelectionEx(), name)
        # Native Body subelement paths must resolve to the source feature.
        Gui.Selection.addSelection(self.doc.Name, self.body.Name,
                                   self.base.Name + "." + self.edges[0])
        self.assertTrue(Gui.Selection.getSelectionEx())
        panel.form.AddRemove.click()
        self.assertTrue(panel.selParams.SelectState)
        self.assertFalse(panel._gateActive)
        self.assertTrue(panel.accept())
        Gui.Selection.addSelection(self.base, "Face1")
        self.assertTrue(Gui.Selection.getSelectionEx())

    def test_invalid_selection_is_identified_inline(self):
        panel = self.createPanel()
        panel.form.AddRemove.click()
        # Simulate a stale/programmatically injected selection bypassing the gate.
        Gui.Selection.removeSelectionGate()
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(self.base, "Face1")
        Gui.Selection.addSelection(self.base, self.edges[0])
        Gui.Selection.addSelectionGate(panel.gate)
        panel.form.AddRemove.click()
        self.assertFalse(panel.selParams.SelectState)
        self.assertIn("Face1", panel.form.ErrorMessage.text())
        self.assertFalse(panel.form.ErrorMessage.isHidden())
        items = [panel.form.tree.topLevelItem(i)
                 for i in range(panel.form.tree.topLevelItemCount())]
        invalid = next(item for item in items if item.text(1) == "Face1")
        self.assertEqual(invalid.foreground(1).color().name(), "#9f1239")
        self.assertFalse(panel.accept())
        # Remove just the highlighted invalid row and keep the valid corner.
        invalid = next(panel.form.tree.topLevelItem(i)
                       for i in range(panel.form.tree.topLevelItemCount())
                       if panel.form.tree.topLevelItem(i).text(1) == "Face1")
        invalid.setCheckState(0, QtCore.Qt.Checked)
        panel.form.pushClearSel.click()
        panel.form.AddRemove.click()
        self.assertTrue(panel.selParams.SelectState)
        self.assertTrue(panel.form.ErrorMessage.isHidden())

    def test_failed_preview_is_visible_and_recovers(self):
        panel = self.createPanel()
        panel.form.Radius.setProperty("rawValue", 1000.0)
        self.assertIn("Reduce the size", panel.form.ErrorMessage.text())
        self.assertFalse(panel.form.ErrorMessage.isHidden())
        self.assertTrue(panel.obj.Shape.isNull())
        self.assertTrue(self.base.Visibility)
        self.assertFalse(panel.obj.Visibility)
        self.assertFalse(panel.accept())
        panel.form.Radius.setProperty("rawValue", 3.0)
        self.assertTrue(panel.form.ErrorMessage.isHidden())
        self.assertTrue(panel.obj.Shape.isValid())
        self.assertFalse(self.base.Visibility)
        self.assertTrue(panel.obj.Visibility)
        panel.form.AddRemove.click()
        panel.reject()
        self.assertFalse(panel._gateActive)
        self.assertFalse(panel._documentObserverActive)
        self.assertEqual(self.body.Group, [self.base])
        Gui.Selection.clearSelection()
        Gui.Selection.addSelection(self.base, "Face1")
        self.assertTrue(Gui.Selection.getSelectionEx())

    def test_document_units_display_and_parse_bare_dimensions(self):
        self.doc.UnitSystem = 3
        App.Units.setSchema(0)  # The document setting wins over the global setting.
        panel = self.createPanel()
        for spin in (panel.form.Radius, panel.form.ChamferSize):
            self.assertEqual(spin.property("unit"), "in")
            self.assertIn("in", spin.property("text"))
        # Unitless input must mean inches, not FreeCAD's internal millimetres.
        panel.form.Radius.setFocus()
        panel.form.Radius.selectAll()
        QTest.keyClicks(panel.form.Radius, "0.25")
        QTest.keyClick(panel.form.Radius, QtCore.Qt.Key_Tab)
        self.assertAlmostEqual(panel.obj.Radius.Value, 6.35)
        self.assertFalse(panel._closed)
        self.doc.UnitSystem = 0
        Gui.updateGui()
        self.assertEqual(panel.form.Radius.property("unit"), "mm")
        self.assertIn("mm", panel.form.Radius.property("text"))
        self.assertAlmostEqual(panel.obj.Radius.Value, 6.35)
        panel.form.Radius.setFocus()
        panel.form.Radius.selectAll()
        QTest.keyClicks(panel.form.Radius, "3")
        QTest.keyClick(panel.form.Radius, QtCore.Qt.Key_Tab)
        self.assertAlmostEqual(panel.obj.Radius.Value, 3)
        self.doc.UnitSystem = 2
        Gui.updateGui()
        # The US customary schema spells inches with a double-prime/quote.
        unit = panel.form.Radius.property("unit")
        self.assertAlmostEqual(App.Units.Quantity("1 " + unit).Value, 25.4)
        self.assertIn(unit, panel.form.Radius.property("text"))
        self.assertAlmostEqual(panel.obj.Radius.Value, 3)
