"""Compatibility identity for packet-producing FreeCAD clients."""

from __future__ import annotations

import os
import sys

import FreeCAD as App

from .packet import SCHEMA_VERSION


COLLABORATION_CORE_VERSION = 1


def default_environment_id() -> str:
    """Return a stable minimum compatibility fingerprint for this client.

    A deployment can provide a stricter Git/addon lock identity through the
    environment variable until lockfile generation is implemented.
    """

    override = os.environ.get("FREECAD_COLLABORATION_ENVIRONMENT_ID", "").strip()
    if override:
        return override
    version = list(App.Version())
    freecad_version = ".".join(version[:3])
    freecad_revision = version[7] if len(version) > 7 else "unknown"
    return (
        f"freecad:{freecad_version}:{freecad_revision}"
        f"|python:{sys.version_info.major}.{sys.version_info.minor}"
        f"|packet:{SCHEMA_VERSION}|collaboration:{COLLABORATION_CORE_VERSION}"
    )
