"""Open one persistent FreeCAD GUI client for a manual collaboration test.

The launcher reads its configuration from ``FREECAD_COLLAB_LIVE_*``
environment variables. Unlike the automated smoke-test peers, this client
stays open for interactive use.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback
import importlib

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtWidgets


# Python feature proxies must already be importable when FreeCAD restores an
# FCStd checkpoint.  Its safe-unpickling policy deliberately refuses to import
# an unknown module merely because a document names it.  Activating the
# workbench (and explicitly importing the proxies used by the collaboration
# fixture) establishes the trusted, installed code before opening either copy.
MODEL_PROXY_MODULES = (
    "SheetMetalBaseCmd",
    "SheetMetalShapedFlangeCmd",
    "SheetMetalBoltConnectionCmd",
    "SheetMetalConnectedPatternCmd",
    "SheetMetalCmd",
)


def _preload_model_proxies():
    try:
        Gui.activateWorkbench("SMWorkbench")
    except Exception:
        # Direct imports below are authoritative for persistence restore; the
        # workbench activation is only useful for its wider command setup.
        pass
    for module_name in MODEL_PROXY_MODULES:
        importlib.import_module(module_name)

def _required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing environment variable {name}")
    return value


def start_client():
    runtime = _required("FREECAD_COLLAB_LIVE_RUNTIME")
    if runtime not in sys.path:
        sys.path.insert(0, runtime)
    from freecad_collaboration.gui import show_panel

    role = _required("FREECAD_COLLAB_LIVE_ROLE")
    server = _required("FREECAD_COLLAB_LIVE_SERVER")
    environment_id = _required("FREECAD_COLLAB_LIVE_ENVIRONMENT")
    document_uid = _required("FREECAD_COLLAB_LIVE_DOCUMENT_UID")

    _preload_model_proxies()

    if role == "source":
        source_path = _required("FREECAD_COLLAB_LIVE_SOURCE")
        document = App.openDocument(source_path)
    elif role == "joiner":
        document = App.newDocument("EnrichedCondoCollaborationClientB")
    else:
        raise RuntimeError(f"unsupported live-client role: {role}")

    App.setActiveDocument(document.Name)
    Gui.activeDocument().activeView().viewAxonometric()
    Gui.activeDocument().activeView().fitAll()

    dock = show_panel()
    panel = dock.widget()
    panel.relay.setText(server)
    panel.client.setText(f"manual-{role}")
    panel.environment.setText(environment_id)
    panel.document_uid.setText(document_uid)
    panel._start(role == "source")
    if panel.session is None:
        raise RuntimeError(panel.log.toPlainText() or "collaboration session did not start")
    # Joining an empty GUI document may replace it with the native FCStd
    # checkpoint document. Follow the session's authoritative document.
    document = panel.session.document

    fitted = {"done": role == "source"}

    def refresh_joined_view():
        if panel.session is not None and panel.session.status == "error":
            log_path = os.environ.get("FREECAD_COLLAB_LIVE_LOG", "").strip()
            if log_path and not Path(log_path).exists():
                Path(log_path).write_text(
                    panel.session.last_error or panel.log.toPlainText(), encoding="utf-8"
                )
        if role == "joiner" and document.Objects and not fitted["done"]:
            Gui.activeDocument().activeView().viewAxonometric()
            Gui.activeDocument().activeView().fitAll()
            fitted["done"] = True

    timer = QtCore.QTimer(Gui.getMainWindow())
    timer.setInterval(500)
    timer.timeout.connect(refresh_joined_view)
    timer.start()
    Gui.getMainWindow()._collaboration_live_view_timer = timer


def guarded_start_client():
    try:
        start_client()
    except Exception as exc:
        error = traceback.format_exc()
        log_path = os.environ.get("FREECAD_COLLAB_LIVE_LOG", "").strip()
        if log_path:
            Path(log_path).write_text(error, encoding="utf-8")
        QtWidgets.QMessageBox.critical(
            Gui.getMainWindow(),
            "Collaboration client failed",
            f"{type(exc).__name__}: {exc}\n\nSee: {log_path or 'Python console'}",
        )


QtCore.QTimer.singleShot(0, guarded_start_client)
