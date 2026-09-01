"""Dockable FreeCAD GUI for the collaboration prototype."""

from __future__ import annotations

import socket
import urllib.request
import uuid

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtWidgets

from .identity import bootstrap_document, ensure_object_uid
from .environment import default_environment_id, write_environment_lock
from .checkpoint import open_checkpoint
from .qt_session import QtDocumentSession


PREFERENCES_PATH = "User parameter:BaseApp/Preferences/CollaborationPrototype"
DOCK_OBJECT_NAME = "FreeCADCollaborationDock"


def _preferences():
    return App.ParamGet(PREFERENCES_PATH)


def _default_client_id():
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


class CollaborationController(QtCore.QObject):
    """Own at most one interactive collaboration session per document."""

    sessionChanged = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.sessions = {}
        App.addDocumentObserver(self)

    def session_for(self, document):
        return self.sessions.get(document.Name) if document is not None else None

    def start(self, document, base_url, client_id, environment_id, document_uid, share):
        if document is None:
            raise RuntimeError("Open or create a document first")
        self.disconnect(document)
        download_checkpoint = not bool(document.Objects) and not share
        if download_checkpoint:
            if not document_uid:
                raise RuntimeError("Enter the shared document UUID before joining")
            checkpoint_url = (
                f"{base_url.rstrip('/')}/documents/{document_uid}/checkpoint"
            )
            with urllib.request.urlopen(checkpoint_url, timeout=30) as response:
                checkpoint = response.read()
            App.closeDocument(document.Name)
            document = open_checkpoint(checkpoint)
            App.setActiveDocument(document.Name)
            download_checkpoint = False
        bootstrap_document(document)
        document.recompute()
        session = QtDocumentSession(
            document,
            base_url,
            client_id,
            environment_id=environment_id,
            document_uid=document_uid or str(document.Uid),
            native_checkpoints=True,
            parent=self,
        )
        self.sessions[document.Name] = session
        if share:
            session.start_share()
        else:
            session.start_join(download_checkpoint=download_checkpoint)
        self.sessionChanged.emit(session)
        return session

    def disconnect(self, document=None):
        document = document or App.ActiveDocument
        if document is None:
            return
        session = self.sessions.pop(document.Name, None)
        if session is not None:
            session.close()
            self.sessionChanged.emit(None)

    def close_all(self):
        for session in list(self.sessions.values()):
            session.close()
        self.sessions.clear()
        self.sessionChanged.emit(None)

    def slotDeletedDocument(self, document):
        session = self.sessions.pop(document.Name, None)
        if session is not None:
            session.close()
            self.sessionChanged.emit(None)


