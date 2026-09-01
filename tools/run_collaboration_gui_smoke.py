"""Exercise the collaboration dock inside a real FreeCAD GUI process.

FreeCAD runs this file as a startup script. The parent smoke-test process sets
``FREECAD_COLLABORATION_GUI_RESULT`` to a temporary JSON result path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import traceback

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtWidgets


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def run():
    result_path = Path(os.environ["FREECAD_COLLABORATION_GUI_RESULT"])
    result = {"ok": False}
    try:
        from freecad_collaboration.commands import register_commands
        from freecad_collaboration.gui import DOCK_OBJECT_NAME, show_panel

        register_commands()
        document = App.newDocument("CollaborationGuiSmoke")
        document.addObject("Part::Box", "Box")
        document.recompute()
        dock = show_panel()
        panel = dock.widget()
        panel.refresh()
        result = {
            "ok": True,
            "dock_object_name": dock.objectName(),
            "dock_visible": dock.isVisible(),
            "document_name": panel.document_name.text(),
            "document_uid": panel.document_uid.text(),
            "show_command_registered": "Collaboration_ShowPanel" in Gui.listCommands(),
            "disconnect_command_registered": "Collaboration_Disconnect" in Gui.listCommands(),
        }
        if result["dock_object_name"] != DOCK_OBJECT_NAME:
            raise RuntimeError("collaboration dock has the wrong object name")
    except Exception as exc:
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    finally:
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        if App.ActiveDocument is not None:
            App.closeDocument(App.ActiveDocument.Name)
        QtCore.QTimer.singleShot(100, QtWidgets.QApplication.instance().quit)


QtCore.QTimer.singleShot(0, run)
