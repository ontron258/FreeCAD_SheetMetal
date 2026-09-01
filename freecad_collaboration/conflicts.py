"""Address-level conflict checks for collaboration packets."""

from __future__ import annotations

from .state import RESULT_PROPERTY_NAMES, RESULT_PROPERTY_TYPES


def operation_address(operation):
    """Return the stable object/property address affected by an operation."""

    if operation.kind in {"create_object", "delete_object"}:
        return operation.object_uid, "*"
    if (
        operation.property_name in RESULT_PROPERTY_NAMES
        or operation.property_type in RESULT_PROPERTY_TYPES
    ):
        return None
    return operation.object_uid, operation.property_name or "*"


def packets_conflict(local_packets, remote_packets) -> bool:
    """Whether any local and remote operation address overlaps."""

    local_addresses = {
        address
        for packet in local_packets
        for operation in packet.operations
        if (address := operation_address(operation)) is not None
    }
    remote_addresses = {
        address
        for packet in remote_packets
        for operation in packet.operations
        if (address := operation_address(operation)) is not None
    }
    for local_uid, local_property in local_addresses:
        for remote_uid, remote_property in remote_addresses:
            if local_uid != remote_uid:
                continue
            if (
                local_property == "*"
                or remote_property == "*"
                or local_property == remote_property
            ):
                return True
    return False
