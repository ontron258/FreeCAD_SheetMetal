"""UUID-addressed serialization for FreeCAD link properties."""

from __future__ import annotations

from .identity import find_object_by_uid


def link_kind(property_type: str):
    """Return the structural link kind represented by a property TypeId."""

    if "LinkSubList" in property_type or "XLinkSubList" in property_type:
        return "sub_list"
    if "LinkSub" in property_type or "XLinkSub" in property_type:
        return "sub"
    if "LinkList" in property_type or "XLinkList" in property_type:
        return "list"
    if "PropertyLink" in property_type or "PropertyXLink" in property_type:
        return "single"
    return None


def _target_reference(owner_document, target, ensure_uid):
    if target is None:
        return None
    reference = {
        "scope": "self" if target.Document is owner_document else "external",
        "object_uid": ensure_uid(target),
        "object_name": target.Name,
    }
    if reference["scope"] == "external":
        reference["document_uid"] = str(target.Document.Uid)
    return reference


def serialize_link_property(obj, property_name: str, ensure_uid):
    property_type = obj.getTypeIdOfProperty(property_name)
    kind = link_kind(property_type)
    if kind is None:
        return None
    value = getattr(obj, property_name)
    if kind == "single":
        encoded = _target_reference(obj.Document, value, ensure_uid)
    elif kind == "list":
        encoded = [
            _target_reference(obj.Document, target, ensure_uid) for target in value
        ]
    elif kind == "sub":
        if not value or value[0] is None:
            encoded = None
        else:
            encoded = {
                "target": _target_reference(obj.Document, value[0], ensure_uid),
                "subelements": list(value[1]),
            }
    else:
        encoded = [
            {
                "target": _target_reference(obj.Document, target, ensure_uid),
                "subelements": list(subelements),
            }
            for target, subelements in value
        ]
    return {"kind": kind, "value": encoded}


def _resolve_target(document, reference, document_resolver=None):
    if reference is None:
        return None
    target_document = document
    if reference.get("scope") == "external":
        if document_resolver is None:
            raise ValueError(
                f"external document {reference.get('document_uid')} cannot be resolved"
            )
        target_document = document_resolver(reference["document_uid"])
        if target_document is None:
            raise ValueError(f"external document not found: {reference['document_uid']}")
    target = find_object_by_uid(target_document, reference["object_uid"])
    if target is None:
        raise ValueError(
            f"link target not found: {reference['object_uid']} "
            f"({reference.get('object_name', '')})"
        )
    return target


def restore_link_property(
    document,
    obj,
    property_name: str,
    structured_value,
    *,
    document_resolver=None,
):
    kind = structured_value["kind"]
    value = structured_value["value"]
    if kind == "single":
        restored = _resolve_target(document, value, document_resolver)
    elif kind == "list":
        restored = [
            _resolve_target(document, reference, document_resolver)
            for reference in value
        ]
    elif kind == "sub":
        if value is None:
            restored = None
        else:
            restored = (
                _resolve_target(document, value["target"], document_resolver),
                list(value["subelements"]),
            )
    elif kind == "sub_list":
        restored = [
            (
                _resolve_target(document, item["target"], document_resolver),
                list(item["subelements"]),
            )
            for item in value
        ]
    else:
        raise ValueError(f"unsupported structured link kind: {kind}")
    setattr(obj, property_name, restored)
