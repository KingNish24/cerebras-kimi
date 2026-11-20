import time
import unittest
from unittest import mock

from app.services import config_test


def draft_catalog() -> dict:
    return {
        "connections": [{
            "id": "conn-1",
            "provider": "kilo",
            "base_url": "https://api.kilo.ai/api/gateway",
            "api_key": "secret-key",
        }],
        "services": {
            "llm": {
                "active_profile_id": "profile-1",
                "active_model_id": "model-1",
                "profiles": [{
                    "id": "profile-1",
                    "name": "Kilo Gateway",
                    "binding": "kilo",
                    "api_key": "",
                    "base_url": "",
                    "models": [{
                        "id": "model-1",
                        "model": "reflection/beam-free",
                        "provider_ref": {
                            "connection_id": "conn-1",
                            "binding": "kilo",
                            "default_base_url": "https://api.kilo.ai/api/gateway",
                        },
                    }],
                }],
            }
        },
    }


class ResolveDraftConfigTests(unittest.TestCase):
    def test_resolves_provider_ref_connection(self) -> None:
        resolved = config_test.resolve_draft_config(draft_catalog(), "llm")
        self.assertEqual(resolved["provider"], "kilo")
        self.assertEqual(resolved["wire_model"], "reflection/beam-free")
        self.assertEqual(resolved["base_url"], "https://api.kilo.ai/api/gateway")
        self.assertEqual(resolved["api_key"], "secret-key")
        self.assertEqual(
            resolved["extra_headers"]["X-KILOCODE-EDITORNAME"], "DeepTutor"
        )

    def test_uses_unsaved_active_selection(self) -> None:
        catalog = draft_catalog()
        catalog["llm_selection"] = {
            "profile_id": "profile-1",
            "model_id": "model-1",
        }
        catalog["services"]["llm"]["active_profile_id"] = None
        catalog["services"]["llm"]["active_model_id"] = None
        resolved = config_test.resolve_draft_config(catalog, "llm")
        self.assertEqual(resolved["wire_model"], "reflection/beam-free")


class RequestShapeTests(unittest.TestCase):
    def test_openai_chat_body(self) -> None:
        config = {
            "provider": "kilo",
            "wire_model": "reflection/beam-free",
            "base_url": "https://api.kilo.ai/api/gateway/",
            "api_key": "secret-key",
            "extra_headers": {"X-KILOCODE-EDITORNAME": "DeepTutor"},
            "reasoning_effort": None,
        }
        url, headers, body = config_test._request_parts(
            config, system="sys", prompt="Reply with OK.", max_tokens=32
        )
        self.assertEqual(url, "https://api.kilo.ai/api/gateway/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer secret-key")
        self.assertEqual(headers["X-KILOCODE-EDITORNAME"], "DeepTutor")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(body["max_tokens"], 32)

    def test_anthropic_body(self) -> None:
        config = {
            "provider": "anthropic",
            "wire_model": "claude-test",
            "base_url": "https://api.anthropic.com/v1",
            "api_key": "key",
            "extra_headers": {},
        }
        url, headers, body = config_test._request_parts(
            config, system="sys", prompt="hi", max_tokens=16
        )
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["x-api-key"], "key")
        self.assertEqual(body["system"], "sys")


class TestRunnerTests(unittest.TestCase):
    def test_runner_emits_completed_with_context_window(self) -> None:
        with mock.patch.object(
            config_test, "complete_with_config", new=mock.AsyncMock(return_value="OK")
        ), mock.patch.object(
            config_test,
            "context_window_for",
            return_value={
                "context_window": 128000,
                "source": "metadata",
                "detail": "",
                "detected_at": 1.0,
            },
        ):
            runner = config_test.ConfigTestRunner()
            test_run = runner.start("llm", draft_catalog())
            for _ in range(200):
                if test_run.is_terminal():
                    break
                time.sleep(0.02)
            events = test_run.snapshot(0)

        self.assertEqual(test_run.status, "completed")
        kinds = [event["type"] for event in events]
        self.assertIn("response", kinds)
        self.assertIn("context_window", kinds)
        self.assertIn("completed", kinds)
        response = next(event for event in events if event["type"] == "response")
        self.assertEqual(response["snippet"], "OK")
        context = next(event for event in events if event["type"] == "context_window")
        self.assertEqual(context["context_window"], 128000)

    def test_probe_failure_is_reported_as_failed_event(self) -> None:
        with mock.patch.object(
            config_test,
            "complete_with_config",
            new=mock.AsyncMock(side_effect=ValueError("HTTP 401: unauthorized")),
        ):
            runner = config_test.ConfigTestRunner()
            test_run = runner.start("llm", draft_catalog())
            for _ in range(200):
                if test_run.is_terminal():
                    break
                time.sleep(0.02)
            events = test_run.snapshot(0)

        self.assertEqual(test_run.status, "failed")
        failed = next(event for event in events if event["type"] == "failed")
        self.assertIn("HTTP 401", failed["message"])


if __name__ == "__main__":
    unittest.main()
