from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.agent.contracts import LangGraphRunRequest
from app.agent.errors import AgentError, EventHistoryExpiredError, MessageConflictError, RunNotFoundError
from app.agent.store import ConversationMemory, EventRecord, RunRecord, TERMINAL_STATUSES, canonical_json, iso_utc, utc_now


class MongoAgentStore:
    """MongoDB implementation of the Run/Conversation/Event store.

    Run completion and its memory write are deliberately performed in one
    Mongo transaction. A replica set (including a single-node replica set) is
    therefore required when ``require_transactions`` is enabled.
    """

    def __init__(
        self,
        uri: str,
        database: str,
        *,
        event_retention_seconds: int = 86400,
        require_transactions: bool = True,
        client: Any = None,
    ) -> None:
        if not uri:
            raise ValueError("MongoDB URI is required")
        self.client = client or MongoClient(uri, serverSelectionTimeoutMS=5000)
        self.db = self.client[database]
        self.event_retention_seconds = event_retention_seconds
        self.require_transactions = require_transactions
        self.runs = self.db.agent_runs
        self.events = self.db.agent_events
        self.memory = self.db.conversation_memory
        self.conversation_states = self.db.conversation_states
        self.tool_metrics = self.db.agent_tool_metrics
        self.run_metrics = self.db.agent_run_metrics
        self.sse_metrics = self.db.agent_sse_metrics
        self.stage_metrics = self.db.agent_stage_metrics
        self.quota_ledger = self.db.quota_ledger
        self._ensure_indexes()

    def validate_connectivity(self) -> None:
        """Fail readiness early when Mongo cannot provide required transactions."""
        hello = self.client.admin.command("hello")
        if self.require_transactions and not hello.get("setName"):
            raise AgentError(
                "AGENT_MONGODB_TRANSACTIONS_REQUIRED",
                "MongoDB Agent 存储要求副本集或分片事务；当前实例是 standalone",
                status_code=503,
                retryable=False,
                details={"host": hello.get("me"), "setName": hello.get("setName")},
            )

    def _ensure_indexes(self) -> None:
        self.runs.create_index([("run_id", ASCENDING)], unique=True)
        self.runs.create_index(
            [("tenant_id", ASCENDING), ("user_id", ASCENDING), ("conversation_id", ASCENDING), ("message_id", ASCENDING)],
            unique=True,
        )
        self.runs.create_index([("status", ASCENDING), ("lease_until", ASCENDING), ("created_at", ASCENDING)])
        self.events.create_index([("run_id", ASCENDING), ("sequence", ASCENDING)], unique=True)
        self.events.create_index([("run_id", ASCENDING), ("created_at", ASCENDING)])
        self.memory.create_index([("run_id", ASCENDING)], unique=True, sparse=True)
        self.memory.create_index([("tenant_id", ASCENDING), ("user_id", ASCENDING), ("conversation_id", ASCENDING), ("created_at", DESCENDING)])
        self.conversation_states.create_index(
            [("tenant_id", ASCENDING), ("user_id", ASCENDING), ("conversation_id", ASCENDING)],
            unique=True,
        )
        self.tool_metrics.create_index([("run_id", ASCENDING), ("tool_call_id", ASCENDING)], unique=True)
        self.run_metrics.create_index([("run_id", ASCENDING)], unique=True)
        self.sse_metrics.create_index([("run_id", ASCENDING), ("created_at", ASCENDING)])
        self.stage_metrics.create_index([("run_id", ASCENDING), ("created_at", ASCENDING)])
        self.quota_ledger.create_index([("reservation_id", ASCENDING), ("operation_id", ASCENDING)], unique=True)

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        if not self.require_transactions:
            yield None
            return
        with self.client.start_session() as session:
            with session.start_transaction():
                yield session

    @staticmethod
    def _run(doc: dict[str, Any]) -> RunRecord:
        return RunRecord(
            run_id=doc["run_id"], conversation_id=doc["conversation_id"], message_id=doc["message_id"],
            tenant_id=doc["tenant_id"], user_id=doc["user_id"], trace_id=doc["trace_id"],
            status=doc["status"], request_hash=doc["request_hash"], request=doc["request"],
            last_sequence=int(doc.get("last_sequence", 0)), route=doc.get("route"), answer=doc.get("answer"),
            citations=doc.get("citations", []), warnings=doc.get("warnings", []), error=doc.get("error"),
            created_at=doc["created_at"], updated_at=doc["updated_at"], lease_owner=doc.get("lease_owner"),
            lease_until=doc.get("lease_until"), lease_generation=int(doc.get("lease_generation", 0)),
            depends_on_run_id=doc.get("depends_on_run_id"),
            base_state_version=int(doc.get("base_state_version", 0)),
        )

    @staticmethod
    def _event(doc: dict[str, Any]) -> EventRecord:
        return EventRecord(doc["run_id"], int(doc["sequence"]), doc["event_name"], doc["event_id"], doc["data"], doc["created_at"])

    def create_or_attach(self, request: LangGraphRunRequest, trace_id: str, *, max_nonterminal_runs: int | None = None) -> tuple[RunRecord, bool]:
        data = request.model_dump(mode="json", by_alias=True)
        request_hash = hashlib.sha256(canonical_json(data).encode()).hexdigest()
        scope = {"tenant_id": request.user.tenant_id, "user_id": request.user.user_id, "conversation_id": str(request.conversation_id), "message_id": str(request.message_id)}
        existing = self.runs.find_one(scope)
        if existing:
            if existing["request_hash"] != request_hash:
                raise MessageConflictError()
            return self._run(existing), False
        if max_nonterminal_runs is not None and self.runs.count_documents({"status": {"$in": ["QUEUED", "RUNNING"]}}) >= max_nonterminal_runs:
            raise AgentError("RUN_QUEUE_FULL", "Agent Run queue is full", status_code=429, retryable=True, details={"maxQueuedRuns": max_nonterminal_runs})
        conversation_scope = {k: scope[k] for k in ("tenant_id", "user_id", "conversation_id")}
        now = iso_utc()
        run_id = str(uuid4())
        previous_state = self.conversation_states.find_one_and_update(
            conversation_scope,
            {"$set": {"active_run_id": run_id, "updated_at": now}, "$setOnInsert": {"state_version": 0, "last_committed_run_id": None, "state": {}}},
            upsert=True,
            return_document=ReturnDocument.BEFORE,
        )
        base_state_version = int(previous_state.get("state_version", 0)) if previous_state else 0
        depends_on_run_id = previous_state.get("active_run_id") if previous_state else None
        doc = {**scope, "run_id": run_id, "trace_id": trace_id, "status": "QUEUED", "request_hash": request_hash, "request": data, "last_sequence": 0, "route": None, "answer": None, "citations": [], "warnings": [], "error": None, "lease_owner": None, "lease_until": None, "lease_generation": 0, "depends_on_run_id": depends_on_run_id, "base_state_version": base_state_version, "created_at": now, "updated_at": now}
        try:
            self.runs.insert_one(doc)
        except DuplicateKeyError:
            existing = self.runs.find_one(scope)
            if existing and existing["request_hash"] == request_hash:
                self.conversation_states.update_one(
                    {**conversation_scope, "active_run_id": run_id},
                    {"$set": {"active_run_id": existing["run_id"], "updated_at": iso_utc()}},
                )
                return self._run(existing), False
            raise MessageConflictError()
        return self._run(doc), True

    def get_run(self, run_id: str, tenant_id: str, user_id: str) -> RunRecord:
        doc = self.runs.find_one({"run_id": run_id, "tenant_id": tenant_id, "user_id": user_id})
        if not doc:
            raise RunNotFoundError()
        return self._run(doc)

    def get_run_unscoped(self, run_id: str) -> RunRecord:
        doc = self.runs.find_one({"run_id": run_id})
        if not doc:
            raise RunNotFoundError()
        return self._run(doc)

    def _get_run_in_session(self, run_id: str, session: Any) -> RunRecord:
        doc = self.runs.find_one({"run_id": run_id}, session=session)
        if not doc:
            raise RunNotFoundError()
        return self._run(doc)

    def list_nonterminal_runs(self) -> list[RunRecord]:
        return [self._run(d) for d in self.runs.find({"status": {"$in": ["QUEUED", "RUNNING"]}}).sort("created_at", ASCENDING)]

    def claim_next_run(self, worker_id: str, *, lease_seconds: int) -> RunRecord | None:
        now = iso_utc()
        until = iso_utc(utc_now() + timedelta(seconds=lease_seconds))
        candidates = self.runs.find(
            {"status": {"$in": ["QUEUED", "RUNNING"]}, "$or": [{"lease_until": None}, {"lease_until": {"$lte": now}}]}
        ).sort("created_at", ASCENDING)
        for candidate in candidates:
            dependency_id = candidate.get("depends_on_run_id")
            if dependency_id:
                dependency = self.runs.find_one({"run_id": dependency_id}, {"status": 1})
                if dependency and dependency.get("status") not in TERMINAL_STATUSES:
                    continue
            state = self.conversation_states.find_one(
                {"tenant_id": candidate["tenant_id"], "user_id": candidate["user_id"], "conversation_id": candidate["conversation_id"]},
                {"state_version": 1},
            )
            base_state_version = int(state.get("state_version", 0)) if state else 0
            doc = self.runs.find_one_and_update(
                {"run_id": candidate["run_id"], "status": {"$in": ["QUEUED", "RUNNING"]}, "$or": [{"lease_until": None}, {"lease_until": {"$lte": now}}]},
                {"$set": {"status": "RUNNING", "lease_owner": worker_id, "lease_until": until, "updated_at": now, "base_state_version": base_state_version}, "$inc": {"lease_generation": 1}},
                return_document=ReturnDocument.AFTER,
            )
            if doc:
                return self._run(doc)
        return None

    def renew_lease(self, run_id: str, worker_id: str, *, lease_seconds: int, lease_generation: int | None = None) -> bool:
        query = {"run_id": run_id, "lease_owner": worker_id, "status": "RUNNING"}
        if lease_generation is not None:
            query["lease_generation"] = lease_generation
        result = self.runs.update_one(query, {"$set": {"lease_until": iso_utc(utc_now() + timedelta(seconds=lease_seconds)), "updated_at": iso_utc()}})
        return result.modified_count == 1

    def release_lease(self, run_id: str, worker_id: str, *, requeue: bool = True, lease_generation: int | None = None) -> bool:
        query = {"run_id": run_id, "lease_owner": worker_id, "status": {"$in": ["QUEUED", "RUNNING"]}}
        if lease_generation is not None:
            query["lease_generation"] = lease_generation
        result = self.runs.update_one(query, {"$set": {"status": "QUEUED" if requeue else "RUNNING", "lease_owner": None, "lease_until": None, "updated_at": iso_utc()}})
        return result.modified_count == 1

    def _append_event(self, run_id: str, event_name: str, payload: dict[str, Any], *, route: str | None = None, session: Any = None) -> EventRecord:
        run_doc = self.runs.find_one({"run_id": run_id}, session=session)
        if not run_doc:
            raise RunNotFoundError()
        run = self._run(run_doc)
        if run.terminal:
            raise RuntimeError("cannot append an event to a terminal run")
        sequence = run.last_sequence + 1
        timestamp = iso_utc()
        data = {"schemaVersion": "1.1", "runId": run.run_id, "messageId": run.message_id, "sequence": sequence, "traceId": run.trace_id, "timestamp": timestamp, "payload": payload}
        doc = {"run_id": run_id, "sequence": sequence, "event_name": event_name, "event_id": f"{run_id}:{sequence}", "data": data, "created_at": timestamp}
        self.events.insert_one(doc, session=session)
        update: dict[str, Any] = {"last_sequence": sequence, "updated_at": timestamp}
        if route is not None: update["route"] = route
        if event_name == "run.completed": update.update(status="SUCCEEDED", answer=str(payload.get("answer", "")), citations=payload.get("citations", []), warnings=payload.get("warnings", []), lease_owner=None, lease_until=None)
        elif event_name == "run.failed": update.update(status="FAILED", error=payload.get("error"), lease_owner=None, lease_until=None)
        elif event_name == "run.cancelled": update.update(status="CANCELLED", lease_owner=None, lease_until=None)
        self.runs.update_one({"run_id": run_id}, {"$set": update}, session=session)
        return self._event(doc)

    def append_event(self, run_id: str, event_name: str, payload: dict[str, Any], *, route: str | None = None) -> EventRecord:
        with self._transaction() as session:
            return self._append_event(run_id, event_name, payload, route=route, session=session)

    def complete_run_with_memory(self, run_id: str, *, user_query: str, assistant_answer: str, route: str | None, map_summary: dict[str, Any] | None, citations: list[dict[str, Any]], warnings: list[str], memory_limit: int = 12, conversation_state: dict[str, Any] | None = None) -> EventRecord:
        with self._transaction() as session:
            run = self._get_run_in_session(run_id, session)
            self.memory.update_one({"run_id": run_id}, {"$setOnInsert": {"run_id": run_id, "tenant_id": run.tenant_id, "user_id": run.user_id, "conversation_id": run.conversation_id, "user_query": user_query.strip()[:4000], "assistant_answer": assistant_answer.strip()[:4000], "route": route, "map_summary": map_summary, "created_at": iso_utc()}}, upsert=True, session=session)
            recent = list(self.memory.find(
                {"tenant_id": run.tenant_id, "user_id": run.user_id, "conversation_id": run.conversation_id},
                {"_id": 1},
                session=session,
            ).sort("created_at", DESCENDING).skip(max(1, min(int(memory_limit), 20))))
            if recent:
                self.memory.delete_many({"_id": {"$in": [item["_id"] for item in recent]}}, session=session)
            if conversation_state is not None:
                state_scope = {"tenant_id": run.tenant_id, "user_id": run.user_id, "conversation_id": run.conversation_id}
                state_doc = self.conversation_states.find_one(state_scope, session=session)
                current_version = int(state_doc.get("state_version", 0)) if state_doc else 0
                if current_version != run.base_state_version:
                    raise AgentError(
                        "STATE_VERSION_CONFLICT",
                        "会话状态已被其他 Run 更新",
                        status_code=409,
                        retryable=True,
                        details={"expected": run.base_state_version, "actual": current_version},
                    )
                self.conversation_states.update_one(
                    state_scope,
                    {"$set": {"state_version": current_version + 1, "last_committed_run_id": run_id, "active_run_id": None, "state": conversation_state, "updated_at": iso_utc()}},
                    upsert=True,
                    session=session,
                )
            return self._append_event(run_id, "run.completed", {"status": "SUCCEEDED", "answer": assistant_answer, "citations": citations, "warnings": warnings}, session=session)

    def list_conversation_memory(self, tenant_id: str, user_id: str, conversation_id: str, *, limit: int = 12) -> list[ConversationMemory]:
        docs = list(self.memory.find({"tenant_id": tenant_id, "user_id": user_id, "conversation_id": conversation_id}).sort("created_at", DESCENDING).limit(max(1, min(int(limit), 20))))
        return [ConversationMemory(d["conversation_id"], d["user_query"], d["assistant_answer"], d.get("route"), d.get("map_summary"), d["created_at"]) for d in reversed(docs)]

    def get_conversation_state(self, tenant_id: str, user_id: str, conversation_id: str) -> dict[str, Any]:
        doc = self.conversation_states.find_one({"tenant_id": tenant_id, "user_id": user_id, "conversation_id": conversation_id})
        if not doc:
            return {"stateVersion": 0, "lastCommittedRunId": None, "activeRunId": None, "state": {}}
        return {
            "stateVersion": int(doc.get("state_version", 0)),
            "lastCommittedRunId": doc.get("last_committed_run_id"),
            "activeRunId": doc.get("active_run_id"),
            "state": doc.get("state", {}),
        }

    def save_conversation_memory(self, tenant_id: str, user_id: str, conversation_id: str, *, user_query: str, assistant_answer: str, route: str | None, map_summary: dict[str, Any] | None = None, limit: int = 12, run_id: str | None = None) -> None:
        doc = {"tenant_id": tenant_id, "user_id": user_id, "conversation_id": conversation_id, "user_query": user_query.strip()[:4000], "assistant_answer": assistant_answer.strip()[:4000], "route": route, "map_summary": map_summary, "created_at": iso_utc()}
        if run_id:
            doc["run_id"] = run_id
            self.memory.update_one({"run_id": run_id}, {"$setOnInsert": doc}, upsert=True)
        else: self.memory.insert_one(doc)
        recent = list(self.memory.find({"tenant_id": tenant_id, "user_id": user_id, "conversation_id": conversation_id}, {"_id": 1}).sort("created_at", DESCENDING).skip(max(1, min(int(limit), 20))))
        if recent: self.memory.delete_many({"_id": {"$in": [d["_id"] for d in recent]}})

    def list_events(self, run_id: str, after_sequence: int) -> list[EventRecord]:
        run = self.get_run_unscoped(run_id)
        cutoff = iso_utc(utc_now() - timedelta(seconds=self.event_retention_seconds))
        earliest = self.events.find_one({"run_id": run_id, "created_at": {"$gte": cutoff}}, sort=[("sequence", ASCENDING)])
        if (earliest is None and run.last_sequence > after_sequence) or (earliest and after_sequence + 1 < earliest["sequence"]):
            raise EventHistoryExpiredError()
        return [self._event(d) for d in self.events.find({"run_id": run_id, "sequence": {"$gt": after_sequence}, "created_at": {"$gte": cutoff}}).sort("sequence", ASCENDING)]

    def has_tool_event(self, run_id: str, event_name: str, tool_call_id: str) -> bool:
        d = self.events.find_one({"run_id": run_id, "event_name": event_name, "data.payload.toolCallId": tool_call_id})
        return d is not None

    def pending_tool_calls(self, run_id: str) -> list[dict[str, Any]]:
        started: dict[str, dict[str, Any]] = {}; completed: set[str] = set()
        for d in self.events.find({"run_id": run_id, "event_name": {"$in": ["tool.started", "tool.completed"]}}).sort("sequence", ASCENDING):
            payload = d["data"].get("payload", {}); call_id = payload.get("toolCallId")
            if not call_id: continue
            if d["event_name"] == "tool.started": started[call_id] = payload
            else: completed.add(call_id)
        return [p for call_id, p in started.items() if call_id not in completed]

    def record_tool_started(self, run_id: str, tool_call_id: str, tool_name: str, arguments: dict[str, Any]) -> None:
        arguments_hash = hashlib.sha256(canonical_json(arguments).encode("utf-8")).hexdigest()
        existing = self.tool_metrics.find_one({"run_id": run_id, "tool_call_id": tool_call_id})
        if existing and (existing.get("tool_name") != tool_name or existing.get("arguments_hash") != arguments_hash):
            raise AgentError("TOOL_CALL_CONFLICT", "同一 toolCallId 不能用于不同的 Tool 参数", status_code=409)
        now = iso_utc(); self.tool_metrics.update_one({"run_id": run_id, "tool_call_id": tool_call_id}, {"$setOnInsert": {"run_id": run_id, "tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": arguments, "arguments_hash": arguments_hash, "status": "RUNNING", "retry_count": 0, "created_at": now}, "$set": {"updated_at": now}}, upsert=True)

    def record_tool_completed(self, run_id: str, tool_call_id: str, *, status: str, duration_ms: int, error_code: str | None = None, retry_count: int = 0) -> None:
        self.tool_metrics.update_one({"run_id": run_id, "tool_call_id": tool_call_id}, {"$set": {"status": status, "duration_ms": max(0, int(duration_ms)), "error_code": error_code, "retry_count": retry_count, "updated_at": iso_utc()}})

    def record_orchestration(self, run_id: str, *, duration_ms: int, status: str, error_code: str | None = None) -> None:
        self.run_metrics.update_one({"run_id": run_id}, {"$set": {"run_id": run_id, "orchestration_duration_ms": max(0, int(duration_ms)), "status": status, "error_code": error_code, "updated_at": iso_utc()}}, upsert=True)

    def record_quota_settlement(self, *, reservation_id: str, operation_id: str, run_id: str, tenant_id: str, total_tokens: int, status: str) -> None:
        self.quota_ledger.update_one(
            {"reservation_id": reservation_id, "operation_id": operation_id},
            {"$setOnInsert": {"run_id": run_id, "tenant_id": tenant_id, "total_tokens": max(0, int(total_tokens)), "status": status, "created_at": iso_utc()}},
            upsert=True,
        )

    def record_sse_stream(self, run_id: str, *, after_sequence: int, last_sequence: int, duration_ms: int, bytes_sent: int, status: str) -> None:
        self.sse_metrics.insert_one({"run_id": run_id, "after_sequence": max(0, int(after_sequence)), "last_sequence": max(0, int(last_sequence)), "duration_ms": max(0, int(duration_ms)), "bytes_sent": max(0, int(bytes_sent)), "status": status, "created_at": iso_utc()})

    def record_stage(self, run_id: str, *, stage_name: str, status: str, duration_ms: int, operation: str | None = None, error_code: str | None = None, attempt: int = 1, tool_call_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        self.stage_metrics.insert_one({"run_id": run_id, "stage_name": stage_name, "operation": operation, "status": status, "duration_ms": max(0, int(duration_ms)), "error_code": error_code, "attempt": max(1, int(attempt)), "tool_call_id": tool_call_id, "metadata": metadata or {}, "created_at": iso_utc()})

    def diagnostics(self, run_id: str, tenant_id: str, user_id: str) -> dict[str, Any]:
        run = self.get_run(run_id, tenant_id, user_id)
        tools = [{"toolCallId": d["tool_call_id"], "toolName": d["tool_name"], "arguments": d.get("arguments", {}), "argumentsHash": d.get("arguments_hash"), "status": d.get("status"), "durationMs": d.get("duration_ms"), "retryCount": d.get("retry_count", 0), "errorCode": d.get("error_code")} for d in self.tool_metrics.find({"run_id": run_id})]
        orchestration = self.run_metrics.find_one({"run_id": run_id})
        stages = [{"stageName": d["stage_name"], "operation": d.get("operation"), "status": d["status"], "durationMs": d["duration_ms"], "errorCode": d.get("error_code"), "attempt": d.get("attempt", 1), "toolCallId": d.get("tool_call_id"), "metadata": d.get("metadata", {})} for d in self.stage_metrics.find({"run_id": run_id}).sort("created_at", ASCENDING)]
        return {"runId": run.run_id, "messageId": run.message_id, "status": run.status, "toolCalls": tools, "orchestration": ({"durationMs": orchestration["orchestration_duration_ms"], "status": orchestration["status"], "errorCode": orchestration.get("error_code")} if orchestration else None), "sseStreams": [{"afterSequence": d["after_sequence"], "lastSequence": d["last_sequence"], "durationMs": d["duration_ms"], "bytesSent": d["bytes_sent"], "status": d["status"]} for d in self.sse_metrics.find({"run_id": run_id}).sort("created_at", ASCENDING)], "stages": stages, "decisionAudit": [s for s in stages if s["stageName"] in {"INPUT_NORMALIZATION", "ROUTING", "HOUSING_PLANNING"}], "modelCalls": [s for s in stages if s["operation"] is not None]}

    def migrate_legacy_checkpoints(self, checkpoint_path: Path) -> bool:
        return False

    def close(self) -> None:
        self.client.close()
