"""Core logic for PromptLab, a local prompt-engineering playground.

The application compares three prompt styles against the same question and the
same generation settings:

* Direct       - the question with minimal extra instruction.
* Structured   - task, context, constraints and output format.
* Step-by-step - chain-of-thought style, with a concise user-facing explanation.

The implementation is dependency-free.  It talks to a local Ollama instance by
default, and can also call an OpenAI-compatible endpoint whose key stays on the
server.  Demo mode is available for evaluation without any model calls.

Usage limits are enforced server-side: daily request / token / cost ceilings,
sequential paced calls, a per-call output cap, and a pre-flight estimate before
a comparison spends anything.

Privacy: questions and responses are held only in memory for the current
browser session.  The only data written to disk is an anonymous daily usage
counter (no prompts, no answers).  See NFR-07 in the SRS.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

APP_VERSION = "1.0.0"

ROOT = Path(__file__).resolve().parent
CONTENT_DIR = ROOT / "content"

# ---------------------------------------------------------------------------
# Content loading (NFR-05: questions and prompt templates are separate files)
# ---------------------------------------------------------------------------


def _load_json(filename: str) -> Any:
    path = CONTENT_DIR / filename
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing content file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc


def _load_strategies() -> list[dict[str, Any]]:
    raw = _load_json("prompts.json")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("content/prompts.json must contain a non-empty list.")
    required = {"id", "label", "tagline", "description", "color", "system", "template"}
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not required.issubset(item):
            raise RuntimeError(
                "Every prompt entry needs id, label, tagline, description, color, system and template."
            )
        if item["id"] in seen:
            raise RuntimeError(f"Duplicate strategy id: {item['id']}")
        if "{question}" not in str(item["template"]):
            raise RuntimeError(
                f"Strategy {item['id']} must contain the {{question}} placeholder."
            )
        seen.add(str(item["id"]))
    if seen != {"direct", "structured", "cot"}:
        raise RuntimeError(
            "prompts.json must define exactly direct, structured and cot."
        )
    return raw


def _load_questions() -> list[dict[str, Any]]:
    raw = _load_json("questions.json")
    if not isinstance(raw, list) or not (5 <= len(raw) <= 10):
        raise RuntimeError("content/questions.json must contain 5-10 questions.")
    required = {"id", "title", "category", "text"}
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not required.issubset(item):
            raise RuntimeError("Every question needs id, title, category and text.")
        if item["id"] in seen:
            raise RuntimeError(f"Duplicate question id: {item['id']}")
        if not str(item["text"]).strip():
            raise RuntimeError(f"Question {item['id']} has empty text.")
        seen.add(str(item["id"]))
    return raw


def _load_demo_answers() -> dict[str, dict[str, Any]]:
    raw = _load_json("demo_answers.json")
    if not isinstance(raw, dict):
        raise RuntimeError("content/demo_answers.json must contain an object.")
    return raw


STRATEGIES: list[dict[str, Any]] = _load_strategies()
STRATEGIES_BY_ID: dict[str, dict[str, Any]] = {item["id"]: item for item in STRATEGIES}
PRESET_QUESTIONS: list[dict[str, Any]] = _load_questions()
PRESET_BY_ID: dict[str, dict[str, Any]] = {
    item["id"]: item for item in PRESET_QUESTIONS
}
DEMO_CORES: dict[str, dict[str, Any]] = _load_demo_answers()

CATEGORY_COLORS = {
    "Explanation": "amber",
    "Summarisation": "cyan",
    "Classification": "pink",
    "Reasoning": "violet",
    "Programming": "green",
    "Comparison": "orange",
    "Custom": "slate",
}

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PlaygroundError(Exception):
    status = 400
    code = "error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        hint: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.hint = hint
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        payload.update(self.details)
        return payload


class ValidationError(PlaygroundError):
    status = 400
    code = "validation_error"


class BudgetError(PlaygroundError):
    status = 429
    code = "budget_exceeded"


class ProviderError(PlaygroundError):
    status = 502
    code = "provider_error"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    max_requests_per_day: int = 60
    max_tokens_per_day: int = 120_000
    max_cost_per_day: float = 0.50
    min_interval_ms: int = 1_200
    max_output_tokens: int = 700
    max_prompt_chars: int = 6_000

    def as_public(self) -> dict[str, Any]:
        return {
            "maxRequestsPerDay": self.max_requests_per_day,
            "maxTokensPerDay": self.max_tokens_per_day,
            "maxCostPerDay": self.max_cost_per_day,
            "minIntervalMs": self.min_interval_ms,
            "maxOutputTokens": self.max_output_tokens,
            "maxPromptChars": self.max_prompt_chars,
        }


@dataclass(frozen=True)
class Pricing:
    input_per_million: float = 0.0
    output_per_million: float = 0.0
    currency: str = "USD"

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return round(
            (prompt_tokens / 1_000_000) * self.input_per_million
            + (completion_tokens / 1_000_000) * self.output_per_million,
            6,
        )

    def as_public(self) -> dict[str, Any]:
        return {
            "inputPerMillion": self.input_per_million,
            "outputPerMillion": self.output_per_million,
            "currency": self.currency,
        }


@dataclass
class Config:
    provider: str = "ollama"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2:latest"
    openai_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_key: str = ""
    temperature: float = 0.4
    limits: Limits = Limits()
    pricing: Pricing = Pricing()
    timeout_seconds: float = 240.0
    allow_reset: bool = True

    @classmethod
    def from_env(
        cls, provider: str | None = None, model: str | None = None
    ) -> "Config":
        provider = (provider or os.getenv("PROMPT_PROVIDER", "ollama")).strip().lower()
        chosen_model = (
            model if model is not None else os.getenv("PROMPT_MODEL", "")
        ).strip()
        limits = Limits(
            max_requests_per_day=_env_int("PROMPT_MAX_REQUESTS_DAY", 60, minimum=1),
            max_tokens_per_day=_env_int(
                "PROMPT_MAX_TOKENS_DAY", 120_000, minimum=1_000
            ),
            max_cost_per_day=_env_float("PROMPT_MAX_COST_DAY", 0.50, minimum=0.0),
            min_interval_ms=_env_int("PROMPT_MIN_INTERVAL_MS", 1_200, minimum=0),
            max_output_tokens=_env_int("PROMPT_MAX_OUTPUT_TOKENS", 700, minimum=64),
            max_prompt_chars=_env_int("PROMPT_MAX_PROMPT_CHARS", 6_000, minimum=500),
        )
        pricing = Pricing(
            input_per_million=_env_float("PROMPT_INPUT_PRICE_PER_M", 0.0, minimum=0.0),
            output_per_million=_env_float(
                "PROMPT_OUTPUT_PRICE_PER_M", 0.0, minimum=0.0
            ),
            currency=os.getenv("PROMPT_CURRENCY", "USD").strip() or "USD",
        )
        cfg = cls(
            provider=provider,
            ollama_url=os.getenv("PROMPT_OLLAMA_URL", "http://127.0.0.1:11434")
            .strip()
            .rstrip("/"),
            ollama_model=os.getenv("PROMPT_OLLAMA_MODEL", "llama3.2:latest").strip()
            or "llama3.2:latest",
            openai_url=os.getenv("PROMPT_OPENAI_URL", "https://api.openai.com/v1")
            .strip()
            .rstrip("/"),
            openai_model=os.getenv("PROMPT_OPENAI_MODEL", "gpt-4o-mini").strip()
            or "gpt-4o-mini",
            openai_key=(
                os.getenv("PROMPT_OPENAI_KEY") or os.getenv("OPENAI_API_KEY", "")
            ).strip(),
            temperature=max(
                0.0, min(2.0, _env_float("PROMPT_TEMPERATURE", 0.4, minimum=0.0))
            ),
            limits=limits,
            pricing=pricing,
            timeout_seconds=_env_float("PROMPT_TIMEOUT_SECONDS", 240.0, minimum=10.0),
            allow_reset=os.getenv("PROMPT_ALLOW_RESET", "1").strip().lower()
            not in {"0", "false", "no", "off"},
        )
        if chosen_model:
            if cfg.provider == "ollama":
                cfg.ollama_model = chosen_model
            elif cfg.provider == "openai":
                cfg.openai_model = chosen_model
        if cfg.provider == "openai":
            _require_secure_provider_url("PROMPT_OPENAI_URL", cfg.openai_url)
        if cfg.provider == "ollama":
            _require_secure_provider_url("PROMPT_OLLAMA_URL", cfg.ollama_url)
        return cfg

    def default_model(self) -> str:
        if self.provider == "ollama":
            return self.ollama_model
        if self.provider == "openai":
            return self.openai_model
        return "demo"

    def public_provider(self) -> dict[str, Any]:
        if self.provider == "ollama":
            return {
                "type": "ollama",
                "label": "Ollama",
                "model": self.ollama_model,
                "isLocal": True,
            }
        if self.provider == "openai":
            return {
                "type": "openai",
                "label": "OpenAI-compatible",
                "model": self.openai_model,
                "isLocal": False,
            }
        return {"type": "demo", "label": "Demo mode", "model": "demo", "isLocal": True}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _require_secure_provider_url(variable: str, url: str) -> None:
    """Allow HTTPS anywhere, and plain HTTP only on the loopback interface."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https" or parsed.hostname in LOOPBACK_HOSTS:
        return
    raise RuntimeError(
        f"{variable} must use HTTPS for non-local providers "
        "(HTTP is allowed only for loopback services)."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def estimate_tokens(text: str) -> int:
    """Cheap conservative estimate (providers often report exact counts)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def estimate_prompt_tokens(system: str, user: str) -> int:
    # The constant covers chat-template overhead.
    return estimate_tokens(system) + estimate_tokens(user) + 8


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Usage budget (anonymous counters only; no prompts or responses)
# ---------------------------------------------------------------------------


class UsageTracker:
    """Persistent daily budget for model calls.

    The file contains counters and timestamps only.  It never stores question
    text, prompts or answers.
    """

    def __init__(self, path: Path, limits: Limits, pricing: Pricing) -> None:
        self.path = path
        self.limits = limits
        self.pricing = pricing
        self._lock = threading.RLock()
        self._data = self._load()
        with self._lock:
            self._ensure_day_locked()

    def _default_data(self) -> dict[str, Any]:
        return {
            "date": date.today().isoformat(),
            "requests": 0,
            "promptTokens": 0,
            "completionTokens": 0,
            "cost": 0.0,
            "byStrategy": {},
        }

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("byStrategy"), dict):
                base = self._default_data()
                base.update(raw)
                return base
        except (OSError, json.JSONDecodeError):
            pass
        return self._default_data()

    def _save_locked(self) -> None:
        try:
            _atomic_write_json(self.path, self._data)
        except OSError as exc:
            print(f"[promptlab] could not save usage: {exc}", file=sys.stderr)

    def _ensure_day_locked(self) -> None:
        today = date.today().isoformat()
        if self._data.get("date") != today:
            self._data = self._default_data()
            self._save_locked()

    def _strategy_locked(self, strategy_id: str) -> dict[str, Any]:
        return self._data["byStrategy"].setdefault(
            strategy_id,
            {"requests": 0, "promptTokens": 0, "completionTokens": 0, "cost": 0.0},
        )

    def preflight(self, calls: list[tuple[int, int]]) -> None:
        """Reject a batch before any call if its worst case crosses a ceiling."""
        if not calls:
            return
        with self._lock:
            self._ensure_day_locked()
            used_tokens = int(self._data["promptTokens"]) + int(
                self._data["completionTokens"]
            )
            used_cost = float(self._data["cost"])
            used_requests = int(self._data["requests"])
            needed_requests = len(calls)
            needed_prompt = sum(int(prompt) for prompt, _ in calls)
            needed_completion = sum(int(output) for _, output in calls)
            worst_tokens = needed_prompt + needed_completion
            worst_cost = self.pricing.cost(needed_prompt, needed_completion)

            if used_requests + needed_requests > self.limits.max_requests_per_day:
                raise BudgetError(
                    f"This comparison needs {needed_requests} calls, but only "
                    f"{
                        max(0, self.limits.max_requests_per_day - used_requests)
                    } remain today.",
                    hint="Wait for the daily reset, raise PROMPT_MAX_REQUESTS_DAY, or reset the counters in the usage panel.",
                    details={"reason": "requests"},
                )
            if used_tokens + worst_tokens > self.limits.max_tokens_per_day:
                raise BudgetError(
                    "The worst-case token ceiling for this comparison would cross today's token budget.",
                    hint="Lower PROMPT_MAX_OUTPUT_TOKENS or raise PROMPT_MAX_TOKENS_DAY.",
                    details={
                        "reason": "tokens",
                        "usedTokens": used_tokens,
                        "worstCaseTokens": worst_tokens,
                    },
                )
            if (
                worst_cost > 0
                and used_cost + worst_cost > self.limits.max_cost_per_day + 1e-9
            ):
                raise BudgetError(
                    "The worst-case cost for this comparison would cross today's cost budget.",
                    hint="Raise PROMPT_MAX_COST_DAY or switch to the local Ollama provider.",
                    details={
                        "reason": "cost",
                        "usedCost": round(used_cost, 6),
                        "worstCaseCost": worst_cost,
                    },
                )

    def check(self, prompt_tokens: int, max_output_tokens: int) -> None:
        """Guard a single call (used between calls as a safety net)."""
        self.preflight([(prompt_tokens, max_output_tokens)])

    def record_call(
        self,
        strategy_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float,
    ) -> None:
        with self._lock:
            self._ensure_day_locked()
            self._data["requests"] = int(self._data["requests"]) + 1
            self._data["promptTokens"] = int(self._data["promptTokens"]) + int(
                prompt_tokens
            )
            self._data["completionTokens"] = int(self._data["completionTokens"]) + int(
                completion_tokens
            )
            self._data["cost"] = round(float(self._data["cost"]) + float(cost), 6)
            bucket = self._strategy_locked(strategy_id)
            bucket["requests"] += 1
            bucket["promptTokens"] += int(prompt_tokens)
            bucket["completionTokens"] += int(completion_tokens)
            bucket["cost"] = round(float(bucket["cost"]) + float(cost), 6)
            self._save_locked()

    def reset(self) -> None:
        with self._lock:
            self._data = self._default_data()
            self._save_locked()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_day_locked()
            used_tokens = int(self._data["promptTokens"]) + int(
                self._data["completionTokens"]
            )
            used_cost = round(float(self._data["cost"]), 6)
            remaining_requests = max(
                0, self.limits.max_requests_per_day - int(self._data["requests"])
            )
            remaining_tokens = max(0, self.limits.max_tokens_per_day - used_tokens)
            remaining_cost = round(
                max(0.0, self.limits.max_cost_per_day - used_cost), 6
            )
            return {
                "date": self._data["date"],
                "requests": int(self._data["requests"]),
                "promptTokens": int(self._data["promptTokens"]),
                "completionTokens": int(self._data["completionTokens"]),
                "tokens": used_tokens,
                "cost": used_cost,
                "byStrategy": json.loads(json.dumps(self._data["byStrategy"])),
                "limits": self.limits.as_public(),
                "pricing": self.pricing.as_public(),
                "remaining": {
                    "requests": remaining_requests,
                    "tokens": remaining_tokens,
                    "cost": remaining_cost,
                },
                "resetsAt": "local midnight",
            }


# ---------------------------------------------------------------------------
# Pacing
# ---------------------------------------------------------------------------


class Pacer:
    """Serialise provider calls and leave a minimum gap between them."""

    def __init__(self, min_interval_ms: int) -> None:
        self.interval = max(0.0, min_interval_ms / 1000.0)
        self._lock = threading.Lock()
        self._last_start = 0.0

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._lock:
            if self._last_start:
                wait = self.interval - (time.monotonic() - self._last_start)
                if wait > 0:
                    time.sleep(wait)
            self._last_start = time.monotonic()
            yield


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class Runtime:
    """Model orchestration, prompt construction and budget enforcement."""

    def __init__(self, config: Config, data_dir: Path) -> None:
        self.config = config
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.usage = UsageTracker(
            self.data_dir / "usage.json",
            config.limits,
            config.pricing,
        )
        self.pacer = Pacer(config.limits.min_interval_ms)
        self._compare_lock = threading.Lock()
        self.preset_by_id = PRESET_BY_ID

    # -- public views -----------------------------------------------------
    def public_config(self) -> dict[str, Any]:
        return {
            "app": {"name": "PromptLab", "version": APP_VERSION},
            "provider": self.config.public_provider(),
            "defaults": {
                "temperature": self.config.temperature,
                "maxOutputTokens": self.config.limits.max_output_tokens,
            },
            "limits": self.config.limits.as_public(),
            "pricing": self.config.pricing.as_public(),
            "usage": self.usage.snapshot(),
            "allowReset": self.config.allow_reset,
            "privacy": (
                "Questions and responses are kept in memory for this browser session only. "
                "Only anonymous daily usage counters are stored on the server."
            ),
            "strategies": [
                {
                    "id": item["id"],
                    "label": item["label"],
                    "tagline": item["tagline"],
                    "description": item["description"],
                    "color": item["color"],
                    "system": item["system"],
                    "template": item["template"],
                }
                for item in STRATEGIES
            ],
            "questions": [
                {
                    "id": item["id"],
                    "title": item["title"],
                    "category": item["category"],
                    "text": item["text"],
                    "color": CATEGORY_COLORS.get(item["category"], "slate"),
                }
                for item in PRESET_QUESTIONS
            ],
            "demo": self.config.provider == "demo",
        }

    def health(self) -> dict[str, Any]:
        provider = self.config.public_provider()
        if self.config.provider == "demo":
            return {
                "ok": True,
                "state": "demo",
                "provider": provider,
                "message": "Demo mode: answers and latency figures are illustrative. No model calls or quota are used.",
                "models": [],
            }
        if self.config.provider == "openai":
            if not self.config.openai_key:
                return {
                    "ok": False,
                    "state": "misconfigured",
                    "provider": provider,
                    "message": "No API key is configured. Set PROMPT_OPENAI_KEY.",
                    "models": [],
                }
            return {
                "ok": True,
                "state": "ready",
                "provider": provider,
                "message": "Hosted endpoint configured. Health is not probed to avoid a billable request.",
                "models": [],
            }
        if self.config.provider != "ollama":
            return {
                "ok": False,
                "state": "misconfigured",
                "provider": provider,
                "message": f"Unknown provider: {self.config.provider}",
                "models": [],
            }

        url = f"{self.config.ollama_url}/api/tags"
        try:
            request = urllib.request.Request(
                url, headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=2.5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            OSError,
            json.JSONDecodeError,
            ValueError,
        ) as exc:
            return {
                "ok": False,
                "state": "offline",
                "provider": provider,
                "message": f"Ollama is not reachable at {self.config.ollama_url}.",
                "detail": str(exc),
                "models": [],
            }

        models = []
        for item in payload.get("models", []):
            if isinstance(item, dict) and item.get("name"):
                models.append(str(item["name"]))
        wanted = self.config.ollama_model
        model_ok = (
            wanted in models
            or f"{wanted}:latest" in models
            or any(name.split(":")[0] == wanted for name in models)
        )
        if not model_ok:
            return {
                "ok": False,
                "state": "model_missing",
                "provider": provider,
                "message": f"Ollama is running, but the model '{wanted}' is not installed.",
                "hint": f"Run: ollama pull {wanted}",
                "models": models,
            }
        return {
            "ok": True,
            "state": "ready",
            "provider": provider,
            "message": "Ollama is ready.",
            "models": models,
        }

    def estimate(self, payload: dict[str, Any]) -> dict[str, Any]:
        question, question_id = self._parse_question(
            payload.get("question"), payload.get("questionId")
        )
        prompts = self._build_variants(question)
        calls: list[tuple[int, int]] = []
        methods = []
        max_tokens = self.config.limits.max_output_tokens
        for strategy in STRATEGIES:
            prompt = prompts[strategy["id"]]
            prompt_estimate = estimate_prompt_tokens(prompt["system"], prompt["user"])
            methods.append(
                {
                    "id": strategy["id"],
                    "label": strategy["label"],
                    "promptTokens": prompt_estimate,
                    "tokensCeiling": prompt_estimate + max_tokens,
                }
            )
            calls.append((prompt_estimate, max_tokens))

        prompt_tokens = sum(prompt for prompt, _ in calls)
        completion_ceiling = sum(output for _, output in calls)
        cost_ceiling = self.config.pricing.cost(prompt_tokens, completion_ceiling)
        snapshot = self.usage.snapshot()
        remaining = snapshot["remaining"]
        blockers: list[dict[str, str]] = []

        if self.config.provider != "demo":
            if len(calls) > remaining["requests"]:
                blockers.append(
                    {
                        "code": "requests",
                        "message": (
                            f"This comparison needs {len(calls)} live calls, but only "
                            f"{remaining['requests']} remain today."
                        ),
                    }
                )
            if prompt_tokens + completion_ceiling > remaining["tokens"]:
                blockers.append(
                    {
                        "code": "tokens",
                        "message": "The worst-case token ceiling exceeds today's remaining token budget.",
                    }
                )
            if cost_ceiling > remaining["cost"] + 1e-9:
                blockers.append(
                    {
                        "code": "cost",
                        "message": "The worst-case cost exceeds today's remaining cost budget.",
                    }
                )

        return {
            "question": {"id": question_id, "text": question},
            "calls": len(calls),
            "promptTokens": prompt_tokens,
            "completionTokensCeiling": completion_ceiling,
            "tokensCeiling": prompt_tokens + completion_ceiling,
            "costCeiling": cost_ceiling,
            "currency": self.config.pricing.currency,
            "fits": not blockers,
            "blockers": blockers,
            "demo": self.config.provider == "demo",
            "remaining": remaining,
            "methods": methods,
        }

    def compare(self, payload: dict[str, Any]) -> dict[str, Any]:
        question, question_id = self._parse_question(
            payload.get("question"), payload.get("questionId")
        )
        if not self._compare_lock.acquire(blocking=False):
            raise PlaygroundError(
                "A comparison is already running. Wait for it to finish.",
                status=409,
                code="comparison_in_progress",
            )
        try:
            return self._compare_locked(question, question_id)
        finally:
            self._compare_lock.release()

    def _compare_locked(self, question: str, question_id: str) -> dict[str, Any]:
        started_at = utc_now_iso()
        prompts = self._build_variants(question)
        question_meta = self._question_meta(question, question_id)
        model = self.config.default_model()
        max_tokens = self.config.limits.max_output_tokens
        started = time.perf_counter()

        provider_problem = self._provider_problem()
        if provider_problem is not None:
            message, hint = provider_problem
            results = [
                self._error_variant(
                    strategy,
                    prompts[strategy["id"]],
                    ProviderError(message, hint=hint),
                )
                for strategy in STRATEGIES
            ]
            return self._compare_response(
                question_meta,
                model,
                results,
                started_at,
                started,
                ok=False,
            )

        if self.config.provider != "demo":
            calls = []
            for strategy in STRATEGIES:
                prompt = prompts[strategy["id"]]
                calls.append(
                    (
                        estimate_prompt_tokens(prompt["system"], prompt["user"]),
                        max_tokens,
                    )
                )
            try:
                self.usage.preflight(calls)
            except BudgetError:
                raise
            except PlaygroundError:
                raise

        results: list[dict[str, Any]] = []
        for strategy in STRATEGIES:
            prompt = prompts[strategy["id"]]
            try:
                result = self._execute_variant(
                    strategy, question, question_id, prompt, model
                )
            except PlaygroundError as exc:
                results.append(self._error_variant(strategy, prompt, exc))
            except Exception as exc:  # keep other variants alive (NFR-04)
                print(f"[promptlab] unexpected variant error: {exc}", file=sys.stderr)
                results.append(
                    self._error_variant(
                        strategy,
                        prompt,
                        ProviderError("Unexpected error while running this prompt."),
                    )
                )
            else:
                results.append(result)

        return self._compare_response(
            question_meta,
            model,
            results,
            started_at,
            started,
            ok=all(item["ok"] for item in results),
        )

    # -- prompt and model helpers ----------------------------------------
    def _build_variants(self, question: str) -> dict[str, dict[str, str]]:
        variants: dict[str, dict[str, str]] = {}
        for strategy in STRATEGIES:
            system = str(strategy["system"]).strip()
            template = str(strategy["template"]).strip()
            if "{question}" not in template:
                raise RuntimeError(
                    f"Strategy {strategy['id']} has no {{question}} placeholder."
                )
            user = template.replace("{question}", question).strip()
            if len(system) + len(user) > self.config.limits.max_prompt_chars:
                raise ValidationError(
                    f"The assembled prompt is too long (max {
                        self.config.limits.max_prompt_chars
                    } characters)."
                )
            variants[strategy["id"]] = {"system": system, "user": user}
        return variants

    def _parse_question(self, raw: Any, raw_id: Any = None) -> tuple[str, str]:
        if not isinstance(raw, str) or not raw.strip():
            raise ValidationError("Enter a question before starting a comparison.")
        question = raw.strip()
        if len(question) > 2_000:
            raise ValidationError("The question is too long (max 2,000 characters).")
        question_id = str(raw_id or "").strip()[:40]
        if question_id not in self.preset_by_id:
            question_id = "custom"
        return question, question_id

    def _question_meta(self, question: str, question_id: str) -> dict[str, Any]:
        if question_id in self.preset_by_id:
            preset = self.preset_by_id[question_id]
            return {
                "id": preset["id"],
                "title": preset["title"],
                "category": preset["category"],
                "text": question,
                "custom": False,
            }
        # Tolerate a custom question that matches a preset exactly.
        for preset in PRESET_QUESTIONS:
            if preset["text"] == question:
                return {
                    "id": preset["id"],
                    "title": preset["title"],
                    "category": preset["category"],
                    "text": question,
                    "custom": False,
                }
        return {
            "id": "custom",
            "title": "Custom question",
            "category": "Custom",
            "text": question,
            "custom": True,
        }

    def _execute_variant(
        self,
        strategy: dict[str, Any],
        question: str,
        question_id: str,
        prompt: dict[str, str],
        model: str,
    ) -> dict[str, Any]:
        if self.config.provider == "demo":
            return self._demo_variant(strategy, question, question_id, prompt)

        prompt_tokens_estimate = estimate_prompt_tokens(
            prompt["system"], prompt["user"]
        )
        self.usage.check(prompt_tokens_estimate, self.config.limits.max_output_tokens)

        messages = []
        if prompt["system"]:
            messages.append({"role": "system", "content": prompt["system"]})
        messages.append({"role": "user", "content": prompt["user"]})

        with self.pacer.slot():
            started = time.perf_counter()
            call = self._call_provider(
                messages=messages,
                model=model,
                temperature=self.config.temperature,
                max_tokens=self.config.limits.max_output_tokens,
            )
            latency_ms = round((time.perf_counter() - started) * 1000, 1)

        content = (call.get("content") or "").strip()
        if not content:
            raise ProviderError(
                "The provider returned an empty response.",
                hint="Try another model or inspect the provider logs.",
            )

        prompt_tokens, prompt_reported = self._token_count(
            call.get("promptTokens"), prompt_tokens_estimate
        )
        completion_estimate = estimate_tokens(content)
        completion_tokens, completion_reported = self._token_count(
            call.get("completionTokens"), completion_estimate
        )
        cost = self.config.pricing.cost(prompt_tokens, completion_tokens)
        finish_reason = str(call.get("finishReason") or "")
        truncated = finish_reason in {"length", "max_tokens"} or bool(
            call.get("truncated")
        )
        self.usage.record_call(strategy["id"], prompt_tokens, completion_tokens, cost)

        return {
            "method": strategy["id"],
            "label": strategy["label"],
            "ok": True,
            "error": None,
            "response": content,
            "prompt": prompt,
            "latencyMs": latency_ms,
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
            "totalTokens": prompt_tokens + completion_tokens,
            "estimatedTokens": not (prompt_reported and completion_reported),
            "cost": cost,
            "currency": self.config.pricing.currency,
            "finishReason": finish_reason,
            "truncated": truncated,
            "loadMs": call.get("loadMs"),
            "demo": False,
        }

    def _demo_variant(
        self,
        strategy: dict[str, Any],
        question: str,
        question_id: str,
        prompt: dict[str, str],
    ) -> dict[str, Any]:
        core = DEMO_CORES.get(question_id)
        if core is None:
            # A custom question that exactly matches a preset can reuse that
            # preset's illustrative demo answer.
            for preset in PRESET_QUESTIONS:
                if preset["text"] == question:
                    core = DEMO_CORES.get(preset["id"])
                    break
        if core is None:
            snippet = (
                question if len(question) <= 140 else question[:137].rstrip() + "..."
            )
            core = {
                "answer": (
                    "Demo mode is on, so no model was called. A live run would answer "
                    f'the custom question: "{snippet}"'
                ),
                "points": [
                    "This card illustrates the structured output shape, not a model answer.",
                    "Switch PROMPT_PROVIDER to `ollama` (or configure a hosted endpoint) for live results.",
                    "The comparison UI, budgets and prompt inspection work the same way.",
                ],
                "steps": [
                    "Read the custom question and identify what it is asking.",
                    "Choose the reasoning approach requested by this strategy.",
                    "In a live run, the model would reason here and return a concise final answer.",
                ],
                "confidence": "Demo",
                "confidence_why": "No model was called in demo mode.",
            }

        content = _demo_content(strategy["id"], core)
        prompt_tokens = estimate_prompt_tokens(prompt["system"], prompt["user"])
        completion_tokens = estimate_tokens(content)
        latency = {
            "direct": 420,
            "structured": 680,
            "cot": 1_180,
        }.get(strategy["id"], 600) + (len(question) * 7 + len(content) * 2) % 260
        return {
            "method": strategy["id"],
            "label": strategy["label"],
            "ok": True,
            "error": None,
            "response": content,
            "prompt": prompt,
            "latencyMs": float(latency),
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
            "totalTokens": prompt_tokens + completion_tokens,
            "estimatedTokens": True,
            "cost": 0.0,
            "currency": self.config.pricing.currency,
            "finishReason": "stop",
            "truncated": False,
            "loadMs": None,
            "demo": True,
        }

    def _error_variant(
        self,
        strategy: dict[str, Any],
        prompt: dict[str, str],
        error: PlaygroundError,
    ) -> dict[str, Any]:
        return {
            "method": strategy["id"],
            "label": strategy["label"],
            "ok": False,
            "error": {
                "code": error.code,
                "message": error.message,
                **({"hint": error.hint} if error.hint else {}),
            },
            "response": "",
            "prompt": prompt,
            "latencyMs": None,
            "promptTokens": None,
            "completionTokens": None,
            "totalTokens": None,
            "estimatedTokens": None,
            "cost": None,
            "currency": self.config.pricing.currency,
            "finishReason": "",
            "truncated": False,
            "loadMs": None,
            "demo": False,
        }

    def _compare_response(
        self,
        question_meta: dict[str, Any],
        model: str,
        results: list[dict[str, Any]],
        started_at: str,
        started: float,
        *,
        ok: bool,
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "demo": self.config.provider == "demo",
            "question": question_meta,
            "provider": self.config.public_provider(),
            "generation": {
                "model": model,
                "temperature": self.config.temperature,
                "maxOutputTokens": self.config.limits.max_output_tokens,
                "sameForAllMethods": True,
            },
            "results": results,
            "startedAt": started_at,
            "completedAt": utc_now_iso(),
            "elapsedMs": round((time.perf_counter() - started) * 1000, 1),
            "usage": self.usage.snapshot(),
        }

    def _provider_problem(self) -> tuple[str, str | None] | None:
        """Quick pre-flight check so an offline provider fails fast."""
        if self.config.provider == "demo":
            return None
        health = self.health()
        if health["ok"]:
            return None
        return str(health["message"]), health.get("hint")

    def _token_count(self, reported: Any, fallback: int) -> tuple[int, bool]:
        try:
            if reported is None:
                raise ValueError
            value = int(reported)
            if value < 0:
                raise ValueError
            return value, True
        except (TypeError, ValueError):
            return fallback, False

    # -- providers --------------------------------------------------------
    def _call_provider(
        self,
        *,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        if self.config.provider == "ollama":
            return self._call_ollama(messages, model, temperature, max_tokens)
        if self.config.provider == "openai":
            return self._call_openai(messages, model, temperature, max_tokens)
        raise ProviderError(f"Unsupported provider: {self.config.provider}")

    def _call_ollama(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        url = f"{self.config.ollama_url}/api/chat"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout_seconds
            ) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = self._redact(_safe_error_body(exc))
            hint = None
            if exc.code == 404:
                hint = f"The model '{model}' may not be installed. Run: ollama pull {
                    model
                }"
            raise ProviderError(
                f"Ollama returned HTTP {exc.code}.",
                hint=hint,
                details={"providerStatus": exc.code, "providerDetail": detail},
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ProviderError(
                f"Could not reach Ollama at {self.config.ollama_url}.",
                hint="Start it with `ollama serve` and check PROMPT_OLLAMA_URL.",
                details={"providerDetail": self._redact(str(exc))},
            ) from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderError("Ollama returned invalid JSON.") from exc
        if data.get("error"):
            raise ProviderError(f"Ollama error: {self._redact(str(data['error']))}")
        message = data.get("message") or {}
        content = message.get("content") or ""
        load_ns = data.get("load_duration") or 0
        return {
            "content": content,
            "promptTokens": data.get("prompt_eval_count"),
            "completionTokens": data.get("eval_count"),
            "finishReason": data.get("done_reason")
            or ("stop" if data.get("done") else ""),
            "loadMs": round(float(load_ns) / 1_000_000, 1) if load_ns else None,
        }

    def _call_openai(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        if not self.config.openai_key:
            raise ProviderError(
                "No hosted API key is configured.",
                hint="Set PROMPT_OPENAI_KEY or switch PROMPT_PROVIDER back to ollama.",
            )
        url = f"{self.config.openai_url}/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.config.openai_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout_seconds
            ) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = self._redact(_safe_error_body(exc))
            raise ProviderError(
                f"Hosted provider returned HTTP {exc.code}.",
                details={"providerStatus": exc.code, "providerDetail": detail},
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ProviderError(
                "Could not reach the hosted provider.",
                hint="Check PROMPT_OPENAI_URL and your network connection.",
                details={"providerDetail": self._redact(str(exc))},
            ) from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ProviderError("The hosted provider returned invalid JSON.") from exc
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("The hosted provider returned no choices.")
        choice = choices[0] or {}
        message = choice.get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        usage = data.get("usage") or {}
        return {
            "content": content,
            "promptTokens": usage.get("prompt_tokens"),
            "completionTokens": usage.get("completion_tokens"),
            "finishReason": choice.get("finish_reason") or "",
        }

    def _redact(self, text: str) -> str:
        key = self.config.openai_key
        if key and key in text:
            return text.replace(key, "[redacted]")
        return text

    # -- maintenance ------------------------------------------------------
    def reset_usage(self) -> dict[str, Any]:
        self.usage.reset()
        return {"usage": self.usage.snapshot()}


# ---------------------------------------------------------------------------
# Helpers used by Runtime
# ---------------------------------------------------------------------------


def _safe_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read(2_000).decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                error = parsed.get("error")
                if isinstance(error, dict):
                    return str(error.get("message") or error)[:500]
                if error:
                    return str(error)[:500]
            return raw[:500]
        except json.JSONDecodeError:
            return raw[:500]
    except OSError:
        return ""


def _demo_content(strategy_id: str, core: dict[str, Any]) -> str:
    answer = str(core.get("answer") or "").strip()
    code = str(core.get("code") or "").strip()
    points = [str(point) for point in core.get("points", []) if str(point).strip()]
    steps = [str(step) for step in core.get("steps", []) if str(step).strip()]
    confidence = str(core.get("confidence") or "Medium").strip()
    confidence_why = str(core.get("confidence_why") or "").strip()

    if strategy_id == "direct":
        return _join_blocks(answer, code)

    if strategy_id == "structured":
        parts = [f"**Answer**\n{answer}"]
        if code:
            parts.append(code)
        if points:
            parts.append(
                "**Key points**\n" + "\n".join(f"- {point}" for point in points)
            )
        confidence_line = f"**Confidence** - {confidence}"
        if confidence_why:
            confidence_line += f". {confidence_why}"
        parts.append(confidence_line)
        return _join_blocks(*parts)

    if strategy_id == "cot":
        reasoning = "**Step-by-step reasoning**"
        if steps:
            reasoning += "\n" + "\n".join(
                f"{index}. {step}" for index, step in enumerate(steps, 1)
            )
        final = f"**Final answer:** {answer}"
        return _join_blocks(reasoning, final, code)

    return _join_blocks(answer, code)


def _join_blocks(*blocks: str) -> str:
    return "\n\n".join(block for block in blocks if block)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_runtime(
    *,
    provider: str | None = None,
    model: str | None = None,
    data_dir: Path | None = None,
) -> Runtime:
    config = Config.from_env(provider=provider, model=model)
    directory = data_dir or Path(os.getenv("PROMPT_DATA_DIR", ROOT / ".data"))
    return Runtime(config, directory)
