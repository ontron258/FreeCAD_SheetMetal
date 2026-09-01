"""Encode FreeCAD's native persistence payloads for packet transport."""

from __future__ import annotations

import base64
import hashlib
import io
from typing import Optional
import zipfile


def encode_content(content) -> str:
    return base64.b64encode(bytes(content)).decode("ascii")


def decode_content(content: str) -> bytearray:
    return bytearray(base64.b64decode(content.encode("ascii")))


def dump_property(obj, property_name: str) -> str:
    return encode_content(obj.dumpPropertyContent(property_name, Compression=9))


def try_dump_property(obj, property_name: str) -> Optional[str]:
    try:
        return dump_property(obj, property_name)
    except Exception:
        return None


def dump_object(obj) -> str:
    return encode_content(obj.dumpContent(9))


def persistence_digest(content) -> str:
    """Hash a native persistence payload without ZIP timestamp metadata."""

    raw = bytes(content)
    digest = hashlib.sha256()
    try:
        with zipfile.ZipFile(io.BytesIO(raw), "r") as archive:
            for name in sorted(archive.namelist()):
                payload = archive.read(name)
                encoded_name = name.encode("utf-8")
                digest.update(len(encoded_name).to_bytes(8, "big"))
                digest.update(encoded_name)
                digest.update(len(payload).to_bytes(8, "big"))
                digest.update(payload)
    except zipfile.BadZipFile:
        digest.update(raw)
    return digest.hexdigest()
