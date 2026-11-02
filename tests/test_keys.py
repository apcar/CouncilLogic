from __future__ import annotations

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.cli import _doctor  # noqa: E402
from model_council.config import default_config  # noqa: E402
from model_council.keys import check_key, rotation_status  # noqa: E402
from model_council.providers.http import HttpResponse  # noqa: E402
from model_council.providers.openai import OpenAIProvider  # noqa: E402
from model_council.secrets import (  # noqa: E402
    ChainedSecretResolver,
    EnvironmentSecretResolver,
    KeychainSecretResolver,
    OnePasswordSecretResolver,
    default_secret_resolver,
    key_rotated_at,
    redact,
)

FAKE = "sk-FAKE-test-key-0000"


def completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeRunner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class KeychainTests(unittest.TestCase):
    def test_reads_with_w_flag_and_minimal_env(self) -> None:
        runner = FakeRunner(completed(FAKE + "\n"))
        resolver = KeychainSecretResolver(service="svc", runner=runner)
        self.assertEqual(resolver.resolve("OPENAI_API_KEY"), FAKE)
        args, kwargs = runner.calls[0]
        self.assertEqual(
            args,
            ["/usr/bin/security", "find-generic-password", "-s", "svc",
             "-a", "OPENAI_API_KEY", "-w"],
        )
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn(FAKE, repr(resolver))

    def test_missing_item_and_failure(self) -> None:
        resolver = KeychainSecretResolver(runner=FakeRunner(completed("", 44)))
        self.assertIsNone(resolver.resolve("X"))
        resolver = KeychainSecretResolver(runner=FakeRunner(OSError("nope")))
        self.assertIsNone(resolver.resolve("X"))

    def test_updated_at_from_mdat(self) -> None:
        attrs = 'keychain: "x"\n    "mdat"<timedate>=0x3230 "20260801120000Z\\000"\n'
        resolver = KeychainSecretResolver(runner=FakeRunner(completed(attrs)))
        self.assertEqual(
            resolver.updated_at("X"),
            datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
        )


class OnePasswordTests(unittest.TestCase):
    def test_reads_reference(self) -> None:
        runner = FakeRunner(completed(FAKE), completed(FAKE))
        env = {"OPENAI_API_KEY_OP_REF": "op://AI/OpenAI/credential"}
        resolver = OnePasswordSecretResolver(env, executable="/opt/op", runner=runner)
        self.assertEqual(resolver.resolve("OPENAI_API_KEY"), FAKE)
        self.assertEqual(
            runner.calls[0][0],
            ["/opt/op", "read", "--no-newline", "op://AI/OpenAI/credential"],
        )
        self.assertEqual(
            resolver.source_for("OPENAI_API_KEY"),
            "1Password (op://AI/OpenAI/credential)",
        )

    def test_no_reference_or_bad_reference(self) -> None:
        resolver = OnePasswordSecretResolver({}, executable="/opt/op", runner=FakeRunner())
        self.assertIsNone(resolver.resolve("OPENAI_API_KEY"))
        bad = OnePasswordSecretResolver({"K_OP_REF": "sk-raw-key"}, executable="/opt/op")
        with self.assertRaises(ValueError) as caught:
            bad.resolve("K")
        self.assertNotIn("sk-raw-key", str(caught.exception))

    def test_op_failure_returns_none(self) -> None:
        env = {"K_OP_REF": "op://v/i/f"}
        resolver = OnePasswordSecretResolver(
            env, executable="/opt/op", runner=FakeRunner(completed("", 1, "not signed in"))
        )
        self.assertIsNone(resolver.resolve("K"))


class ChainTests(unittest.TestCase):
    def test_default_chain_order(self) -> None:
        env = {"MODEL_COUNCIL_KEYCHAIN_SERVICE": "svc", "X_OP_REF": "op://v/i/f"}
        with patch("model_council.secrets.sys.platform", "darwin"):
            chain = default_secret_resolver(env)
        kinds = [type(r).__name__ for r in chain.resolvers]
        self.assertEqual(
            kinds,
            ["KeychainSecretResolver", "OnePasswordSecretResolver", "EnvironmentSecretResolver"],
        )
        with patch("model_council.secrets.sys.platform", "linux"):
            chain = default_secret_resolver({})
        self.assertEqual([type(r).__name__ for r in chain.resolvers], ["EnvironmentSecretResolver"])

    def test_env_fallback_after_keychain_miss(self) -> None:
        chain = ChainedSecretResolver((
            KeychainSecretResolver(runner=FakeRunner(completed("", 44), completed("", 44))),
            EnvironmentSecretResolver({"K": FAKE}),
        ))
        self.assertEqual(chain.resolve("K"), FAKE)

    def test_rotated_at_env_override(self) -> None:
        when = key_rotated_at(EnvironmentSecretResolver({}), "K", {"K_ROTATED_AT": "2026-01-02"})
        self.assertEqual(when, datetime(2026, 1, 2, tzinfo=timezone.utc))


