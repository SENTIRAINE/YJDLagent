from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from app.agent.contracts import LangGraphRunRequest
from app.agent.errors import AgentError
from app.agent.quota import QuotaService
from app.agent.store import AgentStore
from app.config import Settings


def run_request(conversation_id: str, query: str) -> LangGraphRunRequest:
    return LangGraphRunRequest.model_validate(
        {
            "conversationId": conversation_id,
            "messageId": str(uuid4()),
            "query": query,
            "context": {"locale": "zh-CN", "businessObjectIds": []},
            "user": {"tenantId": "tenant-1", "userId": "user-1", "roles": ["USER"]},
        }
    )


class ConversationSerializationTests(unittest.TestCase):
    def test_fast_followup_waits_then_reads_latest_state_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.sqlite3")
            conversation_id = str(uuid4())
            first, _ = store.create_or_attach(run_request(conversation_id, "第一轮"), "trace-1")
            second, _ = store.create_or_attach(run_request(conversation_id, "第二轮"), "trace-2")
            self.assertEqual(second.depends_on_run_id, first.run_id)

            claimed_first = store.claim_next_run("worker-1", lease_seconds=30)
            self.assertEqual(claimed_first.run_id, first.run_id)
            self.assertIsNone(store.claim_next_run("worker-2", lease_seconds=30))

            store.complete_run_with_memory(
                first.run_id,
                user_query="第一轮",
                assistant_answer="第一轮答案",
                route="RAG_QA",
                map_summary=None,
                citations=[],
                warnings=[],
                conversation_state={"summary": "第一轮答案"},
            )
            claimed_second = store.claim_next_run("worker-2", lease_seconds=30)
            self.assertEqual(claimed_second.run_id, second.run_id)
            self.assertEqual(claimed_second.base_state_version, 1)

    def test_state_memory_and_completed_commit_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.sqlite3")
            conversation_id = str(uuid4())
            run, _ = store.create_or_attach(run_request(conversation_id, "测试"), "trace")
            store.complete_run_with_memory(
                run.run_id,
                user_query="测试",
                assistant_answer="答案",
                route="RAG_QA",
                map_summary=None,
                citations=[],
                warnings=[],
                conversation_state={"summary": "答案"},
            )
            state = store.get_conversation_state("tenant-1", "user-1", conversation_id)
            self.assertEqual(state["stateVersion"], 1)
            self.assertEqual(state["lastCommittedRunId"], run.run_id)
            self.assertEqual(len(store.list_conversation_memory("tenant-1", "user-1", conversation_id)), 1)
            self.assertEqual(store.list_events(run.run_id, 0)[-1].event_name, "run.completed")


class QuotaTests(unittest.TestCase):
    def test_user_rate_limit_and_idempotent_release(self) -> None:
        settings = replace(
            Settings.from_env(),
            agent_user_rate_per_minute=1,
            agent_tenant_rate_per_minute=10,
        )
        quota = QuotaService(settings)
        quota.reserve(tenant_id="tenant", user_id="user", run_id="run-1")
        with self.assertRaises(AgentError) as context:
            quota.reserve(tenant_id="tenant", user_id="user", run_id="run-2")
        self.assertEqual(context.exception.code, "RATE_LIMITED")
        quota.release("run-1")
        quota.release("run-1")


if __name__ == "__main__":
    unittest.main()

