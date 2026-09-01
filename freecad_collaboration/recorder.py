"""Capture committed FreeCAD transactions as generic persistence packets."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

import FreeCAD as App

from .identity import ensure_object_uid, get_object_uid
from .links import link_kind, serialize_link_property
from .packet import Operation, TransactionPacket
from .persistence import dump_object, try_dump_property


PropertyKey = Tuple[str, str]


@dataclass
class _TransactionState:
    document: object
    name: str
    created: Dict[str, object] = field(default_factory=dict)
    deleted: Dict[str, Operation] = field(default_factory=dict)
    changed: Dict[PropertyKey, object] = field(default_factory=dict)
    before: Dict[PropertyKey, Optional[str]] = field(default_factory=dict)
    dynamic_added: Dict[PropertyKey, Tuple[str, str, str]] = field(default_factory=dict)
    dynamic_removed: Dict[PropertyKey, Operation] = field(default_factory=dict)


class TransactionRecorder:
    """FreeCAD document observer that emits one packet per committed transaction."""

    def __init__(
        self,
        document,
        *,
        base_revision: int = 0,
        environment_id: str = "",
        on_packet: Optional[Callable[[TransactionPacket], None]] = None,
    ):
        self.document = document
        self.base_revision = int(base_revision)
        self.environment_id = environment_id
        self.on_packet = on_packet
        self.packets: List[TransactionPacket] = []
        self._active: Optional[_TransactionState] = None
        self._suspend_count = 0
        self._installed = False

    def install(self) -> "TransactionRecorder":
        if self._installed:
            return self
        # Command-line FreeCAD disables undo transactions by default.  The GUI
        # normally already has this enabled.
        self.document.UndoMode = 1
        App.addDocumentObserver(self)
        self._installed = True
        return self

    def close(self) -> None:
        if self._installed:
            App.removeDocumentObserver(self)
            self._installed = False
        self._active = None

    @contextmanager
    def suspended(self):
        """Prevent replayed remote changes from being emitted again."""

        self._suspend_count += 1
        try:
            yield
        finally:
            self._suspend_count -= 1

    def _is_ours(self, document) -> bool:
        return document is self.document

    def _is_active_for(self, obj) -> bool:
        return (
            self._suspend_count == 0
            and self._active is not None
            and obj.Document is self.document
        )

    @contextmanager
    def _identity_update(self):
        self._suspend_count += 1
        try:
            yield
        finally:
            self._suspend_count -= 1

    def _uid(self, obj) -> str:
        uid = get_object_uid(obj)
        if uid:
            return uid
        with self._identity_update():
            return ensure_object_uid(obj)

    # FreeCAD document-observer callbacks
    def slotOpenTransaction(self, document, name):
        if self._suspend_count or not self._is_ours(document):
            return
        self._active = _TransactionState(document=document, name=name)

    def slotAbortTransaction(self, document):
        if self._is_ours(document):
            self._active = None

    def slotCreatedObject(self, obj):
        if not self._is_active_for(obj):
            return
        uid = self._uid(obj)
        self._active.created[uid] = obj

    def slotDeletedObject(self, obj):
        if not self._is_active_for(obj):
            return
        uid = self._uid(obj)
        if uid in self._active.created:
            del self._active.created[uid]
            return
        self._active.deleted[uid] = Operation(
            kind="delete_object",
            object_uid=uid,
            object_name=obj.Name,
            object_type=obj.TypeId,
            before_content=dump_object(obj),
        )

    def slotBeforeChangeObject(self, obj, property_name):
        if not self._is_active_for(obj):
            return
        uid = self._uid(obj)
        if uid in self._active.created:
            return
        key = (uid, property_name)
        if key not in self._active.before:
            self._active.before[key] = try_dump_property(obj, property_name)

    def slotChangedObject(self, obj, property_name):
        if not self._is_active_for(obj):
            return
        uid = self._uid(obj)
        if uid in self._active.created:
            return
        key = (uid, property_name)
        self._active.changed[key] = obj

    def slotAppendDynamicProperty(self, obj, property_name):
        if not self._is_active_for(obj):
            return
        uid = self._uid(obj)
        if uid in self._active.created:
            return
        key = (uid, property_name)
        self._active.dynamic_added[key] = (
            obj.getTypeIdOfProperty(property_name),
            obj.getGroupOfProperty(property_name),
            obj.getDocumentationOfProperty(property_name),
        )
        self._active.changed[key] = obj
        self._active.before.setdefault(key, None)

    def slotRemoveDynamicProperty(self, obj, property_name):
        if not self._is_active_for(obj):
            return
        uid = self._uid(obj)
        if uid in self._active.created:
            return
        key = (uid, property_name)
        if key in self._active.dynamic_added:
            self._active.dynamic_added.pop(key, None)
            self._active.changed.pop(key, None)
            self._active.before.pop(key, None)
            return
        self._active.dynamic_removed[key] = Operation(
            kind="remove_property",
            object_uid=uid,
            object_name=obj.Name,
            property_name=property_name,
            property_type=obj.getTypeIdOfProperty(property_name),
            property_group=obj.getGroupOfProperty(property_name),
            property_documentation=obj.getDocumentationOfProperty(property_name),
            before_content=try_dump_property(obj, property_name),
        )

    def slotCommitTransaction(self, document):
        if self._suspend_count or not self._is_ours(document) or self._active is None:
            return
        state = self._active
        self._active = None
        operations: List[Operation] = []

        # Create all objects before restoring their contents during replay so
        # same-document links can resolve even when they point forward.
        for uid, obj in state.created.items():
            if document.getObject(obj.Name) is None:
                continue
            operations.append(
                Operation(
                    kind="create_object",
                    object_uid=uid,
                    object_name=obj.Name,
                    object_type=obj.TypeId,
                    after_content=dump_object(obj),
                )
            )
            for property_name in sorted(obj.PropertiesList):
                property_type = obj.getTypeIdOfProperty(property_name)
                if link_kind(property_type) is None:
                    continue
                operations.append(
                    Operation(
                        kind="set_property",
                        object_uid=uid,
                        object_name=obj.Name,
                        object_type=obj.TypeId,
                        property_name=property_name,
                        property_type=property_type,
                        after_content=try_dump_property(obj, property_name),
                        structured_value=serialize_link_property(
                            obj, property_name, self._uid
                        ),
                    )
                )

        removed_keys = set(state.dynamic_removed)
        deleted_uids = set(state.deleted)
        for key, obj in state.changed.items():
            uid, property_name = key
            if uid in deleted_uids or key in removed_keys:
                continue
            if property_name not in obj.PropertiesList:
                continue
            prop_type, group, documentation = state.dynamic_added.get(key, ("", "", ""))
            if prop_type:
                operations.append(
                    Operation(
                        kind="add_property",
                        object_uid=uid,
                        object_name=obj.Name,
                        property_name=property_name,
                        property_type=prop_type,
                        property_group=group,
                        property_documentation=documentation,
                    )
                )
            operations.append(
                Operation(
                    kind="set_property",
                    object_uid=uid,
                    object_name=obj.Name,
                    object_type=obj.TypeId,
                    property_name=property_name,
                    property_type=obj.getTypeIdOfProperty(property_name),
                    before_content=state.before.get(key),
                    after_content=try_dump_property(obj, property_name),
                    structured_value=(
                        serialize_link_property(obj, property_name, self._uid)
                        if link_kind(obj.getTypeIdOfProperty(property_name))
                        else None
                    ),
                )
            )

        operations.extend(state.dynamic_removed.values())
        operations.extend(state.deleted.values())
        if not operations:
            return

        packet = TransactionPacket(
            document_uid=str(document.Uid),
            name=state.name,
            base_revision=self.base_revision,
            operations=operations,
            environment_id=self.environment_id,
        )
        self.packets.append(packet)
        if self.on_packet:
            self.on_packet(packet)
