"""Startup script for one real FreeCAD GUI collaboration peer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import traceback

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtWidgets


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from freecad_collaboration import bootstrap_document, document_state
from freecad_collaboration.qt_session import QtDocumentSession


CONFIG = json.loads(Path(os.environ["FREECAD_COLLABORATION_GUI_PEER_CONFIG"]).read_text())
SESSION = None
DOCUMENT = None
TIMER = None
EDITED = False
DEADLINE = time.monotonic() + 30


def finish(result):
    global SESSION, TIMER
    try:
        Path(CONFIG["result"]).write_text(json.dumps(result, indent=2), encoding="utf-8")
    finally:
        if TIMER is not None:
            TIMER.stop()
            TIMER = None
        if SESSION is not None:
            SESSION.close()
            SESSION = None
        if DOCUMENT is not None:
            App.closeDocument(DOCUMENT.Name)
        QtCore.QTimer.singleShot(50, QtWidgets.QApplication.instance().quit)


def fail(exc):
    finish(
        {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    )


def poll():
    global EDITED
    try:
        if time.monotonic() > DEADLINE:
            raise TimeoutError(f"{CONFIG['role']} GUI peer timed out")
        if SESSION.status == "error":
            raise RuntimeError(SESSION.last_error)

        ready = Path(CONFIG["ready"])
        if SESSION.status in {"connected", "synchronized"} and not ready.exists():
            ready.write_text("ready", encoding="utf-8")

        if (
            CONFIG["role"] == "sender"
            and not EDITED
            and Path(CONFIG["trigger"]).exists()
            and SESSION.status in {"connected", "synchronized"}
        ):
            DOCUMENT.openTransaction("GUI-process resize")
            DOCUMENT.getObject("Box").Length = 91
            DOCUMENT.recompute()
            DOCUMENT.commitTransaction()
            EDITED = True

        if SESSION.revision == 1 and SESSION.status == "synchronized":
            state = document_state(DOCUMENT)
            finish(
                {
                    "ok": True,
                    "role": CONFIG["role"],
                    "revision": SESSION.revision,
                    "length": float(DOCUMENT.getObject("Box").Length),
                    "definition_hash": state.definition_hash,
                    "result_hash": state.result_hash,
                }
            )
    except Exception as exc:
        fail(exc)


def start():
    global SESSION, DOCUMENT, TIMER
    try:
        DOCUMENT = App.newDocument(f"GuiPeer{CONFIG['role'].title()}")
        if CONFIG["role"] == "sender":
            box = DOCUMENT.addObject("Part::Box", "Box")
            box.Length = 10
            bootstrap_document(DOCUMENT)
            DOCUMENT.recompute()
        SESSION = QtDocumentSession(
            DOCUMENT,
            CONFIG["server"],
            f"gui-{CONFIG['role']}",
            environment_id=CONFIG["environment"],
            document_uid=CONFIG["document_uid"],
        )
        if CONFIG["role"] == "sender":
            SESSION.start_share()
        else:
            SESSION.start_join(download_checkpoint=True)
        TIMER = QtCore.QTimer(Gui.getMainWindow())
        TIMER.setInterval(25)
        TIMER.timeout.connect(poll)
        TIMER.start()
    except Exception as exc:
        fail(exc)


QtCore.QTimer.singleShot(0, start)