class RedactionTests(unittest.TestCase):
    def test_redact_and_provider_repr(self) -> None:
        self.assertEqual(redact(f"bad key {FAKE}", FAKE), "bad key [REDACTED]")
        provider = OpenAIProvider(default_config().providers[0], FAKE)
        self.assertNotIn(FAKE, repr(provider))
        self.assertIn("[REDACTED]", repr(provider))


class KeyCheckTests(unittest.TestCase):
    def _transport(self, status: int, body: bytes = b"{}"):
        seen = []

        def transport(request, timeout):
            seen.append(request)
            return HttpResponse(status_code=status, headers={}, body=body)

        return transport, seen

    def test_valid_invalid_and_headers(self) -> None:
        transport, seen = self._transport(200)
        self.assertEqual(check_key("openai", FAKE, transport=transport)["status"], "valid")
        self.assertEqual(seen[0].full_url, "https://api.openai.com/v1/models")
        self.assertEqual(seen[0].get_method(), "GET")
        transport, seen = self._transport(401)
        self.assertEqual(check_key("anthropic", FAKE, transport=transport)["status"], "invalid")
        self.assertEqual(seen[0].get_header("X-api-key"), FAKE)
        transport, seen = self._transport(200)
        check_key("gemini", FAKE, transport=transport)
        self.assertNotIn(FAKE, seen[0].full_url)
        transport, seen = self._transport(200)
        check_key("qwen", FAKE, endpoint="https://dashscope-us.aliyuncs.com/x", transport=transport)
        self.assertEqual(seen[0].full_url, "https://dashscope-us.aliyuncs.com/compatible-mode/v1/models")

    def test_errors_never_echo_key(self) -> None:
        body = json.dumps({"error": {"message": f"key {FAKE} rate limited"}}).encode()
        transport, _ = self._transport(429, body)
        result = check_key("openai", FAKE, transport=transport)
        self.assertEqual(result["status"], "error")
        self.assertNotIn(FAKE, result["detail"])

        def boom(request, timeout):
            raise OSError(f"failed with {FAKE}")

        result = check_key("openai", FAKE, transport=boom)
        self.assertNotIn(FAKE, result["detail"])

    def test_rotation_status(self) -> None:
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        self.assertTrue(rotation_status(now - timedelta(days=120), now=now)["rotation_due"])
        self.assertFalse(rotation_status(now - timedelta(days=10), now=now)["rotation_due"])
        self.assertIsNone(rotation_status(None)["key_age_days"])


class DoctorTests(unittest.TestCase):
    def _doctor(self, check_keys: bool, status: int = 200):
        calls = []

        def transport(request, timeout):
            calls.append(request.full_url)
            return HttpResponse(status_code=status, headers={}, body=b"{}")

        env = {"OPENAI_API_KEY": FAKE, "OPENAI_API_KEY_ROTATED_AT": "2026-01-01"}
        with tempfile.TemporaryDirectory() as tmp:
            from dataclasses import replace
            config = replace(default_config(), data_dir=Path(tmp))
            out = StringIO()
            with redirect_stdout(out):
                code = _doctor(
                    config, as_json=True, check_keys=check_keys,
                    resolver=EnvironmentSecretResolver(env), transport=transport,
                )
        return code, json.loads(out.getvalue()), calls, out.getvalue()

    def test_doctor_without_check_makes_no_requests(self) -> None:
        _, payload, calls, raw = self._doctor(False)
        self.assertEqual(calls, [])
        openai = next(c for c in payload["checks"] if c["check"] == "provider:openai")
        self.assertEqual(openai["credential_source"], "process environment")
        self.assertTrue(openai["rotation_due"])
        self.assertEqual(openai["key_management_url"], "https://platform.openai.com/api-keys")
        self.assertNotIn("key_check", openai)
        self.assertNotIn(FAKE, raw)

    def test_doctor_check_keys_only_for_present_keys(self) -> None:
        _, payload, calls, raw = self._doctor(True, status=401)
        self.assertEqual(calls, ["https://api.openai.com/v1/models"])
        openai = next(c for c in payload["checks"] if c["check"] == "provider:openai")
        self.assertEqual(openai["key_check"]["status"], "invalid")
        self.assertFalse(openai["ready"])
        missing = next(c for c in payload["checks"] if c["check"] == "provider:anthropic")
        self.assertEqual(missing["key_check"]["status"], "skipped")
        self.assertNotIn(FAKE, raw)


if __name__ == "__main__":
    unittest.main()
