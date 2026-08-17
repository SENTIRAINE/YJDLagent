from __future__ import annotations

import unittest
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient

from app.graph import build_rag_graph
from app.agent.runtime import AgentRuntime
from app.api.app import create_app
from app.config import Settings


FIXTURE_MANIFEST_PATH = Path("tests/fixtures/agent-v1.1/manifest.json")
FIXTURE_MANIFEST = json.loads(FIXTURE_MANIFEST_PATH.read_text(encoding="utf-8"))
CATALOG_VERSION = FIXTURE_MANIFEST["catalogVersion"]
CATALOG_PATH = (
    FIXTURE_MANIFEST_PATH.parent / FIXTURE_MANIFEST["files"]["catalog"]
).resolve()


class CatalogFixtureTools:
    async def catalog(self, _context: object) -> dict[str, object]:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))

    async def health(self, _context: object) -> dict[str, object]:
        return {
            "success": True,
            "data": {
                "status": "READY",
                "catalogVersion": CATALOG_VERSION,
                "housingSnapshot": {"status": "READY"},
            },
        }


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory()
        settings = replace(
            Settings.from_env(),
            agent_database_path=Path(cls.tempdir.name) / "agent.sqlite3",
            agent_checkpoint_database_path=Path(cls.tempdir.name) / "agent-checkpoints.sqlite3",
            langgraph_service_token="test-token",
            agent_tool_service_token="tool-token",
        )
        cls.runtime = AgentRuntime(settings, tools=CatalogFixtureTools())
        cls.client_context = TestClient(create_app(cls.runtime))
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client_context.__exit__(None, None, None)
        cls.tempdir.cleanup()

    def test_health_and_readiness(self) -> None:
        self.assertEqual(self.client.get("/healthz").json(), {"status": "UP"})
        response = self.client.get("/readyz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "READY")
        self.assertEqual(response.json()["model"], self.runtime.settings.openai_model)
        self.assertEqual(
            response.json()["runtimePolicy"]["agentToolTimeoutSeconds"],
            self.runtime.settings.agent_tool_timeout_seconds,
        )
        self.assertEqual(response.json()["toolHealth"]["status"], "READY")

    def test_search_contract_uses_camel_case_and_citations(self) -> None:
        response = self.client.post(
            "/api/v1/rag/search",
            headers={
                "Authorization": "Bearer test-token",
                "X-Trace-Id": "trace-test",
                "X-Tenant-Id": "tenant-test",
                "X-User-Id": "user-test",
            },
            json={
                "query": "购物服务平均覆盖率是多少？",
                "topK": 3,
                "filters": {"documentIds": [], "contentTypes": []},
            },
        )
        self.assertEqual(response.status_code, 200)
        first = response.json()["data"][0]
        self.assertEqual(first["pageStart"], 6)
        self.assertEqual(first["contentType"], "table")
        self.assertTrue(first["documentVersion"].startswith("sha256:"))
        self.assertTrue(first["resourceRef"].startswith("rag:"))
        self.assertNotIn("page_start", first)


class GraphTests(unittest.TestCase):
    def test_rag_subgraph_returns_formula_evidence(self) -> None:
        graph = build_rag_graph()
        result = graph.invoke({"query": "步行指数如何计算？", "top_k": 3})
        self.assertTrue(result["has_evidence"])
        self.assertEqual(result["retrieval_results"][0]["content_type"], "formula")
        self.assertIn("S_a = ∑_{b=1}^{n} W_b", result["context"])
        self.assertIn("第5-6页", result["citations"][0])


if __name__ == "__main__":
    unittest.main()
