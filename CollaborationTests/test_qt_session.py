"""Qt main-thread collaboration controller integration test."""

from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import urlopen

import FreeCAD as App
from PySide import QtCore

from freecad_collaboration import bootstrap_document, get_object_uid
from freecad_collaboration.qt_session import QtDocumentSession


ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class QtDocumentSessionTests(unittest.TestCase):
    def setUp(self):
        self.application = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])
        self.temp = tempfile.TemporaryDirectory(prefix="freecad-qt-collaboration-")
        self.database = Path(self.temp.name) / "relay.sqlite3"
        self.port = free_port()
        self.server_url = f"http://127.0.0.1:{self.port}"
        self.server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "freecad_collaboration.server",
                "--database",
                str(self.database),
                "--port",
                str(self.port),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._wait_for_server()
        self.documents = []
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            session.close()
        for document in reversed(self.documents):
            try:
                App.closeDocument(document.Name)
            except Exception:
                pass
        self.server.terminate()
        try:
            self.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=5)
        if self.server.stdout is not None:
            self.server.stdout.close()
        self.temp.cleanup()

    def _wait_for_server(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with urlopen(f"{self.server_url}/health", timeout=1) as response:
                    if response.status == 200:
                        return
            except Exception:
                time.sleep(0.05)
        self.fail("relay did not start")

    def _wait_until(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition was not reached")

    def test_remote_packet_is_applied_on_qt_main_thread(self):
        source = App.newDocument("QtSource")
        self.documents.append(source)
        box = source.addObject("Part::Box", "Box")
        bootstrap_document(source)
        source.recompute()
        logical_uid = str(source.Uid)

        target = App.newDocument("QtTarget")
        self.documents.append(target)

        source_session = QtDocumentSession(
            source,
            self.server_url,
            "qt-source",
            environment_id="qt-test",
            document_uid=logical_uid,
        )
        target_session = QtDocumentSession(
            target,
            self.server_url,
            "qt-target",
            environment_id="qt-test",
            document_uid=logical_uid,
        )
        self.sessions.extend((source_session, target_session))
        source_session.start_share()
        self._wait_until(lambda: source_session.status == "connected")

        App.setActiveDocument(source.Name)
        source.openTransaction("Qt resize")
        box.Length = 66
        source.recompute()
        source.commitTransaction()

        self._wait_until(
            lambda: source_session.revision == 1
            and source_session.status == "synchronized"
        )
        target_session.start_join(download_checkpoint=True)

        self._wait_until(
            lambda: target_session.revision == 1
            and abs(float(target.Box.Length) - 66.0) < 1e-9
            and source_session.status == "synchronized"
            and target_session.status == "synchronized"
        )
        self.assertEqual(source_session.revision, 1)
        self.assertEqual(target_session.revision, 1)

        object_uid = get_object_uid(target.Box)
        target_session.acquire_locks([object_uid])
        self._wait_until(
            lambda: object_uid in source_session.locks
            and object_uid in target_session.locks
            and source_session.locks[object_uid]["client_id"] == "qt-target"
        )
        target_session.release_locks([object_uid])
        self._wait_until(
            lambda: object_uid not in source_session.locks
            and object_uid not in target_session.locks
        )

        App.setActiveDocument(target.Name)
        target.openTransaction("Qt resize from target")
        target.Box.Width = 27
        target.recompute()
        target.commitTransaction()

        self._wait_until(
            lambda: source_session.revision == 2
            and target_session.revision == 2
            and abs(float(source.Box.Width) - 27.0) < 1e-9
            and source_session.status == "synchronized"
            and target_session.status == "synchronized"
        )


if __name__ == "__main__":
    unittest.main()
