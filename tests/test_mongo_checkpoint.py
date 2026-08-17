from __future__ import annotations

import asyncio
import unittest

import mongomock

from app.agent.mongo_checkpoint import MongoCheckpointSaver


class MongoCheckpointTests(unittest.TestCase):
    def test_round_trip_parent_and_pending_writes(self) -> None:
        async def scenario() -> None:
            saver = MongoCheckpointSaver(
                "mongodb://localhost",
                "checkpoint-test",
                client=mongomock.MongoClient(),
            )
            first_config = {"configurable": {"thread_id": "conversation-1", "checkpoint_ns": "agent-v2"}}
            first = {
                "v": 1,
                "id": "00000000000000000000000000000001",
                "ts": "2026-08-16T00:00:00Z",
                "channel_values": {"answer": "first"},
                "channel_versions": {"answer": 1},
                "versions_seen": {},
            }
            saved = await saver.aput(first_config, first, {"source": "input"}, {"answer": 1})
            await saver.aput_writes(saved, [("answer", "pending")], "task-1")
            second = dict(first)
            second["id"] = "00000000000000000000000000000002"
            second["channel_values"] = {"answer": "second"}
            saved_second = await saver.aput(saved, second, {"source": "loop"}, {"answer": 2})
            result = await saver.aget_tuple(saved_second)
            self.assertIsNotNone(result)
            self.assertEqual(result.checkpoint["channel_values"]["answer"], "second")
            self.assertEqual(result.parent_config["configurable"]["checkpoint_id"], first["id"])
            self.assertEqual(result.pending_writes, [])
            first_result = await saver.aget_tuple(saved)
            self.assertEqual(first_result.pending_writes[0][2], "pending")
            await saver.adelete_thread("conversation-1")
            self.assertIsNone(await saver.aget_tuple(first_config))
            saver.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
