from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest

from app.agent.workflow import EXPECTED_CATALOG_VERSION, compact_catalog, normalize_catalog
from app.config import Settings
from app.tools.spring_client import SpringToolClient, ToolCallContext


RUN_SPRING_E2E = os.getenv("RUN_SPRING_E2E") == "1"
HAS_TOOL_TOKEN = bool(os.getenv("AGENT_TOOL_SERVICE_TOKEN"))


requires_spring_e2e = pytest.mark.skipif(
    not (RUN_SPRING_E2E and HAS_TOOL_TOKEN),
    reason="Spring Tool E2E requires RUN_SPRING_E2E=1 and AGENT_TOOL_SERVICE_TOKEN",
)


def live_client_and_context(trace_id: str) -> tuple[SpringToolClient, ToolCallContext]:
    settings = Settings.from_env()
    return (
        SpringToolClient(
            settings.spring_boot_base_url,
            settings.agent_tool_service_token,
            timeout_seconds=settings.agent_tool_timeout_seconds,
        ),
        ToolCallContext(
            trace_id=trace_id,
            tenant_id="tenant-e2e-test",
            user_id="user-e2e-test",
            run_id=str(uuid4()),
        ),
    )


@requires_spring_e2e
def test_spring_catalog_contract() -> None:
    async def scenario() -> None:
        client, context = live_client_and_context("spring-catalog-e2e-test")
        catalog = normalize_catalog(await client.catalog(context))
        assert catalog["version"] == EXPECTED_CATALOG_VERSION
        assert {tool["name"] for tool in catalog["tools"]} >= {
            "queryMapPoints",
            "queryMapLines",
            "searchHousingCandidates",
        }
        point_tool = next(
            tool for tool in compact_catalog(catalog) if tool["name"] == "queryMapPoints"
        )
        assert {
            layer["layerId"]: layer["district"] for layer in point_tool["layers"]
        } == {0: "沙河口区", 1: "西岗区", 2: "中山区"}

    asyncio.run(scenario())


@requires_spring_e2e
def test_spring_point_and_line_tool_contracts() -> None:
    async def scenario() -> None:
        client, context = live_client_and_context("spring-map-e2e-test")
        response = await client.invoke(
            "queryMapPoints",
            str(uuid4()),
            {
                "layerId": 0,
                "filters": [{"field": "房价", "operator": "<=", "value": 20000}],
                "returnGeometry": True,
                "resultRecordCount": 3,
            },
            context,
        )
        data = response.get("data", response)
        assert data["status"] == "SUCCEEDED"
        result = data["result"]
        assert result["geometryType"] == "point"
        for feature in result["features"]:
            assert feature["geometry"]["spatialReference"]["wkid"] == 4326

        response = await client.invoke(
            "queryMapLines",
            str(uuid4()),
            {
                "layerId": 3,
                "filters": [{"field": "绿视率原始值", "operator": ">=", "value": 0.4}],
                "returnGeometry": True,
                "resultRecordCount": 3,
            },
            context,
        )
        data = response.get("data", response)
        assert data["status"] == "SUCCEEDED"
        result = data["result"]
        assert result["geometryType"] == "polyline"
        for feature in result["features"]:
            assert feature["geometry"]["spatialReference"]["wkid"] == 4326

    asyncio.run(scenario())


@requires_spring_e2e
def test_spring_housing_search_contract() -> None:
    async def scenario() -> None:
        client, context = live_client_and_context("spring-housing-e2e-test")
        catalog = normalize_catalog(await client.catalog(context))
        housing_tool = next(
            tool for tool in catalog["tools"] if tool["name"] == "searchHousingCandidates"
        )
        housing_arguments = {
            "mode": "BUFFER_FILTER",
            "districts": [],
            "hardFilters": {},
            "preferences": {
                "price": {"enabled": False, "level": "PREFER_LOW", "weight": 0},
                "convenience": {"enabled": False, "level": "PREFER_HIGH", "weight": 0},
                "roadWalkability": {"enabled": True, "level": "HIGH", "weight": 1},
            },
            "roadCriteria": {},
            "spatial": {"relation": "WITHIN_ROAD_BUFFER"},
            "display": {"includeRoads": True, "includeBuffers": True},
            "limit": 20,
        }
        client.validate_arguments(housing_tool, housing_arguments)
        response = await client.invoke(
            "searchHousingCandidates",
            str(uuid4()),
            housing_arguments,
            context,
        )
        data = response.get("data", response)
        assert data["status"] == "SUCCEEDED"
        result = data["result"]
        client.validate_result(housing_tool, result)
        assert result["resolvedCriteria"]["bufferMeters"] == 100
        assert result["resolvedCriteria"]["roadWsThresholdPercentile"] == 75
        assert "BUFFER_METERS" in result["resolvedCriteria"]["defaultsApplied"]
        for candidate in result["housingCandidates"]:
            assert candidate["geometry"]["spatialReference"]["wkid"] == 4326
        for road in result["roadFeatures"]:
            assert road["geometry"]["spatialReference"]["wkid"] == 4326
        for overlay in result["bufferOverlays"]:
            assert overlay["geometry"]["spatialReference"]["wkid"] == 4326

    asyncio.run(scenario())
