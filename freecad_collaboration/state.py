"""Canonical model-definition and computed-result hashing."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math

from .identity import IdentityError, get_object_uid
from .persistence import persistence_digest


IGNORED_PROPERTIES = {
    # Session/presentation state rather than shared engineering model state.
    "Visibility",
    # Python proxies are reconstructed from the pinned addon environment. Their
    # pickled runtime representation is not a stable protocol contract.
    "Proxy",
    # Presentation/generated naming is allowed to normalize when a document is
    # reopened. Object identity is carried by CollaborationUid, not Label.
    "Label",
    "DrawingName",
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
    if property_name.startswith("Cache_"):
        return True
    try:
        return "Transient" in obj.getTypeOfProperty(property_name)
    except Exception:
        return False


def _is_result_property(property_name: str, property_type: str) -> bool:
    return property_name in RESULT_PROPERTY_NAMES or property_type in RESULT_PROPERTY_TYPES


def _number(value):
    value = float(value)
    if not math.isfinite(value):
        return str(value)
    if abs(value) < 1e-10:
        value = 0.0
    return format(value, ".10g")


def _vector(value):
    return [_number(value.x), _number(value.y), _number(value.z)]


def _bounds(shape):
    bounds = shape.BoundBox
    return [
        _number(bounds.XMin),
        _number(bounds.YMin),
        _number(bounds.ZMin),
        _number(bounds.XMax),
        _number(bounds.YMax),
        _number(bounds.ZMax),
    ]


def _optional_number_attribute(value, name):
    try:
        return _number(getattr(value, name))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None


def _optional_vector_attribute(value, name):
    try:
        return _vector(getattr(value, name))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None


def _sorted_signatures(values):
    return sorted(values, key=lambda value: json.dumps(value, sort_keys=True))


def _edge_signature(edge):
    signature = {
        "curve": type(edge.Curve).__name__,
        "orientation": str(edge.Orientation),
        "length": _number(edge.Length),
        "bounds": _bounds(edge),
    }
    first = float(edge.FirstParameter)
    last = float(edge.LastParameter)
    if math.isfinite(first) and math.isfinite(last):
        signature["samples"] = [
            _vector(edge.valueAt(first + (last - first) * index / 8.0))
            for index in range(9)
        ]
    return signature


def _face_signature(face):
    signature = {
        "surface": type(face.Surface).__name__,
        "orientation": str(face.Orientation),
        "area": _number(face.Area),
        "center": _vector(face.CenterOfMass),
        "bounds": _bounds(face),
    }
    u_first, u_last, v_first, v_last = map(float, face.ParameterRange)
    if all(math.isfinite(value) for value in (u_first, u_last, v_first, v_last)):
        signature["samples"] = [
            _vector(
                face.valueAt(
                    u_first + (u_last - u_first) * u_index / 2.0,
                    v_first + (v_last - v_first) * v_index / 2.0,
                )
            )
            for u_index in range(3)
            for v_index in range(3)
        ]
    return signature


def _shape_signature(shape):
    if shape.isNull():
        return {"null": True}
    signature = {
        "shape_type": str(shape.ShapeType),
        "orientation": str(shape.Orientation),
        "bounds": _bounds(shape),
        # Aggregate and lower-dimensional TopoShapes do not consistently
        # expose every mass property.  Keep the schema stable by representing
        # unavailable metrics explicitly instead of rejecting valid geometry.
        "area": _optional_number_attribute(shape, "Area"),
        "volume": _optional_number_attribute(shape, "Volume"),
        "length": _optional_number_attribute(shape, "Length"),
        "center": _optional_vector_attribute(shape, "CenterOfMass"),
        "counts": {
            "solids": len(shape.Solids),
            "shells": len(shape.Shells),
            "faces": len(shape.Faces),
            "wires": len(shape.Wires),
            "edges": len(shape.Edges),
            "vertices": len(shape.Vertexes),
        },
        "vertices": sorted(_vector(vertex.Point) for vertex in shape.Vertexes),
        "edges": _sorted_signatures(_edge_signature(edge) for edge in shape.Edges),
        "faces": _sorted_signatures(_face_signature(face) for face in shape.Faces),
    }
    return signature


def _result_property_digest(obj, property_name: str, property_type: str) -> str:
    """Hash computed geometry without native persistence-container noise."""

    value = getattr(obj, property_name)
    shapes = value if isinstance(value, (list, tuple)) else [value]
    # Reading ``ShapeType`` from a null TopoShape raises in FreeCAD, so do not
    # use ``hasattr(shape, "ShapeType")`` as the shape-type probe.  The
    # declared property type is authoritative and also handles empty shape
    # lists without falling back to timestamped native persistence payloads.
    if property_type in {
        "Part::PropertyPartShape",
        "Part::PropertyTopoShapeList",
        "Part::PropertyShapeCache",
    }:
        canonical = json.dumps(
            [_shape_signature(shape) for shape in shapes],
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()
    payload = obj.dumpPropertyContent(property_name, Compression=9)
    return persistence_digest(payload)


def _definition_property_digest(obj, property_name: str, property_type: str) -> str:
    """Hash common value types semantically across native save/reload cycles."""

    value = getattr(obj, property_name)
    if property_type == "App::PropertyMatrix":
        canonical = [_number(number) for number in value.A]
    elif property_type == "Part::PropertyGeometryList":
        canonical = [
            {
                "geometry": type(geometry).__name__,
                "shape": _shape_signature(geometry.toShape()),
            }
            for geometry in value
        ]
    elif property_type.startswith("App::Property") and hasattr(value, "Value"):
        canonical = {
            "value": _number(value.Value),
            "unit": str(getattr(value, "Unit", "")),
        }
    else:
        return persistence_digest(obj.dumpPropertyContent(property_name, Compression=9))
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


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
                if _is_result_property(property_name, property_type):
                    payload_hash = _result_property_digest(
                        obj, property_name, property_type
                    )
                else:
                    payload_hash = _definition_property_digest(
                        obj, property_name, property_type
                    )
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
