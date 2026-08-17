from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.config import Settings


class SettingsTests(unittest.TestCase):
    def settings_with_timeout(self, value: str) -> Settings:
        with patch.dict(
            os.environ,
            {
                "AGENT_TOOL_TIMEOUT_SECONDS": value,
                "AGENT_MAX_RUN_SECONDS": "180",
            },
            clear=False,
        ):
            return Settings.from_env()

    def test_tool_timeout_rejects_catalog_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "greater than the Catalog timeout"):
            self.settings_with_timeout("120")

    def test_tool_timeout_accepts_values_inside_release_window(self) -> None:
        self.assertEqual(self.settings_with_timeout("120.1").agent_tool_timeout_seconds, 120.1)
        self.assertEqual(self.settings_with_timeout("125").agent_tool_timeout_seconds, 125.0)

    def test_tool_timeout_rejects_run_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "lower than AGENT_MAX_RUN_SECONDS"):
            self.settings_with_timeout("180")

    def test_invalid_timeout_aborts_asgi_startup(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_TOOL_TIMEOUT_SECONDS": "120",
                "AGENT_MAX_RUN_SECONDS": "180",
                "LANGGRAPH_SERVICE_TOKEN": "",
                "AGENT_TOOL_SERVICE_TOKEN": "",
                "OPENAI_API_KEY": "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "greater than the Catalog timeout"):
                with TestClient(create_app()):
                    pass

    def test_mongodb_backend_requires_uri(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENT_STORAGE_BACKEND": "mongodb", "AGENT_MONGODB_URI": ""},
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "AGENT_MONGODB_URI"):
                Settings.from_env()

    def test_mongodb_worker_configuration_is_loaded(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AGENT_STORAGE_BACKEND": "mongodb",
                "AGENT_MONGODB_URI": "mongodb://127.0.0.1:27017/?replicaSet=rs0",
                "AGENT_WORKER_ENABLED": "false",
                "AGENT_WORKER_CONCURRENCY": "7",
                "AGENT_WORKER_LEASE_SECONDS": "45",
            },
            clear=False,
        ):
            settings = Settings.from_env()
        self.assertEqual(settings.agent_storage_backend, "mongodb")
        self.assertFalse(settings.agent_worker_enabled)
        self.assertEqual(settings.agent_worker_concurrency, 7)
        self.assertEqual(settings.agent_worker_lease_seconds, 45)

    def test_worker_bounds_are_validated(self) -> None:
        with patch.dict(os.environ, {"AGENT_WORKER_CONCURRENCY": "0"}, clear=False):
            with self.assertRaisesRegex(ValueError, "AGENT_WORKER_CONCURRENCY"):
                Settings.from_env()

    def test_map_result_limit_is_loaded_and_bounded(self) -> None:
        with patch.dict(os.environ, {"AGENT_MAP_RESULT_LIMIT": "50"}, clear=False):
            self.assertEqual(Settings.from_env().agent_map_result_limit, 50)
        for value in ("0", "201"):
            with patch.dict(os.environ, {"AGENT_MAP_RESULT_LIMIT": value}, clear=False):
                with self.assertRaisesRegex(ValueError, "AGENT_MAP_RESULT_LIMIT"):
                    Settings.from_env()


if __name__ == "__main__":
    unittest.main()
