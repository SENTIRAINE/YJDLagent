from __future__ import annotations

import asyncio
from dataclasses import replace

import httpx

from app.agent.llm import OpenAICompatibleChatClient
from app.agent.errors import AgentError
from app.config import Settings


def test_structured_completion_retries_once_after_local_schema_failure() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        content = "{}" if calls == 1 else '{"value":"ok"}'
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}], "model": "test"},
        )

    async def scenario() -> None:
        settings = replace(
            Settings.from_env(),
            openai_chat_completions_url="http://llm.test/v1/chat/completions",
            openai_api_key="test-key",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = OpenAICompatibleChatClient(settings, client=http_client)
            result = await client.complete_json(
                system="Return JSON.",
                user="Return the value.",
                schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
                schema_name="retry_test",
            )
            assert result == {"value": "ok"}

    asyncio.run(scenario())
    assert calls == 2


def test_provider_schema_infers_types_for_const_values() -> None:
    schema = {
        "type": "object",
        "properties": {
            "level": {"const": "PREFER_LOW"},
            "relation": {"const": "WITHIN_ROAD_BUFFER"},
            "enabled": {"const": True},
            "version": {"const": 1},
        },
        "additionalProperties": False,
    }

    adapted = OpenAICompatibleChatClient._provider_compatible_schema(
        schema, require_all=True
    )

    assert adapted["properties"]["level"]["type"] == "string"
    assert adapted["properties"]["relation"]["type"] == "string"
    assert adapted["properties"]["enabled"]["type"] == "boolean"
    assert adapted["properties"]["version"]["type"] == "integer"
    assert adapted["required"] == ["level", "relation", "enabled", "version"]


def test_model_http_failure_keeps_safe_operation_and_provider_request_id() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"x-request-id": "provider-request-1"})

    async def scenario() -> None:
        settings = replace(
            Settings.from_env(),
            openai_chat_completions_url="http://llm.test/v1/chat/completions",
            openai_api_key="test-key",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = OpenAICompatibleChatClient(settings, client=http_client)
            try:
                await client.complete_text(system="system", user="user", operation="answer")
            except AgentError as error:
                assert error.code == "MODEL_HTTP_5XX"
                assert error.retryable
                assert error.details == {
                    "providerStatus": 503,
                    "providerRequestId": "provider-request-1",
                    "operation": "answer",
                }
            else:
                raise AssertionError("expected a model failure")

    asyncio.run(scenario())


def test_usage_propagates_from_child_tasks_and_isolates_concurrent_runs() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "model": "test",
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        )

    async def scenario() -> None:
        settings = replace(
            Settings.from_env(),
            openai_chat_completions_url="http://llm.test/v1/chat/completions",
            openai_api_key="test-key",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = OpenAICompatibleChatClient(settings, client=http_client)

            async def run(call_count: int) -> dict[str, int]:
                client.reset_usage()
                await asyncio.gather(
                    *(
                        asyncio.create_task(
                            client.complete_text(system="system", user=f"run-{call_count}")
                        )
                        for _ in range(call_count)
                    )
                )
                return client.usage()

            one_call, two_calls = await asyncio.gather(run(1), run(2))
            assert one_call == {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
            assert two_calls == {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}

    asyncio.run(scenario())
