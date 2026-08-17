from __future__ import annotations

from copy import deepcopy
from collections import Counter
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

from app.agent.workflow import (
    deterministic_housing_search_arguments,
    normalize_housing_search_arguments,
)
from app.tools.spring_client import SpringToolClient
from scripts.agent_v1_1_acceptance import AcceptanceFailure, LiveAcceptance


FIXTURE_ROOT = Path("tests/fixtures/agent-v1.1")


def catalog_path() -> Path:
    manifest = load_json(FIXTURE_ROOT / "manifest.json")
    return (FIXTURE_ROOT / manifest["files"]["catalog"]).resolve()


def load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def schema_fingerprint(catalog: dict[str, object]) -> str:
    schemas = [
        {
            "name": tool["name"],
            "inputSchema": tool["inputSchema"],
            "outputSchema": tool["outputSchema"],
        }
        for tool in catalog["tools"]
    ]
    payload = json.dumps(
        schemas,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_release_manifest_tracks_catalog_and_geoscene_schema() -> None:
    manifest = load_json(FIXTURE_ROOT / "manifest.json")
    catalog = load_json(catalog_path())

    assert manifest["catalogVersion"] == catalog["version"]
    assert manifest["geoSceneSchemaSha256"] == schema_fingerprint(catalog)
    assert set(manifest["regenerationRequiredWhen"]) == {
        "catalogVersion",
        "housingPolicyVersion",
        "geoSceneSchemaSha256",
        "spatialIndexVersion",
    }


def test_a01_a11_tool_and_sse_fixtures_are_complete_and_contract_valid() -> None:
    tool_fixture = load_json(FIXTURE_ROOT / "tool-fixtures.json")
    sse_fixture = load_json(FIXTURE_ROOT / "agent-sse-fixtures.json")
    catalog = load_json(catalog_path())
    catalog_tools = {tool["name"]: tool for tool in catalog["tools"]}
    expected_ids = {f"A{number:02d}" for number in range(1, 12)}
    tool_cases = {case["id"]: case for case in tool_fixture["cases"]}
    sse_cases = {case["id"]: case for case in sse_fixture["cases"]}

    assert set(tool_cases) == expected_ids
    assert set(sse_cases) == expected_ids
    assert tool_fixture["catalogVersion"] == catalog["version"]

    for case_id, case in tool_cases.items():
        arguments = case.get("arguments")
        if arguments is None:
            continue
        tool = catalog_tools[case["toolName"]]
        SpringToolClient.validate_arguments(tool, arguments)
        query = case["query"]
        deterministic = deterministic_housing_search_arguments(query)
        if deterministic is not None:
            assert deterministic == arguments, case_id
        else:
            assert normalize_housing_search_arguments(
                deepcopy(arguments), query=query
            ) == arguments, case_id

    terminal_events = {"run.completed", "run.failed", "run.cancelled"}
    for case_id, case in sse_cases.items():
        structure = case.get("structure")
        if structure is None:
            assert case_id in {"A09", "A10"}
            continue
        terminals = [event for event in structure if event in terminal_events]
        assert terminals == [case["terminal"]], case_id
        assert structure[-1] == case["terminal"], case_id


def housing_execution() -> dict[str, object]:
    return {
        "status": "SUCCEEDED",
        "result": {
            "policyVersion": "policy-1",
            "dataVersion": "index-1",
            "resolvedCriteria": {"roadWsThresholdPercentile": 75},
            "summary": {"returnedHousingCount": 1, "returnedRoadCount": 1},
            "warnings": [],
            "housingCandidates": [
                {
                    "housingId": "0:1",
                    "layerId": 0,
                    "attributes": {"name": "housing"},
                    "geometry": {
                        "x": 121.6,
                        "y": 38.9,
                        "spatialReference": {"wkid": 4326},
                    },
                    "scores": {"recommendationScore": 90},
                    "spatialEvidence": {"bufferMeters": 100},
                    "reasons": ["matched"],
                    "warnings": [],
                }
            ],
            "roadFeatures": [
                {
                    "roadId": "3:1",
                    "layerId": 3,
                    "attributes": {"WS": 0.8},
                    "geometry": {
                        "paths": [[[121.6, 38.9], [121.7, 39.0]]],
                        "spatialReference": {"wkid": 4326},
                    },
                }
            ],
            "bufferOverlays": [],
        },
    }


def housing_events() -> list[dict[str, object]]:
    execution = housing_execution()
    result = execution["result"]
    candidate = result["housingCandidates"][0]
    road = result["roadFeatures"][0]
    return [
        {
            "event": "map.result",
            "data": {
                "payload": {
                    "overlays": [],
                    "warnings": [],
                    "display": {"layerOrder": []},
                    "resultSets": [
                        {
                            "role": "HOUSING_CANDIDATES",
                            "layerId": 0,
                            "features": [
                                {
                                    "id": candidate["housingId"],
                                    "attributes": {
                                        **candidate["attributes"],
                                        "scores": candidate["scores"],
                                        "spatialEvidence": candidate["spatialEvidence"],
                                        "reasons": candidate["reasons"],
                                        "warnings": candidate["warnings"],
                                    },
                                    "geometry": candidate["geometry"],
                                }
                            ],
                        },
                        {
                            "role": "CONTRIBUTING_ROADS",
                            "layerId": 3,
                            "features": [
                                {
                                    "id": road["roadId"],
                                    "attributes": road["attributes"],
                                    "geometry": road["geometry"],
                                }
                            ],
                        },
                    ],
                }
            },
        }
    ]


def test_live_gate_rejects_agent_rewrite_of_backend_features() -> None:
    runner = object.__new__(LiveAcceptance)
    runner.policy_versions = set()
    runner.data_versions = set()
    runner.sse_cases = {"A01": {}}
    execution = housing_execution()
    events = housing_events()
    runner.validate_backend_result("A01", events, execution)

    events[0]["data"]["payload"]["resultSets"][0]["features"][0]["attributes"][
        "scores"
    ]["recommendationScore"] = 1
    with pytest.raises(AcceptanceFailure, match="rewrote backend housing"):
        runner.validate_backend_result("A01", events, execution)


def test_live_gate_rejects_version_drift_until_regeneration() -> None:
    runner = object.__new__(LiveAcceptance)
    runner.manifest = load_json(FIXTURE_ROOT / "manifest.json")
    runner.policy_versions = {runner.manifest["housingPolicyVersion"]}
    runner.data_versions = {runner.manifest["spatialIndexVersion"]}
    catalog = load_json(catalog_path())
    health = {"catalog": catalog}
    runner.validate_live_versions(health)

    runner.data_versions = {"new-spatial-index"}
    with pytest.raises(AcceptanceFailure, match="spatial index changed"):
        runner.validate_live_versions(health)


def test_performance_report_records_phase_p95_without_relaxing_timeout() -> None:
    runner = object.__new__(LiveAcceptance)
    runner.metrics = [
        {
            "wallMs": 300,
            "toolDurationMs": 120,
            "agentOrchestrationMs": 260,
            "sseDurationMs": 280,
            "sseBytes": 1024,
            "retryCount": 0,
            "errorCode": None,
        },
        {
            "wallMs": 500,
            "toolDurationMs": 220,
            "agentOrchestrationMs": 450,
            "sseDurationMs": 480,
            "sseBytes": 2048,
            "retryCount": 1,
            "errorCode": None,
        },
    ]
    runner.errors = Counter()
    runner.fixture_root = FIXTURE_ROOT
    runner.manifest = load_json(FIXTURE_ROOT / "manifest.json")
    runner.args = SimpleNamespace(mode="Regenerate")

    report = runner.performance_report()

    assert report["p95Ms"] > 0
    assert report["phaseP95Ms"]["toolMs"] > 0
    assert report["phaseP95Ms"]["agentNonToolMs"] > 0
    assert report["phaseP95Ms"]["transportAndSseMs"] > 0


def test_live_gate_writes_geoscene_and_percentile_sse_evidence() -> None:
    local_tmp = Path("tmp")
    local_tmp.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=local_tmp) as directory:
        output = Path(directory)
        runner = object.__new__(LiveAcceptance)
        runner.output = output
        runner.live_tool_responses = {
            case_id: [
                {
                    "result": {
                        "resolvedCriteria": {
                            "roadWsThresholdPercentile": expected_percentile
                        },
                        "summary": {"returnedHousingCount": 1},
                    }
                }
            ]
            for case_id, expected_percentile in (("A03", 75), ("A05", 90))
        }
        runner.runs = {
            case_id: {
                "runId": f"run-{case_id}",
                "toolCallIds": [f"tool-{case_id}"],
                "events": [{"event": "run.completed"}],
                "diagnostics": {"sseStreams": [{"bytesSent": 123}]},
            }
            for case_id in ("A03", "A05")
        }

        files = runner.write_release_evidence(
            {
                "catalog": {"version": "2026-07-29.1"},
                "toolHealth": {"housingSnapshot": {"status": "READY"}},
            }
        )

        assert set(files) == {"geoSceneProbes", "p75P90SseSummary"}
        summary = load_json(output / files["p75P90SseSummary"])
        assert summary["cases"]["A03"]["actualPercentile"] == 75
        assert summary["cases"]["A05"]["actualPercentile"] == 90
