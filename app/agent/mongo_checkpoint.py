from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast

from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    RunnableConfig,
    WRITES_IDX_MAP,
    get_checkpoint_id,
    get_checkpoint_metadata,
)


class MongoCheckpointSaver(BaseCheckpointSaver):
    """Shared LangGraph checkpoint storage backed by MongoDB.

    Payloads are serialized with LangGraph's serde before being stored so custom
    channel values retain the same type information as the SQLite saver.
    """

    def __init__(self, uri: str, database: str, *, client: Any = None) -> None:
        super().__init__()
        if not uri:
            raise ValueError("MongoDB URI is required for Checkpoint storage")
        self.client = client or MongoClient(uri, serverSelectionTimeoutMS=5000)
        self.db = self.client[database]
        self.checkpoints = self.db.agent_checkpoints
        self.writes = self.db.agent_checkpoint_writes
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self.checkpoints.create_index(
            [("thread_id", ASCENDING), ("checkpoint_ns", ASCENDING), ("checkpoint_id", ASCENDING)],
            unique=True,
        )
        self.checkpoints.create_index(
            [("thread_id", ASCENDING), ("checkpoint_ns", ASCENDING), ("checkpoint_id", DESCENDING)]
        )
        self.writes.create_index(
            [
                ("thread_id", ASCENDING),
                ("checkpoint_ns", ASCENDING),
                ("checkpoint_id", ASCENDING),
                ("task_id", ASCENDING),
                ("idx", ASCENDING),
            ],
            unique=True,
        )

    @staticmethod
    def _encoded(value: Any, serde: Any) -> dict[str, str]:
        type_name, raw = serde.dumps_typed(value)
        return {"type": type_name, "value": base64.b64encode(raw).decode("ascii")}

    @staticmethod
    def _decoded(value: dict[str, str], serde: Any) -> Any:
        return serde.loads_typed((value["type"], base64.b64decode(value["value"])))

    @staticmethod
    def _config(config: RunnableConfig, checkpoint_id: str | None = None) -> RunnableConfig:
        configurable = config.get("configurable", {})
        result = {
            "configurable": {
                "thread_id": str(configurable["thread_id"]),
                "checkpoint_ns": str(configurable.get("checkpoint_ns", "")),
            }
        }
        selected = checkpoint_id or configurable.get("checkpoint_id")
        if selected is not None:
            result["configurable"]["checkpoint_id"] = str(selected)
        return cast(RunnableConfig, result)

    def _get_tuple_sync(self, config: RunnableConfig) -> CheckpointTuple | None:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = get_checkpoint_id(config)
        query = {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns}
        if checkpoint_id:
            query["checkpoint_id"] = checkpoint_id
            document = self.checkpoints.find_one(query)
        else:
            document = self.checkpoints.find_one(query, sort=[("checkpoint_id", DESCENDING)])
        if document is None:
            return None
        selected_config = self._config(config, str(document["checkpoint_id"]))
        pending = []
        for write in self.writes.find(
            {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": str(document["checkpoint_id"]),
            }
        ).sort([("task_id", ASCENDING), ("idx", ASCENDING)]):
            pending.append(
                (
                    write["task_id"],
                    write["channel"],
                    self._decoded(write["value"], self.serde),
                )
            )
        parent = None
        if document.get("parent_checkpoint_id"):
            parent = self._config(config, str(document["parent_checkpoint_id"]))
        return CheckpointTuple(
            selected_config,
            self._decoded(document["checkpoint"], self.serde),
            cast(CheckpointMetadata, document.get("metadata", {})),
            parent,
            pending,
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return await asyncio.to_thread(self._get_tuple_sync, config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        if config is None:
            return
        configurable = config["configurable"]
        query: dict[str, Any] = {
            "thread_id": str(configurable["thread_id"]),
            "checkpoint_ns": str(configurable.get("checkpoint_ns", "")),
        }
        if filter:
            for key, value in filter.items():
                query[f"metadata.{key}"] = value
        if before:
            query["checkpoint_id"] = {"$lt": str(before["configurable"].get("checkpoint_id", ""))}
        cursor = self.checkpoints.find(query).sort("checkpoint_id", DESCENDING)
        if limit is not None:
            cursor = cursor.limit(max(0, int(limit)))
        documents = await asyncio.to_thread(list, cursor)
        for document in documents:
            tuple_value = await asyncio.to_thread(
                self._get_tuple_sync,
                self._config(config, str(document["checkpoint_id"])),
            )
            if tuple_value is not None:
                yield tuple_value

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        document = {
            "thread_id": thread_id,
            "checkpoint_ns": checkpoint_ns,
            "checkpoint_id": str(checkpoint["id"]),
            "parent_checkpoint_id": configurable.get("checkpoint_id"),
            "checkpoint": self._encoded(checkpoint, self.serde),
            "metadata": deepcopy(get_checkpoint_metadata(config, metadata)),
            "new_versions": deepcopy(new_versions),
            "schema_version": 1,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        parent_id = configurable.get("checkpoint_id")
        if parent_id:
            parent = await asyncio.to_thread(
                self.checkpoints.find_one,
                {"thread_id": thread_id, "checkpoint_ns": checkpoint_ns, "checkpoint_id": str(parent_id)},
            )
            if parent is None:
                raise ValueError("checkpoint parent version does not exist")

        def put_once() -> None:
            key = {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
                "checkpoint_id": str(checkpoint["id"]),
            }
            try:
                self.checkpoints.insert_one({**key, **document})
            except DuplicateKeyError:
                existing = self.checkpoints.find_one(key)
                if existing is None or existing.get("checkpoint") != document["checkpoint"]:
                    raise ValueError("checkpoint id collision with different payload")

        await asyncio.to_thread(put_once)
        return self._config(config, str(checkpoint["id"]))

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        configurable = config["configurable"]
        thread_id = str(configurable["thread_id"])
        checkpoint_ns = str(configurable.get("checkpoint_ns", ""))
        checkpoint_id = str(configurable.get("checkpoint_id", ""))

        def write_all() -> None:
            for index, (channel, value) in enumerate(writes):
                idx = WRITES_IDX_MAP.get(channel, index)
                filter_key = {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                    "task_id": task_id,
                    "idx": idx,
                }
                document = {
                    **filter_key,
                    "channel": channel,
                    "task_path": task_path,
                    "value": self._encoded(value, self.serde),
                }
                self.writes.replace_one(filter_key, document, upsert=True)

        await asyncio.to_thread(write_all)

    async def adelete_thread(self, thread_id: str) -> None:
        await asyncio.to_thread(self.checkpoints.delete_many, {"thread_id": str(thread_id)})
        await asyncio.to_thread(self.writes.delete_many, {"thread_id": str(thread_id)})

    def close(self) -> None:
        self.client.close()
