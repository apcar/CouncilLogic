from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.models import RunPolicy  # noqa: E402
from model_council.workload import (  # noqa: E402
    estimate_workload,
    require_workload_within_limits,
)


class WorkloadPlanningTests(unittest.TestCase):
    def test_five_provider_upper_bound_fits_default_prompt_budget(self) -> None:
        plan = estimate_workload(
            "Choose the safer implementation.",
            ("openai", "anthropic", "gemini", "mistral", "xai"),
            RunPolicy(),
        )

        self.assertTrue(plan["within_limits"])
        self.assertEqual(plan["provider_count"], 5)
        self.assertLess(
            plan["stage_prompt_chars"]["proposal"],
            plan["stage_prompt_chars"]["jury"],
        )
        self.assertLess(
            plan["stage_prompt_chars"]["jury"],
            plan["stage_prompt_chars"]["synthesis"],
        )
        self.assertLessEqual(
            max(plan["stage_prompt_chars"].values()),
            plan["max_stage_prompt_chars"],
        )

    def test_projected_downstream_growth_can_reject_an_allowed_question(
        self,
    ) -> None:
        policy = RunPolicy(
            max_question_chars=30_000,
            max_stage_prompt_chars=60_000,
        )
        plan = estimate_workload(
            "x" * 30_000,
            ("openai", "anthropic", "gemini", "mistral", "xai"),
            policy,
        )

        self.assertFalse(plan["question_limit_exceeded"])
        self.assertEqual(
            plan["prompt_limit_exceeded_stages"],
            ["synthesis"],
        )
        with self.assertRaisesRegex(
            ValueError,
            "Projected council prompt growth",
        ):
            require_workload_within_limits(plan)

    def test_question_limit_is_reported_separately(self) -> None:
        plan = estimate_workload(
            "eleven chars",
            ("alpha", "beta", "gamma"),
            RunPolicy(max_question_chars=10),
        )

        self.assertTrue(plan["question_limit_exceeded"])
        with self.assertRaisesRegex(ValueError, "Question is too large"):
            require_workload_within_limits(plan)


if __name__ == "__main__":
    unittest.main()
