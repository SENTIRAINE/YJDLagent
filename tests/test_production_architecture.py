from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from app.agent.contracts import LangGraphRunRequest
from app.agent.errors import AgentError
from app.agent.store import AgentStore
from app.agent.runtime import AgentRuntime
from tests.test_agent_runtime import FakeLlm, FakeRag, FakeTools, settings_for


def request(conversation_id: str | None = None, message_id: str | None = None) -> LangGraphRunRequest:
    return LangGraphRunRequest.model_validate(
        {
            "conversationId": conversation_id or str(uuid4()),
            "messageId": message_id or str(uuid4()),
            "query": "查询中山区住宅",
            "context": {"locale": "zh-CN", "map": None, "businessObjectIds": []},
            "user": {"userId": "u-1", "tenantId": "t-1", "roles": ["USER"]},
        }
    )


class ProductionStoreTests(unittest.TestCase):
    def test_rapid_follow_up_cannot_observe_completed_before_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.sqlite3")
            run, _ = store.create_or_attach(request(), "trace-1")
            store.claim_next_run("worker-a", lease_seconds=60)
            store.complete_run_with_memory(
                run.run_id,
                user_query=run.request["query"],
                assistant_answer="第一轮回答",
                route="MAP_QUERY",
                map_summary={"resultCounts": []},
                citations=[],
                warnings=[],
            )
            completed = store.get_run_unscoped(run.run_id)
            self.assertEqual(completed.status, "SUCCEEDED")
            self.assertEqual(store.list_conversation_memory("t-1", "u-1", run.conversation_id)[0].assistant_answer, "第一轮回答")
            self.assertEqual(store.list_events(run.run_id, 0)[-1].event_name, "run.completed")

    def test_multi_instance_claims_are_exclusive_and_expired_leases_take_over(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.sqlite3")
            run, _ = store.create_or_attach(request(), "trace-1")
            first = store.claim_next_run("worker-a", lease_seconds=60)
            second = store.claim_next_run("worker-b", lease_seconds=60)
            self.assertEqual(first.run_id, run.run_id)
            self.assertIsNone(second)
            self.assertFalse(
                store.renew_lease(
                    run.run_id,
                    "worker-a",
                    lease_seconds=60,
                    lease_generation=first.lease_generation - 1,
                )
            )
            store.release_lease(run.run_id, "worker-a", requeue=True)
            takeover = store.claim_next_run("worker-b", lease_seconds=60)
            self.assertEqual(takeover.lease_owner, "worker-b")

    def test_rolling_release_requeues_without_cancel_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.sqlite3")
            run, _ = store.create_or_attach(request(), "trace-1")
            store.claim_next_run("old-instance", lease_seconds=60)
            self.assertTrue(store.release_lease(run.run_id, "old-instance", requeue=True))
            resumed = store.claim_next_run("new-instance", lease_seconds=60)
            self.assertEqual(resumed.status, "RUNNING")
            self.assertNotIn("run.cancelled", [event.event_name for event in store.list_events(run.run_id, 0)])

    def test_queue_limit_keeps_idempotent_retry_attachable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.sqlite3")
            first, _ = store.create_or_attach(request(), "trace-1", max_nonterminal_runs=1)
            attached, created = store.create_or_attach(request(first.conversation_id, first.message_id), "trace-retry", max_nonterminal_runs=1)
            self.assertFalse(created)
            self.assertEqual(attached.run_id, first.run_id)
            with self.assertRaises(AgentError) as context:
                store.create_or_attach(request(), "trace-2", max_nonterminal_runs=1)
            self.assertEqual(context.exception.code, "RUN_QUEUE_FULL")


class ImmediateGraph:
    def __init__(self, *, delay: float = 0.05) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.invocations = 0

    async def aget_state(self, _config):
        return type("Snapshot", (), {"values": {}})()

    async def astream(self, *_args, **_kwargs):
        self.active += 1
        self.invocations += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            yield "updates", {"compose_answer": {"answer": "完成", "intent": "MAP_QUERY"}}
        finally:
            self.active -= 1


class LeaseHangingGraph:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def astream(self, *_args, **_kwargs):
        self.entered.set()
        await asyncio.sleep(30)
        if False:
            yield "updates", {}


class RuntimeWorkerTests(unittest.TestCase):
    @staticmethod
    async def wait_terminal(runtime: AgentRuntime, run_id: str) -> None:
        for _ in range(400):
            if runtime.store.get_run_unscoped(run_id).terminal:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"run {run_id} did not become terminal")

    def test_worker_concurrency_limit_is_enforced(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                settings = replace(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    agent_worker_concurrency=2,
                    agent_worker_poll_seconds=0.01,
                )
                graph = ImmediateGraph(delay=0.1)
                runtime = AgentRuntime(settings, llm=FakeLlm(), rag=FakeRag(), tools=FakeTools())
                runtime._graph = graph
                try:
                    runs = [(await runtime.start_run(request(), f"trace-{index}"))[0] for index in range(5)]
                    await asyncio.gather(*(self.wait_terminal(runtime, run.run_id) for run in runs))
                    self.assertTrue(all(runtime.store.get_run_unscoped(run.run_id).status == "SUCCEEDED" for run in runs))
                    self.assertGreaterEqual(graph.max_active, 1)
                    self.assertLessEqual(graph.max_active, settings.agent_worker_concurrency)
                finally:
                    await runtime.close()

        asyncio.run(scenario())

    def test_two_runtime_instances_execute_one_run_once(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "agent.sqlite3"
                settings = replace(
                    settings_for(database),
                    agent_worker_concurrency=1,
                    agent_worker_poll_seconds=0.01,
                )
                graph = ImmediateGraph(delay=0.1)
                first = AgentRuntime(settings, llm=FakeLlm(), rag=FakeRag(), tools=FakeTools())
                second = AgentRuntime(settings, llm=FakeLlm(), rag=FakeRag(), tools=FakeTools())
                first._graph = graph
                second._graph = graph
                try:
                    second.start_workers()
                    run, _ = await first.start_run(request(), "trace-multi")
                    await self.wait_terminal(first, run.run_id)
                    self.assertEqual(graph.invocations, 1)
                finally:
                    await first.close()
                    await second.close()

        asyncio.run(scenario())

    def test_rolling_release_requeues_and_next_runtime_completes(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "agent.sqlite3"
                settings = replace(
                    settings_for(database),
                    agent_worker_concurrency=1,
                    agent_worker_poll_seconds=0.01,
                    agent_shutdown_grace_seconds=0.01,
                )
                old_graph = LeaseHangingGraph()
                old_runtime = AgentRuntime(settings, llm=FakeLlm(), rag=FakeRag(), tools=FakeTools())
                old_runtime._graph = old_graph
                run, _ = await old_runtime.start_run(request(), "trace-old")
                await asyncio.wait_for(old_graph.entered.wait(), timeout=2)
                await old_runtime.close()

                persisted = AgentStore(database)
                self.assertEqual(persisted.get_run_unscoped(run.run_id).status, "QUEUED")
                self.assertNotIn("run.cancelled", [event.event_name for event in persisted.list_events(run.run_id, 0)])

                new_runtime = AgentRuntime(settings, llm=FakeLlm(), rag=FakeRag(), tools=FakeTools())
                new_runtime._graph = ImmediateGraph(delay=0.01)
                try:
                    new_runtime.start_workers()
                    await self.wait_terminal(new_runtime, run.run_id)
                    self.assertEqual(new_runtime.store.get_run_unscoped(run.run_id).status, "SUCCEEDED")
                finally:
                    await new_runtime.close()

        asyncio.run(scenario())


class MongoStoreTests(unittest.TestCase):
    @unittest.skipUnless(__import__("importlib").util.find_spec("mongomock") and __import__("importlib").util.find_spec("pymongo"), "Mongo test dependencies are not installed")
    def test_mongomock_persistence_contract(self) -> None:
        import mongomock
        from app.agent.mongo_store import MongoAgentStore

        client = mongomock.MongoClient()
        store = MongoAgentStore("mongodb://localhost", "test", client=client, require_transactions=False)
        run, _ = store.create_or_attach(request(), "trace-mongo")
        store.claim_next_run("worker", lease_seconds=60)
        store.complete_run_with_memory(run.run_id, user_query="q", assistant_answer="a", route="RAG_QA", map_summary=None, citations=[], warnings=[])
        self.assertEqual(store.get_run_unscoped(run.run_id).status, "SUCCEEDED")
        self.assertEqual(len(store.list_conversation_memory("t-1", "u-1", run.conversation_id)), 1)
        store.save_conversation_memory("t-1", "u-1", "legacy-conversation", user_query="q1", assistant_answer="a1", route=None)
        store.save_conversation_memory("t-1", "u-1", "legacy-conversation", user_query="q2", assistant_answer="a2", route=None)
        self.assertEqual(len(store.list_conversation_memory("t-1", "u-1", "legacy-conversation")), 2)


if __name__ == "__main__":
    unittest.main()
