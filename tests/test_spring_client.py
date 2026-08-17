from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from app.agent.errors import AgentError
from app.security import SensitiveHeaders
from app.tools.spring_client import SpringToolClient, ToolCallContext


class SpringToolClientTests(unittest.TestCase):
    def test_sensitive_headers_redact_diagnostics_without_changing_values(self) -> None:
        secret = "Bearer temporary-secret"
        headers = SensitiveHeaders(
            {"Authorization": secret, "X-Trace-Id": "trace"}
        )

        self.assertEqual(headers["Authorization"], secret)
        self.assertNotIn("temporary-secret", repr(headers))
        self.assertNotIn("temporary-secret", str(headers))
        self.assertIn("Bearer [REDACTED]", repr(headers))

    def test_retryable_invoke_checks_existing_execution_before_retry(self) -> None:
        calls: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.method == "POST":
                return httpx.Response(
                    503,
                    json={
                        "success": False,
                        "error": {
                            "code": "TOOL_TIMEOUT",
                            "message": "timeout",
                            "retryable": True,
                            "details": {},
                        },
                        "traceId": "trace",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "toolCallId": "00000000-0000-0000-0000-000000000001",
                        "status": "SUCCEEDED",
                        "result": {"layerId": 0, "features": []},
                    },
                    "traceId": "trace",
                },
            )

        async def scenario() -> None:
            async with httpx.AsyncClient(
                base_url="http://spring.test", transport=httpx.MockTransport(handler)
            ) as http_client:
                client = SpringToolClient(
                    "http://spring.test", "service-token", client=http_client
                )
                context = ToolCallContext("trace", "tenant", "user", "run")
                response = await client.invoke_with_recovery(
                    "queryMapPoints",
                    "00000000-0000-0000-0000-000000000001",
                    {
                        "layerId": 0,
                        "filters": [],
                        "returnGeometry": True,
                        "resultRecordCount": 10,
                    },
                    context,
                )
                self.assertEqual(response["data"]["status"], "SUCCEEDED")
                self.assertEqual(response["_agentMetrics"]["retryCount"], 0)

        asyncio.run(scenario())
        self.assertEqual(
            calls,
            [
                ("POST", "/internal/agent-tools/tools/queryMapPoints/invoke"),
                ("GET", "/internal/agent-tools/executions/00000000-0000-0000-0000-000000000001"),
            ],
        )

    def test_a09_retry_reuses_exact_tool_call_id_and_arguments(self) -> None:
        posts: list[dict[str, object]] = []
        execution_checks = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal execution_checks
            if request.method == "POST":
                posts.append(json.loads(request.content))
                if len(posts) == 1:
                    return httpx.Response(
                        503,
                        json={
                            "success": False,
                            "error": {
                                "code": "TOOL_TIMEOUT",
                                "message": "timeout",
                                "retryable": True,
                                "details": {},
                            },
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "toolCallId": posts[-1]["toolCallId"],
                            "status": "SUCCEEDED",
                            "result": {},
                        },
                    },
                )
            execution_checks += 1
            return httpx.Response(
                404,
                json={
                    "success": False,
                    "error": {
                        "code": "EXECUTION_NOT_FOUND",
                        "message": "not found",
                        "retryable": False,
                        "details": {},
                    },
                },
            )

        async def scenario() -> None:
            async with httpx.AsyncClient(
                base_url="http://spring.test", transport=httpx.MockTransport(handler)
            ) as http_client:
                client = SpringToolClient(
                    "http://spring.test", "service-token", client=http_client
                )
                response = await client.invoke_with_recovery(
                    "searchHousingCandidates",
                    "00000000-0000-0000-0000-000000000009",
                    {"mode": "RANK", "districts": []},
                    ToolCallContext("trace", "tenant", "user", "run"),
                )
                self.assertEqual(response["_agentMetrics"]["retryCount"], 1)

        asyncio.run(scenario())
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])
        self.assertEqual(execution_checks, 1)

    def test_a10_tool_call_conflict_is_never_retried(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.method)
            return httpx.Response(
                409,
                json={
                    "success": False,
                    "error": {
                        "code": "TOOL_CALL_CONFLICT",
                        "message": "same id with different arguments",
                        "retryable": True,
                        "details": {},
                    },
                },
            )

        async def scenario() -> None:
            async with httpx.AsyncClient(
                base_url="http://spring.test", transport=httpx.MockTransport(handler)
            ) as http_client:
                client = SpringToolClient(
                    "http://spring.test", "service-token", client=http_client
                )
                with self.assertRaises(AgentError) as raised:
                    await client.invoke_with_recovery(
                        "searchHousingCandidates",
                        "00000000-0000-0000-0000-000000000010",
                        {"mode": "RANK", "districts": []},
                        ToolCallContext("trace", "tenant", "user", "run"),
                    )
                self.assertEqual(raised.exception.code, "TOOL_CALL_CONFLICT")

        asyncio.run(scenario())
        self.assertEqual(calls, ["POST"])

    def test_non_retryable_housing_errors_ignore_retryable_flag(self) -> None:
        for code in ("INVALID_BUFFER_DISTANCE", "INVALID_HOUSING_SEARCH_ARGUMENT"):
            with self.subTest(code=code):
                calls: list[str] = []

                def handler(request: httpx.Request) -> httpx.Response:
                    calls.append(request.method)
                    return httpx.Response(
                        400,
                        json={
                            "success": False,
                            "error": {
                                "code": code,
                                "message": "invalid",
                                "retryable": True,
                                "details": {},
                            },
                        },
                    )

                async def scenario() -> None:
                    async with httpx.AsyncClient(
                        base_url="http://spring.test", transport=httpx.MockTransport(handler)
                    ) as http_client:
                        client = SpringToolClient(
                            "http://spring.test", "service-token", client=http_client
                        )
                        with self.assertRaises(AgentError) as raised:
                            await client.invoke_with_recovery(
                                "searchHousingCandidates",
                                "00000000-0000-0000-0000-000000000011",
                                {"mode": "BUFFER_FILTER", "districts": []},
                                ToolCallContext("trace", "tenant", "user", "run"),
                            )
                        self.assertEqual(raised.exception.code, code)

                asyncio.run(scenario())
                self.assertEqual(calls, ["POST"])


if __name__ == "__main__":
    unittest.main()
