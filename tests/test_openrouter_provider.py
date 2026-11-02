from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from urllib.request import Request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.cli import _doctor  # noqa: E402
from model_council.config import (  # noqa: E402
    DEFAULT_LIVE_PROVIDER_NAMES,
    OPENROUTER_ENDPOINT,
    default_config,
    load_config,
)
from model_council.models import ErrorCategory, ProviderError  # noqa: E402
from model_council.providers.factory import create_provider  # noqa: E402
from model_council.providers.http import HttpResponse, JsonHttpClient  # noqa: E402
from model_council.providers.openrouter import OpenRouterProvider  # noqa: E402
from model_council.secrets import EnvironmentSecretResolver  # noqa: E402

FAKE = "sk-or-v1-FAKE-test-only-0000"


class Transport:
    def __init__(self, *items: HttpResponse) -> None:
        self.items = list(items)
        self.requests: list[Request] = []

    def __call__(self, request: Request, timeout: float) -> HttpResponse:
        self.requests.append(request)
        return self.items.pop(0)


def ok(content: str, finish: str = "stop") -> HttpResponse:
    body = {
        "id": "gen-123",
        "model": "anthropic/claude-sonnet-5.5",
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return HttpResponse(200, {}, json.dumps(body).encode())


def routed_config(name: str = "anthropic"):
    with mock.patch.dict(os.environ, {"MODEL_COUNCIL_ROUTE": "openrouter"}):
        cfg = default_config()
    return next(p for p in cfg.providers if p.name == name)


def client(t: Transport) -> JsonHttpClient:
    return JsonHttpClient(transport=t, sleep=lambda _: None, random_value=lambda: 0.5)


class OpenRouterProviderTests(unittest.TestCase):
    def test_payload_and_parse(self) -> None:
        t = Transport(ok('{"winner":"CANDIDATE_01"}'))
        p = OpenRouterProvider(routed_config(), FAKE, client=client(t))
        r = p.generate(system_prompt="Judge.", user_prompt="Pick.", stage="jury")
        self.assertEqual(r.content, '{"winner":"CANDIDATE_01"}')
        self.assertEqual(r.usage.total_tokens, 15)
        self.assertEqual(r.request_id, "gen-123")
        req = t.requests[0]
        self.assertEqual(req.full_url, OPENROUTER_ENDPOINT)
        self.assertEqual(req.get_header("Authorization"), f"Bearer {FAKE}")
        body = json.loads(req.data)
        self.assertEqual(body["model"], "anthropic/claude-sonnet-5.5")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn("JSON", body["messages"][0]["content"])
        self.assertEqual(body["max_tokens"], 2200)
        self.assertNotIn("enable_thinking", body)

    def test_reasoning_bounded_for_reasoning_seats(self) -> None:
        t = Transport(ok('{"a":1}'))
        p = OpenRouterProvider(routed_config("cohere"), FAKE, client=client(t))
        p.generate(system_prompt="s", user_prompt="u", stage="jury")
        self.assertEqual(json.loads(t.requests[0].data)["reasoning"], {"max_tokens": 800})

    def test_content_filter_is_error(self) -> None:
        t = Transport(ok("", finish="content_filter"))
        p = OpenRouterProvider(routed_config(), FAKE, client=client(t))
        with self.assertRaises(ProviderError) as ctx:
            p.generate(system_prompt="s", user_prompt="u", stage="proposal")
        self.assertEqual(ctx.exception.category, ErrorCategory.CONTENT_FILTER)

    def test_factory_routes_by_endpoint(self) -> None:
        self.assertIsInstance(create_provider(routed_config("gemini"), FAKE), OpenRouterProvider)
        with self.assertRaises(ValueError):
            create_provider(routed_config("gemini"), None)


class RoutingConfigTests(unittest.TestCase):
    def test_env_route_keeps_heterogeneous_lineup(self) -> None:
        with mock.patch.dict(os.environ, {"MODEL_COUNCIL_ROUTE": "openrouter"}):
            routed = default_config()
        direct = default_config()
        self.assertEqual([p.name for p in routed.providers], list(DEFAULT_LIVE_PROVIDER_NAMES))
        self.assertEqual([p.lineage for p in routed.providers], [p.lineage for p in direct.providers])
        self.assertTrue(all(p.secret_name == "OPENROUTER_API_KEY" for p in routed.providers))
        self.assertTrue(all(p.endpoint == OPENROUTER_ENDPOINT for p in routed.providers))
        self.assertEqual(len({p.model.split("/")[0] for p in routed.providers}), 7)
        self.assertTrue(all(p.endpoint != OPENROUTER_ENDPOINT for p in direct.providers))

    def test_file_route_with_per_seat_override(self) -> None:
        toml = (
            '[run]\nroute = "openrouter"\n'
            '[providers.openai]\nopenrouter_model = "openai/gpt-test"\n'
            '[providers.anthropic]\nroute = "direct"\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text(toml)
            cfg = load_config(path)
        by = {p.name: p for p in cfg.providers}
        self.assertEqual(by["openai"].model, "openai/gpt-test")
        self.assertEqual(by["openai"].secret_name, "OPENROUTER_API_KEY")
        self.assertEqual(by["anthropic"].secret_name, "ANTHROPIC_API_KEY")
        self.assertEqual(by["gemini"].extra, {"reasoning": {"effort": "low"}})
        self.assertEqual(by["cohere"].extra, {"reasoning": {"max_tokens": 800}}) if "cohere" in by else None

    def test_bad_route_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text('[run]\nroute = "nope"\n')
            with self.assertRaises(ValueError):
                load_config(path)


class DoctorOpenRouterTests(unittest.TestCase):
    def _run(self, env):
        calls = []

        def transport(request, timeout):
            calls.append(request.full_url)
            return HttpResponse(200, {}, b"{}")

        with mock.patch.dict(os.environ, {"MODEL_COUNCIL_ROUTE": "openrouter"}):
            cfg = default_config()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = replace(cfg, data_dir=Path(tmp))
            out = StringIO()
            with redirect_stdout(out):
                _doctor(cfg, as_json=True, check_keys=False,
                        resolver=EnvironmentSecretResolver(env), transport=transport)
        return json.loads(out.getvalue()), out.getvalue(), calls

    def test_present_never_printed(self) -> None:
        payload, raw, calls = self._run({"OPENROUTER_API_KEY": FAKE})
        self.assertEqual(calls, [])
        self.assertTrue(payload["ready"])
        seat = next(c for c in payload["checks"] if c["check"] == "provider:xai")
        self.assertEqual(seat["route"], "openrouter")
        self.assertEqual(seat["credential"], "OPENROUTER_API_KEY present")
        self.assertNotIn(FAKE, raw)

    def test_missing(self) -> None:
        payload, raw, _ = self._run({})
        self.assertFalse(payload["ready"])
        seat = next(c for c in payload["checks"] if c["check"] == "provider:openai")
        self.assertEqual(seat["credential"], "OPENROUTER_API_KEY missing")


if __name__ == "__main__":
    unittest.main()
