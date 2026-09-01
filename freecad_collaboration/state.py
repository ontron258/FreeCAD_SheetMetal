"""Canonical model-definition and computed-result hashing."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from .identity import IdentityError, get_object_uid
from .persistence import persistence_digest


IGNORED_PROPERTIES = {
    # Session/presentation state rather than shared engineering model state.
    "Visibility",
    # Python proxies are reconstructed from the pinned addon environment. Their
    # pickled runtime representation is not a stable protocol contract.
    "Proxy",
}

RESULT_PROPERTY_NAMES = {
    "Shape",
    "InternalShape",
    "SuppressedShape",
    "Mesh",
    "Points",
}

RESULT_PROPERTY_TYPES = {
    "Part::PropertyPartShape",
    "Part::PropertyTopoShapeList",
    "Part::PropertyShapeCache",
    "Mesh::PropertyMeshKernel",
    "Points::PropertyPointKernel",
}


class StateHashError(RuntimeError):
    """Raised when persistent document state cannot be hashed safely."""


@dataclass(frozen=True)
class DocumentState:
    """Separate hashes for parametric definition and computed geometry."""

    definition_hash: str
    result_hash: str
    object_count: int
    definition_property_count: int
    result_property_count: int

    @classmethod
    def from_dict(cls, value):
        return cls(
            definition_hash=value["definition_hash"],
            result_hash=value["result_hash"],
            object_count=int(value.get("object_count", 0)),
            definition_property_count=int(value.get("definition_property_count", 0)),
            result_property_count=int(value.get("result_property_count", 0)),
        )

    def to_dict(self):
        return {
            "definition_hash": self.definition_hash,
            "result_hash": self.result_hash,
            "object_count": self.object_count,
            "definition_property_count": self.definition_property_count,
            "result_property_count": self.result_property_count,
        }


def _hash_field(digest, value: str) -> None:
    encoded = value.encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)


def _is_transient(obj, property_name: str) -> bool:
    try:
        return "Transient" in obj.getTypeOfProperty(property_name)
    except Exception:
        return False


def _is_result_property(property_name: str, property_type: str) -> bool:
    return property_name in RESULT_PROPERTY_NAMES or property_type in RESULT_PROPERTY_TYPES


def document_state(document) -> DocumentState:
    """Return deterministic hashes for the current persistent object graph.

    Native FreeCAD property dumps are ZIP payloads whose timestamps are not
    deterministic. ``persistence_digest`` hashes their unpacked members, not
    the ZIP container bytes.
    """

    definition = hashlib.sha256()
    result = hashlib.sha256()
    definition_count = 0
    result_count = 0
    addressed_objects = []

    for obj in document.Objects:
        uid = get_object_uid(obj)
        if not uid:
            raise IdentityError(f"object {obj.Name} has no collaboration UUID")
        addressed_objects.append((uid, obj))

    for uid, obj in sorted(addressed_objects, key=lambda item: item[0]):
        for digest in (definition, result):
            _hash_field(digest, "object")
            _hash_field(digest, uid)
            _hash_field(digest, obj.Name)
            _hash_field(digest, obj.TypeId)

        for property_name in sorted(obj.PropertiesList):
            if property_name in IGNORED_PROPERTIES or _is_transient(obj, property_name):
                continue
            property_type = obj.getTypeIdOfProperty(property_name)
            try:
                payload = obj.dumpPropertyContent(property_name, Compression=9)
                payload_hash = persistence_digest(payload)
            except Exception as exc:
                raise StateHashError(
                    f"cannot hash {obj.Name}.{property_name}: {exc}"
                ) from exc

            target = result if _is_result_property(property_name, property_type) else definition
            _hash_field(target, "property")
            _hash_field(target, uid)
            _hash_field(target, property_name)
            _hash_field(target, property_type)
            _hash_field(target, payload_hash)
            if target is result:
                result_count += 1
            else:
                definition_count += 1

    return DocumentState(
        definition_hash=definition.hexdigest(),
        result_hash=result.hexdigest(),
        object_count=len(addressed_objects),
        definition_property_count=definition_count,
        result_property_count=result_count,
    )
