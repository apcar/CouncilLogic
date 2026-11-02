"""Regression tests for the Codex review of release/0.3.1-candidate (3974c70)."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council import cli as cli_module  # noqa: E402
from model_council.approval import issue_token, plan_record, verify_token  # noqa: E402
from model_council.config import (  # noqa: E402
    default_config,
    lineage_report,
    route_via_openrouter,
    validate_run_policy,
)
from model_council.providers.http import HttpResponse  # noqa: E402
from model_council.keys import check_key  # noqa: E402
from model_council.store import CouncilStore  # noqa: E402

QUESTION = "Should we sunset the legacy platform this quarter?"


def _live(tmp: str):
    with patch.dict(os.environ, {"MODEL_COUNCIL_ROUTE": "direct"}):
        return replace(default_config(), data_dir=Path(tmp))


def _swap(config, index: int, **changes):
    providers = list(config.providers)
    providers[index] = replace(providers[index], **changes)
    return replace(config, providers=tuple(providers))


class PlanHashBindsExecutionConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.config = _live(self._tmp.name)
        self.base = plan_record(self.config, QUESTION)
        self.token = issue_token(self.config.data_dir, self.base, "Andrew")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _assert_needs_fresh_approval(self, changed) -> None:
        plan = plan_record(changed, QUESTION)
        self.assertNotEqual(plan["plan_hash"], self.base["plan_hash"])
        with self.assertRaises(PermissionError):
            verify_token(changed.data_dir, plan, "Andrew", self.token)

    def test_unchanged_config_keeps_hash(self) -> None:
        verify_token(self.config.data_dir, plan_record(self.config, QUESTION), "Andrew", self.token)

    def test_reasoning_setting(self) -> None:
        extra = {**self.config.providers[0].extra, "reasoning": {"effort": "high"}}
        self._assert_needs_fresh_approval(_swap(self.config, 0, extra=extra))

    def test_temperature(self) -> None:
        extra = {**self.config.providers[1].extra, "temperature": 0.9}
        self._assert_needs_fresh_approval(_swap(self.config, 1, extra=extra))

    def test_provider_preferences(self) -> None:
        extra = {**self.config.providers[2].extra, "provider": {"order": ["x"]}}
        self._assert_needs_fresh_approval(_swap(self.config, 2, extra=extra))

    def test_credential_name_not_value(self) -> None:
        self._assert_needs_fresh_approval(
            _swap(self.config, 0, secret_name="OTHER_OPENAI_KEY")
        )
        self.assertNotIn("sk-", json.dumps(self.base))

    def test_max_attempts(self) -> None:
        self._assert_needs_fresh_approval(_swap(self.config, 3, max_attempts=5))

    def test_jury_output_cap_below_largest_cap(self) -> None:
        provider = self.config.providers[0]
        caps = dict(provider.stage_max_output_tokens)
        self.assertGreater(max(caps.values()), 3_100)
        caps["jury"] = 3_100
        changed = _swap(self.config, 0, stage_max_output_tokens=caps)
        self.assertEqual(
            plan_record(changed, QUESTION)["estimate"], self.base["estimate"]
        )
        self._assert_needs_fresh_approval(changed)

    def test_endpoint_route_and_openrouter_model(self) -> None:
        self._assert_needs_fresh_approval(
            _swap(self.config, 4, endpoint="https://example.invalid/v1/chat/completions")
        )
        routed = replace(
            self.config,
            providers=tuple(route_via_openrouter(p) for p in self.config.providers),
        )
        self._assert_needs_fresh_approval(routed)
        other = _swap(routed, 0, model="openai/gpt-5.6-mini")
        self.assertNotEqual(
            plan_record(other, QUESTION)["plan_hash"],
            plan_record(routed, QUESTION)["plan_hash"],
        )

    def test_protocol_hash(self) -> None:
        with patch("model_council.approval.protocol_hash", return_value="0" * 64):
            plan = plan_record(self.config, QUESTION)
        self.assertNotEqual(plan["plan_hash"], self.base["plan_hash"])
        self.assertEqual(len(self.base["protocol_hash"]), 64)


class LineageVerificationTests(unittest.TestCase):
    def _routed(self):
        with patch.dict(os.environ, {"MODEL_COUNCIL_ROUTE": "direct"}):
            config = default_config()
        return tuple(route_via_openrouter(p) for p in config.providers)

    def test_default_routed_seats_verified(self) -> None:
        report = lineage_report(self._routed())
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["lineage_count"], 7)

    def test_all_seats_on_one_model_rejected(self) -> None:
        seats = tuple(replace(p, model="openai/gpt-5.6-sol") for p in self._routed())
        report = lineage_report(seats)
        self.assertEqual(report["status"], "invalid")
        self.assertEqual(report["lineage_count"], 1)
        self.assertEqual(len(report["mismatches"]), 6)
        self.assertEqual(len(report["duplicate_models"]), 6)
        with patch.dict(os.environ, {"MODEL_COUNCIL_ROUTE": "direct"}):
            policy = default_config().policy
        with self.assertRaisesRegex(ValueError, "does not match seat lineage"):
            validate_run_policy(policy, seats, synthesis_provider_count=3)

    def test_mismatch_rejected(self) -> None:
        seats = list(self._routed())
        seats[0] = replace(seats[0], model="anthropic/claude-sonnet-5.5-alt")
        report = lineage_report(seats)
        self.assertEqual(report["status"], "invalid")
        self.assertIn("anthropic-claude", report["mismatches"][0])

    def test_duplicate_model_rejected(self) -> None:
        seats = list(self._routed())
        seats[0] = replace(seats[0], lineage="anthropic-claude", model=seats[1].model)
        report = lineage_report(seats)
        self.assertTrue(report["duplicate_models"])
        with self.assertRaisesRegex(ValueError, "Duplicate model identity"):
            validate_run_policy(default_config().policy, seats, synthesis_provider_count=3)

    def test_unknown_prefix_is_unverified(self) -> None:
        seats = list(self._routed())
        seats[5] = replace(seats[5], model="meta-llama/llama-5-405b")
        report = lineage_report(seats)
        self.assertEqual(report["status"], "unverified")
        self.assertEqual(report["unverified_providers"], [seats[5].name])
        plan = plan_record(
            replace(default_config(), providers=tuple(seats)), QUESTION
        )
        self.assertEqual(plan["diversity"]["status"], "unverified")


class KeyRedactionBoundaryTests(unittest.TestCase):
    def test_key_starting_at_190_is_fully_redacted(self) -> None:
        key = "sk-synthetic-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        message = "x" * 190 + key + " trailing"
        body = json.dumps({"error": {"message": message}}).encode()

        def transport(request, timeout):
            return HttpResponse(status_code=500, headers={}, body=body)

        detail = check_key("openai", key, transport=transport)["detail"]
        self.assertNotIn(key[:10], detail)
        self.assertNotIn("sk-synth", detail)
        self.assertTrue(detail.endswith("x" * 10 + "[REDACTED]"), detail[-30:])


class ResumeProvenanceTests(unittest.TestCase):
    def _call(self, args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_module.main(args)
        return code, out.getvalue(), err.getvalue()

    def _paid_run(self, tmp: str) -> str:
        # An unapproved paid run created straight through the library.
        config = _live(tmp)
        policy = {
            **config.policy.to_dict(),
            "synthesis_provider": config.synthesis_provider,
            "synthesis_fallbacks": list(config.synthesis_fallbacks),
        }
        return CouncilStore(Path(tmp)).create_run(
            question=QUESTION, protocol_id="p", protocol_version="v",
            protocol_hash="h", provider_configs=[p.to_dict() for p in config.providers],
            policy=policy,
        )

    def test_unapproved_paid_run_is_not_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MODEL_COUNCIL_ROUTE": "direct"}
        ):
            run_id = self._paid_run(tmp)
            dispatched: list[str] = []

            class Engine:
                def resume(self, rid):
                    dispatched.append(rid)

            def guarded_engine(config, resolver=None):
                return Engine()

            with patch.object(cli_module, "_engine", guarded_engine):
                code, _out, err = self._call(["--data-dir", tmp, "resume", run_id])
            self.assertEqual(code, 4)
            self.assertIn("no recorded human approval", err)
            self.assertEqual(dispatched, [])

    def test_approved_paid_run_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"MODEL_COUNCIL_ROUTE": "direct"}
        ):
            run_id = self._paid_run(tmp)
            store = CouncilStore(Path(tmp))
            digest = hashlib.sha256(b"token").hexdigest()
            store.record_run_approval(digest, "h" * 64, "Andrew", 20)
            store.consume_run_approval(digest, "h" * 64)
            store.attach_run_approval(digest, run_id)
            dispatched: list[str] = []

            class Engine:
                def resume(self, rid):
                    dispatched.append(rid)
                    return {"run_id": rid, "status": "completed", "warnings": []}

            def stub_engine(config, resolver=None):
                return Engine()

            with patch.object(cli_module, "_engine", stub_engine), patch.object(
                cli_module, "_print_result", lambda *a, **k: None
            ):
                code, _out, err = self._call(["--data-dir", tmp, "resume", run_id])
            self.assertEqual(code, 0, err)
            self.assertEqual(dispatched, [run_id])


if __name__ == "__main__":
    unittest.main()
