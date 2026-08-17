from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from contextlib import contextmanager, suppress
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from collections.abc import Iterator
from collections.abc import Callable
from typing import Any, TypeVar
from uuid import uuid4

from app.agent.contracts import LangGraphRunRequest
from app.agent.errors import AgentError, EventHistoryExpiredError, MessageConflictError, RunNotFoundError


TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}
T = TypeVar("T")


def is_sqlite_busy_error(exc: sqlite3.OperationalError) -> bool:
    error_code = getattr(exc, "sqlite_errorcode", None)
    if error_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return True
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    conversation_id: str
    message_id: str
    tenant_id: str
    user_id: str
    trace_id: str
    status: str
    request_hash: str
    request: dict[str, Any]
    last_sequence: int
    route: str | None
    answer: str | None
    citations: list[dict[str, Any]]
    warnings: list[str]
    error: dict[str, Any] | None
    created_at: str
    updated_at: str
    lease_owner: str | None = None
    lease_until: str | None = None
    lease_generation: int = 0
    depends_on_run_id: str | None = None
    base_state_version: int = 0

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class EventRecord:
    run_id: str
    sequence: int
    event_name: str
    event_id: str
    data: dict[str, Any]
    created_at: str

    def to_sse(self) -> str:
        return f"id: {self.event_id}\nevent: {self.event_name}\ndata: {canonical_json(self.data)}\n\n"


@dataclass(frozen=True)
class ConversationMemory:
    conversation_id: str
    user_query: str
    assistant_answer: str
    route: str | None
    map_summary: dict[str, Any] | None
    created_at: str


