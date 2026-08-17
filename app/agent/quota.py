from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
import threading
import time
from uuid import uuid4

from app.agent.errors import AgentError
from app.config import Settings


@dataclass(frozen=True)
class QuotaReservation:
    reservation_id: str
    tenant_id: str
    user_id: str
    run_id: str
    token_limit: int
    created_at: float


class QuotaService:
    """Atomic quota gate with a Redis-compatible boundary.

    The local implementation is intentionally deterministic for development and
    tests. Production deployments can provide a Redis client and implement the
    same reserve/settle/release contract with the Lua token-bucket script.
    """

    def __init__(self, settings: Settings, *, redis_client: object | None = None) -> None:
        self.settings = settings
        self.redis = redis_client
        if self.redis is None and settings.agent_redis_url:
            from redis import Redis

            self.redis = Redis.from_url(settings.agent_redis_url, decode_responses=True)
        self._lock = threading.RLock()
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._active: dict[str, int] = defaultdict(int)
        self._queued: dict[str, int] = defaultdict(int)
        self._daily_tokens: dict[tuple[str, str], int] = defaultdict(int)
        self._reserved_tokens: dict[tuple[str, str], int] = defaultdict(int)
        self._reservations: dict[str, QuotaReservation] = {}

    @staticmethod
    def _day() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _rate_check(self, key: str, limit: int, now: float) -> None:
        bucket = self._requests[key]
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if len(bucket) >= limit:
            raise AgentError(
                "RATE_LIMITED",
                "请求频率超过限制",
                status_code=429,
                retryable=True,
                details={"retryAfter": max(1, int(60 - (now - bucket[0])))},
            )
        bucket.append(now)

    def reserve(self, *, tenant_id: str, user_id: str, run_id: str, queued: bool = True) -> QuotaReservation:
        if self.redis is not None:
            return self._redis_reserve(tenant_id=tenant_id, user_id=user_id, run_id=run_id)
        with self._lock:
            existing = self._reservations.get(run_id)
            if existing is not None:
                return existing
            now = time.monotonic()
            self._rate_check(f"user:{tenant_id}:{user_id}", self.settings.agent_user_rate_per_minute, now)
            self._rate_check(f"tenant:{tenant_id}", self.settings.agent_tenant_rate_per_minute, now)
            if self._active[tenant_id] >= self.settings.agent_tenant_active_runs:
                if self._queued[tenant_id] >= self.settings.agent_tenant_queue_runs:
                    raise AgentError("RUN_QUEUE_FULL", "租户 Run 队列已满", status_code=429, retryable=True)
            if self._queued[tenant_id] >= self.settings.agent_tenant_queue_runs:
                raise AgentError("RUN_QUEUE_FULL", "租户 Run 队列已满", status_code=429, retryable=True)
            day_key = (tenant_id, self._day())
            if self._daily_tokens[day_key] + self._reserved_tokens[day_key] + self.settings.agent_run_max_tokens > self.settings.agent_tenant_daily_tokens:
                raise AgentError("TOKEN_QUOTA_EXCEEDED", "租户 Token 配额已用尽", status_code=429, retryable=False)
            reservation = QuotaReservation(str(uuid4()), tenant_id, user_id, run_id, self.settings.agent_run_max_tokens, now)
            self._reservations[run_id] = reservation
            self._queued[tenant_id] += 1
            self._reserved_tokens[day_key] += reservation.token_limit
            return reservation

    def _redis_reserve(self, *, tenant_id: str, user_id: str, run_id: str) -> QuotaReservation:
        now = int(time.time())
        day = self._day()
        prefix = f"agent:quota:{{{tenant_id}}}"
        reservation_id = str(uuid4())
        script = """
        if redis.call('EXISTS', KEYS[1]) == 1 then return 2 end
        local ur = tonumber(redis.call('GET', KEYS[2]) or '0')
        local tr = tonumber(redis.call('GET', KEYS[3]) or '0')
        local queued = tonumber(redis.call('GET', KEYS[4]) or '0')
        local used = tonumber(redis.call('GET', KEYS[5]) or '0')
        local reserved = tonumber(redis.call('GET', KEYS[6]) or '0')
        if ur >= tonumber(ARGV[1]) then return -1 end
        if tr >= tonumber(ARGV[2]) then return -2 end
        if queued >= tonumber(ARGV[3]) then return -3 end
        if used + reserved + tonumber(ARGV[4]) > tonumber(ARGV[5]) then return -4 end
        redis.call('SET', KEYS[2], ur + 1, 'EX', 60)
        redis.call('SET', KEYS[3], tr + 1, 'EX', 60)
        redis.call('INCR', KEYS[4]); redis.call('EXPIRE', KEYS[4], 86400)
        redis.call('INCRBY', KEYS[6], ARGV[4]); redis.call('EXPIRE', KEYS[6], 172800)
        redis.call('HSET', KEYS[1], 'reservation_id', ARGV[6], 'tenant_id', ARGV[7], 'user_id', ARGV[8], 'token_limit', ARGV[4], 'active', '0')
        redis.call('EXPIRE', KEYS[1], 172800)
        return 1
        """
        keys = [
            f"{prefix}:reservation:{run_id}",
            f"{prefix}:rate:user:{user_id}",
            f"{prefix}:rate:tenant",
            f"{prefix}:queued",
            f"{prefix}:tokens:used:{day}",
            f"{prefix}:tokens:reserved:{day}",
        ]
        result = int(self.redis.eval(script, len(keys), *keys, self.settings.agent_user_rate_per_minute, self.settings.agent_tenant_rate_per_minute, self.settings.agent_tenant_queue_runs, self.settings.agent_run_max_tokens, self.settings.agent_tenant_daily_tokens, reservation_id, tenant_id, user_id))
        errors = {
            -1: ("RATE_LIMITED", "用户请求频率超过限制"),
            -2: ("RATE_LIMITED", "租户请求频率超过限制"),
            -3: ("RUN_QUEUE_FULL", "租户 Run 队列已满"),
            -4: ("TOKEN_QUOTA_EXCEEDED", "租户 Token 配额已用尽"),
        }
        if result < 0:
            code, message = errors[result]
            raise AgentError(code, message, status_code=429, retryable=code != "TOKEN_QUOTA_EXCEEDED")
        if result == 2:
            stored = self.redis.hgetall(keys[0])
            reservation_id = stored.get("reservation_id", reservation_id)
        return QuotaReservation(reservation_id, tenant_id, user_id, run_id, self.settings.agent_run_max_tokens, float(now))

    def mark_active(self, run_id: str, *, tenant_id: str | None = None) -> None:
        if self.redis is not None:
            self._redis_mark_active(run_id, tenant_id=tenant_id)
            return
        with self._lock:
            reservation = self._reservations.get(run_id)
            if reservation and self._queued[reservation.tenant_id] > 0:
                self._queued[reservation.tenant_id] -= 1
                self._active[reservation.tenant_id] += 1

    def _redis_mark_active(self, run_id: str, *, tenant_id: str | None) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required for Redis quota activation")
        prefix = f"agent:quota:{{{tenant_id}}}"
        key = f"{prefix}:reservation:{run_id}"
        script = """
            if redis.call('HGET', KEYS[1], 'active') == '1' then return 1 end
            local active = tonumber(redis.call('GET', KEYS[2]) or '0')
            if active >= tonumber(ARGV[1]) then return -1 end
            redis.call('HSET', KEYS[1], 'active', '1')
            if tonumber(redis.call('GET', KEYS[3]) or '0') > 0 then redis.call('DECR', KEYS[3]) end
            redis.call('INCR', KEYS[2]); redis.call('EXPIRE', KEYS[2], 86400)
            return 1
            """
        result = int(self.redis.eval(script, 3, key, f"{prefix}:active", f"{prefix}:queued", self.settings.agent_tenant_active_runs))
        if result < 0:
            raise AgentError("CONCURRENCY_LIMIT", "租户活跃 Run 已达到上限", status_code=429, retryable=True)

    def settle(self, run_id: str, *, total_tokens: int = 0, cost: float = 0.0, tenant_id: str | None = None) -> None:
        if self.redis is not None:
            self._redis_finish(run_id, total_tokens=max(0, int(total_tokens)), tenant_id=tenant_id)
            return
        with self._lock:
            reservation = self._reservations.pop(run_id, None)
            if not reservation:
                return
            tenant = reservation.tenant_id
            self._active[tenant] = max(0, self._active[tenant] - 1)
            key = (tenant, self._day())
            self._reserved_tokens[key] = max(0, self._reserved_tokens[key] - reservation.token_limit)
            projected = self._daily_tokens[key] + max(0, int(total_tokens))
            if projected > self.settings.agent_tenant_daily_tokens:
                raise AgentError("TOKEN_QUOTA_EXCEEDED", "租户 Token 配额已用尽", status_code=429, retryable=False)
            self._daily_tokens[key] = projected
            if self.settings.agent_tenant_daily_budget and cost > self.settings.agent_tenant_daily_budget:
                raise AgentError("COST_BUDGET_EXCEEDED", "租户费用预算已用尽", status_code=429, retryable=False)

    def release(self, run_id: str, *, tenant_id: str | None = None) -> None:
        if self.redis is not None:
            self._redis_finish(run_id, total_tokens=0, tenant_id=tenant_id)
            return
        with self._lock:
            reservation = self._reservations.pop(run_id, None)
            if not reservation:
                return
            tenant = reservation.tenant_id
            key = (tenant, self._day())
            self._reserved_tokens[key] = max(0, self._reserved_tokens[key] - reservation.token_limit)
            self._queued[tenant] = max(0, self._queued[tenant] - 1)
            self._active[tenant] = max(0, self._active[tenant] - 1)

    def _redis_finish(self, run_id: str, *, total_tokens: int, tenant_id: str | None) -> None:
        if not tenant_id:
            raise ValueError("tenant_id is required for Redis quota settlement")
        day = self._day()
        prefix = f"agent:quota:{{{tenant_id}}}"
        key = f"{prefix}:reservation:{run_id}"
        script = """
            if redis.call('EXISTS', KEYS[1]) == 0 then return 0 end
            local limit = tonumber(redis.call('HGET', KEYS[1], 'token_limit') or '0')
            local active = redis.call('HGET', KEYS[1], 'active')
            if active == '1' then
              if tonumber(redis.call('GET', KEYS[2]) or '0') > 0 then redis.call('DECR', KEYS[2]) end
            else
              if tonumber(redis.call('GET', KEYS[3]) or '0') > 0 then redis.call('DECR', KEYS[3]) end
            end
            local reserved = tonumber(redis.call('GET', KEYS[4]) or '0')
            redis.call('SET', KEYS[4], math.max(0, reserved - limit), 'EX', 172800)
            redis.call('INCRBY', KEYS[5], tonumber(ARGV[1])); redis.call('EXPIRE', KEYS[5], 172800)
            redis.call('DEL', KEYS[1])
            return 1
            """
        self.redis.eval(script, 5, key, f"{prefix}:active", f"{prefix}:queued", f"{prefix}:tokens:reserved:{day}", f"{prefix}:tokens:used:{day}", total_tokens)
