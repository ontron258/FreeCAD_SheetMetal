"""FreeCAD commands for the collaboration prototype."""

from __future__ import annotations

import FreeCAD as App
import FreeCADGui as Gui


class ShowPanelCommand:
    def GetResources(self):
        return {
            "MenuText": "Collaboration panel",
            "ToolTip": "Share or join the active document through a collaboration relay",
        }

    def Activated(self):
        from .gui import show_panel

        show_panel()

    def IsActive(self):
        return True


class DisconnectCommand:
    def GetResources(self):
        return {
            "MenuText": "Disconnect collaboration",
            "ToolTip": "Disconnect the active document from its collaboration relay",
        }

    def Activated(self):
        from .gui import disconnect_active_document

        disconnect_active_document()

    def IsActive(self):
        if App.ActiveDocument is None:
            return False
        try:
            from .gui import controller

            return controller().session_for(App.ActiveDocument) is not None
        except Exception:
            return False


COMMANDS = ["Collaboration_ShowPanel", "Collaboration_Disconnect"]
_registered = False


def register_commands():
    global _registered
    if _registered:
        return COMMANDS
    Gui.addCommand("Collaboration_ShowPanel", ShowPanelCommand())
    Gui.addCommand("Collaboration_Disconnect", DisconnectCommand())
    _registered = True
    return COMMANDS

