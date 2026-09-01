"""Apply collaboration packets through FreeCAD's document API."""

from __future__ import annotations

from contextlib import nullcontext

from .identity import (
    COLLABORATION_UID_PROPERTY,
    ensure_object_uid,
    find_object_by_uid,
)
from .links import restore_link_property
from .persistence import decode_content


class PacketReplayError(RuntimeError):
    """Raised when a packet cannot be safely applied."""


def _resolve(document, operation):
    obj = find_object_by_uid(document, operation.object_uid)
    if obj is None and operation.object_name:
        candidate = document.getObject(operation.object_name)
        if candidate is not None:
            existing = ensure_object_uid(candidate)
            if existing == operation.object_uid:
                obj = candidate
    if obj is None:
        raise PacketReplayError(
            f"object {operation.object_uid} ({operation.object_name}) was not found"
        )
    return obj


def _create_objects(document, operations):
    created = []
    for operation in operations:
        if operation.kind != "create_object":
            continue
        existing = find_object_by_uid(document, operation.object_uid)
        if existing is not None:
            raise PacketReplayError(f"object UUID already exists: {operation.object_uid}")
        if document.getObject(operation.object_name) is not None:
            raise PacketReplayError(f"object name already exists: {operation.object_name}")
        obj = document.addObject(operation.object_type, operation.object_name)
        ensure_object_uid(obj, preferred=operation.object_uid)
        created.append((operation, obj))
    return created


def apply_packet(
    document,
    packet,
    *,
    recorder=None,
    document_resolver=None,
    validate_document_uid: bool = True,
    recompute: bool = True,
):
    """Apply *packet* atomically to *document*.

    ``recorder`` may be the target document's TransactionRecorder.  Its
    suspension context prevents the remote transaction from being echoed back
    to the server.
    """

    if validate_document_uid and str(document.Uid) != packet.document_uid:
        raise PacketReplayError(
            f"packet is for document {packet.document_uid}, not {document.Uid}"
        )

    context = recorder.suspended() if recorder is not None else nullcontext()
    document.UndoMode = 1
    with context:
        document.openTransaction(f"Remote: {packet.name}")
        try:
            created = _create_objects(document, packet.operations)

            # All target names now exist, so native restoreContent can resolve
            # same-document PropertyLink values in the serialized payload.
            for operation, obj in created:
                if operation.after_content:
                    obj.restoreContent(decode_content(operation.after_content))
                if ensure_object_uid(obj) != operation.object_uid:
                    setattr(obj, COLLABORATION_UID_PROPERTY, operation.object_uid)

            for operation in packet.operations:
                if operation.kind in {"create_object", "delete_object"}:
                    continue
                obj = _resolve(document, operation)
                if operation.kind == "add_property":
                    if operation.property_name not in obj.PropertiesList:
                        obj.addProperty(
                            operation.property_type,
                            operation.property_name,
                            operation.property_group,
                            operation.property_documentation,
                        )
                elif operation.kind == "set_property":
                    if operation.property_name not in obj.PropertiesList:
                        raise PacketReplayError(
                            f"property {obj.Name}.{operation.property_name} does not exist"
                        )
                    if operation.structured_value is not None:
                        restore_link_property(
                            document,
                            obj,
                            operation.property_name,
                            operation.structured_value,
                            document_resolver=document_resolver,
                        )
                    elif operation.after_content is None:
                        raise PacketReplayError(
                            f"property {obj.Name}.{operation.property_name} has no payload"
                        )
                    else:
                        obj.restorePropertyContent(
                            operation.property_name,
                            decode_content(operation.after_content),
                        )
                elif operation.kind == "remove_property":
                    if operation.property_name in obj.PropertiesList:
                        obj.removeProperty(operation.property_name)
                else:
                    raise PacketReplayError(f"unsupported operation: {operation.kind}")

            for operation in packet.operations:
                if operation.kind != "delete_object":
                    continue
                obj = _resolve(document, operation)
                document.removeObject(obj.Name)

            if recompute:
                result = document.recompute()
                if result is False:
                    raise PacketReplayError("FreeCAD recompute failed")
            document.commitTransaction()
        except Exception:
            document.abortTransaction()
            raise
    return document
