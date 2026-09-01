"""Native revision-zero checkpoint persistence helpers."""

from __future__ import annotations

import io
import os
from pathlib import Path
import tempfile
import zipfile


def is_native_document_archive(content: bytes) -> bool:
    """Return whether *content* is a complete FCStd document archive."""

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            return "Document.xml" in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def native_checkpoint_bytes(document) -> bytes:
    """Serialize a document through FreeCAD's normal FCStd save path."""

    handle, path = tempfile.mkstemp(prefix="freecad-collaboration-", suffix=".FCStd")
    os.close(handle)
    try:
        document.saveCopy(path)
        return Path(path).read_bytes()
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def open_checkpoint(content: bytes, *, document_name: str | None = None):
    """Restore either a complete FCStd archive or legacy persistence content."""

    import FreeCAD as App

    if not is_native_document_archive(content):
        document = App.newDocument(document_name or "CollaborationCheckpoint")
        document.restoreContent(bytearray(content))
        return document

    handle, path = tempfile.mkstemp(prefix="freecad-collaboration-", suffix=".FCStd")
    os.close(handle)
    try:
        Path(path).write_bytes(content)
        return App.openDocument(path)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
