"""Append-only SQLite revision sequencing for collaboration packets."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import time
from typing import List, Optional

from .packet import TransactionPacket
from .state import DocumentState


class RevisionStoreError(RuntimeError):
    pass


class UnknownDocumentError(RevisionStoreError):
    pass


class StaleRevisionError(RevisionStoreError):
    def __init__(self, submitted_revision: int, head_revision: int):
        super().__init__(
            f"packet is based on revision {submitted_revision}; head is {head_revision}"
        )
        self.submitted_revision = submitted_revision
        self.head_revision = head_revision


class EnvironmentMismatchError(RevisionStoreError):
    pass


class CheckpointConflictError(RevisionStoreError):
    pass


class ObjectLockConflictError(RevisionStoreError):
    def __init__(self, object_uid: str, owner: str):
        super().__init__(f"object {object_uid} is locked by {owner}")
        self.object_uid = object_uid
        self.owner = owner


@dataclass(frozen=True)
class RevisionRecord:
    document_uid: str
    revision: int
    transaction_uid: str
    parent_revision: int
    name: str
    packet_json: str
    definition_hash: str
    result_hash: str
    environment_id: str
    accepted_at: str
    duplicate: bool = False


@dataclass(frozen=True)
class ClientStateReport:
    document_uid: str
    revision: int
    client_id: str
    definition_hash: str
    result_hash: str
    definition_matches: bool
    result_matches: bool


@dataclass(frozen=True)
class DocumentHead:
    document_uid: str
    name: str
    revision: int
    definition_hash: str
    result_hash: str
    environment_id: str
    has_checkpoint: bool


@dataclass(frozen=True)
class ObjectLock:
    document_uid: str
    object_uid: str
    client_id: str
    expires_at: float


@dataclass(frozen=True)
class ValidationJob:
    document_uid: str
    revision: int
    environment_id: str


@dataclass(frozen=True)
class ValidationReport:
    document_uid: str
    revision: int
    worker_id: str
    environment_id: str
    definition_hash: str
    result_hash: str
    environment_matches: bool
    definition_matches: bool
    result_matches: bool
    valid: bool
    error: str
    validated_at: str


class RevisionStore:
    """Serialize packet acceptance and retain an immutable revision log."""

    def __init__(self, path: str):
        self.path = path
        self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_uid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                head_revision INTEGER NOT NULL,
                definition_hash TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                environment_id TEXT NOT NULL,
                checkpoint BLOB,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS revisions (
                document_uid TEXT NOT NULL,
                revision INTEGER NOT NULL,
                transaction_uid TEXT NOT NULL,
                parent_revision INTEGER NOT NULL,
                name TEXT NOT NULL,
                packet_json TEXT NOT NULL,
                definition_hash TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                environment_id TEXT NOT NULL,
                accepted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (document_uid, revision),
                UNIQUE (document_uid, transaction_uid),
                FOREIGN KEY (document_uid) REFERENCES documents(document_uid)
            );

            CREATE TABLE IF NOT EXISTS client_states (
                document_uid TEXT NOT NULL,
                revision INTEGER NOT NULL,
                client_id TEXT NOT NULL,
                definition_hash TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                reported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (document_uid, revision, client_id),
                FOREIGN KEY (document_uid, revision)
                    REFERENCES revisions(document_uid, revision)
            );

            CREATE TABLE IF NOT EXISTS object_locks (
                document_uid TEXT NOT NULL,
                object_uid TEXT NOT NULL,
                client_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (document_uid, object_uid),
                FOREIGN KEY (document_uid) REFERENCES documents(document_uid)
            );

            CREATE TABLE IF NOT EXISTS validation_results (
                document_uid TEXT NOT NULL,
                revision INTEGER NOT NULL,
                worker_id TEXT NOT NULL,
                environment_id TEXT NOT NULL,
                definition_hash TEXT NOT NULL,
                result_hash TEXT NOT NULL,
                environment_matches INTEGER NOT NULL,
                definition_matches INTEGER NOT NULL,
                result_matches INTEGER NOT NULL,
                valid INTEGER NOT NULL,
                error TEXT NOT NULL,
                validated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (document_uid, revision),
                FOREIGN KEY (document_uid, revision)
                    REFERENCES revisions(document_uid, revision)
            );
            """
        )
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(documents)")
        }
        if "checkpoint" not in columns:
            self.connection.execute("ALTER TABLE documents ADD COLUMN checkpoint BLOB")

    def register_document(
        self,
        document_uid: str,
        name: str,
        state: DocumentState,
        *,
        environment_id: str = "",
    ) -> int:
        existing = self.connection.execute(
            "SELECT head_revision FROM documents WHERE document_uid = ?",
            (document_uid,),
        ).fetchone()
        if existing is not None:
            return int(existing["head_revision"])
        self.connection.execute(
            """
            INSERT INTO documents (
                document_uid, name, head_revision, definition_hash,
                result_hash, environment_id
            ) VALUES (?, ?, 0, ?, ?, ?)
            """,
            (
                document_uid,
                name,
                state.definition_hash,
                state.result_hash,
                environment_id,
            ),
        )
        return 0

    def head_revision(self, document_uid: str) -> int:
        row = self.connection.execute(
            "SELECT head_revision FROM documents WHERE document_uid = ?",
            (document_uid,),
        ).fetchone()
        if row is None:
            raise UnknownDocumentError(document_uid)
        return int(row["head_revision"])

    def document_head(self, document_uid: str) -> DocumentHead:
        row = self.connection.execute(
            "SELECT * FROM documents WHERE document_uid = ?",
            (document_uid,),
        ).fetchone()
        if row is None:
            raise UnknownDocumentError(document_uid)
        return DocumentHead(
            document_uid=row["document_uid"],
            name=row["name"],
            revision=int(row["head_revision"]),
            definition_hash=row["definition_hash"],
            result_hash=row["result_hash"],
            environment_id=row["environment_id"],
            has_checkpoint=row["checkpoint"] is not None,
        )

    def store_checkpoint(self, document_uid: str, checkpoint: bytes) -> bool:
        """Store the immutable revision-zero checkpoint.

        Returns ``True`` when inserted and ``False`` for an idempotent retry.
        """

        payload = bytes(checkpoint)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT head_revision, checkpoint FROM documents WHERE document_uid = ?",
                (document_uid,),
            ).fetchone()
            if row is None:
                raise UnknownDocumentError(document_uid)
            existing = row["checkpoint"]
            if existing is not None:
                if bytes(existing) != payload:
                    raise CheckpointConflictError(
                        "a different revision-zero checkpoint is already registered"
                    )
                self.connection.execute("COMMIT")
                return False
            if int(row["head_revision"]) != 0:
                raise CheckpointConflictError(
                    "cannot attach a revision-zero checkpoint after editing has begun"
                )
            self.connection.execute(
                "UPDATE documents SET checkpoint = ? WHERE document_uid = ?",
                (payload, document_uid),
            )
            self.connection.execute("COMMIT")
            return True
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def checkpoint(self, document_uid: str) -> bytes:
        row = self.connection.execute(
            "SELECT checkpoint FROM documents WHERE document_uid = ?",
            (document_uid,),
        ).fetchone()
        if row is None:
            raise UnknownDocumentError(document_uid)
        if row["checkpoint"] is None:
            raise UnknownDocumentError(f"{document_uid} has no checkpoint")
        return bytes(row["checkpoint"])

    def acquire_locks(
        self,
        document_uid: str,
        object_uids,
        client_id: str,
        *,
        ttl_seconds: float = 120,
    ) -> List[ObjectLock]:
        object_uids = sorted({str(uid) for uid in object_uids if uid})
        if not object_uids:
            return []
        expires_at = time.time() + max(5.0, float(ttl_seconds))
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._delete_expired_locks()
            if self.connection.execute(
                "SELECT 1 FROM documents WHERE document_uid = ?", (document_uid,)
            ).fetchone() is None:
                raise UnknownDocumentError(document_uid)
            placeholders = ",".join("?" for _ in object_uids)
            conflicts = self.connection.execute(
                f"""
                SELECT object_uid, client_id FROM object_locks
                WHERE document_uid = ? AND object_uid IN ({placeholders})
                  AND client_id != ?
                """,
                (document_uid, *object_uids, client_id),
            ).fetchone()
            if conflicts is not None:
                raise ObjectLockConflictError(
                    conflicts["object_uid"], conflicts["client_id"]
                )
            for object_uid in object_uids:
                self.connection.execute(
                    """
                    INSERT INTO object_locks (
                        document_uid, object_uid, client_id, expires_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(document_uid, object_uid) DO UPDATE SET
                        client_id = excluded.client_id,
                        expires_at = excluded.expires_at
                    """,
                    (document_uid, object_uid, client_id, expires_at),
                )
            self.connection.execute("COMMIT")
            return self.list_locks(document_uid)
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def release_locks(self, document_uid: str, object_uids, client_id: str):
        object_uids = sorted({str(uid) for uid in object_uids if uid})
        if not object_uids:
            return self.list_locks(document_uid)
        placeholders = ",".join("?" for _ in object_uids)
        self.connection.execute(
            f"""
            DELETE FROM object_locks
            WHERE document_uid = ? AND object_uid IN ({placeholders})
              AND client_id = ?
            """,
            (document_uid, *object_uids, client_id),
        )
        return self.list_locks(document_uid)

    def release_client_locks(self, document_uid: str, client_id: str):
        self.connection.execute(
            "DELETE FROM object_locks WHERE document_uid = ? AND client_id = ?",
            (document_uid, client_id),
        )
        return self.list_locks(document_uid)

    def list_locks(self, document_uid: str) -> List[ObjectLock]:
        self._delete_expired_locks()
        rows = self.connection.execute(
            "SELECT * FROM object_locks WHERE document_uid = ? ORDER BY object_uid",
            (document_uid,),
        ).fetchall()
        return [
            ObjectLock(
                document_uid=row["document_uid"],
                object_uid=row["object_uid"],
                client_id=row["client_id"],
                expires_at=float(row["expires_at"]),
            )
            for row in rows
        ]

    def validate_packet_locks(self, packet: TransactionPacket, client_id: str) -> None:
        self._delete_expired_locks()
        object_uids = sorted({op.object_uid for op in packet.operations if op.object_uid})
        if not object_uids:
            return
        placeholders = ",".join("?" for _ in object_uids)
        conflict = self.connection.execute(
            f"""
            SELECT object_uid, client_id FROM object_locks
            WHERE document_uid = ? AND object_uid IN ({placeholders})
              AND client_id != ?
            """,
            (packet.document_uid, *object_uids, client_id),
        ).fetchone()
        if conflict is not None:
            raise ObjectLockConflictError(conflict["object_uid"], conflict["client_id"])

    def _delete_expired_locks(self):
        self.connection.execute(
            "DELETE FROM object_locks WHERE expires_at <= ?", (time.time(),)
        )

    def append(
        self, packet: TransactionPacket, state: Optional[DocumentState] = None
    ) -> RevisionRecord:
        """Accept a packet without requiring the GUI to hash the whole model.

        A supplied state remains supported for batch/headless clients.  An
        interactive client may omit it; the revision is then provisional until
        a headless validator supplies the authoritative definition and result
        hashes.
        """
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            duplicate = self.connection.execute(
                """
                SELECT * FROM revisions
                WHERE document_uid = ? AND transaction_uid = ?
                """,
                (packet.document_uid, packet.transaction_uid),
            ).fetchone()
            if duplicate is not None:
                self.connection.execute("COMMIT")
                return self._record(duplicate, duplicate=True)

            document = self.connection.execute(
                "SELECT * FROM documents WHERE document_uid = ?",
                (packet.document_uid,),
            ).fetchone()
            if document is None:
                raise UnknownDocumentError(packet.document_uid)

            head = int(document["head_revision"])
            if packet.base_revision != head:
                raise StaleRevisionError(packet.base_revision, head)
            expected_environment = document["environment_id"]
            if expected_environment and packet.environment_id != expected_environment:
                raise EnvironmentMismatchError(
                    f"packet environment {packet.environment_id!r} does not match "
                    f"document environment {expected_environment!r}"
                )

            revision = head + 1
            self.connection.execute(
                """
                INSERT INTO revisions (
                    document_uid, revision, transaction_uid, parent_revision,
                    name, packet_json, definition_hash, result_hash,
                    environment_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    packet.document_uid,
                    revision,
                    packet.transaction_uid,
                    head,
                    packet.name,
                    packet.to_json(),
                    state.definition_hash if state is not None else "",
                    state.result_hash if state is not None else "",
                    packet.environment_id,
                ),
            )
            self.connection.execute(
                """
                UPDATE documents
                SET head_revision = ?, definition_hash = ?, result_hash = ?
                WHERE document_uid = ?
                """,
                (
                    revision,
                    state.definition_hash if state is not None else "",
                    state.result_hash if state is not None else "",
                    packet.document_uid,
                ),
            )
            row = self.connection.execute(
                """
                SELECT * FROM revisions
                WHERE document_uid = ? AND revision = ?
                """,
                (packet.document_uid, revision),
            ).fetchone()
            self.connection.execute("COMMIT")
            return self._record(row)
        except Exception:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def report_client_state(
        self,
        document_uid: str,
        revision: int,
        client_id: str,
        state: DocumentState,
    ) -> ClientStateReport:
        expected = self.connection.execute(
            """
            SELECT definition_hash, result_hash FROM revisions
            WHERE document_uid = ? AND revision = ?
            """,
            (document_uid, revision),
        ).fetchone()
        if expected is None:
            raise UnknownDocumentError(f"{document_uid}@{revision}")
        self.connection.execute(
            """
            INSERT INTO client_states (
                document_uid, revision, client_id, definition_hash, result_hash
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(document_uid, revision, client_id) DO UPDATE SET
                definition_hash = excluded.definition_hash,
                result_hash = excluded.result_hash,
                reported_at = CURRENT_TIMESTAMP
            """,
            (
                document_uid,
                revision,
                client_id,
                state.definition_hash,
                state.result_hash,
            ),
        )
        return ClientStateReport(
            document_uid=document_uid,
            revision=revision,
            client_id=client_id,
            definition_hash=state.definition_hash,
            result_hash=state.result_hash,
            definition_matches=state.definition_hash == expected["definition_hash"],
            result_matches=state.result_hash == expected["result_hash"],
        )

    def list_revisions(self, document_uid: str) -> List[RevisionRecord]:
        rows = self.connection.execute(
            "SELECT * FROM revisions WHERE document_uid = ? ORDER BY revision",
            (document_uid,),
        ).fetchall()
        return [self._record(row) for row in rows]

    def pending_validation_jobs(self, limit: int = 10) -> List[ValidationJob]:
        rows = self.connection.execute(
            """
            SELECT r.document_uid, r.revision, r.environment_id
            FROM revisions AS r
            LEFT JOIN validation_results AS v
              ON v.document_uid = r.document_uid AND v.revision = r.revision
            WHERE v.revision IS NULL
            ORDER BY r.accepted_at, r.document_uid, r.revision
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        return [
            ValidationJob(
                document_uid=row["document_uid"],
                revision=int(row["revision"]),
                environment_id=row["environment_id"],
            )
            for row in rows
        ]

    def report_validation(
        self,
        document_uid: str,
        revision: int,
        worker_id: str,
        environment_id: str,
        state: Optional[DocumentState] = None,
        *,
        error: str = "",
    ) -> ValidationReport:
        expected = self.connection.execute(
            """
            SELECT definition_hash, result_hash, environment_id
            FROM revisions WHERE document_uid = ? AND revision = ?
            """,
            (document_uid, int(revision)),
        ).fetchone()
        if expected is None:
            raise UnknownDocumentError(f"{document_uid}@{revision}")
        definition_hash = state.definition_hash if state is not None else ""
        result_hash = state.result_hash if state is not None else ""
        environment_matches = environment_id == expected["environment_id"]
        provisional = not expected["definition_hash"] and not expected["result_hash"]
        definition_matches = bool(state) and (
            provisional or definition_hash == expected["definition_hash"]
        )
        result_matches = bool(state) and (
            provisional or result_hash == expected["result_hash"]
        )
        valid = bool(
            not error
            and environment_matches
            and definition_matches
            and result_matches
        )
        if valid and provisional:
            self.connection.execute(
                """
                UPDATE revisions SET definition_hash = ?, result_hash = ?
                WHERE document_uid = ? AND revision = ?
                """,
                (definition_hash, result_hash, document_uid, int(revision)),
            )
            self.connection.execute(
                """
                UPDATE documents SET definition_hash = ?, result_hash = ?
                WHERE document_uid = ? AND head_revision = ?
                """,
                (definition_hash, result_hash, document_uid, int(revision)),
            )
        self.connection.execute(
            """
            INSERT INTO validation_results (
                document_uid, revision, worker_id, environment_id,
                definition_hash, result_hash, environment_matches,
                definition_matches, result_matches, valid, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_uid, revision) DO UPDATE SET
                worker_id = excluded.worker_id,
                environment_id = excluded.environment_id,
                definition_hash = excluded.definition_hash,
                result_hash = excluded.result_hash,
                environment_matches = excluded.environment_matches,
                definition_matches = excluded.definition_matches,
                result_matches = excluded.result_matches,
                valid = excluded.valid,
                error = excluded.error,
                validated_at = CURRENT_TIMESTAMP
            """,
            (
                document_uid,
                int(revision),
                worker_id,
                environment_id,
                definition_hash,
                result_hash,
                int(environment_matches),
                int(definition_matches),
                int(result_matches),
                int(valid),
                error,
            ),
        )
        return self.validation_report(document_uid, revision)

    def validation_report(self, document_uid: str, revision: int) -> ValidationReport:
        row = self.connection.execute(
            """
            SELECT * FROM validation_results
            WHERE document_uid = ? AND revision = ?
            """,
            (document_uid, int(revision)),
        ).fetchone()
        if row is None:
            raise UnknownDocumentError(f"no validation for {document_uid}@{revision}")
        return ValidationReport(
            document_uid=row["document_uid"],
            revision=int(row["revision"]),
            worker_id=row["worker_id"],
            environment_id=row["environment_id"],
            definition_hash=row["definition_hash"],
            result_hash=row["result_hash"],
            environment_matches=bool(row["environment_matches"]),
            definition_matches=bool(row["definition_matches"]),
            result_matches=bool(row["result_matches"]),
            valid=bool(row["valid"]),
            error=row["error"],
            validated_at=row["validated_at"],
        )

    @staticmethod
    def _record(row, *, duplicate: bool = False) -> RevisionRecord:
        return RevisionRecord(
            document_uid=row["document_uid"],
            revision=int(row["revision"]),
            transaction_uid=row["transaction_uid"],
            parent_revision=int(row["parent_revision"]),
            name=row["name"],
            packet_json=row["packet_json"],
            definition_hash=row["definition_hash"],
            result_hash=row["result_hash"],
            environment_id=row["environment_id"],
            accepted_at=row["accepted_at"],
            duplicate=duplicate,
        )
