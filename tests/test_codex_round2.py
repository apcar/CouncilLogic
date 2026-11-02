"""Codex round 2: diversity shown before approval; cross-route duplicate models."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.cli import main  # noqa: E402
from model_council.config import (  # noqa: E402
    default_config,
    lineage_report,
    route_via_openrouter,
    validate_run_policy,
)

QUESTION = "Should we sunset the legacy platform this quarter?"
UNKNOWN = "meta-llama/llama-5-405b"


def _call(args: list[str], answer: str | None = None):
    out, err = StringIO(), StringIO()
    seen_before_prompt: list[str] = []

    def fake_input(prompt: str = "") -> str:
        seen_before_prompt.append(out.getvalue())
        out.write(prompt)
        return answer or ""

    with redirect_stdout(out), redirect_stderr(err), patch(
        "builtins.input", fake_input
    ), patch("sys.stdin.isatty", return_value=True):
        code = main(args)
    return code, out.getvalue(), err.getvalue(), seen_before_prompt


class UnknownPrefixCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.config_path = tmp / "council.toml"
        self.config_path.write_text(
            f'[run]\ndata_dir = "{tmp / "data"}"\nroute = "openrouter"\n\n'
            f'[providers.qwen]\nopenrouter_model = "{UNKNOWN}"\n',
            "utf-8",
        )
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("MODEL_COUNCIL_ROUTE", None)
        self.base = ["--config", str(self.config_path)]

    def tearDown(self) -> None:
        self.env.stop()
        self._tmp.cleanup()

    def test_plan_json_has_diversity_and_protocol_hash(self) -> None:
        code, out, err, _ = _call(self.base + ["plan", "--question", QUESTION, "--json"])
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertRegex(plan["protocol_hash"], r"^[0-9a-f]{64}$")
        self.assertEqual(plan["diversity"]["status"], "unverified")
        self.assertEqual(plan["diversity"]["unverified_providers"], ["qwen"])

    def test_text_plan_names_unverified_seats(self) -> None:
        code, out, err, _ = _call(self.base + ["plan", "--question", QUESTION])
        self.assertEqual(code, 0, err)
        self.assertIn("Lineage diversity: UNVERIFIED", out)
        self.assertIn("could not be verified for qwen", out)
        self.assertIn("Protocol hash:", out)

    def test_approve_warns_before_confirmation(self) -> None:
        code, out, err, _ = _call(self.base + ["plan", "--question", QUESTION, "--json"])
        plan_hash = json.loads(out)["plan_hash"]
        code, out, err, before = _call(
            self.base + [
                "approve", "--question", QUESTION,
                "--plan-hash", plan_hash, "--approved-by", "Andrew",
            ],
            answer="wrong",
        )
        self.assertEqual(code, 4)  # declined: nothing approved
        self.assertEqual(len(before), 1, err)
        self.assertIn("Lineage diversity: UNVERIFIED", before[0])
        self.assertIn("could not be verified for qwen", before[0])
        self.assertIn("Protocol hash:", before[0])
        self.assertLess(out.index("could not be verified"), out.index("Type "))


class CrossRouteDuplicateTests(unittest.TestCase):
    def _direct(self):
        with patch.dict(os.environ, {"MODEL_COUNCIL_ROUTE": "direct"}):
            return default_config()

    def test_default_seven_seats_pass(self) -> None:
        config = self._direct()
        report = lineage_report(config.providers)
        self.assertEqual(report["duplicate_models"], [])
        validate_run_policy(config.policy, config.providers, synthesis_provider_count=3)
        routed = tuple(route_via_openrouter(p) for p in config.providers)
        self.assertEqual(lineage_report(routed)["status"], "verified")

    def test_mixed_route_same_model_rejected_with_correct_lineages(self) -> None:
        config = self._direct()
        seats = list(config.providers)
        openai = seats[0]
        # A second seat, routed via OpenRouter, requesting the same OpenAI model;
        # both declare the correct openai-gpt lineage.
        second = replace(
            route_via_openrouter(seats[1]),
            lineage=openai.lineage,
            model=f"openai/{openai.model}",
        )
        seats[1] = second
        report = lineage_report(seats)
        self.assertEqual(report["mismatches"], [])
        self.assertEqual(len(report["duplicate_models"]), 1)
        with self.assertRaisesRegex(ValueError, "Duplicate model identity"):
            validate_run_policy(config.policy, seats, synthesis_provider_count=3)

    def test_case_and_direct_prefix_normalized(self) -> None:
        config = self._direct()
        seats = list(config.providers)
        seats[1] = replace(
            route_via_openrouter(seats[1]),
            lineage=seats[0].lineage,
            model=f"OpenAI/{seats[0].model.upper()}",
        )
        self.assertTrue(lineage_report(seats)["duplicate_models"])

    def test_genuinely_different_models_accepted(self) -> None:
        config = self._direct()
        seats = list(config.providers)
        seats[1] = replace(
            route_via_openrouter(seats[1]),
            lineage=seats[0].lineage,
            model="openai/gpt-5.6-mini",
        )
        report = lineage_report(seats)
        self.assertEqual(report["duplicate_models"], [])
        self.assertEqual(report["mismatches"], [])
        validate_run_policy(config.policy, seats, synthesis_provider_count=3)


if __name__ == "__main__":
    unittest.main()
