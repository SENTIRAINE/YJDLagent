from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import suppress
import logging
import os
from pathlib import Path
import socket
import sqlite3
import time
from typing import Any, AsyncIterator
from uuid import uuid4

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pymongo.errors import PyMongoError

from app.agent.contracts import LangGraphRunRequest
from app.agent.conversation_state import build_committed_state, load_business_state
from app.agent.errors import AgentError
from app.agent.llm import OpenAICompatibleChatClient
from app.agent.map_summary import summarize_map_result
from app.agent.mongo_checkpoint import MongoCheckpointSaver
from app.agent.quota import QuotaService
from app.agent.rag_service import RagEvidenceService
from app.agent.store import RunRecord, is_sqlite_busy_error, iso_utc
from app.agent.store_factory import create_agent_store
from app.agent.workflow import build_agent_graph
from app.agent.workflow import concise_map_result_answer
from app.agent.workflow import normalize_catalog
from app.config import Settings
from app.tools.spring_client import SpringToolClient, ToolCallContext


logger = logging.getLogger(__name__)


class RetryingAsyncSqliteSaver(AsyncSqliteSaver):
    def __init__(
        self,
        connection: aiosqlite.Connection,
        *,
        retry_attempts: int,
        retry_base_delay_ms: int,
    ) -> None:
        super().__init__(connection)
        self._retry_attempts = retry_attempts
        self._retry_base_delay_ms = retry_base_delay_ms

    async def _write_with_retry(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        for attempt in range(self._retry_attempts + 1):
            try:
                return await operation(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not is_sqlite_busy_error(exc) or attempt >= self._retry_attempts:
                    raise
                with suppress(sqlite3.Error):
                    await self.conn.rollback()
                delay = (self._retry_base_delay_ms / 1000) * (2**attempt)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    async def setup(self) -> None:
        await self._write_with_retry(super().setup)

    async def aput(self, *args: Any, **kwargs: Any) -> Any:
        return await self._write_with_retry(super().aput, *args, **kwargs)

    async def aput_writes(self, *args: Any, **kwargs: Any) -> None:
        await self._write_with_retry(super().aput_writes, *args, **kwargs)

    async def adelete_thread(self, *args: Any, **kwargs: Any) -> None:
        await self._write_with_retry(super().adelete_thread, *args, **kwargs)


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        store: Any = None,
        llm: Any = None,
        rag: Any = None,
        tools: Any = None,
    ) -> None:
        self.settings = settings
        self._owns_store = store is None
        self.store = store or create_agent_store(settings)
        self.quota = QuotaService(settings)
        self.llm = llm or OpenAICompatibleChatClient(settings)
        self.rag = rag or RagEvidenceService(settings)
        self.tools = tools or SpringToolClient(
            settings.spring_boot_base_url,
            settings.agent_tool_service_token,
            timeout_seconds=settings.agent_tool_timeout_seconds,
            max_recovery_seconds=max(
                settings.agent_tool_timeout_seconds,
                settings.agent_max_run_seconds - 10,
            ),
        )
        self._checkpoint_connection: aiosqlite.Connection | None = None
        self._checkpoint_saver: Any = None
        self._graph: Any = None
        self._catalog: dict[str, Any] | None = None
        self._tool_health: dict[str, Any] | None = None
        self._initialize_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._worker_tasks: list[asyncio.Task[None]] = []
        self._worker_wakeup = asyncio.Event()
        self._shutting_down = False
        self._shutdown_cancellations: set[str] = set()
        self._lease_lost: set[str] = set()
        self._cancel_reasons: dict[str, str] = {}
        self._quota_reservations: dict[str, str] = {}
        self.worker_id = settings.agent_worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
        )
        self.initialization_error: AgentError | None = None

    @property
    def initialized(self) -> bool:
        return self._graph is not None

    @staticmethod
    def _checkpoint_config(conversation_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": f"agent-v2:{conversation_id}"}}

    @staticmethod
    def _legacy_checkpoint_configs(run: RunRecord) -> tuple[dict[str, Any], ...]:
        return (
            {
                "configurable": {
                    "thread_id": run.conversation_id,
                    "checkpoint_ns": "agent-v2",
                }
            },
            {"configurable": {"thread_id": run.run_id}},
        )

    async def initialize(self, trace_id: str = "langgraph-startup") -> None:
        if self._graph is not None:
            return
        async with self._initialize_lock:
            if self._graph is not None:
                return
            checkpoint_path = Path(self.settings.agent_checkpoint_database_path)
            try:
                if self.settings.agent_checkpoint_backend == "mongodb":
                    self._checkpoint_saver = MongoCheckpointSaver(
                        self.settings.agent_mongodb_uri,
                        self.settings.agent_checkpoint_mongodb_database,
                    )
                    checkpointer = self._checkpoint_saver
                else:
                    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                    self._checkpoint_connection = await aiosqlite.connect(
                        checkpoint_path,
                        timeout=self.settings.agent_sqlite_busy_timeout_ms / 1000,
                    )
                    await self._checkpoint_connection.execute(
                        f"PRAGMA busy_timeout = {self.settings.agent_sqlite_busy_timeout_ms}"
                    )
                    await self._checkpoint_connection.execute("PRAGMA journal_mode = WAL")
                    await self._checkpoint_connection.execute("PRAGMA synchronous = NORMAL")
                    checkpointer = RetryingAsyncSqliteSaver(
                        self._checkpoint_connection,
                        retry_attempts=self.settings.agent_sqlite_write_retry_attempts,
                        retry_base_delay_ms=self.settings.agent_sqlite_write_retry_base_delay_ms,
                    )
                    await checkpointer.setup()
                    self._checkpoint_saver = checkpointer
                await asyncio.to_thread(
                    self.store.migrate_legacy_checkpoints,
                    checkpoint_path,
                )
                await self._refresh_catalog(trace_id)
                self._graph = build_agent_graph(
                    self.llm,
                    self.rag,
                    self.tools,
                    checkpointer=checkpointer,
                    metrics=self.store,
                    map_result_limit=self.settings.agent_map_result_limit,
                )
                if self.store.list_nonterminal_runs():
                    self._ensure_workers()
                self.initialization_error = None
            except sqlite3.OperationalError as exc:
                logger.exception(
                    "LangGraph SQLite initialization failed traceId=%s checkpointPath=%s",
                    trace_id,
                    checkpoint_path,
                )
                await self._close_checkpoint_connection()
                error = AgentError(
                    "AGENT_DATABASE_UNAVAILABLE",
                    "Agent persistence is temporarily unavailable",
                    status_code=503,
                    retryable=True,
                    details={"phase": "initialization"},
                )
                self.initialization_error = error
                raise error from exc
            except PyMongoError as exc:
                logger.exception("MongoDB initialization failed traceId=%s", trace_id)
                await self._close_checkpoint_connection()
                error = AgentError(
                    "AGENT_DATABASE_UNAVAILABLE",
                    "Agent persistence is temporarily unavailable",
                    status_code=503,
                    retryable=True,
                    details={"phase": "initialization", "backend": "mongodb"},
                )
                self.initialization_error = error
                raise error from exc
            except AgentError as exc:
                logger.exception("LangGraph initialization failed traceId=%s", trace_id)
                await self._close_checkpoint_connection()
                self.initialization_error = exc
                raise
            except Exception as exc:
                logger.exception("LangGraph initialization failed traceId=%s", trace_id)
                await self._close_checkpoint_connection()
                error = AgentError(
                    "AGENT_INITIALIZATION_FAILED",
                    "Agent initialization failed",
                    status_code=503,
                    retryable=True,
                    details={"phase": "initialization"},
                )
                self.initialization_error = error
                raise error from exc

    async def _close_checkpoint_connection(self) -> None:
        if self._checkpoint_connection is not None:
            with suppress(Exception):
                await self._checkpoint_connection.close()
            self._checkpoint_connection = None
        if self._checkpoint_saver is not None:
            with suppress(Exception):
                self._checkpoint_saver.close()
            self._checkpoint_saver = None

    async def close(self) -> None:
        self._shutting_down = True
        self._worker_wakeup.set()
        active = list(self._tasks.items())
        if active and self.settings.agent_shutdown_grace_seconds > 0:
            _, pending = await asyncio.wait(
                [task for _, task in active],
                timeout=self.settings.agent_shutdown_grace_seconds,
            )
        else:
            pending = {task for _, task in active if not task.done()}
        for run_id, task in active:
            if task in pending:
                self._shutdown_cancellations.add(run_id)
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        workers = list(self._worker_tasks)
        if workers:
            _, pending_workers = await asyncio.wait(
                workers,
                timeout=max(1.0, self.settings.agent_worker_poll_seconds + 0.5),
            )
            for task in pending_workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        self._worker_tasks.clear()
        await self._close_checkpoint_connection()
        self._graph = None
        if self._owns_store:
            close = getattr(self.store, "close", None)
            if callable(close):
                close()

    async def start_run(
        self, request: LangGraphRunRequest, trace_id: str
    ) -> tuple[RunRecord, bool]:
        await self.initialize(trace_id)
        self._ensure_workers()
        try:
            run, created = self.store.create_or_attach(
                request,
                trace_id,
                max_nonterminal_runs=self.settings.agent_max_queued_runs,
            )
        except sqlite3.OperationalError as exc:
            logger.exception("Run creation persistence failed traceId=%s", trace_id)
            raise AgentError(
                "AGENT_DATABASE_UNAVAILABLE",
                "Agent persistence is temporarily unavailable",
                status_code=503,
                retryable=True,
                details={"phase": "run_creation"},
            ) from exc
        except PyMongoError as exc:
            logger.exception("MongoDB Run creation failed traceId=%s", trace_id)
            raise AgentError(
                "AGENT_DATABASE_UNAVAILABLE",
                "Agent persistence is temporarily unavailable",
                status_code=503,
                retryable=True,
                details={"phase": "run_creation", "backend": "mongodb"},
            ) from exc
        if created:
            try:
                reservation = self.quota.reserve(
                    tenant_id=run.tenant_id,
                    user_id=run.user_id,
                    run_id=run.run_id,
                )
                self._quota_reservations[run.run_id] = reservation.reservation_id
            except AgentError:
                with suppress(Exception):
                    self.store.append_event(
                        run.run_id,
                        "run.failed",
                        {"status": "FAILED", "error": {"code": "QUOTA_EXCEEDED", "message": "租户配额不足", "retryable": False, "details": {}}},
                    )
                raise
            self._worker_wakeup.set()
        return run, created

    async def _load_catalog(self, trace_id: str) -> dict[str, Any]:
        context = ToolCallContext(
            trace_id=trace_id,
            tenant_id="system",
            user_id="system",
            run_id="00000000-0000-0000-0000-000000000000",
        )
        catalog = normalize_catalog(deepcopy(await self.tools.catalog(context)))
        declared_timeout_ms = max(int(tool.get("timeoutMs", 0)) for tool in catalog["tools"])
        if self.settings.agent_tool_timeout_seconds * 1000 <= declared_timeout_ms:
            raise AgentError(
                "INTERNAL_ERROR",
                "Agent Tool 客户端超时必须大于 Catalog 声明值",
                status_code=500,
            )
        return catalog

    async def _refresh_catalog(self, trace_id: str) -> None:
        try:
            catalog = await self._load_catalog(trace_id)
        except AgentError as exc:
            self.initialization_error = exc
            raise
        self._catalog = catalog
        self.initialization_error = None
        logger.info(
            "Agent Tool Catalog loaded traceId=%s version=%s toolCount=%s",
            trace_id,
            catalog["version"],
            len(catalog["tools"]),
        )

    async def refresh_catalog_health(self, trace_id: str) -> None:
        # Readiness is based only on this exchange. A previous READY response
        # must never survive a failed or stale health refresh.
        self._tool_health = None
        catalog = await self._load_catalog(trace_id)
        context = ToolCallContext(
            trace_id=trace_id,
            tenant_id="system-readiness",
            user_id="system-readiness",
            run_id="system-readiness",
        )
        response = await self.tools.health(context)
        health = response.get("data", response)
        if not isinstance(health, dict):
            raise AgentError(
                "TOOL_EXECUTION_FAILED",
                "Spring Tool health response is invalid",
                status_code=503,
                retryable=True,
                details={"phase": "health-decode"},
            )
        self._tool_health = deepcopy(health)
        housing_snapshot = health.get("housingSnapshot")
        if not isinstance(housing_snapshot, dict):
            raise AgentError(
                "TOOL_EXECUTION_FAILED",
                "Spring Tool health is missing housingSnapshot",
                status_code=503,
                retryable=True,
                details={"phase": "housing-snapshot", "toolHealth": health},
            )
        if health.get("status") != "READY" or housing_snapshot.get("status") != "READY":
            raise AgentError(
                "TOOL_EXECUTION_FAILED",
                "Spring Tool housing snapshot is not ready",
                status_code=503,
                retryable=True,
                details={"phase": "readiness", "toolHealth": health},
            )
        health_catalog_version = health.get("catalogVersion")
        snapshot_catalog_version = housing_snapshot.get("catalogVersion")
        reported_versions = {
            str(version)
            for version in (health_catalog_version, snapshot_catalog_version)
            if version is not None
        }
        if not reported_versions or reported_versions != {catalog["version"]}:
            raise AgentError(
                "TOOL_CATALOG_VERSION_MISMATCH",
                "Spring Tool health and Catalog versions do not match",
                status_code=503,
                retryable=True,
                details={
                    "expectedVersion": catalog["version"],
                    "healthVersions": sorted(reported_versions),
                    "toolHealth": health,
                },
            )
        self._catalog = catalog
        self.initialization_error = None

    @property
    def tool_health(self) -> dict[str, Any] | None:
        return deepcopy(self._tool_health)

    def _ensure_workers(self) -> None:
        if not self.settings.agent_worker_enabled or self._shutting_down or self._worker_tasks:
            return
        for index in range(self.settings.agent_worker_concurrency):
            self._worker_tasks.append(
                asyncio.create_task(
                    self._worker_loop(index),
                    name=f"agent-worker-{self.worker_id}-{index}",
                )
            )

    def start_workers(self) -> None:
        self._ensure_workers()

    async def _worker_loop(self, index: int) -> None:
        while not self._shutting_down:
            try:
                run = await asyncio.to_thread(
                    self.store.claim_next_run,
                    self.worker_id,
                    lease_seconds=self.settings.agent_worker_lease_seconds,
                )
                if self._shutting_down:
                    if run is not None:
                        self.store.release_lease(
                            run.run_id,
                            self.worker_id,
                            lease_generation=run.lease_generation,
                            requeue=True,
                        )
                    return
                if run is None:
                    self._worker_wakeup.clear()
                    try:
                        await asyncio.wait_for(
                            self._worker_wakeup.wait(),
                            timeout=self.settings.agent_worker_poll_seconds,
                        )
                    except TimeoutError:
                        pass
                    continue
                execution = asyncio.create_task(
                    self._execute(run, resume=run.last_sequence > 0),
                    name=f"agent-run-{run.run_id}",
                )
                self._tasks[run.run_id] = execution
                renewal = asyncio.create_task(
                    self._renew_lease(run.run_id, execution, run.lease_generation),
                    name=f"agent-lease-{run.run_id}",
                )
                try:
                    await execution
                finally:
                    renewal.cancel()
                    await asyncio.gather(renewal, return_exceptions=True)
                    self._tasks.pop(run.run_id, None)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Agent worker failed workerId=%s slot=%s", self.worker_id, index)
                await asyncio.sleep(self.settings.agent_worker_poll_seconds)

    async def _renew_lease(
        self, run_id: str, execution: asyncio.Task[None], lease_generation: int
    ) -> None:
        interval = max(0.1, self.settings.agent_worker_lease_seconds / 3)
        while not execution.done():
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(
                self.store.renew_lease,
                run_id,
                self.worker_id,
                lease_seconds=self.settings.agent_worker_lease_seconds,
                lease_generation=lease_generation,
            )
            if not renewed:
                self._lease_lost.add(run_id)
                execution.cancel()
                return

    async def _execute(self, run: RunRecord, *, resume: bool = False) -> None:
        execution_started_at = time.perf_counter()
        execution_status = "FAILED"
        execution_error_code: str | None = None
        aggregate: dict[str, Any] = {
            "request": run.request,
            "run_id": run.run_id,
            "trace_id": run.trace_id,
            "warnings": [],
            "citations": [],
            "catalog": self._catalog,
        }
        try:
            self.quota.mark_active(run.run_id, tenant_id=run.tenant_id)
            reset_usage = getattr(self.llm, "reset_usage", None)
            if callable(reset_usage):
                reset_usage()
            await self._refresh_catalog(run.trace_id)
            aggregate["catalog"] = self._catalog
            memory = await asyncio.to_thread(
                self.store.list_conversation_memory,
                run.tenant_id,
                run.user_id,
                run.conversation_id,
                limit=12,
            )
            aggregate["conversation_memory"] = [
                {
                    "query": item.user_query,
                    "answer": item.assistant_answer,
                    "route": item.route,
                    "mapSummary": item.map_summary,
                }
                for item in memory
            ]
            get_state = getattr(self.store, "get_conversation_state", None)
            if callable(get_state):
                aggregate["conversation_state"] = await asyncio.to_thread(
                    get_state, run.tenant_id, run.user_id, run.conversation_id
                )
            config = self._checkpoint_config(run.conversation_id)
            current = self.store.get_run_unscoped(run.run_id)
            if current.last_sequence == 0:
                self.store.append_event(run.run_id, "run.started", {"status": "RUNNING"})
            graph_input: dict[str, Any] | None = aggregate
            if resume:
                snapshot = await self._graph.aget_state(config)
                if snapshot.values:
                    aggregate.update(snapshot.values)
                    graph_input = None
                elif self._checkpoint_saver is not None:
                    # Compatibility order: the first shared-conversation format
                    # incorrectly used checkpoint_ns, while the oldest format
                    # used run_id as the thread. Continue either in place so an
                    # in-flight deployment can finish without replaying Tools.
                    for legacy_config in self._legacy_checkpoint_configs(run):
                        checkpoint = await self._checkpoint_saver.aget_tuple(legacy_config)
                        if checkpoint is None:
                            continue
                        aggregate.update(
                            checkpoint.checkpoint.get("channel_values", {})
                        )
                        config = checkpoint.config
                        graph_input = None
                        break
            async with asyncio.timeout(self.settings.agent_max_run_seconds):
                async for mode, chunk in self._graph.astream(
                    graph_input,
                    config=config,
                    stream_mode=["updates", "custom"],
                    durability="sync",
                ):
                    if mode == "custom":
                        tool_call_id = chunk.get("payload", {}).get("toolCallId")
                        if tool_call_id and self.store.has_tool_event(
                            run.run_id, chunk["event"], tool_call_id
                        ):
                            continue
                        self.store.append_event(run.run_id, chunk["event"], chunk["payload"])
                        continue
                    if mode != "updates":
                        continue
                    for node_name, update in chunk.items():
                        if not isinstance(update, dict):
                            continue
                        aggregate.update(update)
                        if node_name == "route_intent":
                            self.store.append_event(
                                run.run_id,
                                "route.selected",
                                {"intent": update["intent"]},
                                route=update["intent"],
                            )
                        elif node_name == "retrieve_knowledge":
                            self.store.append_event(
                                run.run_id,
                                "retrieval.completed",
                                {"documents": len(update.get("retrieval_results", []))},
                            )
                            for citation in update.get("citations", []):
                                self.store.append_event(run.run_id, "citation.added", citation)
                        elif node_name == "execute_map_tools" and update.get("map_result"):
                            self.store.append_event(run.run_id, "map.result", update["map_result"])
                        elif node_name == "compose_answer" and update.get("answer"):
                            answer = str(update["answer"])
                            for offset in range(0, len(answer), 80):
                                self.store.append_event(
                                    run.run_id,
                                    "answer.delta",
                                    {"content": answer[offset : offset + 80]},
                                )
            usage = getattr(self.llm, "usage", None)
            if callable(usage):
                aggregate["model_usage"] = usage()
                aggregate["total_tokens"] = int(aggregate["model_usage"].get("total_tokens", 0))
            self._complete_run(run, aggregate)
            execution_status = "SUCCEEDED"
        except asyncio.CancelledError:
            execution_status = "CANCELLED"
            shutdown = run.run_id in self._shutdown_cancellations
            lease_lost = run.run_id in self._lease_lost
            try:
                if shutdown:
                    self.store.release_lease(
                        run.run_id,
                        self.worker_id,
                        lease_generation=run.lease_generation,
                        requeue=True,
                    )
                elif not lease_lost:
                    current = self.store.get_run_unscoped(run.run_id)
                    if not current.terminal:
                        if current.last_sequence == 0:
                            self.store.append_event(run.run_id, "run.started", {"status": "RUNNING"})
                        self.store.append_event(
                            run.run_id,
                            "run.cancelled",
                            {
                                "status": "CANCELLED",
                                "reason": self._cancel_reasons.pop(run.run_id, "cancelled"),
                            },
                        )
            except Exception:
                logger.exception(
                    "Failed to persist run cancellation traceId=%s runId=%s",
                    run.trace_id,
                    run.run_id,
                )
            finally:
                self._shutdown_cancellations.discard(run.run_id)
                self._lease_lost.discard(run.run_id)
        except TimeoutError:
            execution_error_code = "RUN_TIMEOUT"
            self._fail_run(
                run.run_id,
                AgentError(
                    "RUN_TIMEOUT",
                    "Agent Run exceeded the maximum execution time",
                    status_code=500,
                    retryable=True,
                ),
            )
            return
        except AgentError as exc:
            execution_error_code = exc.code
            if self._complete_with_answer_fallback(run.run_id, aggregate, exc):
                execution_status = "SUCCEEDED"
                execution_error_code = None
                self._complete_run(run, aggregate)
            else:
                self._fail_run(run.run_id, exc)
        except sqlite3.OperationalError as exc:
            execution_error_code = "AGENT_DATABASE_UNAVAILABLE"
            logger.exception(
                "Run persistence failed traceId=%s runId=%s",
                run.trace_id,
                run.run_id,
            )
            self._fail_run(
                run.run_id,
                AgentError(
                    "AGENT_DATABASE_UNAVAILABLE",
                    "Agent persistence is temporarily unavailable",
                    status_code=503,
                    retryable=True,
                ),
            )
        except Exception:
            execution_error_code = "INTERNAL_ERROR"
            logger.exception(
                "Unexpected LangGraph run failure traceId=%s runId=%s",
                run.trace_id,
                run.run_id,
            )
            self._fail_run(
                run.run_id,
                AgentError("INTERNAL_ERROR", "Agent Run 执行失败", status_code=500),
            )
        finally:
            orchestration_duration_ms = int((time.perf_counter() - execution_started_at) * 1000)
            try:
                total_tokens = int(aggregate.get("total_tokens", 0))
                if execution_status == "SUCCEEDED":
                    self.quota.settle(run.run_id, total_tokens=total_tokens, tenant_id=run.tenant_id)
                else:
                    self.quota.release(run.run_id, tenant_id=run.tenant_id)
                reservation_id = self._quota_reservations.pop(run.run_id, None)
                record_settlement = getattr(self.store, "record_quota_settlement", None)
                if reservation_id and callable(record_settlement):
                    record_settlement(
                        reservation_id=reservation_id,
                        operation_id="terminal",
                        run_id=run.run_id,
                        tenant_id=run.tenant_id,
                        total_tokens=total_tokens,
                        status=execution_status,
                    )
            except Exception:
                logger.exception("Failed to settle quota runId=%s", run.run_id)
            try:
                self.store.record_orchestration(
                    run.run_id,
                    duration_ms=orchestration_duration_ms,
                    status=execution_status,
                    error_code=execution_error_code,
                )
            except Exception:
                logger.exception(
                    "Failed to persist orchestration metrics traceId=%s runId=%s",
                    run.trace_id,
                    run.run_id,
                )
            logger.info(
                "Agent orchestration completed traceId=%s runId=%s durationMs=%s status=%s errorCode=%s",
                run.trace_id,
                run.run_id,
                orchestration_duration_ms,
                execution_status,
                execution_error_code,
            )

    @staticmethod
    def _map_summary(aggregate: dict[str, Any]) -> dict[str, Any] | None:
        existing = aggregate.get("map_summary")
        if isinstance(existing, dict):
            return existing
        return summarize_map_result(aggregate.get("map_result"))

    def _complete_run(self, run: RunRecord, aggregate: dict[str, Any]) -> None:
        state = build_committed_state(
            load_business_state(aggregate.get("conversation_state", {})),
            query=str(run.request.get("query", "")),
            answer=str(aggregate.get("answer", "")),
            intent=aggregate.get("intent"),
            tool_plan=aggregate.get("tool_plan", []),
            map_result=aggregate.get("map_result") or aggregate.get("map_summary"),
            request_context=run.request.get("context", {}),
            updated_at=iso_utc(),
        )
        self.store.complete_run_with_memory(
            run.run_id,
            user_query=str(run.request.get("query", "")),
            assistant_answer=str(aggregate.get("answer", "")),
            route=aggregate.get("intent"),
            map_summary=self._map_summary(aggregate),
            citations=aggregate.get("citations", []),
            warnings=aggregate.get("warnings", []),
            memory_limit=12,
            conversation_state=state,
        )

    def _complete_with_answer_fallback(
        self, run_id: str, aggregate: dict[str, Any], error: AgentError
    ) -> bool:
        """Finish a trustworthy result when only the optional answer model failed."""
        if error.details.get("operation") != "answer":
            return False
        map_result = aggregate.get("map_result")
        citations = aggregate.get("citations", [])
        has_map_result = isinstance(map_result, dict) and bool(
            map_result.get("resultSets") or map_result.get("overlays")
        )
        if not has_map_result and not citations:
            return False
        if has_map_result:
            answer = concise_map_result_answer(map_result)
        else:
            answer = "已找到相关参考资料，您可以先查看页面中的引用内容。"
        warnings = list(dict.fromkeys(aggregate.get("warnings", []) + ["ANSWER_GENERATION_DEGRADED"]))
        aggregate["answer"] = answer
        aggregate["warnings"] = warnings
        for offset in range(0, len(answer), 80):
            self.store.append_event(
                run_id, "answer.delta", {"content": answer[offset : offset + 80]}
            )
        logger.warning(
            "Answer model degraded runId=%s modelErrorCode=%s; deterministic fallback emitted",
            run_id,
            error.code,
        )
        return True

    def _fail_run(self, run_id: str, error: AgentError) -> None:
        try:
            current = self.store.get_run_unscoped(run_id)
            if not current.terminal:
                if current.last_sequence == 0:
                    self.store.append_event(run_id, "run.started", {"status": "RUNNING"})
                for pending in self.store.pending_tool_calls(run_id):
                    with suppress(RuntimeError, sqlite3.OperationalError):
                        self.store.record_tool_completed(
                            run_id,
                            pending["toolCallId"],
                            status="FAILED",
                            duration_ms=0,
                            error_code=error.code,
                        )
                    self.store.append_event(
                        run_id,
                        "tool.completed",
                        {
                            "toolCallId": pending["toolCallId"],
                            "toolName": pending["toolName"],
                            "status": "FAILED",
                            "durationMs": 0,
                        },
                    )
                self.store.append_event(
                    run_id,
                    "run.failed",
                    {"status": "FAILED", "error": error.to_dict()},
                )
        except Exception:
            trace_id = current.trace_id if "current" in locals() else "unknown"
            logger.exception(
                "Failed to persist terminal run failure traceId=%s runId=%s",
                trace_id,
                run_id,
            )

    async def cancel_run(
        self, run_id: str, tenant_id: str, user_id: str, reason: str
    ) -> RunRecord:
        run = self.store.get_run(run_id, tenant_id, user_id)
        if run.terminal:
            return run
        task = self._tasks.get(run_id)
        if task is not None:
            self._cancel_reasons[run_id] = reason
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return self.store.get_run(run_id, tenant_id, user_id)
        if run.last_sequence == 0:
            self.store.append_event(run_id, "run.started", {"status": "RUNNING"})
        self.store.append_event(
            run_id,
            "run.cancelled",
            {"status": "CANCELLED", "reason": reason},
        )
        return self.store.get_run(run_id, tenant_id, user_id)

    async def stream_events(
        self,
        run_id: str,
        tenant_id: str,
        user_id: str,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[str]:
        self.store.get_run(run_id, tenant_id, user_id)
        stream_started_at = time.perf_counter()
        bytes_sent = 0
        last_sequence = after_sequence
        heartbeat_at = asyncio.get_running_loop().time() + self.settings.agent_sse_heartbeat_seconds
        stream_status = "DISCONNECTED"
        try:
            while True:
                for event in self.store.list_events(run_id, last_sequence):
                    last_sequence = event.sequence
                    chunk = event.to_sse()
                    bytes_sent += len(chunk.encode("utf-8"))
                    yield chunk
                run = self.store.get_run(run_id, tenant_id, user_id)
                if run.terminal and last_sequence >= run.last_sequence:
                    stream_status = "COMPLETED"
                    return
                now = asyncio.get_running_loop().time()
                if now >= heartbeat_at:
                    heartbeat = ": heartbeat\n\n"
                    bytes_sent += len(heartbeat.encode("utf-8"))
                    yield heartbeat
                    heartbeat_at = now + self.settings.agent_sse_heartbeat_seconds
                await asyncio.sleep(0.05)
        except Exception:
            stream_status = "FAILED"
            raise
        finally:
            stream_duration_ms = int((time.perf_counter() - stream_started_at) * 1000)
            try:
                self.store.record_sse_stream(
                    run_id,
                    after_sequence=after_sequence,
                    last_sequence=last_sequence,
                    duration_ms=stream_duration_ms,
                    bytes_sent=bytes_sent,
                    status=stream_status,
                )
            except Exception:
                logger.exception("Failed to persist SSE metrics runId=%s", run_id)
            logger.info(
                "SSE stream completed runId=%s afterSequence=%s lastSequence=%s durationMs=%s bytesSent=%s status=%s",
                run_id,
                after_sequence,
                last_sequence,
                stream_duration_ms,
                bytes_sent,
                stream_status,
            )

    def status_data(self, run: RunRecord) -> dict[str, Any]:
        completed = None
        if run.status == "SUCCEEDED":
            completed = {
                "status": "SUCCEEDED",
                "answer": run.answer or "",
                "citations": run.citations,
                "warnings": run.warnings,
            }
        return {
            "runId": run.run_id,
            "status": run.status,
            "lastSequence": run.last_sequence,
            "createdAt": run.created_at,
            "updatedAt": run.updated_at,
            "completed": completed,
            "error": run.error,
        }
