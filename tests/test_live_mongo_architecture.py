from __future__ import annotations

import os
import asyncio
import unittest
from uuid import uuid4

from app.agent.mongo_checkpoint import MongoCheckpointSaver
from app.agent.mongo_store import MongoAgentStore
from tests.test_production_architecture import request


@unittest.skipUnless(os.getenv("RUN_LIVE_MONGO_TESTS") == "1", "set RUN_LIVE_MONGO_TESTS=1")
class LiveMongoArchitectureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.uri = os.getenv("AGENT_MONGODB_URI", "mongodb://127.0.0.1:27017/?replicaSet=rs0")
        self.database = f"yjdl_agent_test_{uuid4().hex}"
        self.first = MongoAgentStore(self.uri, self.database, require_transactions=True)
        self.second = MongoAgentStore(self.uri, self.database, require_transactions=True)
        self.first.validate_connectivity()

    async def asyncTearDown(self) -> None:
        self.first.client.drop_database(self.database)
        self.first.close()
        self.second.close()

    async def test_fast_followup_multi_instance_and_checkpoint(self) -> None:
        conversation_id = str(uuid4())
        first, _ = self.first.create_or_attach(request(conversation_id=conversation_id), "trace-1")
        second, _ = self.second.create_or_attach(request(conversation_id=conversation_id), "trace-2")
        self.assertEqual(second.depends_on_run_id, first.run_id)

        claimed = self.first.claim_next_run("worker-a", lease_seconds=30)
        self.assertEqual(claimed.run_id, first.run_id)
        self.assertIsNone(self.second.claim_next_run("worker-b", lease_seconds=30))
        self.first.complete_run_with_memory(
            first.run_id,
            user_query="第一轮",
            assistant_answer="第一轮答案",
            route="RAG_QA",
            map_summary=None,
            citations=[],
            warnings=[],
            conversation_state={"summary": "第一轮答案"},
        )
        followup = self.second.claim_next_run("worker-b", lease_seconds=30)
        self.assertEqual(followup.run_id, second.run_id)
        self.assertEqual(followup.base_state_version, 1)

        saver = MongoCheckpointSaver(self.uri, self.database)
        config = {"configurable": {"thread_id": conversation_id, "checkpoint_ns": "agent-v2"}}
        checkpoint = {"v": 1, "id": "00000000000000000000000000000001", "ts": "2026-08-16T00:00:00Z", "channel_values": {"answer": "ok"}, "channel_versions": {"answer": 1}, "versions_seen": {}}
        saved = await saver.aput(config, checkpoint, {"source": "input"}, {"answer": 1})
        loaded = await saver.aget_tuple(saved)
        self.assertEqual(loaded.checkpoint["channel_values"]["answer"], "ok")
        saver.close()

    async def test_concurrent_api_creates_form_one_dependency_chain(self) -> None:
        conversation_id = str(uuid4())
        first_request = request(conversation_id=conversation_id)
        second_request = request(conversation_id=conversation_id)
        created = await asyncio.gather(
            asyncio.to_thread(self.first.create_or_attach, first_request, "trace-a"),
            asyncio.to_thread(self.second.create_or_attach, second_request, "trace-b"),
        )
        runs = [item[0] for item in created]
        dependencies = [run.depends_on_run_id for run in runs]
        self.assertEqual(sum(value is None for value in dependencies), 1)
        root = next(run for run in runs if run.depends_on_run_id is None)
        child = next(run for run in runs if run.depends_on_run_id is not None)
        self.assertEqual(child.depends_on_run_id, root.run_id)


if __name__ == "__main__":
    unittest.main()
