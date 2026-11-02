#!/usr/bin/env python3
"""PromptLab HTTP server.

Run:
    python3 app.py                 # local Ollama, http://127.0.0.1:7860
    python3 app.py --demo          # zero-call illustrative demo
    python3 app.py --port 9000

No third-party dependencies are required.  The provider key, when a hosted
OpenAI-compatible endpoint is configured, never leaves the server process.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from playground import (
    APP_VERSION,
    BudgetError,
    PlaygroundError,
    Runtime,
    ValidationError,
    build_runtime,
)

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"

LOGGER = logging.getLogger("promptlab")
RUNTIME: Runtime
SERVER_VERSION = f"PromptLab/{APP_VERSION}"

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


class PromptLabHandler(BaseHTTPRequestHandler):
    server_version = SERVER_VERSION
    sys_version = ""

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.debug("%s - %s", self.address_string(), fmt % args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error_json(
        self,
        status: int,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"error": code, "message": message}
        if hint:
            payload["hint"] = hint
        if extra:
            payload.update(extra)
        self._send_json(status, payload)

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
            return
        if not candidate.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
            return
        suffix = candidate.suffix.lower()
        if suffix not in MIME_TYPES:
            self._send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
            return
        try:
            body = candidate.read_bytes()
        except OSError:
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "read_error",
                "Could not read the file.",
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", MIME_TYPES[suffix])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValidationError("Invalid Content-Length header.") from None
        if length <= 0:
            return {}
        if length > 100_000:
            raise PlaygroundError(
                "Request body is too large.",
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                code="payload_too_large",
            )
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValidationError("Request body must be valid JSON.") from None
        if not isinstance(payload, dict):
            raise ValidationError("Request body must be a JSON object.")
        return payload

    def _handle_error(self, exc: PlaygroundError) -> None:
        extra = exc.details.copy()
        if isinstance(exc, BudgetError):
            extra["usage"] = RUNTIME.usage.snapshot()
        self._send_json(exc.status, {**exc.as_dict(), **extra})

    # -- routing ----------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        try:
            if path == "/api/config":
                self._send_json(HTTPStatus.OK, RUNTIME.public_config())
                return
            if path == "/api/health":
                self._send_json(HTTPStatus.OK, RUNTIME.health())
                return
            if path == "/api/usage":
                self._send_json(
                    HTTPStatus.OK,
                    {"usage": RUNTIME.usage.snapshot()},
                )
                return
            if path in {"/api/compare", "/api/estimate", "/api/usage/reset"}:
                self._send_error_json(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "Use POST for this endpoint.",
                )
                return
            if path.startswith("/api/"):
                self._send_error_json(
                    HTTPStatus.NOT_FOUND, "not_found", "Unknown API endpoint."
                )
                return
            self._serve_static(path)
        except Exception:  # pragma: no cover - last-resort guard
            LOGGER.exception("Unhandled GET error")
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "Unexpected server error.",
            )

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/estimate":
                self._send_json(HTTPStatus.OK, RUNTIME.estimate(payload))
                return
            if path == "/api/compare":
                response = RUNTIME.compare(payload)
                successes = sum(1 for item in response["results"] if item["ok"])
                LOGGER.info(
                    "compare question=%s model=%s ok=%s/%s elapsed=%sms",
                    response["question"]["id"],
                    response["generation"]["model"],
                    successes,
                    len(response["results"]),
                    response["elapsedMs"],
                )
                self._send_json(HTTPStatus.OK, response)
                return
            if path == "/api/usage/reset":
                if not RUNTIME.config.allow_reset:
                    self._send_error_json(
                        HTTPStatus.FORBIDDEN,
                        "reset_disabled",
                        "Usage resets are disabled by configuration.",
                    )
                    return
                if payload.get("confirm") != "reset":
                    self._send_error_json(
                        HTTPStatus.BAD_REQUEST,
                        "confirmation_required",
                        'Send {"confirm":"reset"} to reset the counters.',
                    )
                    return
                LOGGER.warning("Usage counters reset by user request")
                self._send_json(HTTPStatus.OK, RUNTIME.reset_usage())
                return
            if path in {"/api/config", "/api/health", "/api/usage"}:
                self._send_error_json(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "Use GET for this endpoint.",
                )
                return
            self._send_error_json(
                HTTPStatus.NOT_FOUND, "not_found", "Unknown API endpoint."
            )
        except PlaygroundError as exc:
            self._handle_error(exc)
        except Exception:  # pragma: no cover - last-resort guard
            LOGGER.exception("Unhandled POST error")
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "internal_error",
                "Unexpected server error. Check the server log.",
            )


class PromptLabServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PromptLab comparison playground")
    parser.add_argument("--host", default=os.getenv("PROMPT_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.getenv("PROMPT_PORT", "7860"))
    )
    parser.add_argument("--demo", action="store_true", help="zero-call demo mode")
    parser.add_argument(
        "--provider",
        choices=["ollama", "openai", "demo"],
        help="override PROMPT_PROVIDER",
    )
    parser.add_argument("--model", help="override the provider model")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.getenv("PROMPT_DATA_DIR", ROOT / ".data")),
        help="where the anonymous usage counters live",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    provider = "demo" if args.demo else args.provider
    global RUNTIME
    try:
        RUNTIME = build_runtime(
            provider=provider, model=args.model, data_dir=args.data_dir
        )
    except (RuntimeError, OSError) as exc:
        print(f"PromptLab configuration error: {exc}", file=sys.stderr)
        return 1

    try:
        server = PromptLabServer((args.host, args.port), PromptLabHandler)
    except OSError as exc:
        print(
            f"Could not start server on {args.host}:{args.port}: {exc}", file=sys.stderr
        )
        return 1

    config = RUNTIME.config
    limits = config.limits
    provider_info = config.public_provider()
    url = f"http://{args.host}:{args.port}"
    print()
    print("  PromptLab  |  same question, three prompting styles")
    print(f"  {url}")
    print(f"  provider   {provider_info['label']}  ({provider_info['model']})")
    if provider_info["isLocal"]:
        print("  privacy    prompts stay local; no hosted API calls")
    print(
        "  budget     "
        f"{limits.max_requests_per_day} calls/day · "
        f"{limits.max_tokens_per_day:,} tokens/day · "
        f"{config.pricing.currency} {limits.max_cost_per_day:.2f}/day"
    )
    print(
        f"  guardrails {limits.min_interval_ms} ms between calls · "
        f"{limits.max_output_tokens} output-token cap · 3 calls per comparison"
    )
    if config.provider == "demo":
        print("  demo mode  answers and latency are illustrative; no model is called")
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            "  warning    server is bound beyond localhost; reset endpoints are exposed"
        )
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping PromptLab.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
