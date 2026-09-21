########################################################################
#
#  InitGui.py
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

import SheetMetalTools
from engineering_mode import engineering_mode_enabled

Gui = FreeCAD.Gui
SMWBPath = SheetMetalTools.mod_path
SMIconPath = SheetMetalTools.icons_path

# Add translations path.
LanguagePath = os.path.join(SMWBPath, "Resources", "translations")
Gui.addLanguagePath(LanguagePath)
Gui.updateLocale()


class SMWorkbench(Workbench):
    global SHEETMETALWB_VERSION
    global SMIconPath
    global SMWBPath
    global engineering_mode_enabled

    MenuText = FreeCAD.Qt.translate("SheetMetal", "Sheet Metal")
    ToolTip = FreeCAD.Qt.translate(
        "SheetMetal",
        "Sheet Metal workbench allows for designing and unfolding sheet metal parts",
        )
    Icon = os.path.join(SMIconPath, "SMLogo.svg")

    def Initialize(self):
        """Execute when FreeCAD starts."""
        import os.path

        # Import all the needed files that create the workbench commands.
        import ExtrudedCutout
        import SheetMetalBaseCmd
        import SheetMetalBaseShapeCmd
        import SheetMetalBend
        import SheetMetalBoltConnectionCmd
        import SheetMetalConnectedPatternCmd
        import SheetMetalMirroredPartCmd
        import SheetMetalCmd
        import SheetMetalHem
        import SheetMetalCornerReliefCmd
        import SheetMetalCornerTreatmentCmd
        import SheetMetalExtendCmd
        import SheetMetalFoldCmd
        import SheetMetalFormingCmd
        import SheetMetalJunction
        import SheetMetalRelief
        import SheetMetalUnfoldCmd
        import SheetMetalUnfolder
        import SketchOnSheetMetalCmd
        import SheetMetalSketch
        import SheetMetalShapedFlangeCmd
        import SheetMetalFromSolid
        import SheetMetalWeldedMeshCmd

        unfold_commands = [
            "SheetMetal_Unfold", "SheetMetal_UnfoldUpdate",
            "SheetMetal_ToggleFlatPatternWorkspace",
        ]
        if engineering_mode_enabled():
            unfold_commands.insert(1, "SheetMetal_UnattendedUnfold")

        # One definition keeps toolbar, menu and context-menu ordering aligned.
        self.commandGroups = [
            (FreeCAD.Qt.translate("SheetMetal", "Create"),
             FreeCAD.Qt.translate("SheetMetal", "Sheet Metal Create"), [
                 "SheetMetal_NewSketch",
                 "SheetMetal_ShapedFlange",
                 "SheetMetal_AddBase",
                 "SheetMetal_BaseShape",
                 "SheetMetal_FromSolid",
             ]),
            (FreeCAD.Qt.translate("SheetMetal", "Shape"),
             FreeCAD.Qt.translate("SheetMetal", "Sheet Metal Shape"), [
                 "SheetMetal_AddWall",
                 "SheetMetal_AddFoldWall",
                 "SheetMetal_AddBend",
                 "SheetMetal_AddHem",
                 "SheetMetal_Extrude",
                 "SheetMetal_ExtendBySketch",
                 "SheetMetal_Forming",
             ]),
            (FreeCAD.Qt.translate("SheetMetal", "Cuts and Corners"),
             FreeCAD.Qt.translate("SheetMetal", "Sheet Metal Cuts and Corners"), [
                 "SheetMetal_SketchOnSheet",
                 "SheetMetal_AddCutout",
                 "SheetMetal_AddJunction",
                 "SheetMetal_AddRelief",
                 "SheetMetal_AddCornerRelief",
                 "SheetMetal_CornerTreatment",
             ]),
            (FreeCAD.Qt.translate("SheetMetal", "Assembly"),
             FreeCAD.Qt.translate("SheetMetal", "Sheet Metal Assembly"), [
                 "SheetMetal_PlacePartByLCS",
                 "SheetMetal_BoltConnection",
                 "SheetMetal_ConnectedPartPattern",
                 "SheetMetal_MirroredPart",
             ]),
            (FreeCAD.Qt.translate("SheetMetal", "Unfold"),
             FreeCAD.Qt.translate("SheetMetal", "Sheet Metal Unfold"), unfold_commands),
            (FreeCAD.Qt.translate("SheetMetal", "Mesh"),
             FreeCAD.Qt.translate("SheetMetal", "Sheet Metal Mesh"), [
                 "SheetMetal_WeldedMesh",
                 "SheetMetal_EditableMesh",
             ]),
        ]
        menu = FreeCAD.Qt.translate("SheetMetal", "&Sheet Metal")
        self.list = []
        for section, toolbar, commands in self.commandGroups:
            self.appendToolbar(toolbar, commands)
            self.appendMenu([menu, section], commands)
            if self.list:
                self.list.append("Separator")
            self.list.extend(commands)
        Gui.addPreferencePage(os.path.join(SMWBPath, "Resources/panels/SMprefs.ui"), "SheetMetal")
        Gui.addIconPath(SMIconPath)

    def Activated(self):
        """Execute when the workbench is activated."""
        return

    def Deactivated(self):
        """Execute when the workbench is deactivated."""
        return

    def ContextMenu(self, recipient):
        """Execute whenever the user right-clicks on screen."""
        # `recipient` will be either `view` or `tree`.
        #
        self.appendContextMenu(FreeCAD.Qt.translate("SheetMetal", "Sheet Metal"), self.list)
        if recipient and "tree" in str(recipient).lower():
            import SheetMetalTools
            sel = Gui.Selection.getSelection()
            if sel and SheetMetalTools.smIsPartDesign(sel[0]):
                self.appendContextMenu([], ["PartDesign_MoveTip"])

    def GetClassName(self):
        # This function is mandatory if this is a full python workbench.
        return "Gui::PythonWorkbench"


Gui.addWorkbench(SMWorkbench())
