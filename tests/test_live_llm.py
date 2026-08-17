from __future__ import annotations

import os
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.agent.llm import OpenAICompatibleChatClient
from app.agent.rag_service import RagEvidenceService
from app.agent.workflow import build_agent_graph
from app.config import Settings
from app.tools.spring_client import SpringToolClient


FIXTURE_MANIFEST_PATH = Path("tests/fixtures/agent-v1.1/manifest.json")
FIXTURE_MANIFEST = json.loads(FIXTURE_MANIFEST_PATH.read_text(encoding="utf-8"))
CATALOG_PATH = (
    FIXTURE_MANIFEST_PATH.parent / FIXTURE_MANIFEST["files"]["catalog"]
).resolve()


class NoToolCallsExpected:
    async def catalog(self, _context: object) -> dict[str, object]:
        raise AssertionError("RAG_QA must not load the Tool Catalog")


class FixtureMapTools:
    validate_arguments = staticmethod(SpringToolClient.validate_arguments)
    validate_result = staticmethod(SpringToolClient.validate_result)

    def __init__(self) -> None:
        self.catalog_data = json.loads(
            CATALOG_PATH.read_text(encoding="utf-8")
        )
        self.last_call: tuple[str, dict[str, object]] | None = None

    async def catalog(self, _context: object) -> dict[str, object]:
        return self.catalog_data

    async def invoke_with_recovery(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, object],
        _context: object,
    ) -> dict[str, object]:
        self.last_call = (tool_name, arguments)
        layer_id = int(arguments["layerId"])
        layer_names = ["shahekou_1", "xigang_1", "zhongshan_1"]
        return {
            "success": True,
            "data": {
                "toolCallId": tool_call_id,
                "status": "SUCCEEDED",
                "result": {
                    "layerId": layer_id,
                    "layerName": layer_names[layer_id],
                    "geometryType": "point",
                    "total": 1,
                    "exceededTransferLimit": False,
                    "features": [
                        {
                            "attributes": {"OBJECTID": 1, "name": "烟测住宅", "房价": 19000},
                            "geometry": {
                                "x": 121.62,
                                "y": 38.91,
                                "spatialReference": {"wkid": 4326},
                            },
                        }
                    ],
                },
            },
        }


@pytest.mark.skipif(os.getenv("RUN_LIVE_LLM") != "1", reason="live LLM smoke test")
def test_live_gpt54_rag_graph() -> None:
    async def scenario() -> None:
        settings = Settings.from_env()
        graph = build_agent_graph(
            OpenAICompatibleChatClient(settings),
            RagEvidenceService(settings),
            NoToolCallsExpected(),
        )
        run_id = str(uuid4())
        result = await graph.ainvoke(
            {
                "run_id": run_id,
                "trace_id": "live-llm-smoke",
                "request": {
                    "conversationId": str(uuid4()),
                    "messageId": str(uuid4()),
                    "query": "大连市社区生活圈的步行指数如何计算？",
                    "context": {"locale": "zh-CN", "businessObjectIds": []},
                    "user": {"userId": "smoke", "tenantId": "smoke", "roles": ["USER"]},
                },
                "warnings": [],
                "citations": [],
            }
        )
        assert result["intent"] == "RAG_QA"
        assert result["answer"]
        assert result["citations"]
        assert all(citation["excerptAllowed"] is False for citation in result["citations"])
        assert all(citation["excerpt"] == "" for citation in result["citations"])

    import asyncio

    asyncio.run(scenario())


@pytest.mark.skipif(os.getenv("RUN_LIVE_LLM") != "1", reason="live LLM smoke test")
def test_live_gpt54_map_planner() -> None:
    async def scenario() -> None:
        settings = Settings.from_env()
        tools = FixtureMapTools()
        graph = build_agent_graph(
            OpenAICompatibleChatClient(settings),
            RagEvidenceService(settings),
            tools,
        )
        result = await graph.ainvoke(
            {
                "run_id": str(uuid4()),
                "trace_id": "live-map-smoke",
                "request": {
                    "conversationId": str(uuid4()),
                    "messageId": str(uuid4()),
                    "query": "筛选中山区房价不高于 20000 元的住宅",
                    "context": {
                        "locale": "zh-CN",
                        "map": {"visibleLayerIds": [0, 1, 2], "zoom": 13, "extent": None},
                        "businessObjectIds": [],
                    },
                    "user": {"userId": "smoke", "tenantId": "smoke", "roles": ["USER"]},
                },
                "warnings": [],
                "citations": [],
            }
        )
        assert result["intent"] == "MAP_QUERY"
        assert tools.last_call is not None
        assert tools.last_call[0] == "queryMapPoints"
        assert tools.last_call[1]["layerId"] == 2
        assert "outFields" not in tools.last_call[1]
        assert result["map_result"]["resultSets"][0]["returned"] == 1

    import asyncio

    asyncio.run(scenario())
