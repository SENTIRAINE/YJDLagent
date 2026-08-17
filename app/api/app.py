from __future__ import annotations

from contextlib import asynccontextmanager
import logging
import sqlite3
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.agent.errors import AgentError
from app.agent.runtime import AgentRuntime
from app.api.rag import router as rag_router
from app.api.runs import router as runs_router
from app.config import Settings
from app.rag.store import SQLiteVectorStore


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI):
    trace_id = "langgraph-startup"
    runtime = getattr(application.state, "agent_runtime", None)
    if runtime is None:
        # Configuration errors are deployment errors. Let them abort ASGI
        # startup instead of exposing a process that can only return 503.
        runtime = AgentRuntime(Settings.from_env())
        application.state.agent_runtime = runtime
    try:
        settings = runtime.settings
        if settings.langgraph_service_token and settings.agent_tool_service_token and settings.openai_api_key:
            await runtime.initialize(trace_id)
    except AgentError as exc:
        application.state.agent_startup_error = exc
    except sqlite3.OperationalError as exc:
        logger.exception("LangGraph SQLite startup failed traceId=%s", trace_id)
        application.state.agent_startup_error = AgentError(
            "AGENT_DATABASE_UNAVAILABLE",
            "Agent persistence is temporarily unavailable",
            status_code=503,
            retryable=True,
            details={"phase": "startup"},
        )
    except Exception:
        logger.exception("LangGraph startup failed traceId=%s", trace_id)
        application.state.agent_startup_error = AgentError(
            "AGENT_INITIALIZATION_FAILED",
            "Agent initialization failed",
            status_code=503,
            retryable=True,
            details={"phase": "startup"},
        )
    try:
        yield
    finally:
        if runtime is not None:
            await runtime.close()


def create_app(runtime: AgentRuntime | None = None) -> FastAPI:
    application = FastAPI(
        title="YJDL LangGraph RAG Service",
        version="0.2.0",
        description="Contract-driven RAG and LangGraph orchestration service",
        lifespan=lifespan,
    )
    if runtime is not None:
        application.state.agent_runtime = runtime
    application.include_router(rag_router)
    application.include_router(runs_router)
    application.add_api_route("/healthz", health, methods=["GET"], tags=["system"])
    application.add_api_route(
        "/readyz", readiness, methods=["GET"], tags=["system"], response_model=None
    )

    @application.exception_handler(AgentError)
    async def agent_error_handler(request: Request, exc: AgentError) -> JSONResponse:
        trace_id = request.headers.get("X-Trace-Id") or str(uuid4())
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "error": exc.to_dict(), "traceId": trace_id},
            headers={"X-Trace-Id": trace_id},
        )

    @application.exception_handler(sqlite3.OperationalError)
    async def sqlite_error_handler(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        trace_id = request.headers.get("X-Trace-Id") or str(uuid4())
        logger.exception("Unhandled SQLite failure traceId=%s path=%s", trace_id, request.url.path)
        error = AgentError(
            "AGENT_DATABASE_UNAVAILABLE",
            "Agent persistence is temporarily unavailable",
            status_code=503,
            retryable=True,
            details={"phase": "request"},
        )
        return JSONResponse(
            status_code=error.status_code,
            content={"success": False, "error": error.to_dict(), "traceId": trace_id},
            headers={"X-Trace-Id": trace_id},
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        trace_id = request.headers.get("X-Trace-Id") or str(uuid4())
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": {
                    "code": "INVALID_AGENT_REQUEST",
                    "message": "请求参数不符合 Agent 契约",
                    "retryable": False,
                    "details": {
                        "errors": exc.errors(
                            include_url=False, include_context=False, include_input=False
                        )
                    },
                },
                "traceId": trace_id,
            },
            headers={"X-Trace-Id": trace_id},
        )
    return application


def health() -> dict[str, str]:
    return {"status": "UP"}


async def readiness(request: Request) -> dict[str, object] | JSONResponse:
    runtime = getattr(request.app.state, "agent_runtime", None)
    startup_error = getattr(request.app.state, "agent_startup_error", None)
    if runtime is None:
        code = startup_error.code if isinstance(startup_error, AgentError) else "AGENT_INITIALIZATION_FAILED"
        return JSONResponse(status_code=503, content={"status": "NOT_READY", "reason": code})
    try:
        trace_id = request.headers.get("X-Trace-Id") or "langgraph-readiness"
        if not runtime.initialized:
            await runtime.initialize(trace_id)
        await runtime.refresh_catalog_health(trace_id)
    except AgentError as exc:
        content: dict[str, object] = {"status": "NOT_READY", "reason": exc.code}
        current_health = runtime.tool_health
        if current_health is not None:
            content["toolHealth"] = current_health
        return JSONResponse(status_code=503, content=content)
    settings = runtime.settings
    try:
        metadata = SQLiteVectorStore(settings.database_path).metadata()
    except (FileNotFoundError, KeyError, ValueError) as exc:
        return JSONResponse(status_code=503, content={"status": "NOT_READY", "reason": str(exc)})
    missing = []
    if not settings.openai_api_key:
        missing.append("OPENAI_API_KEY")
    if not settings.langgraph_service_token:
        missing.append("LANGGRAPH_SERVICE_TOKEN")
    if not settings.agent_tool_service_token:
        missing.append("AGENT_TOOL_SERVICE_TOKEN")
    if missing:
        return JSONResponse(
            status_code=503,
            content={"status": "NOT_READY", "reason": "missing required configuration", "missing": missing},
        )
    agent_database = (
        f"mongodb:{settings.agent_mongodb_database}"
        if settings.agent_storage_backend == "mongodb"
        else str(settings.agent_database_path)
    )
    checkpoint_database = (
        f"mongodb:{settings.agent_checkpoint_mongodb_database}"
        if settings.agent_checkpoint_backend == "mongodb"
        else str(settings.agent_checkpoint_database_path)
    )
    return {
        "status": "READY",
        "model": settings.openai_model,
        "runtimePolicy": {
            "agentMaxRunSeconds": settings.agent_max_run_seconds,
            "agentToolTimeoutSeconds": settings.agent_tool_timeout_seconds,
        },
        "index": metadata,
        "toolHealth": runtime.tool_health,
        "agentDatabase": agent_database,
        "checkpointDatabase": checkpoint_database,
    }


app = create_app()