class CollaborationPanel(QtWidgets.QWidget):
    """Connection controls and live status for the active document."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.session = None
        self._build_ui()
        self._load_preferences()
        self.controller.sessionChanged.connect(self.refresh)
        self.document_timer = QtCore.QTimer(self)
        self.document_timer.setInterval(500)
        self.document_timer.timeout.connect(self.refresh)
        self.document_timer.start()
        self.refresh()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        form = QtWidgets.QFormLayout()
        self.relay = QtWidgets.QLineEdit()
        self.client = QtWidgets.QLineEdit()
        self.environment = QtWidgets.QLineEdit()
        self.document_uid = QtWidgets.QLineEdit()
        self.document_uid.setPlaceholderText("Uses the active Document.Uid")
        form.addRow("Relay URL", self.relay)
        form.addRow("Client ID", self.client)
        form.addRow("Environment", self.environment)
        form.addRow("Document UUID", self.document_uid)
        layout.addLayout(form)

        buttons = QtWidgets.QHBoxLayout()
        self.share_button = QtWidgets.QPushButton("Share current")
        self.join_button = QtWidgets.QPushButton("Join")
        self.disconnect_button = QtWidgets.QPushButton("Disconnect")
        buttons.addWidget(self.share_button)
        buttons.addWidget(self.join_button)
        buttons.addWidget(self.disconnect_button)
        layout.addLayout(buttons)

        self.discard_conflict_button = QtWidgets.QPushButton(
            "Discard conflicting local edits"
        )
        self.discard_conflict_button.setToolTip(
            "Return this document to the latest accepted server revision"
        )
        layout.addWidget(self.discard_conflict_button)

        self.save_lockfile_button = QtWidgets.QPushButton("Save environment lockfile")
        layout.addWidget(self.save_lockfile_button)

        lock_buttons = QtWidgets.QHBoxLayout()
        self.lock_button = QtWidgets.QPushButton("Lock selection")
        self.unlock_button = QtWidgets.QPushButton("Unlock selection")
        lock_buttons.addWidget(self.lock_button)
        lock_buttons.addWidget(self.unlock_button)
        layout.addLayout(lock_buttons)

        status_form = QtWidgets.QFormLayout()
        self.document_name = QtWidgets.QLabel("-")
        self.status = QtWidgets.QLabel("disconnected")
        self.revision = QtWidgets.QLabel("0")
        self.lock_count = QtWidgets.QLabel("0")
        self.validation = QtWidgets.QLabel("not validated")
        status_form.addRow("Active document", self.document_name)
        status_form.addRow("Status", self.status)
        status_form.addRow("Revision", self.revision)
        status_form.addRow("Feature locks", self.lock_count)
        status_form.addRow("Headless validation", self.validation)
        layout.addLayout(status_form)

        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(200)
        self.log.setPlaceholderText("Connection and validation messages")
        layout.addWidget(self.log)

        self.share_button.clicked.connect(lambda: self._start(True))
        self.join_button.clicked.connect(lambda: self._start(False))
        self.disconnect_button.clicked.connect(self._disconnect)
        self.discard_conflict_button.clicked.connect(self._discard_conflict)
        self.save_lockfile_button.clicked.connect(self._save_lockfile)
        self.lock_button.clicked.connect(lambda: self._set_selection_locked(True))
        self.unlock_button.clicked.connect(lambda: self._set_selection_locked(False))

    def _load_preferences(self):
        preferences = _preferences()
        self.relay.setText(preferences.GetString("RelayUrl", "http://127.0.0.1:8765"))
        client_id = preferences.GetString("ClientId", "") or _default_client_id()
        self.client.setText(client_id)
        self.environment.setText(
            preferences.GetString("EnvironmentId", "") or default_environment_id()
        )

    def _save_preferences(self):
        preferences = _preferences()
        preferences.SetString("RelayUrl", self.relay.text().strip())
        preferences.SetString("ClientId", self.client.text().strip())
        preferences.SetString("EnvironmentId", self.environment.text().strip())

    def _start(self, share):
        document = App.ActiveDocument
        if document is None:
            self._append("Open or create a document first")
            return
        self._save_preferences()
        try:
            session = self.controller.start(
                document,
                self.relay.text().strip(),
                self.client.text().strip(),
                self.environment.text().strip(),
                self.document_uid.text().strip(),
                share,
            )
        except Exception as exc:
            self._append(f"ERROR: {exc}")
            return
        self._bind_session(session)
        action = "Sharing" if share else "Joining"
        self._append(f"{action} {session.document.Label} as {session.document_uid}")
        self.refresh(session)

    def _disconnect(self):
        document = App.ActiveDocument
        self.controller.disconnect(document)
        self._bind_session(None)
        self._append("Disconnected")
        self.refresh()

    def _set_selection_locked(self, locked):
        if self.session is None:
            self._append("Connect this document before managing locks")
            return
        selected = [
            obj for obj in Gui.Selection.getSelection() if obj.Document is self.session.document
        ]
        if not selected:
            self._append("Select one or more objects in the active document")
            return
        object_uids = [ensure_object_uid(obj) for obj in selected]
        if locked:
            self.session.acquire_locks(object_uids)
            self._append(f"Requested {len(object_uids)} feature lock(s)")
        else:
            self.session.release_locks(object_uids)
            self._append(f"Released {len(object_uids)} feature lock(s)")

    def _discard_conflict(self):
        if self.session is None:
            return
        self.session.discard_local_conflict()
        self._append("Discarded conflicting local edits")

    def _save_lockfile(self):
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save collaboration environment lockfile",
            "collaboration-environment.lock.json",
            "JSON files (*.json)",
        )
        if not path:
            return
        try:
            identity = write_environment_lock(path)
            self.environment.setText(identity)
            self._save_preferences()
            self._append(f"Saved environment lockfile: {identity}")
        except Exception as exc:
            self._append(f"ERROR: {exc}")

    def _bind_session(self, session):
        if self.session is session:
            return
        if self.session is not None:
            try:
                self.session.statusChanged.disconnect(self._status_changed)
                self.session.messageReceived.disconnect(self._message_received)
            except (RuntimeError, TypeError):
                pass
        self.session = session
        if session is not None:
            session.statusChanged.connect(self._status_changed)
            session.messageReceived.connect(self._message_received)

    @QtCore.Slot(str)
    def _status_changed(self, status):
        self.status.setText(status)
        if self.session is not None:
            self.revision.setText(str(self.session.revision))
            if status == "error" and self.session.last_error:
                self._append(f"ERROR: {self.session.last_error}")

    @QtCore.Slot(object)
    def _message_received(self, message):
        message_type = message.get("type", "message")
        if message_type in {"accepted", "state_report", "catch_up"}:
            self._append(f"{message_type}: revision {self.session.revision}")
        elif message_type == "lock_state":
            self._append(f"feature locks: {len(message.get('locks', []))}")
        elif message_type == "validation_state":
            status = "valid" if message.get("valid") else "INVALID"
            self._append(f"headless validation: revision {message['revision']} {status}")
        elif message_type in {"error", "transport_error"}:
            self._append(f"ERROR: {message.get('message', message_type)}")
        self.refresh()

    def _append(self, message):
        self.log.appendPlainText(message)

    @QtCore.Slot()
    @QtCore.Slot(object)
    def refresh(self, _unused=None):
        document = App.ActiveDocument
        self.document_name.setText(document.Label if document is not None else "-")
        active_session = self.controller.session_for(document)
        self._bind_session(active_session)
        if active_session is None:
            self.status.setText("disconnected")
            self.revision.setText("0")
            self.lock_count.setText("0")
            self.validation.setText("not validated")
            if document is not None and not self.document_uid.hasFocus():
                self.document_uid.setText(str(document.Uid))
        else:
            self.status.setText(active_session.status)
            self.revision.setText(str(active_session.revision))
            self.lock_count.setText(str(len(active_session.locks)))
            validation = active_session.validation_status
            if active_session.validation_revision:
                validation += f" (revision {active_session.validation_revision})"
            self.validation.setText(validation)
            if not self.document_uid.hasFocus():
                self.document_uid.setText(active_session.document_uid)
        enabled = document is not None and active_session is None
        self.share_button.setEnabled(enabled)
        self.join_button.setEnabled(enabled)
        self.disconnect_button.setEnabled(active_session is not None)
        self.lock_button.setEnabled(active_session is not None)
        self.unlock_button.setEnabled(active_session is not None)
        self.discard_conflict_button.setEnabled(
            active_session is not None and active_session.status == "conflict"
        )


_controller = None
_dock = None


def controller():
    global _controller
    if _controller is None:
        _controller = CollaborationController(Gui.getMainWindow())
    return _controller


def show_panel():
    global _dock
    main_window = Gui.getMainWindow()
    if _dock is None:
        _dock = QtWidgets.QDockWidget("Collaboration", main_window)
        _dock.setObjectName(DOCK_OBJECT_NAME)
        _dock.setWidget(CollaborationPanel(controller(), _dock))
        main_window.addDockWidget(QtCore.Qt.RightDockWidgetArea, _dock)
    _dock.show()
    _dock.raise_()
    return _dock


def disconnect_active_document():
    controller().disconnect(App.ActiveDocument)
