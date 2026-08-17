from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

import httpx

from app.tools.spring_client import SpringToolClient


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "tests/fixtures/agent-v1.1"
TERMINALS = {"run.completed", "run.failed", "run.cancelled"}


class AcceptanceFailure(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def schema_fingerprint(catalog: dict[str, Any]) -> str:
    schemas = [
        {
            "name": tool["name"],
            "inputSchema": tool["inputSchema"],
            "outputSchema": tool["outputSchema"],
        }
        for tool in catalog["tools"]
    ]
    return hashlib.sha256(canonical(schemas).encode("utf-8")).hexdigest()


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def parse_sse(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    normalized = text.replace("\r\n", "\n")
    for block in normalized.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key] = value.lstrip()
        if "event" not in fields or "data" not in fields:
            continue
        events.append(
            {
                "id": fields.get("id"),
                "event": fields["event"],
                "data": json.loads(fields["data"]),
            }
        )
    return events


def event_structure(events: list[dict[str, Any]]) -> list[str]:
    structure: list[str] = []
    for event in events:
        name = event["event"]
        if name == "answer.delta":
            if not structure or structure[-1] != "answer.delta+":
                structure.append("answer.delta+")
        else:
            structure.append(name)
    return structure


def validate_sse(
    case: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    compare_structure: bool = True,
) -> None:
    if not events:
        raise AcceptanceFailure(f"{case['id']}: empty SSE stream")
    names = [event["event"] for event in events]
    terminals = [name for name in names if name in TERMINALS]
    if terminals != [case["terminal"]] or names[-1] != case["terminal"]:
        raise AcceptanceFailure(
            f"{case['id']}: expected one terminal {case['terminal']}, got {terminals}"
        )
    if compare_structure and event_structure(events) != case["structure"]:
        raise AcceptanceFailure(
            f"{case['id']}: SSE structure drift: {event_structure(events)}"
        )
    for sequence, event in enumerate(events, start=1):
        data = event["data"]
        if data.get("schemaVersion") != "1.1" or data.get("sequence") != sequence:
            raise AcceptanceFailure(f"{case['id']}: invalid SSE envelope at sequence {sequence}")
        if event["id"] != f"{data.get('runId')}:{sequence}":
            raise AcceptanceFailure(f"{case['id']}: invalid SSE id at sequence {sequence}")
        if event["event"] == "tool.completed" and not isinstance(
            data.get("payload", {}).get("durationMs"), int
        ):
            raise AcceptanceFailure(f"{case['id']}: tool.completed is missing durationMs")
    terminal = events[-1]["data"]["payload"]
    expected_error = case.get("errorCode")
    if expected_error and terminal.get("error", {}).get("code") != expected_error:
        raise AcceptanceFailure(
            f"{case['id']}: expected error {expected_error}, got {terminal.get('error')}"
        )
    if case["terminal"] == "run.completed":
        answer = "".join(
            event["data"]["payload"]["content"]
            for event in events
            if event["event"] == "answer.delta"
        )
        if answer != terminal.get("answer"):
            raise AcceptanceFailure(f"{case['id']}: answer.delta does not match terminal answer")


def validate_frontend_evidence(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AcceptanceFailure(f"frontend regression evidence is missing: {path}")
    evidence = read_json(path)
    for viewport in ("desktop", "mobile"):
        state = evidence.get(viewport)
        if not isinstance(state, dict):
            raise AcceptanceFailure(f"frontend evidence is missing {viewport}")
        for check in ("bufferVisible", "roadsVisible", "housingVisible", "clearRemovesAll"):
            if state.get(check) is not True:
                raise AcceptanceFailure(f"frontend evidence failed {viewport}.{check}")
    return evidence


def fixture_state(fixture_root: Path, require_production: bool) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest = read_json(fixture_root / "manifest.json")
    tools = read_json(fixture_root / "tool-fixtures.json")
    sse = read_json(fixture_root / "agent-sse-fixtures.json")
    catalog_path = (fixture_root / manifest["files"]["catalog"]).resolve()
    catalog = read_json(catalog_path)
    expected_ids = {f"A{number:02d}" for number in range(1, 12)}
    tool_ids = {case["id"] for case in tools["cases"]}
    sse_ids = {case["id"] for case in sse["cases"]}
    if tool_ids != expected_ids or sse_ids != expected_ids:
        raise AcceptanceFailure("A01-A11 fixtures are incomplete")
    if manifest["catalogVersion"] != catalog["version"]:
        raise AcceptanceFailure("fixture Catalog version does not match the checked-in Catalog")
    if tools.get("catalogVersion") != manifest["catalogVersion"]:
        raise AcceptanceFailure("Tool fixture Catalog version does not match manifest")
    if sse.get("catalogVersion") != manifest["catalogVersion"]:
        raise AcceptanceFailure("Agent SSE fixture Catalog version does not match manifest")
    for fixture_name, fixture in (("Tool", tools), ("Agent SSE", sse)):
        if fixture.get("housingPolicyVersion") != manifest.get("housingPolicyVersion"):
            raise AcceptanceFailure(
                f"{fixture_name} fixture Housing policy version does not match manifest"
            )
        if fixture.get("spatialIndexVersion") != manifest.get("spatialIndexVersion"):
            raise AcceptanceFailure(
                f"{fixture_name} fixture spatial index version does not match manifest"
            )
    if manifest["geoSceneSchemaSha256"] != schema_fingerprint(catalog):
        raise AcceptanceFailure("GeoScene/Catalog schema fingerprint changed; regenerate fixtures")
    if require_production:
        if manifest.get("source") != "controlled-live-smoke":
            raise AcceptanceFailure("production release requires controlled-live-smoke fixtures")
        if not manifest.get("spatialIndexVersion"):
            raise AcceptanceFailure("production fixture has no spatialIndexVersion")
        baseline = read_json(fixture_root / manifest["files"]["performanceBaseline"])
        if not baseline.get("p95Ms") or int(baseline.get("sampleCount", 0)) < 1:
            raise AcceptanceFailure("production performance baseline is missing")
        expected_ids = {f"A{number:02d}" for number in range(1, 12)}
        for key in ("liveToolFixtures", "liveAgentSseFixtures"):
            path = fixture_root / manifest["files"].get(key, "")
            if not path.is_file():
                raise AcceptanceFailure(f"production fixture is missing: {key}")
            live_fixture = read_json(path)
            if set(live_fixture.get("cases", {})) != expected_ids:
                raise AcceptanceFailure(f"production fixture {key} does not cover A01-A11")
    return manifest, tools, sse


def run_deterministic(fixture_root: Path, output: Path) -> dict[str, Any]:
    manifest, tools, _ = fixture_state(fixture_root, require_production=False)
    command = [
        str(ROOT / ".venv/Scripts/python.exe") if os.name == "nt" else sys.executable,
        "-m",
        "pytest",
        "-q",
    ]
    if not Path(command[0]).exists():
        command[0] = sys.executable
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode != 0:
        raise AcceptanceFailure(f"deterministic test suite failed with {completed.returncode}")
    report = {
        "mode": "Deterministic",
        "status": "PASSED",
        "catalogVersion": manifest["catalogVersion"],
        "caseCount": len(tools["cases"]),
        "generatedAt": datetime.now(UTC).isoformat(),
    }
    write_json(output / "acceptance-report.json", report)
    return report


class LiveAcceptance:
    def __init__(self, args: argparse.Namespace, fixture_root: Path, output: Path) -> None:
        self.args = args
        self.fixture_root = fixture_root
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)
        self.manifest, tool_fixture, sse_fixture = fixture_state(
            fixture_root, require_production=args.require_production_fixtures
        )
        self.tool_cases = {case["id"]: case for case in tool_fixture["cases"]}
        self.sse_cases = {case["id"]: case for case in sse_fixture["cases"]}
        self.agent_token = os.getenv("LANGGRAPH_SERVICE_TOKEN", "")
        self.tool_token = os.getenv("AGENT_TOOL_SERVICE_TOKEN", "")
        if not self.agent_token or not self.tool_token:
            raise AcceptanceFailure(
                "LANGGRAPH_SERVICE_TOKEN and AGENT_TOOL_SERVICE_TOKEN are required"
            )
        self.agent = httpx.Client(
            base_url=args.agent_url.rstrip("/"), timeout=args.timeout_seconds
        )
        self.spring = httpx.Client(
            base_url=args.spring_url.rstrip("/"), timeout=args.timeout_seconds
        )
        self.runs: dict[str, dict[str, Any]] = {}
        self.metrics: list[dict[str, Any]] = []
        self.errors: Counter[str] = Counter()
        self.policy_versions: set[str] = set()
        self.data_versions: set[str] = set()
        self.live_tool_responses: dict[str, Any] = {}
        self.live_sse: dict[str, Any] = {}
        self.regenerated_tool_cases: dict[str, dict[str, Any]] = {}
        self.regenerated_sse_cases: dict[str, list[dict[str, Any]]] = {}
        self.regenerated_sse_expectations: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        self.agent.close()
        self.spring.close()

    def spring_headers(self, trace_id: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.tool_token}",
            "X-Trace-Id": trace_id,
            "X-Tenant-Id": self.args.tenant_id,
            "X-User-Id": self.args.user_id,
            "X-Run-Id": "agent-v1.1-acceptance",
        }

    def agent_headers(self, trace_id: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.agent_token}",
            "X-Trace-Id": trace_id,
            "X-Tenant-Id": self.args.tenant_id,
            "X-User-Id": self.args.user_id,
        }

    def get_json(self, client: httpx.Client, path: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
        response = client.get(path, headers=headers)
        if response.status_code != 200:
            raise AcceptanceFailure(f"GET {path} returned {response.status_code}: {response.text}")
        return response.json()

    def capture_health(self) -> dict[str, Any]:
        actuator = self.get_json(self.spring, "/actuator/health")
        tool_health_response = self.get_json(
            self.spring,
            "/internal/agent-tools/health",
            self.spring_headers("acceptance-health"),
        )
        catalog_response = self.get_json(
            self.spring,
            "/internal/agent-tools/catalog",
            self.spring_headers("acceptance-catalog"),
        )
        ready = self.get_json(self.agent, "/readyz")
        tool_health = tool_health_response.get("data", tool_health_response)
        catalog = catalog_response.get("data", catalog_response)
        snapshot = tool_health.get("housingSnapshot")
        if tool_health.get("status") != "READY" or not isinstance(snapshot, dict) or snapshot.get("status") != "READY":
            raise AcceptanceFailure(f"Spring Tool housingSnapshot is not READY: {tool_health}")
        versions = {
            str(value)
            for value in (tool_health.get("catalogVersion"), snapshot.get("catalogVersion"))
            if value is not None
        }
        if versions != {catalog.get("version")}:
            raise AcceptanceFailure(f"Tool health Catalog mismatch: {versions} vs {catalog.get('version')}")
        if ready.get("status") != "READY" or ready.get("toolHealth", {}).get("housingSnapshot", {}).get("status") != "READY":
            raise AcceptanceFailure(f"Agent is not ready: {ready}")
        controlled_model_required = (
            self.args.mode == "Regenerate" or self.args.require_production_fixtures
        )
        if controlled_model_required and not self.args.expected_model:
            raise AcceptanceFailure(
                "controlled Regenerate/production Live acceptance requires "
                "AGENT_ACCEPTANCE_EXPECTED_MODEL"
            )
        if self.args.expected_model and ready.get("model") != self.args.expected_model:
            raise AcceptanceFailure(
                f"Agent model mismatch: expected {self.args.expected_model}, "
                f"got {ready.get('model')}"
            )
        runtime_policy = ready.get("runtimePolicy")
        if not isinstance(runtime_policy, dict):
            raise AcceptanceFailure("Agent /readyz is missing runtimePolicy")
        try:
            live_run_timeout = float(runtime_policy["agentMaxRunSeconds"])
            live_tool_timeout = float(runtime_policy["agentToolTimeoutSeconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AcceptanceFailure("Agent /readyz runtimePolicy is invalid") from exc
        if not 120 < live_tool_timeout < live_run_timeout:
            raise AcceptanceFailure(
                "Agent runtime tool timeout must be greater than Catalog 120 seconds "
                "and lower than the Run timeout"
            )
        expected_timeout = os.getenv("AGENT_TOOL_TIMEOUT_SECONDS", "")
        if expected_timeout and live_tool_timeout != float(expected_timeout):
            raise AcceptanceFailure(
                f"Agent runtime tool timeout mismatch: expected {expected_timeout}, "
                f"got {live_tool_timeout}"
            )
        write_json(self.output / "spring-actuator-health.json", actuator)
        write_json(self.output / "spring-tool-health.json", tool_health_response)
        write_json(self.output / "spring-tool-catalog.json", catalog_response)
        write_json(self.output / "agent-readyz.json", ready)
        return {"toolHealth": tool_health, "catalog": catalog, "ready": ready}

    def request_body(self, case: dict[str, Any]) -> dict[str, Any]:
        return {
            "conversationId": str(uuid4()),
            "messageId": str(uuid4()),
            "query": case["query"],
            "context": {
                "locale": "zh-CN",
                "map": {
                    "visibleLayerIds": [0, 1, 2, 3, 4, 5],
                    "zoom": 13,
                    "extent": None,
                },
                "businessObjectIds": [],
            },
            "user": {
                "userId": self.args.user_id,
                "tenantId": self.args.tenant_id,
                "roles": ["USER"],
            },
        }

    def run_agent_case(
        self,
        case_id: str,
        body: dict[str, Any] | None = None,
        *,
        fixture_case_id: str | None = None,
    ) -> dict[str, Any]:
        fixture_case_id = fixture_case_id or case_id
        case = self.tool_cases[fixture_case_id]
        expected_sse = self.sse_cases[fixture_case_id]
        body = deepcopy(body) if body is not None else self.request_body(case)
        trace_id = f"agent-v1.1-{case_id.lower()}-{uuid4()}"
        started = time.perf_counter()
        response = self.agent.post(
            "/api/v1/runs/stream",
            headers=self.agent_headers(trace_id),
            json=body,
        )
        wall_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            raise AcceptanceFailure(
                f"{case_id}: Agent stream returned {response.status_code}: {response.text}"
            )
        events = parse_sse(response.text)
        validate_sse(
            expected_sse,
            events,
            compare_structure=self.args.mode != "Regenerate",
        )
        run_id = response.headers.get("X-Run-Id")
        if not run_id:
            raise AcceptanceFailure(f"{case_id}: X-Run-Id is missing")
        diagnostics_response = self.get_json(
            self.agent,
            f"/api/v1/runs/{run_id}/diagnostics",
            self.agent_headers(trace_id),
        )
        diagnostics = diagnostics_response["data"]
        expected_tool = case.get("toolName")
        calls = diagnostics["toolCalls"]
        if expected_tool is None:
            if calls:
                raise AcceptanceFailure(f"{case_id}: contract failure unexpectedly invoked a Tool")
        else:
            if len(calls) != 1 or calls[0]["toolName"] != expected_tool:
                raise AcceptanceFailure(f"{case_id}: expected exactly one {expected_tool} call")
            if expected_tool not in self.catalog_tools:
                raise AcceptanceFailure(f"{case_id}: live Catalog is missing {expected_tool}")
            try:
                SpringToolClient.validate_arguments(
                    self.catalog_tools[expected_tool], calls[0]["arguments"]
                )
            except Exception as exc:
                raise AcceptanceFailure(
                    f"{case_id}: Agent emitted arguments rejected by the live Catalog"
                ) from exc
            arguments_changed = canonical(calls[0]["arguments"]) != canonical(
                case["arguments"]
            )
            if arguments_changed and self.args.mode != "Regenerate":
                raise AcceptanceFailure(
                    f"{case_id}: model/Planner changed Tool arguments\n"
                    f"expected={canonical(case['arguments'])}\nactual={canonical(calls[0]['arguments'])}"
                )
            if self.args.mode == "Regenerate":
                self.regenerated_tool_cases[fixture_case_id] = {
                    "toolName": calls[0]["toolName"],
                    "arguments": deepcopy(calls[0]["arguments"]),
                }
        executions: list[dict[str, Any]] = []
        for call in calls:
            execution_response = self.get_json(
                self.spring,
                f"/internal/agent-tools/executions/{call['toolCallId']}",
                self.spring_headers(trace_id),
            )
            execution = execution_response.get("data", execution_response)
            executions.append(execution)
            self.validate_backend_result(case_id, events, execution)
        sse_metric = diagnostics["sseStreams"][-1]
        if sse_metric["bytesSent"] != len(response.content):
            raise AcceptanceFailure(
                f"{case_id}: persisted SSE byte count {sse_metric['bytesSent']} "
                f"does not match HTTP body {len(response.content)}"
            )
        terminal_payload = events[-1]["data"]["payload"]
        error_code = terminal_payload.get("error", {}).get("code")
        if error_code:
            self.errors[error_code] += 1
        metric = {
            "caseId": case_id,
            "runId": run_id,
            "toolDurationMs": sum((call.get("durationMs") or 0) for call in calls),
            "agentOrchestrationMs": diagnostics.get("orchestration", {}).get("durationMs", 0),
            "wallMs": wall_ms,
            "sseBytes": sse_metric["bytesSent"],
            "sseDurationMs": sse_metric["durationMs"],
            "retryCount": sum(call.get("retryCount", 0) for call in calls),
            "errorCode": error_code,
        }
        self.metrics.append(metric)
        self.live_tool_responses[case_id] = executions
        self.live_sse[case_id] = {
            "structure": event_structure(events),
            "events": events,
            "diagnostics": diagnostics,
        }
        self.regenerated_sse_cases[fixture_case_id] = events
        case_output = self.output / case_id
        case_output.mkdir(parents=True, exist_ok=True)
        (case_output / "agent.sse.txt").write_text(response.text, encoding="utf-8")
        write_json(case_output / "diagnostics.json", diagnostics_response)
        write_json(case_output / "spring-executions.json", executions)
        result = {
            "body": body,
            "runId": run_id,
            "toolCallIds": [call["toolCallId"] for call in calls],
            "events": events,
            "diagnostics": diagnostics,
        }
        self.runs[case_id] = result
        return result

    def validate_backend_result(
        self, case_id: str, events: list[dict[str, Any]], execution: dict[str, Any]
    ) -> None:
        if execution.get("status") != "SUCCEEDED":
            return
        result = execution.get("result")
        if not isinstance(result, dict) or "housingCandidates" not in result:
            return
        self.policy_versions.add(str(result.get("policyVersion")))
        self.data_versions.add(str(result.get("dataVersion")))
        map_events = [event for event in events if event["event"] == "map.result"]
        if len(map_events) != 1:
            raise AcceptanceFailure(f"{case_id}: expected one map.result")
        payload = map_events[0]["data"]["payload"]
        if canonical(payload["overlays"]) != canonical(result["bufferOverlays"]):
            raise AcceptanceFailure(f"{case_id}: Agent rewrote backend buffer overlays")
        actual_housing: dict[int, list[dict[str, Any]]] = {}
        for result_set in payload["resultSets"]:
            if result_set["role"] == "HOUSING_CANDIDATES":
                actual_housing[result_set["layerId"]] = result_set["features"]
        expected_housing: dict[int, list[dict[str, Any]]] = {}
        for item in result["housingCandidates"]:
            attributes = deepcopy(item.get("attributes") or {})
            attributes.setdefault("scores", item.get("scores", {}))
            attributes.setdefault("spatialEvidence", item.get("spatialEvidence", {}))
            attributes.setdefault("reasons", item.get("reasons", []))
            attributes.setdefault("warnings", item.get("warnings", []))
            expected_housing.setdefault(item["layerId"], []).append(
                {
                    "id": item["housingId"],
                    "attributes": attributes,
                    "geometry": item["geometry"],
                }
            )
        actual_roads: dict[int, list[dict[str, Any]]] = {}
        for result_set in payload["resultSets"]:
            if result_set["role"] == "CONTRIBUTING_ROADS":
                actual_roads[result_set["layerId"]] = result_set["features"]
        expected_roads: dict[int, list[dict[str, Any]]] = {}
        for item in result["roadFeatures"]:
            expected_roads.setdefault(item["layerId"], []).append(
                {
                    "id": item["roadId"],
                    "attributes": item.get("attributes", {}),
                    "geometry": item["geometry"],
                }
            )
        if canonical(actual_housing) != canonical(expected_housing):
            raise AcceptanceFailure(
                f"{case_id}: Agent rewrote backend housing attributes, scores or geometry"
            )
        if canonical(actual_roads) != canonical(expected_roads):
            raise AcceptanceFailure(
                f"{case_id}: Agent rewrote backend road attributes or geometry"
            )
        expected = self.sse_cases[case_id]
        expected_percentile = expected.get("roadPercentile")
        if expected_percentile is not None:
            actual = result.get("resolvedCriteria", {}).get("roadWsThresholdPercentile")
            if actual != expected_percentile and self.args.mode != "Regenerate":
                raise AcceptanceFailure(
                    f"{case_id}: expected P{expected_percentile}, got {actual}"
                )
            if self.args.mode == "Regenerate":
                self.regenerated_sse_expectations.setdefault(fixture_case_id, {})[
                    "roadPercentile"
                ] = actual
        warning = expected.get("requiredWarning")
        if warning and warning not in payload.get("warnings", []):
            raise AcceptanceFailure(f"{case_id}: missing warning {warning}")
        required_layers = set(expected.get("requiredLayers", []))
        actual_layers = set(payload.get("display", {}).get("layerOrder", []))
        if not required_layers.issubset(actual_layers):
            raise AcceptanceFailure(f"{case_id}: missing required display layers")

    def run_a09(self) -> None:
        original = self.runs["A01"]
        repeated = self.run_agent_case(
            "A09", original["body"], fixture_case_id="A01"
        )
        if repeated["runId"] != original["runId"]:
            raise AcceptanceFailure("A09: messageId did not return the same runId")
        if repeated["toolCallIds"] != original["toolCallIds"]:
            raise AcceptanceFailure("A09: messageId did not preserve toolCallId")
        changed = deepcopy(original["body"])
        changed["query"] += "，只要5套"
        response = self.agent.post(
            "/api/v1/runs/stream",
            headers=self.agent_headers(f"agent-v1.1-a09-conflict-{uuid4()}"),
            json=changed,
        )
        if response.status_code != 409 or response.json().get("error", {}).get("code") != "MESSAGE_CONFLICT":
            raise AcceptanceFailure("A09: changed Agent request did not return MESSAGE_CONFLICT")
        self.live_sse["A09"].update(
            {
                "fixtureRef": "A01",
                "sameRunId": True,
                "sameToolCallIds": True,
                "messageConflict": True,
            }
        )
        replay_executions = self.live_tool_responses["A09"]
        self.live_tool_responses["A09"] = {
            "fixtureRef": "A01",
            "executions": replay_executions,
            "runId": original["runId"],
            "toolCallIds": original["toolCallIds"],
            "replayedRunId": repeated["runId"],
            "replayedToolCallIds": repeated["toolCallIds"],
            "messageConflict": True,
        }

    def run_a10(self) -> None:
        source = self.tool_cases["A01"]
        tool_call_id = str(uuid4())
        trace_id = f"agent-v1.1-a10-{uuid4()}"
        path = f"/internal/agent-tools/tools/{source['toolName']}/invoke"
        request = {
            "toolCallId": tool_call_id,
            "arguments": deepcopy(source["arguments"]),
            "dryRun": True,
        }
        first = self.spring.post(path, headers=self.spring_headers(trace_id), json=request)
        second = self.spring.post(path, headers=self.spring_headers(trace_id), json=request)
        if first.status_code != 200 or second.status_code != 200 or canonical(first.json()) != canonical(second.json()):
            raise AcceptanceFailure("A10: identical Spring Tool call was not idempotent")
        changed = deepcopy(request)
        changed["arguments"].update(self.tool_cases["A10"]["changedArguments"])
        conflict = self.spring.post(path, headers=self.spring_headers(trace_id), json=changed)
        code = conflict.json().get("error", {}).get("code") if conflict.content else None
        if conflict.status_code != 409 or code != self.tool_cases["A10"]["expectedErrorCode"]:
            raise AcceptanceFailure(f"A10: changed Tool arguments did not conflict: {conflict.text}")
        capture = {"first": first.json(), "second": second.json(), "conflict": conflict.json()}
        self.live_tool_responses["A10"] = capture
        self.live_sse["A10"] = {
            "notApplicable": True,
            "reason": "A10 verifies Spring Tool idempotency and has no Agent SSE stream",
        }
        write_json(self.output / "A10" / "spring-idempotency-conflict.json", capture)
        self.errors[code] += 1

    def performance_report(self) -> dict[str, Any]:
        successful = [float(item["wallMs"]) for item in self.metrics if not item["errorCode"]]
        successful_runs = [item for item in self.metrics if not item["errorCode"]]
        phase_samples = {
            "toolMs": [float(item["toolDurationMs"]) for item in successful_runs],
            "agentOrchestrationMs": [
                float(item["agentOrchestrationMs"]) for item in successful_runs
            ],
            "sseStreamMs": [float(item["sseDurationMs"]) for item in successful_runs],
            "agentNonToolMs": [
                float(max(0, item["agentOrchestrationMs"] - item["toolDurationMs"]))
                for item in successful_runs
            ],
            "transportAndSseMs": [
                float(max(0, item["wallMs"] - item["agentOrchestrationMs"]))
                for item in successful_runs
            ],
        }
        phase_p95_ms = {
            phase: round(percentile(samples, 0.95), 2)
            for phase, samples in phase_samples.items()
        }
        report = {
            "sampleCount": len(successful),
            "p50Ms": round(percentile(successful, 0.50), 2),
            "p75Ms": round(percentile(successful, 0.75), 2),
            "p90Ms": round(percentile(successful, 0.90), 2),
            "p95Ms": round(percentile(successful, 0.95), 2),
            "sseBytes": sum(item["sseBytes"] for item in self.metrics),
            "retryCount": sum(item["retryCount"] for item in self.metrics),
            "errorCodes": dict(sorted(self.errors.items())),
            "phaseP95Ms": phase_p95_ms,
            "runs": self.metrics,
        }
        baseline = read_json(self.fixture_root / self.manifest["files"]["performanceBaseline"])
        baseline_p95 = baseline.get("p95Ms")
        if self.args.mode != "Regenerate":
            if not baseline_p95:
                raise AcceptanceFailure("P95 baseline is missing; run controlled Regenerate first")
            if report["p95Ms"] > float(baseline_p95) * 2:
                diagnosis = {
                    "status": "RELEASE_BLOCKED",
                    "reason": "P95_REGRESSION",
                    "currentP95Ms": report["p95Ms"],
                    "baselineP95Ms": float(baseline_p95),
                    "thresholdMs": float(baseline_p95) * 2,
                    "phaseP95Ms": phase_p95_ms,
                    "investigateInOrder": [
                        "Tool and network exchange (toolMs)",
                        "Agent orchestration, serialization and SQLite persistence (agentNonToolMs)",
                        "SSE serialization and transport (sseStreamMs, transportAndSseMs)",
                    ],
                    "timeoutChangeAllowed": False,
                    "requiredAction": (
                        "Locate the regressed phase before changing any Tool or Run timeout."
                    ),
                }
                write_json(self.output / "performance-regression-diagnosis.json", diagnosis)
                raise AcceptanceFailure(
                    f"P95 regression: {report['p95Ms']} ms exceeds 2x baseline {baseline_p95} ms"
                )
        return report

    def validate_live_versions(self, health: dict[str, Any]) -> None:
        catalog = health["catalog"]
        expected_catalog = self.manifest["catalogVersion"]
        if catalog.get("version") != expected_catalog:
            raise AcceptanceFailure(
                f"live Catalog changed from fixture {expected_catalog} to "
                f"{catalog.get('version')}; run controlled Regenerate"
            )
        actual_schema = schema_fingerprint(catalog)
        if actual_schema != self.manifest["geoSceneSchemaSha256"]:
            raise AcceptanceFailure(
                "live GeoScene/Catalog schema changed; run controlled Regenerate"
            )
        expected_policy = self.manifest.get("housingPolicyVersion")
        if self.policy_versions != {expected_policy}:
            raise AcceptanceFailure(
                f"live Housing policy changed: expected {expected_policy}, "
                f"got {sorted(self.policy_versions)}; run controlled Regenerate"
            )
        expected_index = self.manifest.get("spatialIndexVersion")
        if self.data_versions != {expected_index}:
            raise AcceptanceFailure(
                f"live spatial index changed: expected {expected_index}, "
                f"got {sorted(self.data_versions)}; run controlled Regenerate"
            )

    def regenerate(self, health: dict[str, Any], performance: dict[str, Any]) -> None:
        if len(self.policy_versions) != 1 or len(self.data_versions) != 1:
            raise AcceptanceFailure(
                f"cannot regenerate with inconsistent versions: policy={self.policy_versions}, data={self.data_versions}"
            )
        catalog = health["catalog"]
        now = datetime.now(UTC).isoformat()
        manifest = deepcopy(self.manifest)
        safe_catalog_version = re.sub(
            r"[^A-Za-z0-9._-]+", "-", str(catalog["version"])
        ).strip("-.")
        if not safe_catalog_version:
            raise AcceptanceFailure("Catalog version cannot be used as a fixture filename")
        manifest["files"]["catalog"] = (
            f"../../../docs/docs/examples/agent-tool-catalog-{safe_catalog_version}.json"
        )
        manifest.update(
            {
                "catalogVersion": catalog["version"],
                "housingPolicyVersion": next(iter(self.policy_versions)),
                "geoSceneSchemaSha256": schema_fingerprint(catalog),
                "spatialIndexVersion": next(iter(self.data_versions)),
                "source": "controlled-live-smoke",
                "generatedAt": now,
            }
        )
        catalog_path = (self.fixture_root / manifest["files"]["catalog"]).resolve()
        expected_examples_root = (ROOT / "docs/docs/examples").resolve()
        if catalog_path.parent != expected_examples_root:
            raise AcceptanceFailure("regenerated Catalog path escaped docs examples directory")
        write_json(catalog_path, catalog)

        fixture_header = {
            "schemaVersion": 1,
            "catalogVersion": catalog["version"],
            "housingPolicyVersion": next(iter(self.policy_versions)),
            "spatialIndexVersion": next(iter(self.data_versions)),
            "generatedAt": now,
        }
        tool_fixture = read_json(
            self.fixture_root / manifest["files"]["toolFixtures"]
        )
        tool_cases = {case["id"]: case for case in tool_fixture["cases"]}
        for case_id, generated in self.regenerated_tool_cases.items():
            if case_id in tool_cases:
                tool_cases[case_id]["toolName"] = generated["toolName"]
                tool_cases[case_id]["arguments"] = generated["arguments"]
        tool_fixture.update(fixture_header)
        tool_fixture["source"] = "controlled-live-smoke"
        tool_fixture["cases"] = list(tool_cases.values())
        write_json(
            self.fixture_root / manifest["files"]["toolFixtures"], tool_fixture
        )
        sse_fixture = read_json(
            self.fixture_root / manifest["files"]["agentSseFixtures"]
        )
        sse_cases = {case["id"]: case for case in sse_fixture["cases"]}
        for case_id, events in self.regenerated_sse_cases.items():
            if case_id not in sse_cases or not events:
                continue
            case = sse_cases[case_id]
            case["structure"] = event_structure(events)
            case["terminal"] = events[-1]["event"]
            if case["terminal"] == "run.failed":
                case["errorCode"] = (
                    events[-1]["data"].get("payload", {}).get("error", {}).get("code")
                )
            if case_id in self.regenerated_sse_expectations:
                case.update(self.regenerated_sse_expectations[case_id])
        sse_fixture.update(fixture_header)
        sse_fixture["source"] = "controlled-live-smoke"
        sse_fixture["cases"] = list(sse_cases.values())
        write_json(
            self.fixture_root / manifest["files"]["agentSseFixtures"], sse_fixture
        )
        write_json(
            self.fixture_root / manifest["files"]["liveToolFixtures"],
            {**fixture_header, "cases": self.live_tool_responses},
        )
        write_json(
            self.fixture_root / manifest["files"]["liveAgentSseFixtures"],
            {**fixture_header, "cases": self.live_sse},
        )
        write_json(
            self.fixture_root / manifest["files"]["performanceBaseline"],
            {
                "schemaVersion": 1,
                "source": "controlled-live-smoke",
                "sampleCount": performance["sampleCount"],
                "p50Ms": performance["p50Ms"],
                "p75Ms": performance["p75Ms"],
                "p90Ms": performance["p90Ms"],
                "p95Ms": performance["p95Ms"],
                "phaseP95Ms": performance["phaseP95Ms"],
                "capturedAt": now,
            },
        )
        # Write manifest last. A failed regeneration must not point deterministic
        # acceptance at a partially generated fixture set.
        write_json(self.fixture_root / "manifest.json", manifest)

    def write_release_evidence(self, health: dict[str, Any]) -> dict[str, Any]:
        generated_at = datetime.now(UTC).isoformat()
        probes = {
            "schemaVersion": 1,
            "generatedAt": generated_at,
            "catalogVersion": health["catalog"]["version"],
            "toolHealth": health["toolHealth"],
            "housingSnapshot": health["toolHealth"].get("housingSnapshot"),
            "cases": self.live_tool_responses,
        }
        write_json(self.output / "geoscene-probes.json", probes)

        percentile_cases: dict[str, Any] = {}
        for case_id, expected_percentile in (("A03", 75), ("A05", 90)):
            run = self.runs[case_id]
            executions = self.live_tool_responses[case_id]
            execution = executions[0]
            result = execution.get("result", {})
            actual_percentile = result.get("resolvedCriteria", {}).get(
                "roadWsThresholdPercentile"
            )
            if actual_percentile != expected_percentile:
                raise AcceptanceFailure(
                    f"{case_id}: release evidence expected P{expected_percentile}, "
                    f"got {actual_percentile}"
                )
            percentile_cases[case_id] = {
                "expectedPercentile": expected_percentile,
                "actualPercentile": actual_percentile,
                "runId": run["runId"],
                "toolCallIds": run["toolCallIds"],
                "eventStructure": event_structure(run["events"]),
                "terminal": run["events"][-1]["event"],
                "sseBytes": run["diagnostics"]["sseStreams"][-1]["bytesSent"],
                "resolvedCriteria": result.get("resolvedCriteria"),
                "summary": result.get("summary"),
            }
        percentile_summary = {
            "schemaVersion": 1,
            "generatedAt": generated_at,
            "cases": percentile_cases,
        }
        write_json(self.output / "p75-p90-sse-summary.json", percentile_summary)
        return {
            "geoSceneProbes": "geoscene-probes.json",
            "p75P90SseSummary": "p75-p90-sse-summary.json",
        }

    def run(self) -> dict[str, Any]:
        health = self.capture_health()
        self.catalog_tools = {
            tool["name"]: tool for tool in health["catalog"]["tools"]
        }
        for case_id in ("A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08", "A11"):
            self.run_agent_case(case_id)
        self.run_a09()
        self.run_a10()
        if self.args.mode == "Live":
            self.validate_live_versions(health)
        performance = self.performance_report()
        evidence_files = self.write_release_evidence(health)
        if self.args.mode == "Regenerate":
            self.regenerate(health, performance)
        if self.args.frontend_evidence:
            frontend = validate_frontend_evidence(Path(self.args.frontend_evidence))
            write_json(self.output / "frontend-regression-evidence.json", frontend)
        elif self.args.require_frontend_evidence:
            raise AcceptanceFailure("production release requires external frontend regression evidence")
        report = {
            "mode": self.args.mode,
            "status": "PASSED",
            "catalogVersion": health["catalog"]["version"],
            "housingPolicyVersions": sorted(self.policy_versions),
            "spatialIndexVersions": sorted(self.data_versions),
            "performance": performance,
            "evidence": evidence_files,
            "generatedAt": datetime.now(UTC).isoformat(),
        }
        write_json(self.output / "acceptance-report.json", report)
        return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent v1.1 SSE release acceptance")
    parser.add_argument("--mode", choices=("Deterministic", "Live", "Regenerate"), default="Deterministic")
    parser.add_argument("--agent-url", default=os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--spring-url", default=os.getenv("SPRING_BOOT_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--fixture-root", default=str(DEFAULT_FIXTURES))
    parser.add_argument("--output", default=str(ROOT / "artifacts/agent-v1.1-acceptance"))
    parser.add_argument("--tenant-id", default=os.getenv("AGENT_ACCEPTANCE_TENANT_ID", "acceptance-tenant"))
    parser.add_argument("--user-id", default=os.getenv("AGENT_ACCEPTANCE_USER_ID", "acceptance-user"))
    parser.add_argument("--timeout-seconds", type=float, default=190.0)
    parser.add_argument(
        "--expected-model", default=os.getenv("AGENT_ACCEPTANCE_EXPECTED_MODEL", "")
    )
    parser.add_argument("--frontend-evidence")
    parser.add_argument("--require-frontend-evidence", action="store_true")
    parser.add_argument("--require-production-fixtures", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fixture_root = Path(args.fixture_root).resolve()
    output = Path(args.output).resolve()
    try:
        if args.mode == "Deterministic":
            report = run_deterministic(fixture_root, output)
        else:
            runner = LiveAcceptance(args, fixture_root, output)
            try:
                report = runner.run()
            finally:
                runner.close()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (AcceptanceFailure, httpx.HTTPError, OSError, ValueError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
