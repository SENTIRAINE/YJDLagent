from __future__ import annotations

from typing import Any


class AgentError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}
        self.retry_count = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class MessageConflictError(AgentError):
    def __init__(self) -> None:
        super().__init__(
            "MESSAGE_CONFLICT",
            "同一 messageId 不能用于不同的请求内容",
            status_code=409,
        )


class RunNotFoundError(AgentError):
    def __init__(self) -> None:
        super().__init__("RUN_NOT_FOUND", "Run 不存在或不属于当前身份", status_code=404)


class EventHistoryExpiredError(AgentError):
    def __init__(self) -> None:
        super().__init__("EVENT_HISTORY_EXPIRED", "请求的事件历史已超过保留窗口", status_code=410)
