from __future__ import annotations

from typing import Any

from app.config import Settings


def create_agent_store(settings: Settings) -> Any:
    """Create the configured durable Run/Conversation/Event store.

    The import is intentionally lazy so SQLite-only deployments and tests do not
    require a running MongoDB server (or a Mongo driver at import time).
    """
    if settings.agent_storage_backend == "mongodb":
        from app.agent.mongo_store import MongoAgentStore

        store = MongoAgentStore(
            settings.agent_mongodb_uri,
            settings.agent_mongodb_database,
            event_retention_seconds=settings.agent_event_retention_seconds,
            require_transactions=settings.agent_mongodb_require_transactions,
        )
        store.validate_connectivity()
        return store
    from app.agent.store import AgentStore

    return AgentStore(
        settings.agent_database_path,
        event_retention_seconds=settings.agent_event_retention_seconds,
        busy_timeout_ms=settings.agent_sqlite_busy_timeout_ms,
        write_retry_attempts=settings.agent_sqlite_write_retry_attempts,
        write_retry_base_delay_ms=settings.agent_sqlite_write_retry_base_delay_ms,
    )
