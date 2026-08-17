from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
import yaml

from app.agent.contracts import LangGraphRunRequest
from app.agent.conversation_state import load_business_state
from app.agent.errors import AgentError, EventHistoryExpiredError
from app.agent.rag_service import RagEvidenceService
from app.agent.runtime import AgentRuntime
from app.agent.store import AgentStore
from app.agent.workflow import (
    build_map_result,
    build_housing_search_map_result,
    concise_map_result_answer,
    compact_catalog,
    contextualize_followup_query,
    explicit_buffer_meters,
    explicit_price_max,
    is_explicit_road_map_query,
    is_frozen_point_map_query,
    is_housing_search_query,
    normalize_catalog,
    normalize_housing_search_arguments,
    normalize_user_query,
    stable_tool_call_id,
)
from app.api.app import create_app
from app.config import Settings
from app.tools.spring_client import SpringToolClient


FIXTURE_MANIFEST_PATH = Path("tests/fixtures/agent-v1.1/manifest.json")
FIXTURE_MANIFEST = json.loads(FIXTURE_MANIFEST_PATH.read_text(encoding="utf-8"))
CATALOG_VERSION = FIXTURE_MANIFEST["catalogVersion"]
POLICY_VERSION = FIXTURE_MANIFEST["housingPolicyVersion"]
CATALOG_PATH = (
    FIXTURE_MANIFEST_PATH.parent / FIXTURE_MANIFEST["files"]["catalog"]
).resolve()


class FakeLlm:
    async def complete_json(self, *, schema_name: str, **_: object) -> dict[str, object]:
        if schema_name == "agent_route":
            return {"intent": "MAP_QUERY", "reason": "地图筛选", "clarification": ""}
        if schema_name == "map_tool_plan":
            return {
                "toolCalls": [
                    {
                        "toolName": "queryMapPoints",
                        "arguments": {
                            "layerId": 2,
                            "filters": [{"field": "房价", "operator": "<=", "value": 20000}],
                            "outFields": ["name", "房价"],
                            "returnGeometry": True,
                            "resultRecordCount": 200,
                        },
                    }
                ],
                "summary": "中山区住宅房价筛选",
            }
        if schema_name == "housing_search_plan":
            return {
                "mode": "BUFFER_FILTER",
                "districts": [],
                "hardFilters": {"priceMin": None, "priceMax": 12000},
                "preferences": {
                    "price": {"enabled": False, "level": "PREFER_LOW", "weight": 0},
                    "convenience": {"enabled": False, "level": "PREFER_HIGH", "weight": 0},
                    "roadWalkability": {"enabled": True, "level": "HIGH"},
                },
                "roadCriteria": {"wsMin": None, "gviMin": None, "noiMax": None},
                "spatial": {"relation": "WITHIN_ROAD_BUFFER", "bufferMeters": None},
                "display": {"includeRoads": True, "includeBuffers": True},
                "limit": 20,
            }
        if schema_name == "grounded_answer":
            return {
                "supported": True,
                "answer": "已找到 1 个符合条件的住宅。",
                "citationOrdinals": [],
            }
        raise AssertionError(schema_name)

    async def complete_text(self, **_: object) -> str:
        return "已找到 1 个符合条件的住宅。"


class MemoryCapturingLlm(FakeLlm):
    def __init__(self) -> None:
        self.payloads: dict[str, list[dict[str, object]]] = {}

    async def complete_json(
        self, *, schema_name: str, user: str = "{}", **kwargs: object
    ) -> dict[str, object]:
        self.payloads.setdefault(schema_name, []).append(json.loads(user))
        return await super().complete_json(schema_name=schema_name, user=user, **kwargs)


class ContextAwarePlannerLlm(MemoryCapturingLlm):
    async def complete_json(
        self, *, schema_name: str, user: str = "{}", **kwargs: object
    ) -> dict[str, object]:
        payload = json.loads(user)
        self.payloads.setdefault(schema_name, []).append(payload)
        if schema_name == "map_tool_plan":
            query = str(payload["query"])
            is_road = "道路" in query
            return {
                "toolCalls": [
                    {
                        "toolName": "queryMapLines" if is_road else "queryMapPoints",
                        "arguments": {
                            "layerId": 3 if is_road else 2,
                            "filters": [],
                            "returnGeometry": True,
                            "resultRecordCount": 200,
                        },
                    }
                ],
                "summary": "中山区道路查询" if is_road else "中山区住宅查询",
            }
        return await FakeLlm.complete_json(self, schema_name=schema_name, user=user, **kwargs)


class RagRouteLlm(FakeLlm):
    async def complete_json(self, *, schema_name: str, **kwargs: object) -> dict[str, object]:
        if schema_name == "agent_route":
            return {"intent": "RAG_QA", "reason": "知识问题", "clarification": ""}
        if schema_name == "grounded_answer":
            return {
                "supported": True,
                "answer": "步行指数由设施权重及相关衰减因素计算得出[1]。",
                "citationOrdinals": [1],
            }
        return await super().complete_json(schema_name=schema_name, **kwargs)

    async def complete_text(self, **_: object) -> str:
        return "步行指数由设施权重及相关衰减因素计算得出[1]。"


class HybridLlm(FakeLlm):
    async def complete_json(self, *, schema_name: str, **kwargs: object) -> dict[str, object]:
        if schema_name == "agent_route":
            return {"intent": "HYBRID", "reason": "地图筛选和知识解释", "clarification": ""}
        if schema_name == "grounded_answer":
            return {
                "supported": True,
                "answer": "已找到住宅，并依据知识库说明相关生活圈指标[1]。",
                "citationOrdinals": [1],
            }
        return await super().complete_json(schema_name=schema_name, **kwargs)


class NoHousingPlannerLlm(FakeLlm):
    async def complete_json(self, *, schema_name: str, **kwargs: object) -> dict[str, object]:
        if schema_name == "housing_search_plan":
            raise AssertionError("common fuzzy housing queries must use deterministic planning")
        return await super().complete_json(schema_name=schema_name, **kwargs)


class MultiCallLlm(FakeLlm):
    async def complete_json(self, *, schema_name: str, **kwargs: object) -> dict[str, object]:
        if schema_name == "map_tool_plan":
            return {
                "toolCalls": [
                    {
                        "toolName": "queryMapPoints",
                        "arguments": {
                            "layerId": layer_id,
                            "filters": [{"field": "房价", "operator": "<=", "value": 20000}],
                            "returnGeometry": True,
                            "resultRecordCount": 200,
                        },
                    }
                    for layer_id in (2, 1)
                ],
                "summary": "中山区和西岗区住宅筛选",
            }
        return await super().complete_json(schema_name=schema_name, **kwargs)


class FakeRag:
    def search(self, *_: object, **__: object) -> list[object]:
        return []


class FakeTools:
    validate_arguments = staticmethod(SpringToolClient.validate_arguments)
    validate_result = staticmethod(SpringToolClient.validate_result)

    def __init__(self) -> None:
        self.catalog_data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        self.health_data = {
            "status": "READY",
            "catalogVersion": CATALOG_VERSION,
            "housingSnapshot": {"status": "READY"},
        }
        self.invoke_count = 0

    async def catalog(self, _context: object) -> dict[str, object]:
        return self.catalog_data

    async def health(self, _context: object) -> dict[str, object]:
        return {
            "success": True,
            "data": self.health_data,
        }

    async def invoke(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, object],
        _context: object,
        **_: object,
    ) -> dict[str, object]:
        self.invoke_count += 1
        self.last_call = (tool_name, tool_call_id, arguments)
        if tool_name == "searchHousingCandidates":
            return {
                "success": True,
                "data": {
                    "toolCallId": tool_call_id,
                    "status": "SUCCEEDED",
                    "durationMs": 10,
                    "result": {
                        "policyVersion": POLICY_VERSION,
                        "dataVersion": "fake-data-2026-07-29",
                        "mode": arguments["mode"],
                        "statisticsScope": {"type": "SUPPORTED_REGION", "districts": ["中山区", "西岗区", "沙河口区"]},
                        "resolvedCriteria": {
                            "priceMin": arguments["hardFilters"].get("priceMin"),
                            "priceMax": arguments["hardFilters"].get("priceMax"),
                            "bufferMeters": 100,
                            "roadWsThreshold": 50,
                            "roadWsThresholdPercentile": 75,
                            "defaultsApplied": ["BUFFER_METERS"],
                            "relaxationApplied": False,
                        },
                        "summary": {"matchedHousingCount": 1, "returnedHousingCount": 1, "matchedRoadCount": 1, "returnedRoadCount": 1},
                        "housingCandidates": [{
                            "housingId": "2:7",
                            "layerId": 2,
                            "attributes": {"OBJECTID": 7, "name": "示例住宅", "房价": 11800, "归一化总分": 84.2, "覆盖度评分": 100},
                            "geometry": {"x": 121.62, "y": 38.91, "spatialReference": {"wkid": 4326}},
                            "scores": {"priceAffordabilityPercentile": None, "conveniencePercentile": 86, "nearbyRoadWsRaw": 76, "nearbyRoadWsPercentile": 81, "recommendationScore": 83.5, "weights": {"price": 0, "convenience": 0, "roadWalkability": 1}},
                            "spatialEvidence": {"bufferMeters": 100, "nearbyRoadCount": 1, "nearestRoadDistanceMeters": 20, "contributingRoadIds": ["3:9"]},
                            "reasons": ["房价满足预算"],
                            "warnings": [],
                        }],
                        "roadFeatures": [{"roadId": "3:9", "layerId": 3, "attributes": {"name": "示例道路", "WS": 76}, "geometry": {"paths": [[[121.61, 38.90], [121.63, 38.92]]], "spatialReference": {"wkid": 4326}}}],
                        "bufferOverlays": [{"overlayId": "buf-1", "kind": "ROAD_BUFFER", "geometryType": "polygon", "spatialReference": {"wkid": 4326}, "sourceRoadIds": ["3:9"], "attributes": {"bufferMeters": 100, "sourceRoadCount": 1, "sourceRoadIdsTruncated": False}, "geometry": {"rings": [[[121.60, 38.89], [121.64, 38.89], [121.64, 38.93], [121.60, 38.89]]], "spatialReference": {"wkid": 4326}}}],
                        "warnings": [],
                    },
                },
            }
        layer_id = int(arguments["layerId"])
        layer_names = {
            0: "shahekou_1",
            1: "xigang_1",
            2: "zhongshan_1",
            3: "ZhongShan",
            4: "XiGang",
            5: "ShaHeKou",
        }
        is_road = layer_id >= 3
        return {
            "success": True,
            "data": {
                "toolCallId": tool_call_id,
                "status": "SUCCEEDED",
                "result": {
                    "layerId": layer_id,
                    "layerName": layer_names[layer_id],
                    "geometryType": "polyline" if is_road else "point",
                    "total": 1,
                    "exceededTransferLimit": False,
                    "features": [
                        {
                            "attributes": (
                                {"OBJECTID_12": 7, "name": "示例道路", "WS": 76}
                                if is_road
                                else {"OBJECTID": 7, "name": "示例住宅", "房价": 18000, "归一化总分": 76.4, "覆盖度评分": 100}
                            ),
                            "geometry": (
                                {
                                    "paths": [[[121.61, 38.90], [121.63, 38.92]]],
                                    "spatialReference": {"wkid": 4326},
                                }
                                if is_road
                                else {
                                    "x": 121.62,
                                    "y": 38.91,
                                    "spatialReference": {"wkid": 4326},
                                }
                            ),
                        }
                    ],
                },
            },
        }

    async def invoke_with_recovery(self, *args: object, **kwargs: object) -> dict[str, object]:
        return await self.invoke(*args, **kwargs)


