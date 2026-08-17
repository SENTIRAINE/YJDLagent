from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

import httpx
from jsonschema import Draft202012Validator

from app.agent.errors import AgentError
from app.config import Settings
from app.security import SensitiveHeaders


class OpenAICompatibleChatClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.url = settings.openai_chat_completions_url
        self.model = settings.openai_model
        self.api_key = settings.openai_api_key
        self.timeout = httpx.Timeout(settings.agent_max_run_seconds)
        self._client = client
        self._usage: ContextVar[dict[str, int]] = ContextVar(
            f"model_usage_{id(self)}",
            default={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )

    def reset_usage(self) -> None:
        self._usage.set({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    def usage(self) -> dict[str, int]:
        return dict(self._usage.get())

    async def _post(self, body: dict[str, Any], *, operation: str) -> dict[str, Any]:
        if not self.api_key:
            raise AgentError(
                "MODEL_CONFIGURATION_ERROR",
                "模型服务凭据未配置",
                status_code=500,
                details={"operation": operation},
            )
        headers = SensitiveHeaders({"Authorization": f"Bearer {self.api_key}"})
        error: Exception | None = None
        try:
            if self._client is not None:
                response = await self._client.post(self.url, headers=headers, json=body)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(self.url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
            usage = data.get("usage", {})
            if isinstance(usage, dict):
                current = self._usage.get()
                # LangGraph creates child asyncio tasks with a copied Context.
                # Mutating the inherited per-run object keeps usage visible to
                # the parent task while reset_usage still isolates concurrent runs.
                current["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
                current["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
                current["total_tokens"] += int(usage.get("total_tokens", 0) or 0)
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise KeyError("empty message content")
            return {
                "content": content.strip(),
                "model": data.get("model", self.model),
                "providerRequestId": response.headers.get("x-request-id") or response.headers.get("request-id"),
            }
        except httpx.ConnectTimeout as exc:
            error = exc
            code, retryable, details = "MODEL_CONNECT_TIMEOUT", True, {}
        except httpx.ReadTimeout as exc:
            error = exc
            code, retryable, details = "MODEL_READ_TIMEOUT", True, {}
        except httpx.HTTPStatusError as exc:
            error = exc
            status = exc.response.status_code
            code = "MODEL_HTTP_5XX" if status >= 500 else "MODEL_HTTP_ERROR"
            retryable = status >= 500 or status == 429
            details = {
                "providerStatus": status,
                "providerRequestId": exc.response.headers.get("x-request-id") or exc.response.headers.get("request-id"),
            }
        except httpx.RequestError as exc:
            error = exc
            code, retryable, details = "MODEL_CONNECTION_FAILED", True, {}
        except (KeyError, TypeError, ValueError) as exc:
            error = exc
            code, retryable, details = "MODEL_INVALID_RESPONSE", False, {}
        else:
            raise AssertionError("unreachable")
        details["operation"] = operation
        raise AgentError(
                code,
                "模型服务暂时无法完成请求",
                status_code=503 if retryable else 500,
                retryable=retryable,
                details=details,
            ) from error

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        schema_name: str,
        operation: str | None = None,
        max_completion_tokens: int = 800,
        strict: bool = True,
    ) -> dict[str, Any]:
        provider_schema = self._provider_compatible_schema(schema, require_all=strict)
        validation_error: Exception | None = None
        for attempt in range(2):
            retry_instruction = (
                "\n上一次输出未通过本地 JSON Schema 校验。请严格只返回符合 Schema 的 JSON。"
                if attempt
                else ""
            )
            response = await self._post(
                {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user + retry_instruction},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "strict": strict,
                            "schema": provider_schema,
                        },
                    },
                    "max_completion_tokens": max_completion_tokens,
                },
                operation=operation or schema_name,
            )
            try:
                value = json.loads(response["content"])
                Draft202012Validator(schema).validate(value)
                return value
            except Exception as exc:
                validation_error = exc
        raise AgentError(
            "MODEL_SCHEMA_INVALID",
            "模型返回了不符合约定的数据结构",
            status_code=500,
            details={"operation": operation or schema_name},
        ) from validation_error

    @classmethod
    def _provider_compatible_schema(cls, value: Any, *, require_all: bool = False) -> Any:
        unsupported = {
            "format",
            "maxItems",
            "maxLength",
            "maximum",
            "minItems",
            "minLength",
            "minimum",
            "pattern",
            "uniqueItems",
        }
        if isinstance(value, dict):
            result = {
                key: cls._provider_compatible_schema(item, require_all=require_all)
                for key, item in value.items()
                if key not in unsupported
            }
            if "const" in result and "type" not in result:
                const = result["const"]
                if const is None:
                    result["type"] = "null"
                elif isinstance(const, bool):
                    result["type"] = "boolean"
                elif isinstance(const, int):
                    result["type"] = "integer"
                elif isinstance(const, float):
                    result["type"] = "number"
                elif isinstance(const, str):
                    result["type"] = "string"
            if require_all and isinstance(result.get("properties"), dict):
                result["required"] = list(result["properties"].keys())
            return result
        if isinstance(value, list):
            return [cls._provider_compatible_schema(item, require_all=require_all) for item in value]
        return value

    async def complete_text(
        self,
        *,
        system: str,
        user: str,
        max_completion_tokens: int = 1000,
        operation: str = "text",
    ) -> str:
        response = await self._post(
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_completion_tokens": max_completion_tokens,
            },
            operation=operation,
        )
        return response["content"]
