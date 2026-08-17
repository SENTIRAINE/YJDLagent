from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import re
import time
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from app.agent.errors import AgentError
from app.security import SensitiveHeaders


TOOL_NAME_RE = re.compile(r"^[a-z][A-Za-z0-9]{1,63}$")
NON_RETRYABLE_TOOL_CODES = {
    "INVALID_BUFFER_DISTANCE",
    "INVALID_HOUSING_SEARCH_ARGUMENT",
    "TOOL_CALL_CONFLICT",
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolCallContext:
    trace_id: str
    tenant_id: str
    user_id: str
    run_id: str


class SpringToolClient:
    def __init__(
        self,
        base_url: str,
        service_token: str,
        timeout_seconds: float = 125.0,
        max_recovery_seconds: float | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.service_token = service_token
        self.timeout_seconds = timeout_seconds
        self.timeout = httpx.Timeout(timeout_seconds)
        self.max_recovery_seconds = max_recovery_seconds or (timeout_seconds * 3 + 2)
        self._client = client

    def _headers(self, context: ToolCallContext, idempotency_key: str | None = None) -> dict[str, str]:
        if not self.service_token:
            raise AgentError(
                "INTERNAL_ERROR",
                "Spring Boot Tool 服务凭据未配置",
                status_code=500,
            )
        headers = SensitiveHeaders(
            {
            "Authorization": f"Bearer {self.service_token}",
            "X-Trace-Id": context.trace_id,
            "X-Tenant-Id": context.tenant_id,
            "X-User-Id": context.user_id,
            "X-Run-Id": context.run_id,
            }
        )
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            if self._client is not None:
                response = await self._client.request(method, path, **kwargs)
            else:
                async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
                    response = await client.request(method, path, **kwargs)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if isinstance(exc, httpx.ConnectTimeout):
                raise AgentError(
                    "TOOL_TIMEOUT",
                    "Spring Tool connection timed out",
                    status_code=503,
                    retryable=True,
                    details={"phase": "connect", "method": method, "path": path},
                ) from exc
            if isinstance(exc, httpx.ReadTimeout):
                raise AgentError(
                    "TOOL_TIMEOUT",
                    "Spring Tool response timed out",
                    status_code=503,
                    retryable=True,
                    details={"phase": "read", "method": method, "path": path},
                ) from exc
            if isinstance(exc, httpx.ConnectError):
                raise AgentError(
                    "TOOL_EXECUTION_FAILED",
                    "Spring Tool connection failed",
                    status_code=503,
                    retryable=True,
                    details={"phase": "connect", "method": method, "path": path},
                ) from exc
            if isinstance(exc, ValueError):
                raise AgentError(
                    "TOOL_EXECUTION_FAILED",
                    "Spring Tool returned invalid JSON",
                    status_code=502,
                    retryable=True,
                    details={"phase": "decode", "method": method, "path": path},
                ) from exc
            if isinstance(exc, httpx.HTTPError):
                raise AgentError(
                    "TOOL_EXECUTION_FAILED",
                    "Spring Tool HTTP exchange failed",
                    status_code=503,
                    retryable=True,
                    details={"phase": "http", "method": method, "path": path},
                ) from exc
            raise AgentError(
                "TOOL_TIMEOUT",
                "地图查询服务暂时不可用",
                status_code=503,
                retryable=True,
            ) from exc
        if response.is_error:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            raise AgentError(
                str(error.get("code", "TOOL_EXECUTION_FAILED")),
                str(error.get("message", "地图查询服务拒绝了请求")),
                status_code=response.status_code,
                retryable=bool(error.get("retryable", response.status_code >= 500)),
                details=error.get("details") if isinstance(error.get("details"), dict) else {},
            )
        if not isinstance(data, dict):
            raise AgentError("TOOL_EXECUTION_FAILED", "Tool 返回格式非法", status_code=500)
        return data

    async def catalog(self, context: ToolCallContext) -> dict[str, Any]:
        return await self._request(
            "GET", "/internal/agent-tools/catalog", headers=self._headers(context)
        )

    async def health(self, context: ToolCallContext) -> dict[str, Any]:
        return await self._request(
            "GET", "/internal/agent-tools/health", headers=self._headers(context)
        )

    async def invoke(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ToolCallContext,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if not TOOL_NAME_RE.fullmatch(tool_name):
            raise ValueError("tool_name must be a lowerCamelCase identifier")
        return await self._request(
            "POST",
            f"/internal/agent-tools/tools/{tool_name}/invoke",
            headers=self._headers(context, idempotency_key),
            json={"toolCallId": tool_call_id, "arguments": arguments, "dryRun": dry_run},
        )

    async def execution(self, tool_call_id: str, context: ToolCallContext) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/internal/agent-tools/executions/{tool_call_id}",
            headers=self._headers(context),
        )

    async def invoke_with_recovery(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        context: ToolCallContext,
        *,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        started_at = time.monotonic()
        last_error: AgentError | None = None
        for attempt in range(max_retries + 1):
            try:
                response = await self.invoke(
                    tool_name,
                    tool_call_id,
                    arguments,
                    context,
                )
                response["_agentMetrics"] = {"retryCount": attempt}
                return response
            except AgentError as exc:
                last_error = exc
                if not exc.retryable or exc.code in NON_RETRYABLE_TOOL_CODES:
                    raise
                try:
                    remaining = self.max_recovery_seconds - (time.monotonic() - started_at)
                    if remaining <= 0:
                        break
                    execution = await asyncio.wait_for(
                        self.execution(tool_call_id, context),
                        timeout=min(5.0, remaining),
                    )
                    data = execution.get("data", execution)
                    if data.get("status") in {"SUCCEEDED", "FAILED", "REJECTED"}:
                        execution["_agentMetrics"] = {"retryCount": attempt}
                        return execution
                except TimeoutError:
                    logger.warning(
                        "Tool execution status check timed out toolCallId=%s",
                        tool_call_id,
                    )
                except AgentError as status_error:
                    if status_error.code != "EXECUTION_NOT_FOUND" and not status_error.retryable:
                        raise
                if attempt >= max_retries:
                    break
                delay = 0.25 * (2**attempt)
                remaining = self.max_recovery_seconds - (time.monotonic() - started_at)
                if remaining < self.timeout_seconds + delay:
                    break
                await asyncio.sleep(delay)
        assert last_error is not None
        last_error.retry_count = attempt
        raise last_error

    @staticmethod
    def validate_arguments(tool: dict[str, Any], arguments: dict[str, Any]) -> None:
        try:
            Draft202012Validator(tool["inputSchema"]).validate(arguments)
        except Exception as exc:
            raise AgentError(
                "INVALID_TOOL_ARGUMENT",
                "Planner 生成的 Tool 参数不符合 Catalog Schema",
                status_code=400,
            ) from exc

    @staticmethod
    def validate_result(tool: dict[str, Any], result: dict[str, Any]) -> None:
        try:
            Draft202012Validator(tool["outputSchema"]).validate(result)
        except Exception as exc:
            raise AgentError(
                "TOOL_EXECUTION_FAILED",
                "Tool 返回结果不符合 Catalog Schema",
                status_code=500,
                retryable=True,
            ) from exc
