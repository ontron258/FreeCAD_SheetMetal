"""Transaction-level collaboration primitives for FreeCAD.

This package is intentionally independent from the SheetMetal commands.  It is
the shared core that can eventually be imported by a GUI addon and by a
headless FreeCAD worker.
"""

from .identity import (
    COLLABORATION_UID_PROPERTY,
    bootstrap_document,
    ensure_object_uid,
    find_object_by_uid,
    get_object_uid,
)
from .packet import Operation, TransactionPacket
from .replay import PacketReplayError, apply_packet
from .revision_store import (
    CheckpointConflictError,
    ClientStateReport,
    DocumentHead,
    EnvironmentMismatchError,
    ObjectLock,
    ObjectLockConflictError,
    RevisionRecord,
    RevisionStore,
    StaleRevisionError,
    UnknownDocumentError,
)
from .state import DocumentState, StateHashError, document_state

__all__ = [
    "COLLABORATION_UID_PROPERTY",
    "CheckpointConflictError",
    "ClientStateReport",
    "DocumentHead",
    "DocumentState",
    "EnvironmentMismatchError",
    "Operation",
    "ObjectLock",
    "ObjectLockConflictError",
    "PacketReplayError",
    "RevisionRecord",
    "RevisionStore",
    "StaleRevisionError",
    "StateHashError",
    "TransactionPacket",
    "TransactionRecorder",
    "UnknownDocumentError",
    "apply_packet",
    "bootstrap_document",
    "document_state",
    "ensure_object_uid",
    "find_object_by_uid",
    "get_object_uid",
]


def __getattr__(name):
    """Keep server-side imports independent from a FreeCAD installation."""

    if name == "TransactionRecorder":
        from .recorder import TransactionRecorder

        return TransactionRecorder
    raise AttributeError(name)
