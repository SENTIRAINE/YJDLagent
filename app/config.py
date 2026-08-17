from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CATALOG_MAX_TOOL_TIMEOUT_SECONDS = 120.0


def _path_from_env(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default))
    return value if value.is_absolute() else PROJECT_ROOT / value


def _bool_from_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class Settings:
    database_path: Path
    processed_path: Path
    source_glob: str
    embedding_provider: str
    embedding_dimension: int
    embedding_base_url: str
    embedding_model: str
    embedding_api_key: str
    dense_weight: float
    lexical_weight: float
    number_weight: float
    default_top_k: int
    spring_boot_base_url: str
    agent_storage_backend: str = "sqlite"
    agent_database_path: Path = PROJECT_ROOT / "data/index/agent.sqlite3"
    agent_mongodb_uri: str = ""
    agent_mongodb_database: str = "yjdl_agent"
    agent_mongodb_require_transactions: bool = True
    agent_checkpoint_backend: str = "sqlite"
    agent_checkpoint_mongodb_database: str = "yjdl_agent"
    agent_checkpoint_database_path: Path = PROJECT_ROOT / "data/index/agent-checkpoints.sqlite3"
    agent_sqlite_busy_timeout_ms: int = 5000
    agent_sqlite_write_retry_attempts: int = 4
    agent_sqlite_write_retry_base_delay_ms: int = 50
    agent_event_retention_seconds: int = 86400
    agent_max_run_seconds: int = 180
    agent_tool_timeout_seconds: float = 125.0
    agent_sse_heartbeat_seconds: int = 15
    agent_map_result_limit: int = 50
    agent_worker_enabled: bool = True
    agent_worker_concurrency: int = 4
    agent_worker_lease_seconds: int = 60
    agent_worker_poll_seconds: float = 0.2
    agent_max_queued_runs: int = 100
    agent_shutdown_grace_seconds: float = 10.0
    agent_user_rate_per_minute: int = 10
    agent_tenant_rate_per_minute: int = 120
    agent_tenant_active_runs: int = 20
    agent_tenant_queue_runs: int = 100
    agent_run_max_tokens: int = 12000
    agent_tenant_daily_tokens: int = 2_000_000
    agent_tenant_daily_budget: float = 0.0
    agent_redis_url: str = ""
    agent_worker_id: str = ""
    langgraph_service_token: str = ""
    agent_tool_service_token: str = ""
    openai_chat_completions_url: str = "https://kuaipao.pro/v1/chat/completions"
    openai_model: str = "gpt-5.4"
    openai_api_key: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            database_path=_path_from_env("RAG_DATABASE_PATH", "data/index/rag.sqlite3"),
            processed_path=_path_from_env("RAG_PROCESSED_PATH", "data/processed/chunks.jsonl"),
            source_glob=os.getenv("RAG_SOURCE_GLOB", "*.pdf"),
            embedding_provider=os.getenv("RAG_EMBEDDING_PROVIDER", "hash").lower(),
            embedding_dimension=int(os.getenv("RAG_EMBEDDING_DIMENSION", "768")),
            embedding_base_url=os.getenv("RAG_EMBEDDING_BASE_URL", "http://localhost:7997/v1"),
            embedding_model=os.getenv("RAG_EMBEDDING_MODEL", "BAAI/bge-m3"),
            embedding_api_key=os.getenv("RAG_EMBEDDING_API_KEY", ""),
            dense_weight=float(os.getenv("RAG_DENSE_WEIGHT", "0.55")),
            lexical_weight=float(os.getenv("RAG_LEXICAL_WEIGHT", "0.35")),
            number_weight=float(os.getenv("RAG_NUMBER_WEIGHT", "0.10")),
            default_top_k=int(os.getenv("RAG_DEFAULT_TOP_K", "5")),
            spring_boot_base_url=os.getenv("SPRING_BOOT_BASE_URL", "http://localhost:8080").rstrip("/"),
            agent_storage_backend=os.getenv("AGENT_STORAGE_BACKEND", "sqlite").strip().lower(),
            agent_database_path=_path_from_env("AGENT_DATABASE_PATH", "data/index/agent.sqlite3"),
            agent_mongodb_uri=os.getenv("AGENT_MONGODB_URI", "").strip(),
            agent_mongodb_database=os.getenv("AGENT_MONGODB_DATABASE", "yjdl_agent").strip(),
            agent_mongodb_require_transactions=_bool_from_env(
                "AGENT_MONGODB_REQUIRE_TRANSACTIONS", True
            ),
            agent_checkpoint_backend=os.getenv("AGENT_CHECKPOINT_BACKEND", "sqlite").strip().lower(),
            agent_checkpoint_mongodb_database=os.getenv(
                "AGENT_CHECKPOINT_MONGODB_DATABASE", ""
            ).strip(),
            agent_checkpoint_database_path=_path_from_env(
                "AGENT_CHECKPOINT_DATABASE_PATH", "data/index/agent-checkpoints.sqlite3"
            ),
            agent_sqlite_busy_timeout_ms=int(os.getenv("AGENT_SQLITE_BUSY_TIMEOUT_MS", "5000")),
            agent_sqlite_write_retry_attempts=int(
                os.getenv("AGENT_SQLITE_WRITE_RETRY_ATTEMPTS", "4")
            ),
            agent_sqlite_write_retry_base_delay_ms=int(
                os.getenv("AGENT_SQLITE_WRITE_RETRY_BASE_DELAY_MS", "50")
            ),
            agent_event_retention_seconds=int(os.getenv("AGENT_EVENT_RETENTION_SECONDS", "86400")),
            agent_max_run_seconds=int(os.getenv("AGENT_MAX_RUN_SECONDS", "180")),
            agent_tool_timeout_seconds=float(os.getenv("AGENT_TOOL_TIMEOUT_SECONDS", "125")),
            agent_sse_heartbeat_seconds=int(os.getenv("AGENT_SSE_HEARTBEAT_SECONDS", "15")),
            agent_map_result_limit=int(os.getenv("AGENT_MAP_RESULT_LIMIT", "50")),
            agent_worker_enabled=_bool_from_env("AGENT_WORKER_ENABLED", True),
            agent_worker_concurrency=int(os.getenv("AGENT_WORKER_CONCURRENCY", "4")),
            agent_worker_lease_seconds=int(os.getenv("AGENT_WORKER_LEASE_SECONDS", "60")),
            agent_worker_poll_seconds=float(os.getenv("AGENT_WORKER_POLL_SECONDS", "0.2")),
            agent_max_queued_runs=int(os.getenv("AGENT_MAX_QUEUED_RUNS", "100")),
            agent_shutdown_grace_seconds=float(
                os.getenv("AGENT_SHUTDOWN_GRACE_SECONDS", "10")
            ),
            agent_user_rate_per_minute=int(os.getenv("AGENT_USER_RATE_PER_MINUTE", "10")),
            agent_tenant_rate_per_minute=int(os.getenv("AGENT_TENANT_RATE_PER_MINUTE", "120")),
            agent_tenant_active_runs=int(os.getenv("AGENT_TENANT_ACTIVE_RUNS", "20")),
            agent_tenant_queue_runs=int(os.getenv("AGENT_TENANT_QUEUE_RUNS", "100")),
            agent_run_max_tokens=int(os.getenv("AGENT_RUN_MAX_TOKENS", "12000")),
            agent_tenant_daily_tokens=int(os.getenv("AGENT_TENANT_DAILY_TOKENS", "2000000")),
            agent_tenant_daily_budget=float(os.getenv("AGENT_TENANT_DAILY_BUDGET", "0")),
            agent_redis_url=os.getenv("AGENT_REDIS_URL", "").strip(),
            agent_worker_id=os.getenv("AGENT_WORKER_ID", "").strip(),
            langgraph_service_token=os.getenv("LANGGRAPH_SERVICE_TOKEN", ""),
            agent_tool_service_token=os.getenv("AGENT_TOOL_SERVICE_TOKEN", ""),
            openai_chat_completions_url=os.getenv(
                "OPENAI_CHAT_COMPLETIONS_URL", "https://kuaipao.pro/v1/chat/completions"
            ),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-5.4"),
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        )
        if settings.embedding_dimension <= 0:
            raise ValueError("RAG_EMBEDDING_DIMENSION must be positive")
        weights = settings.dense_weight + settings.lexical_weight + settings.number_weight
        if abs(weights - 1.0) > 1e-6:
            raise ValueError("RAG retrieval weights must add up to 1.0")
        if settings.agent_event_retention_seconds <= 0:
            raise ValueError("AGENT_EVENT_RETENTION_SECONDS must be positive")
        if settings.agent_storage_backend not in {"sqlite", "mongodb"}:
            raise ValueError("AGENT_STORAGE_BACKEND must be sqlite or mongodb")
        if settings.agent_checkpoint_backend not in {"sqlite", "mongodb"}:
            raise ValueError("AGENT_CHECKPOINT_BACKEND must be sqlite or mongodb")
        if settings.agent_storage_backend == "mongodb" and not settings.agent_mongodb_uri:
            raise ValueError("AGENT_MONGODB_URI is required for the mongodb backend")
        if settings.agent_storage_backend == "mongodb" and not settings.agent_mongodb_database:
            raise ValueError("AGENT_MONGODB_DATABASE must not be blank")
        if settings.agent_checkpoint_backend == "mongodb" and not settings.agent_mongodb_uri:
            raise ValueError("AGENT_MONGODB_URI is required for the mongodb checkpoint backend")
        if settings.agent_checkpoint_backend == "mongodb" and not settings.agent_checkpoint_mongodb_database:
            raise ValueError("AGENT_CHECKPOINT_MONGODB_DATABASE must not be blank")
        if (
            settings.agent_storage_backend == "sqlite"
            and settings.agent_database_path.resolve()
            == settings.agent_checkpoint_database_path.resolve()
        ):
            raise ValueError("AGENT_DATABASE_PATH and AGENT_CHECKPOINT_DATABASE_PATH must be different")
        if settings.agent_sqlite_busy_timeout_ms <= 0:
            raise ValueError("AGENT_SQLITE_BUSY_TIMEOUT_MS must be positive")
        if settings.agent_sqlite_write_retry_attempts < 0:
            raise ValueError("AGENT_SQLITE_WRITE_RETRY_ATTEMPTS must not be negative")
        if settings.agent_sqlite_write_retry_base_delay_ms <= 0:
            raise ValueError("AGENT_SQLITE_WRITE_RETRY_BASE_DELAY_MS must be positive")
        if settings.agent_max_run_seconds <= 0:
            raise ValueError("AGENT_MAX_RUN_SECONDS must be positive")
        if settings.agent_tool_timeout_seconds <= CATALOG_MAX_TOOL_TIMEOUT_SECONDS:
            raise ValueError(
                "AGENT_TOOL_TIMEOUT_SECONDS must be greater than the Catalog timeout "
                f"({CATALOG_MAX_TOOL_TIMEOUT_SECONDS:g} seconds)"
            )
        if settings.agent_tool_timeout_seconds >= settings.agent_max_run_seconds:
            raise ValueError("AGENT_TOOL_TIMEOUT_SECONDS must be lower than AGENT_MAX_RUN_SECONDS")
        if settings.agent_sse_heartbeat_seconds <= 0:
            raise ValueError("AGENT_SSE_HEARTBEAT_SECONDS must be positive")
        if not 1 <= settings.agent_map_result_limit <= 200:
            raise ValueError("AGENT_MAP_RESULT_LIMIT must be from 1 to 200")
        if settings.agent_worker_concurrency <= 0:
            raise ValueError("AGENT_WORKER_CONCURRENCY must be positive")
        if settings.agent_worker_lease_seconds <= 0:
            raise ValueError("AGENT_WORKER_LEASE_SECONDS must be positive")
        if settings.agent_worker_poll_seconds <= 0:
            raise ValueError("AGENT_WORKER_POLL_SECONDS must be positive")
        if settings.agent_max_queued_runs <= 0:
            raise ValueError("AGENT_MAX_QUEUED_RUNS must be positive")
        if settings.agent_shutdown_grace_seconds < 0:
            raise ValueError("AGENT_SHUTDOWN_GRACE_SECONDS must not be negative")
        for name in ("agent_user_rate_per_minute", "agent_tenant_rate_per_minute", "agent_tenant_active_runs", "agent_tenant_queue_runs", "agent_run_max_tokens", "agent_tenant_daily_tokens"):
            if getattr(settings, name) <= 0:
                raise ValueError(f"{name.upper()} must be positive")
        if settings.agent_tenant_daily_budget < 0:
            raise ValueError("AGENT_TENANT_DAILY_BUDGET must not be negative")
        if settings.agent_redis_url and not settings.agent_redis_url.startswith(("redis://", "rediss://")):
            raise ValueError("AGENT_REDIS_URL must be a redis:// or rediss:// URL")
        if not settings.openai_chat_completions_url.startswith(("http://", "https://")):
            raise ValueError("OPENAI_CHAT_COMPLETIONS_URL must be an HTTP(S) URL")
        return settings
