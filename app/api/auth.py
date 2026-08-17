from __future__ import annotations

from dataclasses import dataclass
import hmac

from fastapi import Header, Request

from app.agent.errors import AgentError


@dataclass(frozen=True)
class InternalIdentity:
    trace_id: str
    tenant_id: str
    user_id: str


async def require_internal_identity(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    trace_id: str | None = Header(default=None, alias="X-Trace-Id"),
    tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> InternalIdentity:
    runtime = request.app.state.agent_runtime
    expected = runtime.settings.langgraph_service_token
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise AgentError(
            "INVALID_SERVICE_IDENTITY",
            "内部服务身份无效",
            status_code=401,
        )
    if not trace_id or not tenant_id or not user_id:
        raise AgentError(
            "MISSING_INTERNAL_HEADER",
            "缺少必需的内部身份请求头",
            status_code=401,
        )
    if any(len(value) > 128 for value in (trace_id, tenant_id, user_id)):
        raise AgentError(
            "MISSING_INTERNAL_HEADER",
            "内部身份请求头长度非法",
            status_code=401,
        )
    return InternalIdentity(trace_id, tenant_id, user_id)
