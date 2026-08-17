from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse

from app.agent.contracts import CancelRequest, LangGraphRunRequest
from app.agent.errors import AgentError
from app.api.auth import InternalIdentity, require_internal_identity


router = APIRouter(prefix="/api/v1/runs", tags=["LangGraph Runs"])


def success(data: object, trace_id: str) -> dict[str, object]:
    return {"success": True, "data": data, "traceId": trace_id}


def runtime_for(request: Request):
    runtime = getattr(request.app.state, "agent_runtime", None)
    if runtime is not None:
        return runtime
    startup_error = getattr(request.app.state, "agent_startup_error", None)
    if isinstance(startup_error, AgentError):
        raise startup_error
    raise AgentError(
        "AGENT_INITIALIZATION_FAILED",
        "Agent initialization failed",
        status_code=503,
        retryable=True,
    )


def validate_body_identity(request: LangGraphRunRequest, identity: InternalIdentity) -> None:
    if request.user.tenant_id != identity.tenant_id or request.user.user_id != identity.user_id:
        raise AgentError(
            "IDENTITY_CONTEXT_MISMATCH",
            "Header 身份与请求体用户上下文不一致",
            status_code=403,
        )


@router.post("", status_code=202, operation_id="createLangGraphRun")
async def create_run(
    body: LangGraphRunRequest,
    request: Request,
    identity: InternalIdentity = Depends(require_internal_identity),
) -> dict[str, object]:
    validate_body_identity(body, identity)
    runtime = runtime_for(request)
    run, _ = await runtime.start_run(body, identity.trace_id)
    return success(runtime.status_data(run), identity.trace_id)


@router.post("/stream", operation_id="streamLangGraphRun")
async def stream_run(
    body: LangGraphRunRequest,
    request: Request,
    identity: InternalIdentity = Depends(require_internal_identity),
) -> StreamingResponse:
    validate_body_identity(body, identity)
    runtime = runtime_for(request)
    run, _ = await runtime.start_run(body, identity.trace_id)
    return StreamingResponse(
        runtime.stream_events(
            run.run_id,
            identity.tenant_id,
            identity.user_id,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Run-Id": run.run_id},
    )


@router.get("/{runId}", operation_id="getLangGraphRun")
async def get_run(
    request: Request,
    run_id: UUID = Path(alias="runId"),
    identity: InternalIdentity = Depends(require_internal_identity),
) -> dict[str, object]:
    runtime = runtime_for(request)
    run = runtime.store.get_run(
        str(run_id), identity.tenant_id, identity.user_id
    )
    return success(runtime.status_data(run), identity.trace_id)


@router.get("/{runId}/events", operation_id="replayLangGraphRunEvents")
async def replay_events(
    request: Request,
    run_id: UUID = Path(alias="runId"),
    after_sequence: int = Query(default=0, alias="afterSequence", ge=0),
    identity: InternalIdentity = Depends(require_internal_identity),
) -> StreamingResponse:
    runtime = runtime_for(request)
    runtime.store.get_run(
        str(run_id), identity.tenant_id, identity.user_id
    )
    runtime.store.list_events(str(run_id), after_sequence)
    return StreamingResponse(
        runtime.stream_events(
            str(run_id),
            identity.tenant_id,
            identity.user_id,
            after_sequence=after_sequence,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/{runId}/diagnostics", operation_id="getLangGraphRunDiagnostics")
async def get_run_diagnostics(
    request: Request,
    run_id: UUID = Path(alias="runId"),
    identity: InternalIdentity = Depends(require_internal_identity),
) -> dict[str, object]:
    runtime = runtime_for(request)
    data = runtime.store.diagnostics(
        str(run_id), identity.tenant_id, identity.user_id
    )
    return success(data, identity.trace_id)


@router.post("/{runId}/cancel", operation_id="cancelLangGraphRun")
async def cancel_run(
    body: CancelRequest,
    request: Request,
    run_id: UUID = Path(alias="runId"),
    identity: InternalIdentity = Depends(require_internal_identity),
) -> dict[str, object]:
    runtime = runtime_for(request)
    run = await runtime.cancel_run(
        str(run_id), identity.tenant_id, identity.user_id, body.reason
    )
    return success(runtime.status_data(run), identity.trace_id)
