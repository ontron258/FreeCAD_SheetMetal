"""Persistent identity helpers for collaboration packets."""

from __future__ import annotations

import uuid
from typing import Dict, Iterable, Optional


COLLABORATION_UID_PROPERTY = "CollaborationUid"
COLLABORATION_PROPERTY_GROUP = "Collaboration"


class IdentityError(RuntimeError):
    """Raised when a document contains invalid collaboration identities."""


def _property_type(obj, name: str) -> str:
    try:
        return obj.getTypeIdOfProperty(name)
    except Exception:
        return ""


def _property_value(obj, name: str) -> str:
    try:
        value = getattr(obj, name)
    except Exception:
        return ""
    return str(value or "")


def get_object_uid(obj) -> Optional[str]:
    """Return the collaboration UUID assigned to *obj*, if any.

    ``App::Part.Uid`` is not returned directly.  It is used only as the seed
    when a CollaborationUid is first created.  Keeping a dedicated property
    gives the collaboration protocol identical behavior for Parts, sketches,
    bodies, links, and arbitrary workbench features.
    """

    if _property_type(obj, COLLABORATION_UID_PROPERTY) != "App::PropertyUUID":
        return None
    value = _property_value(obj, COLLABORATION_UID_PROPERTY)
    return value or None


def _candidate_part_uid(obj) -> Optional[str]:
    if _property_type(obj, "Uid") != "App::PropertyUUID":
        return None
    value = _property_value(obj, "Uid")
    return value or None


def _uids_in_document(document, exclude=None) -> set[str]:
    values = set()
    for candidate in document.Objects:
        if candidate is exclude:
            continue
        value = get_object_uid(candidate)
        if value:
            values.add(value)
    return values


def ensure_object_uid(obj, preferred: Optional[str] = None) -> str:
    """Ensure *obj* has a document-unique persistent collaboration UUID.

    Existing ``App::Part.Uid`` values are reused as the initial identity when
    possible.  Duplicate collaboration UUIDs are replaced, which handles a
    FreeCAD copy operation that copied dynamic properties verbatim.
    """

    existing = get_object_uid(obj)
    used = _uids_in_document(obj.Document, exclude=obj)
    if existing and existing not in used:
        return existing

    if COLLABORATION_UID_PROPERTY not in obj.PropertiesList:
        obj.addProperty(
            "App::PropertyUUID",
            COLLABORATION_UID_PROPERTY,
            COLLABORATION_PROPERTY_GROUP,
            "Stable identity used by the FreeCAD collaboration protocol",
        )

    candidate = preferred or _candidate_part_uid(obj) or str(uuid.uuid4())
    if candidate in used:
        candidate = str(uuid.uuid4())
        while candidate in used:
            candidate = str(uuid.uuid4())

    setattr(obj, COLLABORATION_UID_PROPERTY, candidate)
    try:
        # 1 = read-only, 2 = hidden, 3 = both.
        obj.setEditorMode(COLLABORATION_UID_PROPERTY, 3)
    except Exception:
        pass
    return candidate


def bootstrap_document(document) -> Dict[str, str]:
    """Assign stable identities to every current object in *document*.

    The returned mapping is keyed by collaboration UUID.  Bootstrap is an
    explicit model migration and should be wrapped in a user-visible FreeCAD
    transaction by the eventual GUI addon.
    """

    result: Dict[str, str] = {}
    for obj in tuple(document.Objects):
        uid = ensure_object_uid(obj)
        if uid in result:
            raise IdentityError(f"duplicate collaboration UUID: {uid}")
        result[uid] = obj.Name
    return result


def find_object_by_uid(document, uid: str):
    """Resolve a collaboration UUID to a FreeCAD object."""

    for obj in document.Objects:
        if get_object_uid(obj) == uid:
            return obj
    return None


def validate_unique_uids(objects: Iterable) -> None:
    seen = set()
    for obj in objects:
        uid = get_object_uid(obj)
        if not uid:
            continue
        if uid in seen:
            raise IdentityError(f"duplicate collaboration UUID: {uid}")
        seen.add(uid)
