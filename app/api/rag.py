from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.agent.contracts import RagSearchRequest
from app.agent.errors import AgentError
from app.api.auth import InternalIdentity, require_internal_identity


router = APIRouter(prefix="/api/v1/rag", tags=["rag"])


@router.post("/search", operation_id="searchRag")
def search(
    body: RagSearchRequest,
    request: Request,
    identity: InternalIdentity = Depends(require_internal_identity),
) -> dict[str, object]:
    try:
        service = request.app.state.agent_runtime.rag
        results = service.search(
            body.query,
            top_k=body.top_k,
            document_ids=body.filters.document_ids,
            content_types=body.filters.content_types,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise AgentError(
            "INTERNAL_ERROR", "RAG 检索服务暂时不可用", status_code=500
        ) from exc
    return {
        "success": True,
        "data": [service.to_search_result(result) for result in results],
        "traceId": identity.trace_id,
    }
