"""Tests for PromptLab. No model calls and no network access are required."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playground import (  # noqa: E402
    PRESET_QUESTIONS,
    STRATEGIES,
    BudgetError,
    Config,
    Limits,
    Pricing,
    ProviderError,
    Runtime,
    ValidationError,
    estimate_prompt_tokens,
)

import app as server_app  # noqa: E402


class ContentTests(unittest.TestCase):
    def test_question_bank_matches_srs(self):
        self.assertEqual(len(PRESET_QUESTIONS), 6)
        categories = {question["category"] for question in PRESET_QUESTIONS}
        self.assertEqual(
            categories,
            {
                "Explanation",
                "Summarisation",
                "Classification",
                "Reasoning",
                "Programming",
                "Comparison",
            },
        )
        for question in PRESET_QUESTIONS:
            self.assertTrue(question["id"])
            self.assertTrue(question["text"].strip())

    def test_three_strategies_preserve_question(self):
        self.assertEqual(
            [strategy["id"] for strategy in STRATEGIES], ["direct", "structured", "cot"]
        )
        self.assertEqual(STRATEGIES[2]["label"], "Step-by-step explanation")
        for strategy in STRATEGIES:
            self.assertIn("{question}", strategy["template"])
            self.assertTrue(strategy["system"].strip())
            self.assertTrue(strategy["label"])


class DemoRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)
        self.runtime = Runtime(Config(provider="demo"), self.data_dir)

    def tearDown(self):
        self.temp.cleanup()

    def test_compare_returns_three_distinct_prompts(self):
        question = PRESET_QUESTIONS[0]
        response = self.runtime.compare(
            {"question": question["text"], "questionId": question["id"]}
        )

        self.assertTrue(response["ok"])
        self.assertTrue(response["demo"])
        self.assertEqual(len(response["results"]), 3)
        self.assertEqual(
            [item["method"] for item in response["results"]],
            ["direct", "structured", "cot"],
        )
        self.assertTrue(all(item["ok"] for item in response["results"]))
        self.assertTrue(response["generation"]["sameForAllMethods"])

        users = []
        for item in response["results"]:
            self.assertIn(question["text"], item["prompt"]["user"])
            self.assertTrue(item["response"])
            users.append(item["prompt"]["user"])
        self.assertEqual(
            len(set(users)), 3, "The three prompts should not be identical."
        )
        self.assertEqual(response["results"][0]["prompt"]["user"], question["text"])

        # Demo mode must not consume the live budget.
        self.assertEqual(self.runtime.usage.snapshot()["requests"], 0)

    def test_estimate_has_three_calls_and_a_ceiling(self):
        estimate = self.runtime.estimate(
            {
                "question": PRESET_QUESTIONS[3]["text"],
                "questionId": PRESET_QUESTIONS[3]["id"],
            }
        )
        self.assertEqual(estimate["calls"], 3)
        self.assertEqual(len(estimate["methods"]), 3)
        self.assertGreater(estimate["tokensCeiling"], estimate["promptTokens"])
        self.assertTrue(estimate["fits"])
        self.assertTrue(estimate["demo"])

    def test_empty_question_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.runtime.compare({"question": "   "})
        with self.assertRaises(ValidationError):
            self.runtime.estimate({"question": ""})

    def test_demo_mode_writes_no_question_text(self):
        question = PRESET_QUESTIONS[1]["text"]
        self.runtime.compare({"question": question, "questionId": "Q2"})
        for path in self.data_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    question, path.read_text(encoding="utf-8", errors="ignore")
                )


class FailureAndBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _runtime(self, **limit_overrides):
        limits = Limits(
            max_requests_per_day=limit_overrides.get("max_requests_per_day", 60),
            max_tokens_per_day=limit_overrides.get("max_tokens_per_day", 120_000),
            max_cost_per_day=limit_overrides.get("max_cost_per_day", 0.50),
            min_interval_ms=0,
            max_output_tokens=limit_overrides.get("max_output_tokens", 700),
        )
        pricing = limit_overrides.get("pricing", Pricing())
        config = Config(provider="ollama", limits=limits, pricing=pricing)
        runtime = Runtime(config, self.data_dir)
        runtime._provider_problem = lambda: None
        return runtime

    def test_one_method_failure_does_not_hide_the_others(self):
        runtime = self._runtime()
        calls = {"count": 0}

        def fake_provider(**_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise ProviderError("simulated upstream failure")
            return {
                "content": "A successful answer.",
                "promptTokens": 10,
                "completionTokens": 4,
                "finishReason": "stop",
            }

        runtime._call_provider = fake_provider
        response = runtime.compare(
            {
                "question": "Explain the difference between a process and a thread.",
                "questionId": "Q1",
            }
        )

        self.assertFalse(response["ok"])
        self.assertEqual(
            [item["ok"] for item in response["results"]], [False, True, True]
        )
        self.assertEqual(response["results"][0]["error"]["code"], "provider_error")
        self.assertEqual(response["results"][1]["response"], "A successful answer.")
        self.assertEqual(response["results"][2]["response"], "A successful answer.")
        # The failed call is not billed, so only two calls are recorded.
        self.assertEqual(runtime.usage.snapshot()["requests"], 2)

    def test_request_budget_blocks_before_any_call(self):
        runtime = self._runtime(max_requests_per_day=2)
        called = {"value": False}

        def should_not_run(**_kwargs):
            called["value"] = True
            return {"content": "no", "promptTokens": 1, "completionTokens": 1}

        runtime._call_provider = should_not_run
        with self.assertRaises(BudgetError) as context:
            runtime.compare({"question": "A question?", "questionId": "custom"})
        self.assertEqual(context.exception.status, 429)
        self.assertEqual(context.exception.details["reason"], "requests")
        self.assertFalse(called["value"])
        self.assertEqual(runtime.usage.snapshot()["requests"], 0)

    def test_cost_budget_blocks_before_any_call(self):
        pricing = Pricing(input_per_million=1000.0, output_per_million=1000.0)
        runtime = self._runtime(max_cost_per_day=0.000001, pricing=pricing)
        with self.assertRaises(BudgetError) as context:
            runtime.compare({"question": "A question?", "questionId": "custom"})
        self.assertEqual(context.exception.details["reason"], "cost")

    def test_usage_file_contains_no_prompt_or_answer(self):
        runtime = self._runtime()

        def fake_provider(**_kwargs):
            return {
                "content": "SECRET_MODEL_ANSWER",
                "promptTokens": 10,
                "completionTokens": 4,
                "finishReason": "stop",
            }

        runtime._call_provider = fake_provider
        question = "SECRET_USER_QUESTION"
        runtime.compare({"question": question, "questionId": "custom"})
        usage_text = (self.data_dir / "usage.json").read_text(encoding="utf-8")
        self.assertNotIn(question, usage_text)
        self.assertNotIn("SECRET_MODEL_ANSWER", usage_text)
        self.assertIn('"requests": 3', usage_text)


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_api_key_is_not_exposed_in_public_views(self):
        secret = "sk-test-super-secret-key"
        runtime = Runtime(
            Config(
                provider="openai",
                openai_model="test-model",
                openai_key=secret,
                limits=Limits(min_interval_ms=0),
            ),
            self.data_dir,
        )
        blob = json.dumps(runtime.public_config()) + json.dumps(runtime.health())
        self.assertNotIn(secret, blob)
        self.assertIn("test-model", blob)
        self.assertEqual(
            runtime._redact(f"provider said {secret}"), "provider said [redacted]"
        )

    def test_model_can_be_configured_from_environment(self):
        with mock.patch.dict(os.environ, {"PROMPT_MODEL": "env-model"}, clear=False):
            config = Config.from_env(provider="ollama")
        self.assertEqual(config.ollama_model, "env-model")
        self.assertEqual(config.default_model(), "env-model")

    def test_remote_hosted_endpoint_must_use_https(self):
        with mock.patch.dict(
            os.environ,
            {
                "PROMPT_PROVIDER": "openai",
                "PROMPT_OPENAI_URL": "http://example.com/v1",
                "PROMPT_OPENAI_KEY": "test-key",
            },
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                Config.from_env()

    def test_remote_ollama_endpoint_must_use_https(self):
        with mock.patch.dict(
            os.environ,
            {"PROMPT_OLLAMA_URL": "http://example.com:11434"},
            clear=False,
        ):
            with self.assertRaises(RuntimeError):
                Config.from_env(provider="ollama")

    def test_loopback_hosted_endpoint_may_use_http(self):
        with mock.patch.dict(
            os.environ,
            {
                "PROMPT_OPENAI_URL": "http://127.0.0.1:8080/v1",
                "PROMPT_OPENAI_KEY": "test-key",
            },
            clear=False,
        ):
            config = Config.from_env(provider="openai")
        self.assertEqual(config.openai_url, "http://127.0.0.1:8080/v1")

    def test_prompt_estimate_is_sane(self):
        tokens = estimate_prompt_tokens("system", "a user question")
        self.assertGreater(tokens, 8)


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Runtime(Config(provider="demo"), Path(self.temp.name))
        server_app.RUNTIME = self.runtime
        self.server = server_app.PromptLabServer(
            ("127.0.0.1", 0), server_app.PromptLabHandler
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )
        self.thread.daemon = True
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read()

    def _post(self, path, payload):
        url = f"http://127.0.0.1:{self.port}{path}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())

    def test_index_and_config(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"PromptLab", body)
        status, body = self._get("/api/config")
        self.assertEqual(status, 200)
        config = json.loads(body)
        self.assertEqual(len(config["questions"]), 6)
        self.assertEqual(len(config["strategies"]), 3)
        self.assertNotIn("openai_key", body.decode("utf-8"))

    def test_compare_endpoint_returns_three_results(self):
        question = PRESET_QUESTIONS[4]
        status, payload = self._post(
            "/api/compare",
            {"question": question["text"], "questionId": question["id"]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["results"]), 3)
        self.assertTrue(all(item["ok"] for item in payload["results"]))
        self.assertEqual(payload["question"]["id"], "Q5")


if __name__ == "__main__":
    unittest.main()