class FailingTools(FakeTools):
    async def invoke_with_recovery(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise AgentError(
            "TOOL_TIMEOUT", "地图查询暂时超时", status_code=503, retryable=True
        )


class MismatchedLayerTools(FakeTools):
    async def invoke(self, *args: object, **kwargs: object) -> dict[str, object]:
        response = await super().invoke(*args, **kwargs)
        response["data"]["result"]["layerId"] = 0
        response["data"]["result"]["layerName"] = "shahekou_1"
        return response


class PartialFailureTools(FakeTools):
    async def invoke_with_recovery(
        self,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, object],
        context: object,
        **kwargs: object,
    ) -> dict[str, object]:
        if arguments["layerId"] == 1:
            raise AgentError(
                "TOOL_TIMEOUT", "西岗区地图查询超时", status_code=503, retryable=True
            )
        return await self.invoke(tool_name, tool_call_id, arguments, context, **kwargs)


class SlowLlm(FakeLlm):
    async def complete_json(self, *, schema_name: str, **kwargs: object) -> dict[str, object]:
        if schema_name == "agent_route":
            await asyncio.sleep(10)
        return await super().complete_json(schema_name=schema_name, **kwargs)


class AnswerFailingLlm(FakeLlm):
    async def complete_json(self, *, schema_name: str, **kwargs: object) -> dict[str, object]:
        if schema_name == "grounded_answer":
            raise AgentError(
                "MODEL_READ_TIMEOUT",
                "answer model timed out",
                status_code=503,
                retryable=True,
                details={"operation": "answer"},
            )
        return await super().complete_json(schema_name=schema_name, **kwargs)


class AnswerUnsupportedLlm(FakeLlm):
    async def complete_json(self, *, schema_name: str, **kwargs: object) -> dict[str, object]:
        if schema_name == "grounded_answer":
            return {"supported": False, "answer": "", "citationOrdinals": []}
        return await super().complete_json(schema_name=schema_name, **kwargs)


class HangingGraph:
    async def astream(self, *_: object, **__: object):
        await asyncio.sleep(10)
        if False:
            yield "updates", {}


