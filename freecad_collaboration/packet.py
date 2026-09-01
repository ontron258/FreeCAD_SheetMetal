"""Versioned, transport-neutral collaboration packet schema."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from typing import Any, Dict, List, Optional
import uuid


SCHEMA_VERSION = 1


@dataclass
class Operation:
    """One persistent mutation within a FreeCAD transaction."""

    kind: str
    object_uid: str
    object_name: str
    object_type: str = ""
    property_name: str = ""
    property_type: str = ""
    property_group: str = ""
    property_documentation: str = ""
    before_content: Optional[str] = None
    after_content: Optional[str] = None
    structured_value: Any = None

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Operation":
        return cls(**value)


@dataclass
class TransactionPacket:
    """A transaction submitted relative to one server revision."""

    document_uid: str
    name: str
    base_revision: int
    operations: List[Operation]
    transaction_uid: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: int = SCHEMA_VERSION
    environment_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, *, indent: Optional[int] = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "TransactionPacket":
        data = dict(value)
        data["operations"] = [Operation.from_dict(op) for op in data["operations"]]
        packet = cls(**data)
        if packet.schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported packet schema {packet.schema_version}; expected {SCHEMA_VERSION}"
            )
        return packet

    @classmethod
    def from_json(cls, value: str) -> "TransactionPacket":
        return cls.from_dict(json.loads(value))
