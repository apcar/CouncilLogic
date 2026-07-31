from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.models import RunPolicy  # noqa: E402
from model_council.protocol import (  # noqa: E402
    aggregate_juries,
    candidate_label,
    synthesis_prompts,
)
from model_council.workload import (  # noqa: E402
    combined_prompt_chars,
    estimate_workload,
    maximum_proposal_artifact,
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

    def test_synthesis_bound_covers_maximal_all_candidate_tie(
        self,
    ) -> None:
        question = "Bound a tied synthesis."
        providers = ("openai", "anthropic", "gemini", "mistral", "xai")
        labels = [
            candidate_label(index) for index in range(len(providers))
        ]
        artifacts = {
            label: maximum_proposal_artifact(label) for label in labels
        }
        judgments = []
        for juror_index in range(len(providers)):
            ranking = (
                labels[juror_index:] + labels[:juror_index]
            )
            judgments.append(
                {
                    "winner": ranking[0],
                    "ranking": ranking,
                    "confidence": "medium",
                    "abstain": False,
                    "rationale": "r",
                    "material_disagreements": [
                        (
                            f"juror {juror_index} disagreement {item}: "
                            + "x" * 280
                        )[:280]
                        for item in range(4)
                    ],
                    "verification_needed": [
                        (
                            f"juror {juror_index} verification {item}: "
                            + "x" * 280
                        )[:280]
                        for item in range(4)
                    ],
                }
            )
        aggregate = aggregate_juries(judgments, labels)
        self.assertTrue(aggregate["tie"])
        self.assertEqual(set(aggregate["tied_candidates"]), set(labels))
        actual_synthesis_chars = combined_prompt_chars(
            synthesis_prompts(
                question,
                artifacts,
                aggregate,
                judgments,
            )
        )
        plan = estimate_workload(
            question,
            providers,
            RunPolicy(),
        )

        self.assertGreaterEqual(
            plan["stage_prompt_chars"]["synthesis"],
            actual_synthesis_chars,
        )
        boundary_plan = estimate_workload(
            question,
            providers,
            RunPolicy(
                max_stage_prompt_chars=actual_synthesis_chars - 1
            ),
        )
        self.assertIn(
            "synthesis",
            boundary_plan["prompt_limit_exceeded_stages"],
        )

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