class UnexpectedFailureTools(FakeTools):
    async def invoke_with_recovery(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("unexpected failure")


class TerminalFailureTools(FakeTools):
    async def invoke_with_recovery(
        self,
        _tool_name: str,
        tool_call_id: str,
        _arguments: dict[str, object],
        _context: object,
        **_: object,
    ) -> dict[str, object]:
        return {
            "success": True,
            "data": {
                "toolCallId": tool_call_id,
                "status": "FAILED",
                "error": {
                    "code": "GEOSCENE_QUERY_FAILED",
                    "message": "GeoScene timed out",
                    "retryable": True,
                    "details": {"layerId": 0},
                },
            },
        }


class InitializationFailureTools(FakeTools):
    async def catalog(self, _context: object) -> dict[str, object]:
        raise RuntimeError("catalog initialization failed")


def event_names(stream: str) -> list[str]:
    return [line.removeprefix("event: ") for line in stream.splitlines() if line.startswith("event: ")]


def event_data(stream: str) -> list[dict[str, object]]:
    return [json.loads(line.removeprefix("data: ")) for line in stream.splitlines() if line.startswith("data: ")]


def event_ids(stream: str) -> list[str]:
    return [line.removeprefix("id: ") for line in stream.splitlines() if line.startswith("id: ")]


def validate_openapi_events(stream: str) -> None:
    contract = yaml.safe_load(Path("docs/docs/agent-api-v1.openapi.yaml").read_text(encoding="utf-8"))
    uri = "urn:yjdl:agent-openapi"
    registry = Registry().with_resource(
        uri, Resource.from_contents(contract, default_specification=DRAFT202012)
    )
    envelope_validator = Draft202012Validator(
        {"$ref": f"{uri}#/components/schemas/SseEnvelope"}, registry=registry
    )
    payload_schemas = {
        "run.started": "RunStartedPayload",
        "route.selected": "RouteSelectedPayload",
        "retrieval.completed": "RetrievalCompletedPayload",
        "tool.started": "ToolStartedPayload",
        "tool.completed": "ToolCompletedPayload",
        "map.result": "MapResultPayload",
        "citation.added": "Citation",
        "answer.delta": "AnswerDeltaPayload",
        "run.completed": "RunCompletedPayload",
        "run.failed": "RunFailedPayload",
        "run.cancelled": "RunCancelledPayload",
    }
    names = event_names(stream)
    data = event_data(stream)
    ids = event_ids(stream)
    assert len(ids) == len(names) == len(data)
    terminal_names = {"run.completed", "run.failed", "run.cancelled"}
    assert sum(name in terminal_names for name in names) == 1
    assert names[-1] in terminal_names
    for name, envelope in zip(names, data, strict=True):
        assert envelope["schemaVersion"] == "1.1"
        envelope_validator.validate(envelope)
        Draft202012Validator(
            {"$ref": f"{uri}#/components/schemas/{payload_schemas[name]}"}, registry=registry
        ).validate(envelope["payload"])
    assert ids == [f"{item['runId']}:{item['sequence']}" for item in data]
    terminal = data[-1]
    if names[-1] == "run.completed":
        delta_text = "".join(
            item["payload"]["content"]
            for name, item in zip(names, data, strict=True)
            if name == "answer.delta"
        )
        assert delta_text == terminal["payload"]["answer"]


def request_body(query: str, *, conversation_id: str | None = None, message_id: str | None = None):
    return {
        "conversationId": conversation_id or str(uuid4()),
        "messageId": message_id or str(uuid4()),
        "query": query,
        "context": {
            "locale": "zh-CN",
            "map": {"visibleLayerIds": [0, 1, 2, 3, 4, 5], "zoom": 13, "extent": None},
            "businessObjectIds": [],
        },
        "user": {"userId": "u-1", "tenantId": "tenant-1", "roles": ["USER"]},
    }


def settings_for(path: Path) -> Settings:
    return replace(
        Settings.from_env(),
        agent_database_path=path,
        agent_checkpoint_database_path=path.with_name(f"{path.stem}-checkpoints{path.suffix}"),
        langgraph_service_token="langgraph-test-token",
        agent_tool_service_token="tool-test-token",
        agent_sse_heartbeat_seconds=1,
    )


def housing_plan() -> dict[str, object]:
    return {
        "mode": "RANK",
        "districts": [],
        "hardFilters": {"priceMin": None, "priceMax": 12000},
        "preferences": {
            "price": {"enabled": False, "level": "PREFER_LOW", "weight": 0},
            "convenience": {"enabled": True, "level": "PREFER_HIGH", "weight": None},
            "roadWalkability": {"enabled": True, "level": "PREFER_HIGH", "weight": None},
        },
        "roadCriteria": {"wsMin": None, "gviMin": None, "noiMax": None},
        "spatial": {"relation": "WITHIN_ROAD_BUFFER", "bufferMeters": None},
        "display": {"includeRoads": True, "includeBuffers": True},
        "limit": 20,
    }


class RuntimeTests(unittest.TestCase):
    def test_tool_call_id_is_stable_per_run_and_logical_call(self) -> None:
        run_id = str(uuid4())

        first = stable_tool_call_id(run_id, "searchHousingCandidates", 0)

        self.assertEqual(first, stable_tool_call_id(run_id, "searchHousingCandidates", 0))
        self.assertNotEqual(first, stable_tool_call_id(run_id, "searchHousingCandidates", 1))
        self.assertNotEqual(first, stable_tool_call_id(run_id, "queryMapPoints", 0))

    def test_v11_polygon_sse_fixture_matches_openapi(self) -> None:
        stream = Path("docs/docs/examples/agent-sse-housing-buffer.txt").read_text(
            encoding="utf-8"
        )

        validate_openapi_events(stream)
        payload = next(
            item["payload"] for item in event_data(stream) if item["payload"].get("queryId")
        )
        self.assertEqual(payload["overlays"][0]["geometryType"], "polygon")
        self.assertEqual(
            payload["display"]["layerOrder"],
            ["ROAD_BUFFER", "CONTRIBUTING_ROADS", "HOUSING_CANDIDATES"],
        )

    def test_catalog_version_mismatch_is_rejected(self) -> None:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        catalog["version"] = "2026-07-28.1"

        with self.assertRaises(AgentError) as raised:
            normalize_catalog(catalog)

        self.assertEqual(raised.exception.code, "TOOL_CATALOG_VERSION_MISMATCH")
        self.assertEqual(raised.exception.details["expectedVersion"], CATALOG_VERSION)

    def test_housing_preference_query_routes_to_joint_search(self) -> None:
        self.assertTrue(is_housing_search_query("帮我挑一套房价12000以内，便利度和周边道路步行指数高一点的房子"))
        self.assertTrue(is_housing_search_query("显示步行指数高一点的道路附近的小区"))
        self.assertTrue(is_housing_search_query("找高步行指数道路附近百来米以内的住房"))
        self.assertTrue(is_housing_search_query("帮我挑一套便利度高一点的房子"))
        self.assertTrue(is_housing_search_query("帮我找价格尽量低的房子"))
        self.assertTrue(is_housing_search_query("便宜点，出门路好走"))
        self.assertFalse(is_housing_search_query("筛选中山区 GVI 不低于 0.4 的道路"))

    def test_elder_colloquialisms_normalize_before_housing_planning(self) -> None:
        query, audit = normalize_user_query("中山去想找个住得方便的地方，房价一万五以内")

        self.assertEqual(query, "中山区想找个住得方便的地方，房价1.5万以内")
        self.assertTrue(audit)
        self.assertTrue(is_housing_search_query(query))
        self.assertEqual(explicit_price_max(query), 15000)

    def test_district_followup_inherits_housing_entity_only(self) -> None:
        query, audit = contextualize_followup_query(
            "我只要中山区的",
            [
                {
                    "query": "这些房子里面最便利的小区是哪个",
                    "answer": "可以继续按行政区筛选。",
                    "route": "MAP_QUERY",
                    "mapSummary": None,
                }
            ],
        )

        self.assertEqual(query, "我只要中山区的，查询对象为住宅小区")
        self.assertEqual(
            audit,
            [{"kind": "conversation_entity_inheritance", "from": "住宅", "to": "住宅"}],
        )

    def test_district_followup_inherits_road_entity_only(self) -> None:
        query, audit = contextualize_followup_query(
            "我只要中山区的",
            [{"query": "筛选步行指数高的道路", "answer": "", "mapSummary": None}],
        )

        self.assertEqual(query, "我只要中山区的，查询对象为道路")
        self.assertEqual(audit[0]["from"], "道路")

    def test_explicit_current_entity_overrides_conversation_memory(self) -> None:
        query, audit = contextualize_followup_query(
            "我只要中山区的道路",
            [{"query": "这些房子里哪个小区便利", "answer": "", "mapSummary": None}],
        )

        self.assertEqual(query, "我只要中山区的道路")
        self.assertEqual(audit, [])

    def test_district_only_query_without_memory_does_not_guess_entity(self) -> None:
        query, audit = contextualize_followup_query("我只要中山区的", [])

        self.assertEqual(query, "我只要中山区的")
        self.assertEqual(audit, [])

    def test_price_ceiling_and_living_preference_do_not_require_housing_noun(self) -> None:
        query, _ = normalize_user_query("中山区房价不超过一万五，便利一点")

        self.assertTrue(is_housing_search_query(query))
        self.assertEqual(explicit_price_max(query), 15000)

    def test_price_ceiling_accepts_frontend_square_meter_wording(self) -> None:
        self.assertEqual(explicit_price_max("房价不高于每平方米 12000 元"), 12000)
        self.assertEqual(explicit_price_max("房价不超过每平米 12000 元"), 12000)
        self.assertEqual(explicit_price_max("每平方米房价不高于 12000 元"), 12000)

    def test_concise_map_answer_counts_primary_point_results(self) -> None:
        answer = concise_map_result_answer(
            {
                "resultSets": [
                    {"role": "PRIMARY_RESULTS", "geometryType": "point", "returned": 200}
                ]
            }
        )

        self.assertEqual(
            answer,
            "查询完成：已找到 200 个符合条件的住宅点位，已显示在地图和左侧结果中。",
        )

    def test_concise_map_answer_describes_truncated_results_as_displayed_prefix(self) -> None:
        answer = concise_map_result_answer(
            {
                "resultSets": [
                    {
                        "role": "PRIMARY_RESULTS",
                        "geometryType": "point",
                        "returned": 50,
                        "total": 200,
                        "exceededTransferLimit": True,
                    }
                ]
            }
        )

        self.assertIn("当前显示前 50 个", answer)
        self.assertNotIn("已找到 50 个", answer)

    def test_concise_map_answer_handles_empty_housing_result(self) -> None:
        answer = concise_map_result_answer(
            {"queryId": "housing-tool-call", "resultSets": [], "overlays": []}
        )

        self.assertEqual(
            answer,
            "查询完成：暂未找到符合条件的候选小区，您可以在左侧调整筛选条件。",
        )

    def test_implicit_housing_preferences_use_existing_default_weight_semantics(self) -> None:
        arguments = normalize_housing_search_arguments(
            housing_plan(), query="便宜点，出门路好走"
        )

        self.assertTrue(arguments["preferences"]["price"]["enabled"])
        self.assertTrue(arguments["preferences"]["roadWalkability"]["enabled"])
        self.assertEqual(arguments["preferences"]["price"]["weight"], 0.5)
        self.assertEqual(arguments["preferences"]["roadWalkability"]["weight"], 0.5)

    def test_invalid_ten_thousand_meter_buffer_is_rejected(self) -> None:
        with self.assertRaises(AgentError) as raised:
            explicit_buffer_meters("找道路 1万米附近的小区")

        self.assertEqual(raised.exception.code, "INVALID_BUFFER_DISTANCE")

    def test_a01_a02_default_preferences_keep_hard_price_and_supported_region_scope(self) -> None:
        arguments = normalize_housing_search_arguments(
            housing_plan(),
            query="帮我挑一套房价12000以内，便利度和道路步行指数高一点的房子",
        )

        self.assertEqual(arguments["districts"], [])
        self.assertEqual(arguments["hardFilters"], {"priceMax": 12000})
        self.assertEqual(arguments["preferences"]["price"]["weight"], 0)
        self.assertNotIn("weight", arguments["preferences"]["convenience"])
        self.assertNotIn("weight", arguments["preferences"]["roadWalkability"])
        self.assertEqual(arguments["spatial"], {"relation": "WITHIN_ROAD_BUFFER"})

    def test_user_intent_overrides_planner_invented_scope_preferences_and_weights(self) -> None:
        plan = housing_plan()
        plan["districts"] = ["中山区", "西岗区", "沙河口区"]
        plan["preferences"]["price"] = {
            "enabled": True,
            "level": "PREFER_LOW",
            "weight": 0.4,
        }
        plan["preferences"]["convenience"]["weight"] = 0.35
        plan["preferences"]["roadWalkability"]["weight"] = 0.25
        plan["preferences"]["convenience"]["level"] = "HIGH"
        plan["preferences"]["roadWalkability"]["level"] = "HIGH"
        plan["display"]["includeBuffers"] = False

        arguments = normalize_housing_search_arguments(
            plan,
            query="帮我挑一套房价12000以内，便利度和周边道路步行指数高一点的房子",
        )

        self.assertEqual(arguments["districts"], [])
        self.assertEqual(arguments["hardFilters"], {"priceMax": 12000})
        self.assertFalse(arguments["preferences"]["price"]["enabled"])
        self.assertEqual(arguments["preferences"]["price"]["weight"], 0)
        self.assertNotIn("weight", arguments["preferences"]["convenience"])
        self.assertNotIn("weight", arguments["preferences"]["roadWalkability"])
        self.assertEqual(arguments["preferences"]["convenience"]["level"], "PREFER_HIGH")
        self.assertEqual(
            arguments["preferences"]["roadWalkability"]["level"], "PREFER_HIGH"
        )
        self.assertEqual(arguments["mode"], "RANK")
        self.assertEqual(
            arguments["display"], {"includeRoads": True, "includeBuffers": True}
        )
        self.assertEqual(arguments["limit"], 20)

    def test_convenience_only_query_disables_invented_road_preference(self) -> None:
        arguments = normalize_housing_search_arguments(
            housing_plan(),
            query="帮我挑一套便利度高一点的房子",
        )

        self.assertEqual(arguments["preferences"]["convenience"]["weight"], 1)
        self.assertFalse(arguments["preferences"]["roadWalkability"]["enabled"])
        self.assertEqual(arguments["preferences"]["roadWalkability"]["weight"], 0)
        self.assertEqual(arguments["hardFilters"], {})
        self.assertEqual(arguments["roadCriteria"], {})
        self.assertEqual(
            arguments["display"], {"includeRoads": False, "includeBuffers": False}
        )

    def test_explicit_housing_limit_is_preserved_and_validated(self) -> None:
        arguments = normalize_housing_search_arguments(
            housing_plan(),
            query="帮我推荐5套便利度高一点的房子",
        )
        self.assertEqual(arguments["limit"], 5)

        with self.assertRaises(AgentError) as raised:
            normalize_housing_search_arguments(
                housing_plan(),
                query="帮我推荐60套便利度高一点的房子",
            )
        self.assertEqual(raised.exception.code, "INVALID_HOUSING_SEARCH_ARGUMENT")

    def test_a04_explicit_weights_are_normalized(self) -> None:
        plan = housing_plan()
        plan["preferences"]["convenience"]["weight"] = 0.7
        plan["preferences"]["roadWalkability"]["weight"] = 0.2

        arguments = normalize_housing_search_arguments(
            plan,
            query="帮我按便利度七成、道路步行两成的相对权重挑房",
        )

        self.assertAlmostEqual(arguments["preferences"]["convenience"]["weight"], 7 / 9)
        self.assertAlmostEqual(arguments["preferences"]["roadWalkability"]["weight"], 2 / 9)
        self.assertEqual(arguments["hardFilters"], {})
        self.assertEqual(arguments["roadCriteria"], {})

    def test_only_explicit_numeric_road_criteria_survive_planner_normalization(self) -> None:
        invented = housing_plan()
        invented["roadCriteria"] = {"wsMin": 80, "gviMin": 0.4, "noiMax": 60}

        fuzzy = normalize_housing_search_arguments(
            invented,
            query="显示步行指数很高的道路附近的小区",
        )
        self.assertEqual(fuzzy["roadCriteria"], {})

        explicit = normalize_housing_search_arguments(
            invented,
            query="显示 WS 不低于 80、GVI 不低于 0.4、NOI 不高于 60 的道路附近小区",
        )
        self.assertEqual(
            explicit["roadCriteria"],
            {"wsMin": 80, "gviMin": 0.4, "noiMax": 60},
        )

        wrong_values = housing_plan()
        wrong_values["roadCriteria"] = {"wsMin": 10, "gviMin": 0.1, "noiMax": 99}
        grounded = normalize_housing_search_arguments(
            wrong_values,
            query="显示道路步行指数不低于 75、绿视率不低于 0.35、道路噪声不高于 55 的道路附近小区",
        )
        self.assertEqual(
            grounded["roadCriteria"],
            {"wsMin": 75, "gviMin": 0.35, "noiMax": 55},
        )

        with self.assertRaises(AgentError) as inverted:
            normalize_housing_search_arguments(
                invented,
                query="显示 GVI 不高于 0.4 的道路附近小区",
            )
        self.assertEqual(inverted.exception.code, "INVALID_HOUSING_SEARCH_ARGUMENT")

    def test_a05_very_high_buffer_filter_is_preserved(self) -> None:
        plan = housing_plan()
        plan["mode"] = "BUFFER_FILTER"
        plan["preferences"]["convenience"] = {
            "enabled": False,
            "level": "PREFER_HIGH",
            "weight": 0,
        }
        plan["preferences"]["roadWalkability"] = {
            "enabled": True,
            "level": "VERY_HIGH",
            "weight": None,
        }

        arguments = normalize_housing_search_arguments(
            plan,
            query="显示步行指数很高的道路附近的小区",
        )

        self.assertEqual(arguments["preferences"]["roadWalkability"]["level"], "VERY_HIGH")
        self.assertEqual(arguments["preferences"]["roadWalkability"]["weight"], 1)
        response = asyncio.run(
            FakeTools().invoke(
                "searchHousingCandidates",
                "00000000-0000-0000-0000-000000000005",
                arguments,
                object(),
            )
        )
        result = response["data"]["result"]
        result["resolvedCriteria"]["roadWsThresholdPercentile"] = 90
        payload = build_housing_search_map_result(
            "00000000-0000-0000-0000-000000000005",
            result,
        )
        self.assertIn(
            {"field": "WS", "operator": "PERCENTILE_GTE", "value": 90, "unit": "percentile"},
            payload["appliedFilters"],
        )

    def test_a06_prefer_low_does_not_generate_price_max(self) -> None:
        plan = housing_plan()
        plan["hardFilters"] = {"priceMin": None, "priceMax": None}
        plan["preferences"]["price"] = {
            "enabled": True,
            "level": "PREFER_LOW",
            "weight": None,
        }
        plan["preferences"]["convenience"]["enabled"] = False
        plan["preferences"]["roadWalkability"]["enabled"] = False

        arguments = normalize_housing_search_arguments(
            plan,
            query="帮我找价格尽量低的房子",
        )

        self.assertEqual(arguments["hardFilters"], {})
        self.assertEqual(arguments["preferences"]["price"]["weight"], 1)

    def test_a07_out_of_range_buffer_is_rejected_without_clamping(self) -> None:
        with self.assertRaises(AgentError) as raised:
            explicit_buffer_meters("显示步行指数高的道路10000米附近的小区")

        self.assertEqual(raised.exception.code, "INVALID_BUFFER_DISTANCE")
        self.assertEqual(raised.exception.details["bufferMeters"], 10000)

    def test_a08_empty_housing_keeps_roads_buffers_and_warning(self) -> None:
        arguments = normalize_housing_search_arguments(
            {
                **housing_plan(),
                "mode": "BUFFER_FILTER",
                "preferences": {
                    "price": {"enabled": False, "level": "PREFER_LOW", "weight": 0},
                    "convenience": {"enabled": False, "level": "PREFER_HIGH", "weight": 0},
                    "roadWalkability": {"enabled": True, "level": "HIGH", "weight": None},
                },
            },
            query="显示步行指数高的道路附近的小区",
        )
        response = asyncio.run(
            FakeTools().invoke(
                "searchHousingCandidates",
                "00000000-0000-0000-0000-000000000008",
                arguments,
                object(),
            )
        )
        result = response["data"]["result"]
        result["housingCandidates"] = []
        result["summary"]["matchedHousingCount"] = 0
        result["summary"]["returnedHousingCount"] = 0
        result["warnings"] = ["NO_HOUSING_IN_BUFFER"]

        payload = build_housing_search_map_result(
            "00000000-0000-0000-0000-000000000008",
            result,
        )

        self.assertEqual(
            [item["role"] for item in payload["resultSets"]],
            ["CONTRIBUTING_ROADS"],
        )
        self.assertTrue(payload["overlays"])
        self.assertIn("NO_HOUSING_IN_BUFFER", payload["warnings"])

    def test_a11_new_walk_conflicting_filters_and_disabled_road_are_rejected(self) -> None:
        with self.assertRaises(AgentError) as new_walk:
            normalize_housing_search_arguments(
                housing_plan(),
                query="用新步行代替道路WS帮我挑房",
            )
        self.assertEqual(new_walk.exception.code, "INVALID_HOUSING_SEARCH_ARGUMENT")

        conflicting = housing_plan()
        conflicting["hardFilters"] = {"priceMin": 15000, "priceMax": 12000}
        with self.assertRaises(AgentError) as price:
            normalize_housing_search_arguments(conflicting, query="帮我挑房")
        self.assertEqual(price.exception.code, "INVALID_HOUSING_SEARCH_ARGUMENT")

        disabled = housing_plan()
        disabled["mode"] = "BUFFER_FILTER"
        disabled["preferences"]["roadWalkability"]["enabled"] = False
        with self.assertRaises(AgentError) as road:
            normalize_housing_search_arguments(disabled, query="显示道路附近的小区")
        self.assertEqual(road.exception.code, "INVALID_HOUSING_SEARCH_ARGUMENT")

        unknown = normalize_housing_search_arguments(
            housing_plan(), query="帮我按便利度高一点挑房"
        )
        unknown["roadCriteria"]["airQualityIndex"] = 80
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        tool = next(item for item in catalog["tools"] if item["name"] == "searchHousingCandidates")
        with self.assertRaises(AgentError) as metric:
            SpringToolClient.validate_arguments(tool, unknown)
        self.assertEqual(metric.exception.code, "INVALID_TOOL_ARGUMENT")

    def test_housing_joint_search_emits_point_road_and_buffer_layers(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                tools = FakeTools()
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=NoHousingPlannerLlm(),
                    rag=FakeRag(),
                    tools=tools,
                )
                request = LangGraphRunRequest.model_validate(
                    request_body("显示步行指数高一点的道路附近的小区")
                )
                run, _ = await runtime.start_run(request, "trace-housing-buffer")
                stream = "".join(
                    [chunk async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1")]
                )
                payload = next(
                    item["payload"] for item in event_data(stream) if item["payload"].get("queryId")
                )
                self.assertEqual(tools.invoke_count, 1)
                self.assertEqual(tools.last_call[0], "searchHousingCandidates")
                self.assertEqual(
                    tools.last_call[2]["spatial"],
                    {"relation": "WITHIN_ROAD_BUFFER"},
                )
                self.assertEqual(
                    tools.last_call[2]["preferences"]["roadWalkability"]["weight"],
                    1,
                )
                self.assertEqual(
                    {item["role"] for item in payload["resultSets"]},
                    {"HOUSING_CANDIDATES", "CONTRIBUTING_ROADS"},
                )
                self.assertEqual(payload["overlays"][0]["kind"], "ROAD_BUFFER")
                self.assertEqual(payload["overlays"][0]["attributes"]["bufferMeters"], 100)
                self.assertIn(
                    {"field": "WS", "operator": "PERCENTILE_GTE", "value": 75, "unit": "percentile"},
                    payload["appliedFilters"],
                )
                self.assertEqual(payload["display"]["layerOrder"], ["ROAD_BUFFER", "CONTRIBUTING_ROADS", "HOUSING_CANDIDATES"])
                validate_openapi_events(stream)
                await runtime.close()

        asyncio.run(scenario())

    def test_exact_fuzzy_buyer_query_uses_one_grounded_joint_search(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                tools = FakeTools()
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=NoHousingPlannerLlm(),
                    rag=FakeRag(),
                    tools=tools,
                )
                request = LangGraphRunRequest.model_validate(
                    request_body(
                        "帮我挑一套房价12000以内，便利度和周边道路步行指数高一点的房子"
                    )
                )
                run, _ = await runtime.start_run(request, "trace-exact-fuzzy-buyer-query")
                stream = "".join(
                    [
                        chunk
                        async for chunk in runtime.stream_events(
                            run.run_id, "tenant-1", "u-1"
                        )
                    ]
                )
                await runtime.close()

                self.assertEqual(tools.invoke_count, 1)
                self.assertEqual(tools.last_call[0], "searchHousingCandidates")
                arguments = tools.last_call[2]
                self.assertEqual(arguments["mode"], "RANK")
                self.assertEqual(arguments["districts"], [])
                self.assertEqual(arguments["hardFilters"], {"priceMax": 12000})
                self.assertEqual(
                    arguments["preferences"],
                    {
                        "price": {"enabled": False, "level": "PREFER_LOW", "weight": 0},
                        "convenience": {"enabled": True, "level": "PREFER_HIGH"},
                        "roadWalkability": {"enabled": True, "level": "PREFER_HIGH"},
                    },
                )
                self.assertEqual(arguments["roadCriteria"], {})
                self.assertEqual(
                    arguments["spatial"], {"relation": "WITHIN_ROAD_BUFFER"}
                )
                self.assertEqual(
                    arguments["display"],
                    {"includeRoads": True, "includeBuffers": True},
                )
                self.assertEqual(arguments["limit"], 20)
                names = event_names(stream)
                self.assertEqual(
                    [name for name in names if name.startswith("tool.")],
                    ["tool.started", "tool.completed"],
                )
                self.assertEqual(names[-1], "run.completed")
                validate_openapi_events(stream)

        asyncio.run(scenario())

    def test_exact_frontend_housing_query_keeps_filters_and_returns_clean_answer(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                tools = FakeTools()
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=NoHousingPlannerLlm(),
                    rag=FakeRag(),
                    tools=tools,
                )
                request = LangGraphRunRequest.model_validate(
                    request_body(
                        "请使用现有住宅候选搜索工具，在中山区范围内筛选住宅，"
                        "房价不高于每平方米 12000 元，优先选择便利度较高的小区，"
                        "优先选择周边 100 米内道路步行指数较高的住宅，最多返回 20 个小区，"
                        "在地图上显示候选住宅、相关道路和道路缓冲范围。不要自行放宽条件。"
                    )
                )
                run, _ = await runtime.start_run(request, "trace-exact-frontend-housing-query")
                stream = "".join(
                    [
                        chunk
                        async for chunk in runtime.stream_events(
                            run.run_id, "tenant-1", "u-1"
                        )
                    ]
                )
                await runtime.close()

                self.assertEqual(tools.invoke_count, 1)
                self.assertEqual(tools.last_call[0], "searchHousingCandidates")
                arguments = tools.last_call[2]
                self.assertEqual(arguments["districts"], ["中山区"])
                self.assertEqual(arguments["hardFilters"], {"priceMax": 12000})
                self.assertEqual(arguments["spatial"], {"relation": "WITHIN_ROAD_BUFFER", "bufferMeters": 100})
                self.assertEqual(arguments["limit"], 20)

                data = event_data(stream)
                map_payload = next(
                    item["payload"] for item in data if item["payload"].get("queryId")
                )
                self.assertIn(
                    {"field": "房价", "operator": "<=", "value": 12000},
                    map_payload["appliedFilters"],
                )
                housing_set = next(
                    item for item in map_payload["resultSets"]
                    if item["role"] == "HOUSING_CANDIDATES"
                )
                attributes = housing_set["features"][0]["attributes"]
                for field in ("房价", "归一化总分", "覆盖度评分"):
                    self.assertIn(field, attributes)

                completed = data[-1]["payload"]
                self.assertEqual(
                    completed["answer"],
                    "查询完成：已找到 1 个符合条件的候选小区，已显示在地图和左侧结果中。"
                    "同时显示 1 条相关道路。",
                )
                self.assertLessEqual(len(completed["answer"]), 60)
                self.assertFalse(any(warning in completed["answer"] for warning in completed["warnings"]))
                validate_openapi_events(stream)

        asyncio.run(scenario())

    def test_missing_or_malformed_wgs84_geometry_is_a_contract_failure(self) -> None:
        call = {
            "toolCallId": "00000000-0000-0000-0000-000000000099",
            "toolName": "queryMapPoints",
            "arguments": {"filters": []},
        }
        output = {
            "result": {
                "layerId": 2,
                "layerName": "zhongshan_1",
                "geometryType": "point",
                "total": 1,
                "exceededTransferLimit": False,
                "features": [
                    {
                        "attributes": {"OBJECTID": 99},
                        "geometry": {"x": 121.65, "y": 38.92},
                    }
                ],
            }
        }
        with self.assertRaises(AgentError) as generic:
            build_map_result([call], [output])
        self.assertEqual(generic.exception.code, "TOOL_EXECUTION_FAILED")

        housing_response = asyncio.run(
            FakeTools().invoke(
                "searchHousingCandidates",
                "00000000-0000-0000-0000-000000000098",
                normalize_housing_search_arguments(
                    {
                        **housing_plan(),
                        "mode": "BUFFER_FILTER",
                        "preferences": {
                            "price": {"enabled": False, "level": "PREFER_LOW", "weight": 0},
                            "convenience": {"enabled": False, "level": "PREFER_HIGH", "weight": 0},
                            "roadWalkability": {"enabled": True, "level": "HIGH", "weight": None},
                        },
                    },
                    query="显示步行指数高的道路附近的小区",
                ),
                object(),
            )
        )
        housing_result = housing_response["data"]["result"]
        housing_result["bufferOverlays"][0]["geometry"]["rings"][0][-1] = [0, 0]
        with self.assertRaises(AgentError) as housing:
            build_housing_search_map_result(
                "00000000-0000-0000-0000-000000000098",
                housing_result,
            )
        self.assertEqual(housing.exception.code, "TOOL_EXECUTION_FAILED")

    def test_frozen_point_map_query_is_not_treated_as_district_ambiguity(self) -> None:
        self.assertTrue(is_frozen_point_map_query("筛选中山区房价不高于 20000 的住宅"))
        self.assertFalse(
            is_frozen_point_map_query("结合知识库说明指标，并筛选中山区住宅")
        )

    def test_explicit_road_threshold_is_map_query_but_cross_layer_is_not(self) -> None:
        self.assertTrue(is_explicit_road_map_query("筛选中山区 GVI 不低于 0.4 的道路"))
        self.assertFalse(is_explicit_road_map_query("筛选中山区 GVI 不低于 0.4 的住宅"))

    def test_catalog_compact_view_preserves_frozen_district_mapping(self) -> None:
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        compact = compact_catalog(catalog)
        point_tool = next(item for item in compact if item["name"] == "queryMapPoints")
        districts = {
            layer["layerId"]: layer["district"] for layer in point_tool["layers"]
        }
        self.assertEqual(
            {0: "沙河口区", 1: "西岗区", 2: "中山区"},
            districts,
        )

    def test_map_run_emits_contract_order_and_persists_checkpoint(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "agent.sqlite3"
                tools = FakeTools()
                runtime = AgentRuntime(
                    settings_for(database), llm=FakeLlm(), rag=FakeRag(), tools=tools
                )
                body = request_body("筛选中山区房价不高于 20000 的住宅")
                request = LangGraphRunRequest.model_validate(body)
                run, created = await runtime.start_run(request, "trace-1")
                stream = "".join(
                    [
                        chunk
                        async for chunk in runtime.stream_events(
                            run.run_id, "tenant-1", "u-1"
                        )
                    ]
                )
                self.assertTrue(created)
                self.assertEqual(
                    event_names(stream),
                    [
                        "run.started",
                        "route.selected",
                        "tool.started",
                        "tool.completed",
                        "map.result",
                        "answer.delta",
                        "run.completed",
                    ],
                )
                data = event_data(stream)
                validate_openapi_events(stream)
                self.assertEqual([item["sequence"] for item in data], list(range(1, 8)))
                map_payload = data[4]["payload"]
                self.assertEqual(map_payload["resultSets"][0]["returned"], 1)
                self.assertEqual(map_payload["resultSets"][0]["features"][0]["id"], "2:7")
                self.assertEqual(tools.invoke_count, 1)
                self.assertNotIn("outFields", tools.last_call[2])
                connection = sqlite3.connect(runtime.settings.agent_checkpoint_database_path)
                try:
                    checkpoint_count = connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
                finally:
                    connection.close()
                self.assertGreater(checkpoint_count, 0)
                business_connection = sqlite3.connect(database)
                try:
                    checkpoint_table = business_connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'checkpoints'"
                    ).fetchone()
                finally:
                    business_connection.close()
                self.assertIsNone(checkpoint_table)
                first_tool_call_id = data[2]["payload"]["toolCallId"]
                self.assertTrue(
                    runtime.store.has_tool_event(
                        run.run_id, "tool.started", first_tool_call_id
                    )
                )
                self.assertFalse(
                    runtime.store.has_tool_event(
                        run.run_id, "tool.started", str(uuid4())
                    )
                )
                await runtime.close()

        asyncio.run(scenario())

    def test_agent_store_retries_busy_writes_and_sets_busy_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "agent.sqlite3"
            store = AgentStore(
                database,
                busy_timeout_ms=37,
                write_retry_attempts=2,
                write_retry_base_delay_ms=1,
            )
            with store._connect() as connection:
                self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 37)

            request = LangGraphRunRequest.model_validate(request_body("test query"))
            original = store._create_or_attach_once
            attempts = 0

            def flaky_write(*args: object, **kwargs: object):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise sqlite3.OperationalError("database is locked")
                return original(*args, **kwargs)

            store._create_or_attach_once = flaky_write  # type: ignore[method-assign]
            run, created = store.create_or_attach(request, "trace-retry")
            self.assertTrue(created)
            self.assertEqual(run.trace_id, "trace-retry")
            self.assertEqual(attempts, 3)

    def test_conversation_memory_is_scoped_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.sqlite3")
            for index in range(14):
                store.save_conversation_memory(
                    "tenant-1",
                    "user-1",
                    "conversation-1",
                    user_query=f"query-{index}",
                    assistant_answer=f"answer-{index}",
                    route="MAP_QUERY",
                    map_summary={"returned": index},
                )

            memories = store.list_conversation_memory(
                "tenant-1", "user-1", "conversation-1"
            )
            self.assertEqual(len(memories), 12)
            self.assertEqual(memories[0].user_query, "query-2")
            self.assertEqual(memories[-1].map_summary, {"returned": 13})
            self.assertEqual(
                store.list_conversation_memory("tenant-1", "other-user", "conversation-1"),
                [],
            )
            self.assertEqual(
                store.list_conversation_memory("other-tenant", "user-1", "conversation-1"),
                [],
            )

    def test_second_run_inherits_housing_entity_and_preference(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "agent.sqlite3"
                llm = ContextAwarePlannerLlm()
                tools = FakeTools()
                runtime = AgentRuntime(
                    settings_for(database), llm=llm, rag=FakeRag(), tools=tools
                )
                try:
                    conversation_id = str(uuid4())
                    first_request = LangGraphRunRequest.model_validate(
                        request_body(
                            "这些房子里面最便利的小区是哪个",
                            conversation_id=conversation_id,
                        )
                    )
                    first_run, _ = await runtime.start_run(first_request, "trace-memory-first")
                    first_stream = "".join(
                        [
                            chunk
                            async for chunk in runtime.stream_events(
                                first_run.run_id, "tenant-1", "u-1"
                            )
                        ]
                    )
                    validate_openapi_events(first_stream)

                    second_request = LangGraphRunRequest.model_validate(
                        request_body("我只要中山区的", conversation_id=conversation_id)
                    )
                    second_run, _ = await runtime.start_run(
                        second_request, "trace-memory-second"
                    )
                    second_stream = "".join(
                        [
                            chunk
                            async for chunk in runtime.stream_events(
                                second_run.run_id, "tenant-1", "u-1"
                            )
                        ]
                    )
                    validate_openapi_events(second_stream)

                    memories = runtime.store.list_conversation_memory(
                        "tenant-1", "u-1", conversation_id
                    )
                    self.assertEqual(len(memories), 2)
                    self.assertEqual(memories[0].user_query, "这些房子里面最便利的小区是哪个")
                    self.assertIn("resultCounts", memories[0].map_summary)
                    self.assertEqual(tools.last_call[0], "searchHousingCandidates")
                    self.assertEqual(tools.last_call[2]["districts"], ["中山区"])
                    self.assertTrue(
                        tools.last_call[2]["preferences"]["convenience"]["enabled"]
                    )
                finally:
                    await runtime.close()

        asyncio.run(scenario())

    def test_real_user_followup_inherits_hard_filters_and_adds_convenience(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=FakeLlm(),
                    rag=FakeRag(),
                    tools=FakeTools(),
                )
                try:
                    conversation_id = str(uuid4())
                    first_request = LangGraphRunRequest.model_validate(
                        request_body(
                            "筛选中山区房价不高于 20000 的住宅",
                            conversation_id=conversation_id,
                        )
                    )
                    first, _ = await runtime.start_run(first_request, "trace-real-first")
                    first_stream = "".join(
                        [
                            chunk
                            async for chunk in runtime.stream_events(
                                first.run_id, "tenant-1", "u-1"
                            )
                        ]
                    )
                    validate_openapi_events(first_stream)
                    snapshot = await runtime._graph.aget_state(
                        {
                            "configurable": {
                                "thread_id": f"agent-v2:{conversation_id}",
                            }
                        }
                    )
                    self.assertNotIn("map_result", snapshot.values)
                    self.assertNotIn("tool_outputs", snapshot.values)
                    self.assertIn("map_summary", snapshot.values)
                    self.assertLess(
                        len(json.dumps(snapshot.values["map_summary"], ensure_ascii=False)),
                        5000,
                    )

                    committed = runtime.store.get_conversation_state(
                        "tenant-1", "u-1", conversation_id
                    )
                    business_state = load_business_state(committed)
                    self.assertEqual(business_state.entity_context.entity_type, "HOUSING")
                    self.assertEqual(business_state.query_context.hard_filters["priceMax"], 20000)

                    second_request = LangGraphRunRequest.model_validate(
                        request_body(
                            "从这里面选社区便利度稍微高一些的",
                            conversation_id=conversation_id,
                        )
                    )
                    second, _ = await runtime.start_run(second_request, "trace-real-second")
                    second_stream = "".join(
                        [
                            chunk
                            async for chunk in runtime.stream_events(
                                second.run_id, "tenant-1", "u-1"
                            )
                        ]
                    )
                    validate_openapi_events(second_stream)

                    self.assertEqual(runtime.tools.last_call[0], "searchHousingCandidates")
                    arguments = runtime.tools.last_call[2]
                    self.assertEqual(arguments["districts"], ["中山区"])
                    self.assertEqual(arguments["hardFilters"]["priceMax"], 20000)
                    self.assertTrue(arguments["preferences"]["convenience"]["enabled"])
                    self.assertEqual(
                        arguments["preferences"]["convenience"]["level"], "PREFER_HIGH"
                    )
                    route = next(
                        event.data["payload"]["intent"]
                        for event in runtime.store.list_events(second.run_id, 0)
                        if event.event_name == "route.selected"
                    )
                    self.assertEqual(route, "MAP_QUERY")
                finally:
                    await runtime.close()

        asyncio.run(scenario())

    def test_conversation_requests_are_deterministic_and_preserve_business_state(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                llm = MemoryCapturingLlm()
                tools = FakeTools()
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=llm,
                    rag=FakeRag(),
                    tools=tools,
                )
                try:
                    conversation_id = str(uuid4())
                    first_request = LangGraphRunRequest.model_validate(
                        request_body(
                            "筛选中山区房价不高于 20000 的住宅",
                            conversation_id=conversation_id,
                        )
                    )
                    first, _ = await runtime.start_run(first_request, "trace-state-first")
                    _ = "".join(
                        [
                            chunk
                            async for chunk in runtime.stream_events(
                                first.run_id, "tenant-1", "u-1"
                            )
                        ]
                    )
                    model_calls_before = sum(len(items) for items in llm.payloads.values())
                    tool_calls_before = tools.invoke_count

                    for index, query in enumerate(("我们都说了什么，总结一下", "干得不错")):
                        request = LangGraphRunRequest.model_validate(
                            request_body(query, conversation_id=conversation_id)
                        )
                        run, _ = await runtime.start_run(request, f"trace-conversation-{index}")
                        stream = "".join(
                            [
                                chunk
                                async for chunk in runtime.stream_events(
                                    run.run_id, "tenant-1", "u-1"
                                )
                            ]
                        )
                        route = next(
                            event.data["payload"]["intent"]
                            for event in runtime.store.list_events(run.run_id, 0)
                            if event.event_name == "route.selected"
                        )
                        self.assertEqual(route, "CONVERSATION")

                    self.assertEqual(sum(len(items) for items in llm.payloads.values()), model_calls_before)
                    self.assertEqual(tools.invoke_count, tool_calls_before)
                    committed = load_business_state(
                        runtime.store.get_conversation_state(
                            "tenant-1", "u-1", conversation_id
                        )
                    )
                    self.assertEqual(committed.query_context.hard_filters["priceMax"], 20000)
                    self.assertEqual(
                        committed.query_context.last_successful_query,
                        "筛选中山区房价不高于 20000 的住宅",
                    )
                finally:
                    await runtime.close()

        asyncio.run(scenario())

    def test_road_followup_and_current_override_select_line_layer(self) -> None:
        async def run_case(history_query: str, current_query: str) -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "agent.sqlite3"
                llm = ContextAwarePlannerLlm()
                tools = FakeTools()
                runtime = AgentRuntime(
                    settings_for(database), llm=llm, rag=FakeRag(), tools=tools
                )
                conversation_id = str(uuid4())
                runtime.store.save_conversation_memory(
                    "tenant-1",
                    "u-1",
                    conversation_id,
                    user_query=history_query,
                    assistant_answer="已完成上一轮查询。",
                    route="MAP_QUERY",
                    map_summary=None,
                )
                request = LangGraphRunRequest.model_validate(
                    request_body(current_query, conversation_id=conversation_id)
                )
                run, _ = await runtime.start_run(request, "trace-road-followup")
                stream = "".join(
                    [
                        chunk
                        async for chunk in runtime.stream_events(
                            run.run_id, "tenant-1", "u-1"
                        )
                    ]
                )
                validate_openapi_events(stream)
                self.assertEqual(tools.last_call[0], "queryMapLines")
                self.assertEqual(tools.last_call[2]["layerId"], 3)
                await runtime.close()

        async def scenario() -> None:
            await run_case("筛选步行指数高的道路", "我只要中山区的")
            await run_case("这些房子里哪个小区便利", "我只要中山区的道路")

        asyncio.run(scenario())

    def test_checkpoint_initialization_error_is_traced_and_structured(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=FakeLlm(),
                    rag=FakeRag(),
                    tools=FakeTools(),
                )
                with patch(
                    "app.agent.runtime.aiosqlite.connect",
                    new=AsyncMock(side_effect=sqlite3.OperationalError("database is locked")),
                ):
                    with self.assertLogs("app.agent.runtime", level="ERROR") as logs:
                        with self.assertRaises(AgentError) as raised:
                            await runtime.initialize("trace-init-failure")
                self.assertEqual(raised.exception.code, "AGENT_DATABASE_UNAVAILABLE")
                self.assertTrue(raised.exception.retryable)
                output = "\n".join(logs.output)
                self.assertIn("traceId=trace-init-failure", output)
                self.assertIn("Traceback", output)
                await runtime.close()

        asyncio.run(scenario())

    def test_legacy_checkpoint_tables_are_migrated_out_of_run_database(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "agent.sqlite3"
                settings = settings_for(database)
                connection = sqlite3.connect(database)
                try:
                    connection.executescript(
                        """
                        CREATE TABLE checkpoints (
                            thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
                            checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT,
                            type TEXT, checkpoint BLOB, metadata BLOB,
                            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
                        );
                        CREATE TABLE writes (
                            thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
                            checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, idx INTEGER NOT NULL,
                            channel TEXT NOT NULL, type TEXT, value BLOB,
                            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                        );
                        INSERT INTO checkpoints VALUES ('thread-1', '', 'checkpoint-1', NULL, 'json', X'01', X'02');
                        INSERT INTO writes VALUES ('thread-1', '', 'checkpoint-1', 'task-1', 0, 'value', 'json', X'03');
                        """
                    )
                    connection.commit()
                finally:
                    connection.close()

                runtime = AgentRuntime(
                    settings,
                    llm=FakeLlm(),
                    rag=FakeRag(),
                    tools=FakeTools(),
                )
                await runtime.initialize("trace-migration")

                source = sqlite3.connect(database)
                target = sqlite3.connect(settings.agent_checkpoint_database_path)
                try:
                    source_tables = {
                        row[0]
                        for row in source.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        )
                    }
                    self.assertNotIn("checkpoints", source_tables)
                    self.assertNotIn("writes", source_tables)
                    self.assertEqual(target.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 1)
                    self.assertEqual(target.execute("SELECT COUNT(*) FROM writes").fetchone()[0], 1)
                finally:
                    source.close()
                    target.close()
                await runtime.close()

        asyncio.run(scenario())

    def test_non_sqlite_initialization_error_is_traced_and_structured(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=FakeLlm(),
                    rag=FakeRag(),
                    tools=InitializationFailureTools(),
                )
                with self.assertLogs("app.agent.runtime", level="ERROR") as logs:
                    with self.assertRaises(AgentError) as raised:
                        await runtime.initialize("trace-generic-init-failure")
                self.assertEqual(raised.exception.code, "AGENT_INITIALIZATION_FAILED")
                self.assertTrue(raised.exception.retryable)
                output = "\n".join(logs.output)
                self.assertIn("traceId=trace-generic-init-failure", output)
                self.assertIn("Traceback", output)
                await runtime.close()

        asyncio.run(scenario())

    def test_cross_layer_request_is_clarified_without_tool_or_llm_route(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                tools = FakeTools()
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=FakeLlm(),
                    rag=FakeRag(),
                    tools=tools,
                )
                request = LangGraphRunRequest.model_validate(
                    request_body("筛选中山区绿视率高、步行指数高的住宅")
                )
                run, _ = await runtime.start_run(request, "trace-2")
                stream = "".join(
                    [chunk async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1")]
                )
                self.assertEqual(
                    event_names(stream),
                    ["run.started", "route.selected", "answer.delta", "run.completed"],
                )
                self.assertEqual(event_data(stream)[1]["payload"]["intent"], "CLARIFY")
                validate_openapi_events(stream)
                self.assertEqual(tools.invoke_count, 0)
                await runtime.close()

        asyncio.run(scenario())

    def test_rag_citations_never_include_excerpt(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                settings = settings_for(Path(directory) / "agent.sqlite3")
                runtime = AgentRuntime(
                    settings,
                    llm=RagRouteLlm(),
                    rag=RagEvidenceService(settings),
                    tools=FakeTools(),
                )
                request = LangGraphRunRequest.model_validate(request_body("步行指数如何计算？"))
                run, _ = await runtime.start_run(request, "trace-3")
                stream = "".join(
                    [chunk async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1")]
                )
                names = event_names(stream)
                validate_openapi_events(stream)
                self.assertIn("retrieval.completed", names)
                self.assertIn("citation.added", names)
                completed = event_data(stream)[-1]["payload"]
                self.assertTrue(completed["citations"])
                self.assertTrue(all(item["excerpt"] == "" for item in completed["citations"]))
                self.assertTrue(all(item["excerptAllowed"] is False for item in completed["citations"]))
                await runtime.close()

        asyncio.run(scenario())

    def test_hybrid_path_emits_rag_before_map_and_keeps_citations(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                settings = settings_for(Path(directory) / "agent.sqlite3")
                runtime = AgentRuntime(
                    settings,
                    llm=HybridLlm(),
                    rag=RagEvidenceService(settings),
                    tools=FakeTools(),
                )
                request = LangGraphRunRequest.model_validate(
                    request_body("结合知识库说明生活圈指标，并筛选中山区房价不高于 20000 的住宅")
                )
                run, _ = await runtime.start_run(request, "trace-hybrid")
                stream = "".join(
                    [chunk async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1")]
                )
                names = event_names(stream)
                self.assertEqual(event_data(stream)[1]["payload"]["intent"], "HYBRID")
                self.assertLess(names.index("retrieval.completed"), names.index("tool.started"))
                self.assertLess(names.index("citation.added"), names.index("map.result"))
                self.assertTrue(event_data(stream)[-1]["payload"]["citations"])
                validate_openapi_events(stream)
                await runtime.close()

        asyncio.run(scenario())

    def test_tool_failure_has_completed_event_before_run_failure(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=FakeLlm(),
                    rag=FakeRag(),
                    tools=FailingTools(),
                )
                request = LangGraphRunRequest.model_validate(
                    request_body("筛选中山区房价不高于 20000 的住宅")
                )
                run, _ = await runtime.start_run(request, "trace-failure")
                stream = "".join(
                    [chunk async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1")]
                )
                self.assertEqual(
                    event_names(stream),
                    [
                        "run.started",
                        "route.selected",
                        "tool.started",
                        "tool.completed",
                        "run.failed",
                    ],
                )
                self.assertEqual(event_data(stream)[-1]["payload"]["error"]["code"], "TOOL_TIMEOUT")
                validate_openapi_events(stream)
                await runtime.close()

        asyncio.run(scenario())

    def test_a07_a11_contract_errors_emit_one_stable_failed_terminal(self) -> None:
        async def scenario() -> None:
            cases = (
                (
                    "显示步行指数高的道路10000米附近的小区",
                    "INVALID_BUFFER_DISTANCE",
                ),
                (
                    "用新步行代替道路WS帮我挑房",
                    "INVALID_HOUSING_SEARCH_ARGUMENT",
                ),
            )
            with tempfile.TemporaryDirectory() as directory:
                tools = FakeTools()
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=NoHousingPlannerLlm(),
                    rag=FakeRag(),
                    tools=tools,
                )
                try:
                    for query, expected_code in cases:
                        with self.subTest(query=query):
                            request = LangGraphRunRequest.model_validate(request_body(query))
                            run, _ = await runtime.start_run(
                                request, f"trace-{expected_code.lower()}"
                            )
                            stream = "".join(
                                [
                                    chunk
                                    async for chunk in runtime.stream_events(
                                        run.run_id, "tenant-1", "u-1"
                                    )
                                ]
                            )
                            names = event_names(stream)
                            terminals = [
                                name
                                for name in names
                                if name in {"run.completed", "run.failed", "run.cancelled"}
                            ]
                            self.assertEqual(terminals, ["run.failed"], query)
                            self.assertNotIn("tool.started", names)
                            self.assertEqual(
                                event_data(stream)[-1]["payload"]["error"]["code"],
                                expected_code,
                            )
                            validate_openapi_events(stream)
                    self.assertEqual(tools.invoke_count, 0)
                finally:
                    await runtime.close()

        asyncio.run(scenario())

    def test_unexpected_tool_failure_closes_pending_tool_event(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=FakeLlm(),
                    rag=FakeRag(),
                    tools=UnexpectedFailureTools(),
                )
                request = LangGraphRunRequest.model_validate(
                    request_body("筛选中山区房价不高于 20000 的住宅")
                )
                run, _ = await runtime.start_run(request, "trace-unexpected")
                stream = "".join(
                    [chunk async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1")]
                )
                self.assertEqual(
                    event_names(stream)[-3:],
                    ["tool.started", "tool.completed", "run.failed"],
                )
                self.assertEqual(runtime.store.pending_tool_calls(run.run_id), [])
                validate_openapi_events(stream)
                await runtime.close()

        asyncio.run(scenario())

    def test_terminal_tool_execution_preserves_backend_error(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=FakeLlm(),
                    rag=FakeRag(),
                    tools=TerminalFailureTools(),
                )
                request = LangGraphRunRequest.model_validate(
                    request_body("筛选中山区房价不高于 20000 的住宅")
                )
                run, _ = await runtime.start_run(request, "trace-terminal-tool-failure")
                stream = "".join(
                    [chunk async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1")]
                )

                error = event_data(stream)[-1]["payload"]["error"]
                self.assertEqual(error["code"], "GEOSCENE_QUERY_FAILED")
                self.assertTrue(error["retryable"])
                self.assertEqual(error["details"], {"layerId": 0})
                validate_openapi_events(stream)
                await runtime.close()

        asyncio.run(scenario())

    def test_mismatched_tool_layer_never_reaches_map_result(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=FakeLlm(),
                    rag=FakeRag(),
                    tools=MismatchedLayerTools(),
                )
                request = LangGraphRunRequest.model_validate(
                    request_body("筛选中山区房价不高于 20000 的住宅")
                )
                run, _ = await runtime.start_run(request, "trace-mismatch")
                stream = "".join(
                    [chunk async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1")]
                )
                self.assertNotIn("map.result", event_names(stream))
                self.assertEqual(event_names(stream)[-2:], ["tool.completed", "run.failed"])
                await runtime.close()

        asyncio.run(scenario())

    def test_partial_tool_failure_keeps_successful_map_result(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=MultiCallLlm(),
                    rag=FakeRag(),
                    tools=PartialFailureTools(),
                )
                request = LangGraphRunRequest.model_validate(
                    request_body("筛选中山区和西岗区房价不高于 20000 的住宅")
                )
                run, _ = await runtime.start_run(request, "trace-partial")
                stream = "".join(
                    [chunk async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1")]
                )
                names = event_names(stream)
                self.assertEqual(names.count("tool.started"), 2)
                self.assertEqual(names.count("tool.completed"), 2)
                self.assertIn("map.result", names)
                self.assertEqual(names[-1], "run.completed")
                map_payload = next(
                    item["payload"]
                    for item in event_data(stream)
                    if item["payload"].get("queryId")
                )
                self.assertEqual(len(map_payload["resultSets"]), 1)
                self.assertTrue(map_payload["warnings"])
                validate_openapi_events(stream)
                await runtime.close()

        asyncio.run(scenario())

    def test_cancel_is_idempotent_and_preserves_reason(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=SlowLlm(),
                    rag=FakeRag(),
                    tools=FakeTools(),
                )
                request = LangGraphRunRequest.model_validate(request_body("筛选中山区住宅"))
                run, _ = await runtime.start_run(request, "trace-cancel")
                for _ in range(50):
                    if runtime.store.get_run(run.run_id, "tenant-1", "u-1").last_sequence:
                        break
                    await asyncio.sleep(0.01)
                first = await runtime.cancel_run(
                    run.run_id, "tenant-1", "u-1", "user_requested"
                )
                second = await runtime.cancel_run(
                    run.run_id, "tenant-1", "u-1", "duplicate"
                )
                self.assertEqual(first.status, "CANCELLED")
                self.assertEqual(second.last_sequence, first.last_sequence)
                events = runtime.store.list_events(run.run_id, 0)
                self.assertEqual(events[-1].event_name, "run.cancelled")
                self.assertEqual(events[-1].data["payload"]["reason"], "user_requested")
                await runtime.close()

        asyncio.run(scenario())

    def test_heartbeat_does_not_consume_sequence(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = AgentRuntime(
                    replace(
                        settings_for(Path(directory) / "agent.sqlite3"),
                        agent_sse_heartbeat_seconds=0.2,
                    ),
                    llm=SlowLlm(),
                    rag=FakeRag(),
                    tools=FakeTools(),
                )
                request = LangGraphRunRequest.model_validate(request_body("筛选中山区住宅"))
                run, _ = await runtime.start_run(request, "trace-heartbeat")
                chunks: list[str] = []

                async def consume() -> None:
                    async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1"):
                        chunks.append(chunk)

                consumer = asyncio.create_task(consume())
                await asyncio.sleep(0.6)
                await runtime.cancel_run(run.run_id, "tenant-1", "u-1", "heartbeat_test")
                await consumer
                stream = "".join(chunks)
                self.assertIn(": heartbeat\n\n", stream)
                data = event_data(stream)
                self.assertEqual(
                    [item["sequence"] for item in data], list(range(1, len(data) + 1))
                )
                await runtime.close()

        asyncio.run(scenario())

    def test_run_timeout_emits_contract_failure(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                settings = replace(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    agent_max_run_seconds=1,
                )
                runtime = AgentRuntime(
                    settings,
                    llm=FakeLlm(),
                    rag=FakeRag(),
                    tools=FakeTools(),
                )
                runtime._graph = HangingGraph()
                request = LangGraphRunRequest.model_validate(
                    request_body("请帮我查看地图数据")
                )
                run, _ = await runtime.start_run(request, "trace-run-timeout")
                stream = "".join(
                    [
                        chunk
                        async for chunk in runtime.stream_events(
                            run.run_id, "tenant-1", "u-1"
                        )
                    ]
                )
                self.assertEqual(event_names(stream)[-1], "run.failed")
                error = event_data(stream)[-1]["payload"]["error"]
                self.assertEqual(error["code"], "RUN_TIMEOUT")
                self.assertTrue(error["retryable"])
                validate_openapi_events(stream)
                await runtime.close()

        asyncio.run(scenario())

    def test_fully_expired_history_is_rejected(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "agent.sqlite3"
                settings = replace(settings_for(database), agent_event_retention_seconds=1)
                runtime = AgentRuntime(settings, llm=FakeLlm(), rag=FakeRag(), tools=FakeTools())
                request = LangGraphRunRequest.model_validate(
                    request_body("筛选中山区房价不高于 20000 的住宅")
                )
                run, _ = await runtime.start_run(request, "trace-expired")
                _ = [chunk async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1")]
                connection = sqlite3.connect(database)
                try:
                    connection.execute(
                        "UPDATE agent_events SET created_at = '2000-01-01T00:00:00Z' WHERE run_id = ?",
                        (run.run_id,),
                    )
                    connection.commit()
                finally:
                    connection.close()
                with self.assertRaises(EventHistoryExpiredError):
                    runtime.store.list_events(run.run_id, 0)
                await runtime.close()

        asyncio.run(scenario())

    def test_running_checkpoint_is_completed_after_runtime_restart(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "agent.sqlite3"
                settings = replace(
                    settings_for(database),
                    agent_worker_lease_seconds=1,
                    agent_worker_poll_seconds=0.01,
                )
                request = LangGraphRunRequest.model_validate(
                    request_body("筛选中山区房价不高于 20000 的住宅")
                )
                first_runtime = AgentRuntime(
                    settings, llm=FakeLlm(), rag=FakeRag(), tools=FakeTools()
                )
                await first_runtime.initialize()
                run, _ = first_runtime.store.create_or_attach(request, "trace-restart")
                claimed = first_runtime.store.claim_next_run(
                    first_runtime.worker_id,
                    lease_seconds=settings.agent_worker_lease_seconds,
                )
                self.assertIsNotNone(claimed)
                initial_state = {
                    "request": run.request,
                    "run_id": run.run_id,
                    "trace_id": run.trace_id,
                    "warnings": [],
                    "citations": [],
                    "catalog": first_runtime._catalog,
                }
                await first_runtime._graph.ainvoke(
                    initial_state,
                    config={"configurable": {"thread_id": run.run_id}},
                    durability="sync",
                )
                self.assertEqual(
                    first_runtime.store.get_run_unscoped(run.run_id).status, "RUNNING"
                )
                await first_runtime.close()
                await asyncio.sleep(1.05)

                second_runtime = AgentRuntime(
                    settings, llm=FakeLlm(), rag=FakeRag(), tools=FakeTools()
                )
                try:
                    await second_runtime.initialize()
                    for _ in range(300):
                        recovered = second_runtime.store.get_run_unscoped(run.run_id)
                        if recovered.terminal:
                            break
                        await asyncio.sleep(0.01)
                    self.assertEqual(recovered.status, "SUCCEEDED")
                    events = second_runtime.store.list_events(run.run_id, 0)
                    self.assertEqual(events[0].event_name, "run.started")
                    self.assertEqual(events[-1].event_name, "run.completed")
                finally:
                    await second_runtime.close()

        asyncio.run(scenario())


    def test_answer_failure_after_map_result_completes_with_warning_and_trace(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=AnswerFailingLlm(),
                    rag=FakeRag(),
                    tools=FakeTools(),
                )
                request = LangGraphRunRequest.model_validate(
                    request_body("筛选中山区房价不高于 20000 的住宅")
                )
                run, _ = await runtime.start_run(request, "trace-answer-fallback")
                stream = "".join(
                    [chunk async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1")]
                )
                completed = event_data(stream)[-1]["payload"]
                diagnostics = runtime.store.diagnostics(run.run_id, "tenant-1", "u-1")

                self.assertEqual(event_names(stream)[-1], "run.completed")
                self.assertNotIn("ANSWER_GENERATION_DEGRADED", completed["warnings"])
                self.assertIn("住宅点位", completed["answer"])
                self.assertEqual(diagnostics["orchestration"]["status"], "SUCCEEDED")
                self.assertFalse(
                    any(
                        item["stageName"] == "ANSWER_GENERATION"
                        and item.get("errorCode") == "MODEL_READ_TIMEOUT"
                        for item in diagnostics["stages"]
                    )
                )
                self.assertTrue(diagnostics["decisionAudit"])
                await runtime.close()

        asyncio.run(scenario())

    def test_unsupported_answer_never_disclaims_a_valid_map_result(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory() as directory:
                runtime = AgentRuntime(
                    settings_for(Path(directory) / "agent.sqlite3"),
                    llm=AnswerUnsupportedLlm(),
                    rag=FakeRag(),
                    tools=FakeTools(),
                )
                request = LangGraphRunRequest.model_validate(
                    request_body("筛选中山区房价不高于 20000 的住宅")
                )
                run, _ = await runtime.start_run(request, "trace-answer-unsupported")
                stream = "".join(
                    [chunk async for chunk in runtime.stream_events(run.run_id, "tenant-1", "u-1")]
                )
                completed = event_data(stream)[-1]["payload"]

                self.assertEqual(event_names(stream)[-1], "run.completed")
                self.assertNotIn("ANSWER_GENERATION_DEGRADED", completed["warnings"])
                self.assertIn("查询完成", completed["answer"])
                self.assertNotIn("不足以支持", completed["answer"])
                await runtime.close()

        asyncio.run(scenario())


class ApiContractTests(unittest.TestCase):
    def test_readiness_rejects_catalog_version_drift_without_replacing_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tools = FakeTools()
            runtime = AgentRuntime(
                settings_for(Path(directory) / "agent.sqlite3"),
                llm=FakeLlm(),
                rag=FakeRag(),
                tools=tools,
            )
            with TestClient(create_app(runtime)) as client:
                self.assertEqual(client.get("/readyz").status_code, 200)
                tools.catalog_data["version"] = "2026-07-28.1"

                response = client.get("/readyz")

                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    response.json()["reason"],
                    "TOOL_CATALOG_VERSION_MISMATCH",
                )
                self.assertEqual(runtime._catalog["version"], CATALOG_VERSION)

    def test_readiness_uses_current_housing_snapshot_without_ready_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tools = FakeTools()
            runtime = AgentRuntime(
                settings_for(Path(directory) / "agent.sqlite3"),
                llm=FakeLlm(),
                rag=FakeRag(),
                tools=tools,
            )
            with TestClient(create_app(runtime)) as client:
                self.assertEqual(client.get("/readyz").status_code, 200)
                for status in ("WARMING", "DEGRADED", "STALE"):
                    with self.subTest(status=status):
                        tools.health_data["housingSnapshot"] = {"status": status}
                        response = client.get("/readyz")
                        self.assertEqual(response.status_code, 503)
                        self.assertEqual(
                            response.json()["toolHealth"]["housingSnapshot"]["status"],
                            status,
                        )
                tools.health_data["housingSnapshot"] = {"status": "READY"}
                self.assertEqual(client.get("/readyz").status_code, 200)

    def test_readiness_rejects_health_catalog_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tools = FakeTools()
            tools.health_data["catalogVersion"] = "2026-07-28.1"
            runtime = AgentRuntime(
                settings_for(Path(directory) / "agent.sqlite3"),
                llm=FakeLlm(),
                rag=FakeRag(),
                tools=tools,
            )
            with TestClient(create_app(runtime)) as client:
                response = client.get("/readyz")

            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["reason"], "TOOL_CATALOG_VERSION_MISMATCH")
            self.assertEqual(response.json()["toolHealth"]["catalogVersion"], "2026-07-28.1")

    def test_run_creation_database_failure_preserves_trace_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(
                settings_for(Path(directory) / "agent.sqlite3"),
                llm=FakeLlm(),
                rag=FakeRag(),
                tools=FakeTools(),
            )

            def fail_create(*_: object, **__: object):
                raise sqlite3.OperationalError("database is locked")

            runtime.store.create_or_attach = fail_create  # type: ignore[method-assign]
            headers = {
                "Authorization": "Bearer langgraph-test-token",
                "X-Trace-Id": "trace-database-failure",
                "X-Tenant-Id": "tenant-1",
                "X-User-Id": "u-1",
            }
            with TestClient(create_app(runtime)) as client:
                response = client.post(
                    "/api/v1/runs",
                    headers=headers,
                    json=request_body("test query"),
                )

            self.assertEqual(response.status_code, 503)
            payload = response.json()
            self.assertFalse(payload["success"])
            self.assertEqual(payload["error"]["code"], "AGENT_DATABASE_UNAVAILABLE")
            self.assertTrue(payload["error"]["retryable"])
            self.assertEqual(payload["traceId"], "trace-database-failure")
            self.assertEqual(response.headers["X-Trace-Id"], "trace-database-failure")

    def test_stream_auth_identity_idempotency_and_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tools = FakeTools()
            runtime = AgentRuntime(
                settings_for(Path(directory) / "agent.sqlite3"),
                llm=FakeLlm(),
                rag=FakeRag(),
                tools=tools,
            )
            headers = {
                "Authorization": "Bearer langgraph-test-token",
                "X-Trace-Id": "trace-api",
                "X-Tenant-Id": "tenant-1",
                "X-User-Id": "u-1",
            }
            body = request_body("筛选中山区房价不高于 20000 的住宅")
            with TestClient(create_app(runtime)) as client:
                unauthorized = client.post("/api/v1/runs/stream", json=body)
                self.assertEqual(unauthorized.status_code, 401)

                mismatch = client.post(
                    "/api/v1/runs/stream",
                    headers={**headers, "X-User-Id": "other"},
                    json=body,
                )
                self.assertEqual(mismatch.status_code, 403)
                self.assertEqual(mismatch.json()["error"]["code"], "IDENTITY_CONTEXT_MISMATCH")

                first = client.post("/api/v1/runs/stream", headers=headers, json=body)
                self.assertEqual(first.status_code, 200)
                run_id = first.headers["X-Run-Id"]
                self.assertEqual(event_names(first.text)[-1], "run.completed")
                first_tool_call_ids = [
                    item["payload"]["toolCallId"]
                    for name, item in zip(event_names(first.text), event_data(first.text), strict=True)
                    if name == "tool.started"
                ]

                second = client.post("/api/v1/runs/stream", headers=headers, json=body)
                self.assertEqual(second.headers["X-Run-Id"], run_id)
                self.assertEqual(tools.invoke_count, 1)
                second_tool_call_ids = [
                    item["payload"]["toolCallId"]
                    for name, item in zip(event_names(second.text), event_data(second.text), strict=True)
                    if name == "tool.started"
                ]
                self.assertEqual(second_tool_call_ids, first_tool_call_ids)
                diagnostics = client.get(
                    f"/api/v1/runs/{run_id}/diagnostics", headers=headers
                )
                self.assertEqual(diagnostics.status_code, 200)
                metrics = diagnostics.json()["data"]
                self.assertEqual(
                    [item["toolCallId"] for item in metrics["toolCalls"]],
                    first_tool_call_ids,
                )
                self.assertEqual(metrics["toolCalls"][0]["status"], "SUCCEEDED")
                self.assertEqual(metrics["toolCalls"][0]["arguments"]["filters"][0]["value"], 20000)
                self.assertEqual(metrics["orchestration"]["status"], "SUCCEEDED")
                self.assertEqual(len(metrics["sseStreams"]), 2)
                self.assertTrue(all(item["bytesSent"] > 0 for item in metrics["sseStreams"]))

                conflict_body = {**body, "query": "筛选西岗区住宅"}
                conflict = client.post(
                    "/api/v1/runs/stream", headers=headers, json=conflict_body
                )
                self.assertEqual(conflict.status_code, 409)
                self.assertEqual(conflict.json()["error"]["code"], "MESSAGE_CONFLICT")

                replay = client.get(
                    f"/api/v1/runs/{run_id}/events?afterSequence=2", headers=headers
                )
                self.assertEqual(replay.status_code, 200)
                self.assertTrue(all(item["sequence"] > 2 for item in event_data(replay.text)))

    def test_running_message_retry_attaches_and_http_cancel_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(
                settings_for(Path(directory) / "agent.sqlite3"),
                llm=SlowLlm(),
                rag=FakeRag(),
                tools=FakeTools(),
            )
            headers = {
                "Authorization": "Bearer langgraph-test-token",
                "X-Trace-Id": "trace-running-attach",
                "X-Tenant-Id": "tenant-1",
                "X-User-Id": "u-1",
            }
            body = request_body("筛选中山区住宅")
            with TestClient(create_app(runtime)) as client:
                first = client.post("/api/v1/runs", headers=headers, json=body)
                second = client.post("/api/v1/runs", headers=headers, json=body)
                self.assertEqual(first.status_code, 202)
                self.assertEqual(second.status_code, 202)
                run_id = first.json()["data"]["runId"]
                self.assertEqual(second.json()["data"]["runId"], run_id)
                self.assertIn(
                    runtime.store.get_run_unscoped(run_id).status,
                    {"QUEUED", "RUNNING"},
                )
                self.assertLessEqual(len(runtime._tasks), 1)

                cancelled = client.post(
                    f"/api/v1/runs/{run_id}/cancel",
                    headers=headers,
                    json={"reason": "http_cancel"},
                )
                duplicate = client.post(
                    f"/api/v1/runs/{run_id}/cancel",
                    headers=headers,
                    json={"reason": "duplicate"},
                )
                self.assertEqual(cancelled.json()["data"]["status"], "CANCELLED")
                self.assertEqual(
                    duplicate.json()["data"]["lastSequence"],
                    cancelled.json()["data"]["lastSequence"],
                )


if __name__ == "__main__":
    unittest.main()