class AgentStore:
    def __init__(
        self,
        path: Path,
        event_retention_seconds: int = 86400,
        *,
        busy_timeout_ms: int = 5000,
        write_retry_attempts: int = 4,
        write_retry_base_delay_ms: int = 50,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.event_retention_seconds = event_retention_seconds
        self.busy_timeout_ms = busy_timeout_ms
        self.write_retry_attempts = write_retry_attempts
        self.write_retry_base_delay_ms = write_retry_base_delay_ms
        self._lock = threading.RLock()
        self._initialize()

    def close(self) -> None:
        """Compatibility hook for the shared Runtime store lifecycle."""
        return None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
            connection.commit()
        except Exception:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise
        finally:
            connection.close()

    def _write_with_retry(self, operation: Callable[[], T]) -> T:
        for attempt in range(self.write_retry_attempts + 1):
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if not is_sqlite_busy_error(exc) or attempt >= self.write_retry_attempts:
                    raise
                delay = (self.write_retry_base_delay_ms / 1000) * (2**attempt)
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _initialize(self) -> None:
        self._write_with_retry(self._initialize_once)

    def _initialize_once(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL DEFAULT 0,
                    route TEXT,
                    answer TEXT,
                    citations_json TEXT NOT NULL DEFAULT '[]',
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    error_json TEXT,
                    lease_owner TEXT,
                    lease_until TEXT,
                    lease_generation INTEGER NOT NULL DEFAULT 0,
                    depends_on_run_id TEXT,
                    base_state_version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, user_id, conversation_id, message_id)
                );
                CREATE TABLE IF NOT EXISTS agent_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_name TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_agent_events_created
                    ON agent_events(run_id, created_at, sequence);
                CREATE TABLE IF NOT EXISTS conversation_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    user_query TEXT NOT NULL,
                    assistant_answer TEXT NOT NULL,
                    route TEXT,
                    map_summary_json TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_states (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    state_version INTEGER NOT NULL DEFAULT 0,
                    last_committed_run_id TEXT,
                    active_run_id TEXT,
                    state_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, user_id, conversation_id)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_memory_scope
                    ON conversation_memory(tenant_id, user_id, conversation_id, id DESC);
                CREATE TABLE IF NOT EXISTS agent_tool_metrics (
                    run_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, tool_call_id),
                    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS agent_run_metrics (
                    run_id TEXT PRIMARY KEY,
                    orchestration_duration_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS agent_sse_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    after_sequence INTEGER NOT NULL,
                    last_sequence INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    bytes_sent INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_agent_sse_metrics_run
                    ON agent_sse_metrics(run_id, id);
                CREATE TABLE IF NOT EXISTS agent_stage_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    stage_name TEXT NOT NULL,
                    operation TEXT,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    error_code TEXT,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    tool_call_id TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES agent_runs(run_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_agent_stage_metrics_run
                    ON agent_stage_metrics(run_id, id);
                CREATE TABLE IF NOT EXISTS quota_ledger (
                    reservation_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (reservation_id, operation_id)
                );
                """
            )
            run_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(agent_runs)")
            }
            for name, declaration in (
                ("lease_owner", "TEXT"),
                ("lease_until", "TEXT"),
                ("lease_generation", "INTEGER NOT NULL DEFAULT 0"),
                ("depends_on_run_id", "TEXT"),
                ("base_state_version", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if name not in run_columns:
                    connection.execute(f"ALTER TABLE agent_runs ADD COLUMN {name} {declaration}")
            memory_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(conversation_memory)")
            }
            if "run_id" not in memory_columns:
                connection.execute("ALTER TABLE conversation_memory ADD COLUMN run_id TEXT")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_memory_run "
                "ON conversation_memory(run_id) WHERE run_id IS NOT NULL"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_runs_claim "
                "ON agent_runs(status, lease_until, created_at)"
            )

    def list_conversation_memory(
        self, tenant_id: str, user_id: str, conversation_id: str, *, limit: int = 12
    ) -> list[ConversationMemory]:
        limit = max(1, min(int(limit), 20))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT conversation_id, user_query, assistant_answer, route,
                       map_summary_json, created_at
                FROM conversation_memory
                WHERE tenant_id = ? AND user_id = ? AND conversation_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (tenant_id, user_id, conversation_id, limit),
            ).fetchall()
        return [
            ConversationMemory(
                row["conversation_id"], row["user_query"], row["assistant_answer"],
                row["route"], json.loads(row["map_summary_json"]) if row["map_summary_json"] else None,
                row["created_at"],
            )
            for row in reversed(rows)
        ]

    def get_conversation_state(
        self, tenant_id: str, user_id: str, conversation_id: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT state_version, last_committed_run_id, active_run_id, state_json
                   FROM conversation_states
                   WHERE tenant_id = ? AND user_id = ? AND conversation_id = ?""",
                (tenant_id, user_id, conversation_id),
            ).fetchone()
        if row is None:
            return {
                "stateVersion": 0,
                "lastCommittedRunId": None,
                "activeRunId": None,
                "state": {},
            }
        return {
            "stateVersion": int(row["state_version"]),
            "lastCommittedRunId": row["last_committed_run_id"],
            "activeRunId": row["active_run_id"],
            "state": json.loads(row["state_json"]),
        }

    def save_conversation_memory(
        self,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        *,
        user_query: str,
        assistant_answer: str,
        route: str | None,
        map_summary: dict[str, Any] | None = None,
        limit: int = 12,
        run_id: str | None = None,
    ) -> None:
        def operation() -> None:
            with self._lock, self._connect() as connection:
                self._save_conversation_memory_in_connection(
                    connection,
                    tenant_id,
                    user_id,
                    conversation_id,
                    user_query=user_query,
                    assistant_answer=assistant_answer,
                    route=route,
                    map_summary=map_summary,
                    limit=limit,
                    run_id=run_id,
                )

        self._write_with_retry(operation)

    @staticmethod
    def _save_conversation_memory_in_connection(
        connection: sqlite3.Connection,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        *,
        user_query: str,
        assistant_answer: str,
        route: str | None,
        map_summary: dict[str, Any] | None,
        limit: int,
        run_id: str | None,
    ) -> None:
        query = user_query.strip()[:4000]
        answer = assistant_answer.strip()[:4000]
        summary_json = canonical_json(map_summary) if map_summary else None
        connection.execute(
            """
            INSERT INTO conversation_memory
                (run_id, tenant_id, user_id, conversation_id, user_query,
                 assistant_answer, route, map_summary_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) WHERE run_id IS NOT NULL DO NOTHING
            """,
            (
                run_id,
                tenant_id,
                user_id,
                conversation_id,
                query,
                answer,
                route,
                summary_json,
                iso_utc(),
            ),
        )
        connection.execute(
            """
            DELETE FROM conversation_memory
            WHERE tenant_id = ? AND user_id = ? AND conversation_id = ?
              AND id NOT IN (
                  SELECT id FROM conversation_memory
                  WHERE tenant_id = ? AND user_id = ? AND conversation_id = ?
                  ORDER BY id DESC LIMIT ?
              )
            """,
            (
                tenant_id,
                user_id,
                conversation_id,
                tenant_id,
                user_id,
                conversation_id,
                max(1, min(int(limit), 20)),
            ),
        )

    def record_tool_started(
        self,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        arguments_json = canonical_json(arguments)
        arguments_hash = hashlib.sha256(arguments_json.encode("utf-8")).hexdigest()

        def operation() -> None:
            with self._lock, self._connect() as connection:
                existing = connection.execute(
                    """
                    SELECT tool_name, arguments_hash FROM agent_tool_metrics
                    WHERE run_id = ? AND tool_call_id = ?
                    """,
                    (run_id, tool_call_id),
                ).fetchone()
                if existing is not None and (
                    existing["tool_name"] != tool_name
                    or existing["arguments_hash"] != arguments_hash
                ):
                    raise AgentError(
                        "TOOL_CALL_CONFLICT",
                        "同一 toolCallId 不能用于不同的 Tool 参数",
                        status_code=409,
                    )
                now = iso_utc()
                connection.execute(
                    """
                    INSERT INTO agent_tool_metrics (
                        run_id, tool_call_id, tool_name, arguments_json, arguments_hash,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?)
                    ON CONFLICT(run_id, tool_call_id) DO UPDATE SET
                        status = CASE
                            WHEN agent_tool_metrics.status IN ('SUCCEEDED', 'FAILED', 'REJECTED')
                                THEN agent_tool_metrics.status
                            ELSE 'RUNNING'
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        run_id,
                        tool_call_id,
                        tool_name,
                        arguments_json,
                        arguments_hash,
                        now,
                        now,
                    ),
                )

        self._write_with_retry(operation)

    def record_tool_completed(
        self,
        run_id: str,
        tool_call_id: str,
        *,
        status: str,
        duration_ms: int,
        retry_count: int = 0,
        error_code: str | None = None,
    ) -> None:
        self._write_with_retry(
            lambda: self._record_tool_completed_once(
                run_id,
                tool_call_id,
                status=status,
                duration_ms=duration_ms,
                retry_count=retry_count,
                error_code=error_code,
            )
        )

    def _record_tool_completed_once(
        self,
        run_id: str,
        tool_call_id: str,
        *,
        status: str,
        duration_ms: int,
        retry_count: int,
        error_code: str | None,
    ) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agent_tool_metrics
                SET status = ?, duration_ms = ?, retry_count = ?, error_code = ?, updated_at = ?
                WHERE run_id = ? AND tool_call_id = ?
                """,
                (
                    status,
                    max(0, int(duration_ms)),
                    max(0, int(retry_count)),
                    error_code,
                    iso_utc(),
                    run_id,
                    tool_call_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("tool metrics must be started before completion")

    def record_orchestration(
        self,
        run_id: str,
        *,
        duration_ms: int,
        status: str,
        error_code: str | None,
    ) -> None:
        def operation() -> None:
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO agent_run_metrics (
                        run_id, orchestration_duration_ms, status, error_code, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        orchestration_duration_ms = excluded.orchestration_duration_ms,
                        status = excluded.status,
                        error_code = excluded.error_code,
                        updated_at = excluded.updated_at
                    """,
                    (run_id, max(0, int(duration_ms)), status, error_code, iso_utc()),
                )

        self._write_with_retry(operation)

    def record_quota_settlement(
        self,
        *,
        reservation_id: str,
        operation_id: str,
        run_id: str,
        tenant_id: str,
        total_tokens: int,
        status: str,
    ) -> None:
        self._write_with_retry(
            lambda: self._record_quota_settlement_once(
                reservation_id, operation_id, run_id, tenant_id, total_tokens, status
            )
        )

    def _record_quota_settlement_once(self, reservation_id: str, operation_id: str, run_id: str, tenant_id: str, total_tokens: int, status: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO quota_ledger
                   (reservation_id, operation_id, run_id, tenant_id, total_tokens, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (reservation_id, operation_id, run_id, tenant_id, max(0, int(total_tokens)), status, iso_utc()),
            )

    def record_sse_stream(
        self,
        run_id: str,
        *,
        after_sequence: int,
        last_sequence: int,
        duration_ms: int,
        bytes_sent: int,
        status: str,
    ) -> None:
        def operation() -> None:
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO agent_sse_metrics (
                        run_id, after_sequence, last_sequence, duration_ms,
                        bytes_sent, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        max(0, int(after_sequence)),
                        max(0, int(last_sequence)),
                        max(0, int(duration_ms)),
                        max(0, int(bytes_sent)),
                        status,
                        iso_utc(),
                    ),
                )

        self._write_with_retry(operation)

    def record_stage(
        self,
        run_id: str,
        *,
        stage_name: str,
        status: str,
        duration_ms: int,
        operation: str | None = None,
        error_code: str | None = None,
        attempt: int = 1,
        tool_call_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist internal-only graph spans without storing prompts or raw user text."""
        metadata_json = canonical_json(metadata or {})

        def operation_write() -> None:
            with self._lock, self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO agent_stage_metrics (
                        run_id, stage_name, operation, status, duration_ms, error_code,
                        attempt, tool_call_id, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        stage_name,
                        operation,
                        status,
                        max(0, int(duration_ms)),
                        error_code,
                        max(1, int(attempt)),
                        tool_call_id,
                        metadata_json,
                        iso_utc(),
                    ),
                )

        self._write_with_retry(operation_write)

    def diagnostics(self, run_id: str, tenant_id: str, user_id: str) -> dict[str, Any]:
        run = self.get_run(run_id, tenant_id, user_id)
        with self._connect() as connection:
            tool_rows = connection.execute(
                """
                SELECT * FROM agent_tool_metrics
                WHERE run_id = ? ORDER BY created_at, tool_call_id
                """,
                (run_id,),
            ).fetchall()
            orchestration = connection.execute(
                "SELECT * FROM agent_run_metrics WHERE run_id = ?", (run_id,)
            ).fetchone()
            sse_rows = connection.execute(
                "SELECT * FROM agent_sse_metrics WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
            stage_rows = connection.execute(
                "SELECT * FROM agent_stage_metrics WHERE run_id = ? ORDER BY id", (run_id,)
            ).fetchall()
        stages = [
            {
                "stageName": row["stage_name"],
                "operation": row["operation"],
                "status": row["status"],
                "durationMs": row["duration_ms"],
                "errorCode": row["error_code"],
                "attempt": row["attempt"],
                "toolCallId": row["tool_call_id"],
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in stage_rows
        ]
        return {
            "runId": run.run_id,
            "messageId": run.message_id,
            "status": run.status,
            "toolCalls": [
                {
                    "toolCallId": row["tool_call_id"],
                    "toolName": row["tool_name"],
                    "arguments": json.loads(row["arguments_json"]),
                    "argumentsHash": row["arguments_hash"],
                    "status": row["status"],
                    "durationMs": row["duration_ms"],
                    "retryCount": row["retry_count"],
                    "errorCode": row["error_code"],
                }
                for row in tool_rows
            ],
            "orchestration": (
                {
                    "durationMs": orchestration["orchestration_duration_ms"],
                    "status": orchestration["status"],
                    "errorCode": orchestration["error_code"],
                }
                if orchestration is not None
                else None
            ),
            "sseStreams": [
                {
                    "afterSequence": row["after_sequence"],
                    "lastSequence": row["last_sequence"],
                    "durationMs": row["duration_ms"],
                    "bytesSent": row["bytes_sent"],
                    "status": row["status"],
                }
                for row in sse_rows
            ],
            "stages": stages,
            "decisionAudit": [
                item for item in stages if item["stageName"] in {"INPUT_NORMALIZATION", "ROUTING", "HOUSING_PLANNING"}
            ],
            "modelCalls": [item for item in stages if item["operation"] is not None],
        }

    def migrate_legacy_checkpoints(self, checkpoint_path: Path) -> bool:
        checkpoint_path = Path(checkpoint_path)
        if self.path.resolve() == checkpoint_path.resolve():
            raise ValueError("checkpoint database must be separate from the Run database")
        return self._write_with_retry(
            lambda: self._migrate_legacy_checkpoints_once(checkpoint_path)
        )

    def _migrate_legacy_checkpoints_once(self, checkpoint_path: Path) -> bool:
        with self._lock, self._connect() as connection:
            legacy_tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('checkpoints', 'writes')"
                ).fetchall()
            }
            if not legacy_tables:
                return False
            if legacy_tables != {"checkpoints", "writes"}:
                raise sqlite3.OperationalError("incomplete legacy checkpoint schema")

            connection.execute("ATTACH DATABASE ? AS checkpoint_db", (str(checkpoint_path),))
            try:
                target_tables = {
                    row["name"]
                    for row in connection.execute(
                        "SELECT name FROM checkpoint_db.sqlite_master "
                        "WHERE type = 'table' AND name IN ('checkpoints', 'writes')"
                    ).fetchall()
                }
                if target_tables != {"checkpoints", "writes"}:
                    raise sqlite3.OperationalError("checkpoint target schema is not initialized")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT OR IGNORE INTO checkpoint_db.checkpoints (
                        thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                        type, checkpoint, metadata
                    )
                    SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                           type, checkpoint, metadata
                    FROM main.checkpoints
                    """
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO checkpoint_db.writes (
                        thread_id, checkpoint_ns, checkpoint_id, task_id, idx,
                        channel, type, value
                    )
                    SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx,
                           channel, type, value
                    FROM main.writes
                    """
                )
                connection.execute("DROP TABLE main.writes")
                connection.execute("DROP TABLE main.checkpoints")
                connection.commit()
            finally:
                if not connection.in_transaction:
                    connection.execute("DETACH DATABASE checkpoint_db")
            return True

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            conversation_id=row["conversation_id"],
            message_id=row["message_id"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            trace_id=row["trace_id"],
            status=row["status"],
            request_hash=row["request_hash"],
            request=json.loads(row["request_json"]),
            last_sequence=row["last_sequence"],
            route=row["route"],
            answer=row["answer"],
            citations=json.loads(row["citations_json"]),
            warnings=json.loads(row["warnings_json"]),
            error=json.loads(row["error_json"]) if row["error_json"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            lease_owner=row["lease_owner"],
            lease_until=row["lease_until"],
            lease_generation=int(row["lease_generation"] or 0),
            depends_on_run_id=row["depends_on_run_id"],
            base_state_version=int(row["base_state_version"] or 0),
        )

    def create_or_attach(
        self,
        request: LangGraphRunRequest,
        trace_id: str,
        *,
        max_nonterminal_runs: int | None = None,
    ) -> tuple[RunRecord, bool]:
        request_data = request.model_dump(mode="json", by_alias=True)
        request_json = canonical_json(request_data)
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        identity = request.user
        conversation_id = str(request.conversation_id)
        message_id = str(request.message_id)

        return self._write_with_retry(
            lambda: self._create_or_attach_once(
                request_json,
                request_hash,
                identity.tenant_id,
                identity.user_id,
                conversation_id,
                message_id,
                trace_id,
                max_nonterminal_runs,
            )
        )

    def _create_or_attach_once(
        self,
        request_json: str,
        request_hash: str,
        tenant_id: str,
        user_id: str,
        conversation_id: str,
        message_id: str,
        trace_id: str,
        max_nonterminal_runs: int | None = None,
    ) -> tuple[RunRecord, bool]:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM agent_runs
                WHERE tenant_id = ? AND user_id = ? AND conversation_id = ? AND message_id = ?
                """,
                (tenant_id, user_id, conversation_id, message_id),
            ).fetchone()
            if existing:
                record = self._row_to_run(existing)
                if record.request_hash != request_hash:
                    raise MessageConflictError()
                return record, False

            if max_nonterminal_runs is not None:
                queued = connection.execute(
                    "SELECT COUNT(*) AS count FROM agent_runs "
                    "WHERE status IN ('QUEUED', 'RUNNING')"
                ).fetchone()["count"]
                if int(queued) >= max_nonterminal_runs:
                    raise AgentError(
                        "RUN_QUEUE_FULL",
                        "Agent Run queue is full",
                        status_code=429,
                        retryable=True,
                        details={"maxQueuedRuns": max_nonterminal_runs},
                    )

            state_row = connection.execute(
                """SELECT state_version FROM conversation_states
                   WHERE tenant_id = ? AND user_id = ? AND conversation_id = ?""",
                (tenant_id, user_id, conversation_id),
            ).fetchone()
            base_state_version = int(state_row["state_version"]) if state_row else 0
            dependency = connection.execute(
                """SELECT run_id FROM agent_runs
                   WHERE tenant_id = ? AND user_id = ? AND conversation_id = ?
                     AND status IN ('QUEUED', 'RUNNING')
                   ORDER BY created_at DESC LIMIT 1""",
                (tenant_id, user_id, conversation_id),
            ).fetchone()
            depends_on_run_id = dependency["run_id"] if dependency else None

            now = iso_utc()
            run_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO agent_runs (
                    run_id, conversation_id, message_id, tenant_id, user_id, trace_id,
                    status, request_hash, request_json, depends_on_run_id,
                    base_state_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    conversation_id,
                    message_id,
                    tenant_id,
                    user_id,
                    trace_id,
                    request_hash,
                    request_json,
                    depends_on_run_id,
                    base_state_version,
                    now,
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO conversation_states
                   (tenant_id, user_id, conversation_id, state_version,
                    last_committed_run_id, active_run_id, state_json, updated_at)
                   VALUES (?, ?, ?, ?, NULL, ?, '{}', ?)
                   ON CONFLICT(tenant_id, user_id, conversation_id) DO UPDATE SET
                     active_run_id = excluded.active_run_id, updated_at = excluded.updated_at""",
                (tenant_id, user_id, conversation_id, base_state_version, run_id, now),
            )
            row = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            return self._row_to_run(row), True

    def get_run(self, run_id: str, tenant_id: str, user_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id = ? AND tenant_id = ? AND user_id = ?",
                (run_id, tenant_id, user_id),
            ).fetchone()
        if row is None:
            raise RunNotFoundError()
        return self._row_to_run(row)

    def get_run_unscoped(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise RunNotFoundError()
        return self._row_to_run(row)

    def list_nonterminal_runs(self) -> list[RunRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_runs WHERE status IN ('QUEUED', 'RUNNING') ORDER BY created_at"
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def claim_next_run(
        self, worker_id: str, *, lease_seconds: int
    ) -> RunRecord | None:
        return self._write_with_retry(
            lambda: self._claim_next_run_once(worker_id, lease_seconds=lease_seconds)
        )

    def _claim_next_run_once(
        self, worker_id: str, *, lease_seconds: int
    ) -> RunRecord | None:
        now = iso_utc()
        lease_until = iso_utc(utc_now() + timedelta(seconds=lease_seconds))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM agent_runs
                WHERE status IN ('QUEUED', 'RUNNING')
                  AND (lease_until IS NULL OR lease_until <= ?)
                  AND (depends_on_run_id IS NULL OR depends_on_run_id IN
                       (SELECT run_id FROM agent_runs WHERE status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')))
                ORDER BY created_at
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE agent_runs
                SET status = 'RUNNING', lease_owner = ?, lease_until = ?,
                    lease_generation = lease_generation + 1, updated_at = ?,
                    base_state_version = COALESCE((
                        SELECT state_version FROM conversation_states
                        WHERE tenant_id = agent_runs.tenant_id
                          AND user_id = agent_runs.user_id
                          AND conversation_id = agent_runs.conversation_id
                    ), 0)
                WHERE run_id = ?
                """,
                (worker_id, lease_until, now, row["run_id"]),
            )
            claimed = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id = ?", (row["run_id"],)
            ).fetchone()
            return self._row_to_run(claimed)

    def renew_lease(
        self,
        run_id: str,
        worker_id: str,
        *,
        lease_seconds: int,
        lease_generation: int | None = None,
    ) -> bool:
        def operation() -> bool:
            with self._lock, self._connect() as connection:
                generation_clause = ""
                parameters: list[object] = [
                    iso_utc(utc_now() + timedelta(seconds=lease_seconds)),
                    iso_utc(),
                    run_id,
                    worker_id,
                ]
                if lease_generation is not None:
                    generation_clause = " AND lease_generation = ?"
                    parameters.append(lease_generation)
                cursor = connection.execute(
                    """
                    UPDATE agent_runs SET lease_until = ?, updated_at = ?
                    WHERE run_id = ? AND lease_owner = ? AND status = 'RUNNING'
                    """ + generation_clause,
                    parameters,
                )
                return cursor.rowcount == 1

        return self._write_with_retry(operation)

    def release_lease(
        self,
        run_id: str,
        worker_id: str,
        *,
        requeue: bool = True,
        lease_generation: int | None = None,
    ) -> bool:
        def operation() -> bool:
            with self._lock, self._connect() as connection:
                generation_clause = ""
                parameters: list[object] = [
                    1 if requeue else 0,
                    iso_utc(),
                    run_id,
                    worker_id,
                ]
                if lease_generation is not None:
                    generation_clause = " AND lease_generation = ?"
                    parameters.append(lease_generation)
                cursor = connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = CASE WHEN ? THEN 'QUEUED' ELSE status END,
                        lease_owner = NULL, lease_until = NULL, updated_at = ?
                    WHERE run_id = ? AND lease_owner = ?
                      AND status IN ('QUEUED', 'RUNNING')
                    """ + generation_clause,
                    parameters,
                )
                return cursor.rowcount == 1

        return self._write_with_retry(operation)

    def append_event(
        self,
        run_id: str,
        event_name: str,
        payload: dict[str, Any],
        *,
        route: str | None = None,
    ) -> EventRecord:
        return self._write_with_retry(
            lambda: self._append_event_once(run_id, event_name, payload, route=route)
        )

    def _append_event_once(
        self,
        run_id: str,
        event_name: str,
        payload: dict[str, Any],
        *,
        route: str | None = None,
    ) -> EventRecord:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._append_event_in_connection(
                connection, run_id, event_name, payload, route=route
            )

    def _append_event_in_connection(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_name: str,
        payload: dict[str, Any],
        *,
        route: str | None = None,
    ) -> EventRecord:
        row = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFoundError()
        run = self._row_to_run(row)
        if run.terminal:
            raise RuntimeError("cannot append an event to a terminal run")
        sequence = run.last_sequence + 1
        timestamp = iso_utc()
        data = {
            "schemaVersion": "1.1",
            "runId": run.run_id,
            "messageId": run.message_id,
            "sequence": sequence,
            "traceId": run.trace_id,
            "timestamp": timestamp,
            "payload": payload,
        }
        event_id = f"{run.run_id}:{sequence}"
        connection.execute(
            """
            INSERT INTO agent_events (run_id, sequence, event_name, event_id, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, sequence, event_name, event_id, canonical_json(data), timestamp),
        )

        status = run.status
        answer = run.answer
        citations_json = canonical_json(run.citations)
        warnings_json = canonical_json(run.warnings)
        error_json = canonical_json(run.error) if run.error else None
        if event_name == "run.completed":
            status = "SUCCEEDED"
            answer = str(payload.get("answer", ""))
            citations_json = canonical_json(payload.get("citations", []))
            warnings_json = canonical_json(payload.get("warnings", []))
        elif event_name == "run.failed":
            status = "FAILED"
            error_json = canonical_json(payload.get("error", {}))
        elif event_name == "run.cancelled":
            status = "CANCELLED"
        terminal = status in TERMINAL_STATUSES
        connection.execute(
            """
            UPDATE agent_runs SET last_sequence = ?, status = ?, route = COALESCE(?, route),
                answer = ?, citations_json = ?, warnings_json = ?, error_json = ?, updated_at = ?,
                lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END,
                lease_until = CASE WHEN ? THEN NULL ELSE lease_until END
            WHERE run_id = ?
            """,
            (
                sequence,
                status,
                route,
                answer,
                citations_json,
                warnings_json,
                error_json,
                timestamp,
                1 if terminal else 0,
                1 if terminal else 0,
                run_id,
            ),
        )
        return EventRecord(run_id, sequence, event_name, event_id, data, timestamp)

    def complete_run_with_memory(
        self,
        run_id: str,
        *,
        user_query: str,
        assistant_answer: str,
        route: str | None,
        map_summary: dict[str, Any] | None,
        citations: list[dict[str, Any]],
        warnings: list[str],
        memory_limit: int = 12,
        conversation_state: dict[str, Any] | None = None,
    ) -> EventRecord:
        def operation() -> EventRecord:
            with self._lock, self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if row is None:
                    raise RunNotFoundError()
                run = self._row_to_run(row)
                self._save_conversation_memory_in_connection(
                    connection,
                    run.tenant_id,
                    run.user_id,
                    run.conversation_id,
                    user_query=user_query,
                    assistant_answer=assistant_answer,
                    route=route,
                    map_summary=map_summary,
                    limit=memory_limit,
                    run_id=run_id,
                )
                if conversation_state is not None:
                    state_row = connection.execute(
                        """SELECT state_version FROM conversation_states
                           WHERE tenant_id = ? AND user_id = ? AND conversation_id = ?""",
                        (run.tenant_id, run.user_id, run.conversation_id),
                    ).fetchone()
                    current_version = int(state_row["state_version"]) if state_row else 0
                    if current_version != run.base_state_version:
                        raise AgentError(
                            "STATE_VERSION_CONFLICT",
                            "会话状态已被其他 Run 更新",
                            status_code=409,
                            retryable=True,
                            details={"expected": run.base_state_version, "actual": current_version},
                        )
                    next_version = current_version + 1
                    now = iso_utc()
                    connection.execute(
                        """INSERT INTO conversation_states
                           (tenant_id, user_id, conversation_id, state_version,
                            last_committed_run_id, active_run_id, state_json, updated_at)
                           VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                           ON CONFLICT(tenant_id, user_id, conversation_id) DO UPDATE SET
                             state_version = excluded.state_version,
                             last_committed_run_id = excluded.last_committed_run_id,
                             active_run_id = NULL,
                             state_json = excluded.state_json,
                             updated_at = excluded.updated_at""",
                        (
                            run.tenant_id,
                            run.user_id,
                            run.conversation_id,
                            next_version,
                            run_id,
                            canonical_json(conversation_state),
                            now,
                        ),
                    )
                return self._append_event_in_connection(
                    connection,
                    run_id,
                    "run.completed",
                    {
                        "status": "SUCCEEDED",
                        "answer": assistant_answer,
                        "citations": citations,
                        "warnings": warnings,
                    },
                )

        return self._write_with_retry(operation)

    def list_events(self, run_id: str, after_sequence: int) -> list[EventRecord]:
        cutoff = iso_utc(utc_now() - timedelta(seconds=self.event_retention_seconds))
        with self._connect() as connection:
            run_row = connection.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise RunNotFoundError()
            earliest = connection.execute(
                "SELECT MIN(sequence) AS sequence FROM agent_events WHERE run_id = ? AND created_at >= ?",
                (run_id, cutoff),
            ).fetchone()["sequence"]
            run_last_sequence = int(run_row["last_sequence"])
            if earliest is None and run_last_sequence > after_sequence:
                raise EventHistoryExpiredError()
            if earliest is not None and after_sequence + 1 < earliest:
                raise EventHistoryExpiredError()
            rows = connection.execute(
                """
                SELECT * FROM agent_events
                WHERE run_id = ? AND sequence > ? AND created_at >= ?
                ORDER BY sequence
                """,
                (run_id, after_sequence, cutoff),
            ).fetchall()
        return [
            EventRecord(
                run_id=row["run_id"],
                sequence=row["sequence"],
                event_name=row["event_name"],
                event_id=row["event_id"],
                data=json.loads(row["data_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def has_tool_event(self, run_id: str, event_name: str, tool_call_id: str) -> bool:
        if event_name not in {"tool.started", "tool.completed"}:
            return False
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data_json FROM agent_events WHERE run_id = ? AND event_name = ?",
                (run_id, event_name),
            ).fetchall()
        return any(
            json.loads(row["data_json"]).get("payload", {}).get("toolCallId") == tool_call_id
            for row in rows
        )

    def pending_tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_name, data_json FROM agent_events
                WHERE run_id = ? AND event_name IN ('tool.started', 'tool.completed')
                ORDER BY sequence
                """,
                (run_id,),
            ).fetchall()
        started: dict[str, dict[str, Any]] = {}
        completed: set[str] = set()
        for row in rows:
            payload = json.loads(row["data_json"]).get("payload", {})
            tool_call_id = payload.get("toolCallId")
            if not tool_call_id:
                continue
            if row["event_name"] == "tool.started":
                started[tool_call_id] = payload
            else:
                completed.add(tool_call_id)
        return [payload for tool_call_id, payload in started.items() if tool_call_id not in completed]
