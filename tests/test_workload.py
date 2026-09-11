from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.models import RunPolicy  # noqa: E402
from model_council.protocol import (  # noqa: E402
    JURY_REPAIR_ERROR_MAX_CHARS,
    JURY_REPAIR_INPUT_MAX_CHARS,
    aggregate_juries,
    candidate_label,
    jury_repair_prompts,
    synthesis_prompts,
)
from model_council.workload import (  # noqa: E402
    combined_prompt_chars,
    estimate_workload,
    maximum_proposal_artifact,
    require_workload_within_limits,
)


class WorkloadPlanningTests(unittest.TestCase):
    def test_jury_repair_prompt_is_projected_only_when_enabled(self) -> None:
        providers = ("alpha", "beta", "gamma")

        disabled = estimate_workload(
            "Bound an optional repair.",
            providers,
            RunPolicy(jury_repair_attempts=0),
        )
        enabled = estimate_workload(
            "Bound an optional repair.",
            providers,
            RunPolicy(jury_repair_attempts=1),
        )
        self.assertNotIn("jury_repair", disabled["stage_prompt_chars"])
        self.assertIn("jury_repair", enabled["stage_prompt_chars"])
        self.assertLessEqual(
            enabled["stage_prompt_chars"]["jury_repair"],
            enabled["max_stage_prompt_chars"],
        )
        boundary = estimate_workload(
            "Bound an optional repair.",
            providers,
            RunPolicy(
                jury_repair_attempts=1,
                max_stage_prompt_chars=(
                    enabled["stage_prompt_chars"]["jury_repair"] - 1
                ),
            ),
        )
        self.assertIn(
            "jury_repair", boundary["prompt_limit_exceeded_stages"]
        )

    def test_seven_provider_upper_bound_fits_default_prompt_budget(self) -> None:
        providers = (
            "openai",
            "anthropic",
            "gemini",
            "mistral",
            "xai",
            "qwen",
            "cohere",
        )
        plan = estimate_workload(
            "Choose the safer implementation.",
            providers,
            RunPolicy(max_calls=20),
            synthesis_providers=("openai", "anthropic", "gemini"),
        )

        self.assertTrue(plan["within_limits"])
        self.assertEqual(plan["provider_count"], 7)
        self.assertEqual(
            plan["synthesis_providers"],
            ["openai", "anthropic", "gemini"],
        )
        self.assertEqual(plan["mandatory_calls"], 17)
        self.assertEqual(plan["recovery_call_capacity"], 3)
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

    def test_jury_repair_bound_covers_control_character_expansion(
        self,
    ) -> None:
        providers = (
            "openai",
            "anthropic",
            "gemini",
            "mistral",
            "xai",
            "qwen",
            "cohere",
        )
        labels = [
            candidate_label(index) for index in range(len(providers))
        ]
        repair_object = json.dumps(
            {
                "winner": None,
                "ranking": [],
                "confidence": "low",
                "abstain": True,
                "rationale": "",
                "material_disagreements": [],
                "verification_needed": [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        response = (
            "\0" * (JURY_REPAIR_INPUT_MAX_CHARS - len(repair_object))
            + repair_object
        )
        repair_system, repair_user, _decision = jury_repair_prompts(
            response,
            "\0" * JURY_REPAIR_ERROR_MAX_CHARS,
            labels,
        )
        actual_chars = combined_prompt_chars((repair_system, repair_user))

        plan = estimate_workload(
            "Bound the repair stage.",
            providers,
            RunPolicy(jury_repair_attempts=1, max_calls=20),
            synthesis_providers=("openai", "anthropic", "gemini"),
        )

        self.assertLessEqual(
            actual_chars,
            plan["stage_prompt_chars"]["jury_repair"],
        )
        self.assertLessEqual(
            plan["stage_prompt_chars"]["jury_repair"],
            plan["max_stage_prompt_chars"],
        )
        self.assertTrue(plan["within_limits"])

    def test_plain_question_capacity_reports_exact_synthesis_boundary(
        self,
    ) -> None:
        providers = (
            "openai",
            "anthropic",
            "gemini",
            "mistral",
            "xai",
            "qwen",
            "cohere",
        )
        policy = RunPolicy(jury_repair_attempts=1)
        plan = estimate_workload("A question.", providers, policy)
        maximum = plan["effective_plain_question_max_chars"]

        self.assertIsInstance(maximum, int)
        self.assertEqual(plan["limiting_stages"], ["synthesis"])
        self.assertEqual(
            plan["plain_question_headroom_chars"],
            maximum - len("A question."),
        )
        at_boundary = estimate_workload("x" * maximum, providers, policy)
        over_boundary = estimate_workload(
            "x" * (maximum + 1), providers, policy
        )
        self.assertTrue(at_boundary["within_limits"])
        self.assertEqual(
            at_boundary["stage_prompt_chars"]["synthesis"],
            policy.max_stage_prompt_chars,
        )
        self.assertEqual(
            over_boundary["prompt_limit_exceeded_stages"],
            ["synthesis"],
        )

    def test_plain_question_headroom_accounts_for_json_escaping(self) -> None:
        providers = ("alpha", "beta", "gamma")
        policy = RunPolicy(max_stage_prompt_chars=23_000)
        plain = estimate_workload("x", providers, policy)
        escaped = estimate_workload('"', providers, policy)

        self.assertEqual(
            escaped["effective_plain_question_max_chars"],
            plain["effective_plain_question_max_chars"],
        )
        self.assertEqual(
            escaped["plain_question_headroom_chars"],
            plain["plain_question_headroom_chars"] - 1,
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
            (
                "openai",
                "anthropic",
                "gemini",
                "mistral",
                "xai",
                "qwen",
                "cohere",
            ),
            policy,
        )

        self.assertFalse(plan["question_limit_exceeded"])
        self.assertEqual(
            plan["prompt_limit_exceeded_stages"],
            ["jury", "synthesis"],
        )
        with self.assertRaisesRegex(
            ValueError,
            "Projected council prompt growth",
        ):
            require_workload_within_limits(plan)

    def test_optional_eighth_provider_fits_a_small_bench_question(self) -> None:
        plan = estimate_workload(
            "Canary the optional provider.",
            (
                "openai",
                "anthropic",
                "gemini",
                "mistral",
                "xai",
                "qwen",
                "cohere",
                "upstage",
            ),
            RunPolicy(max_calls=20),
        )

        self.assertTrue(plan["within_limits"])
        self.assertEqual(plan["provider_count"], 8)
        self.assertLessEqual(
            plan["stage_prompt_chars"]["synthesis"],
            plan["max_stage_prompt_chars"],
        )

    def test_synthesis_bound_covers_maximal_all_candidate_tie(
        self,
    ) -> None:
        question = "Bound a tied synthesis."
        providers = (
            "openai",
            "anthropic",
            "gemini",
            "mistral",
            "xai",
            "qwen",
            "cohere",
        )
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

    def test_unpaired_surrogate_is_rejected_by_shared_preflight(self) -> None:
        plan = estimate_workload(
            "invalid \ud800 question",
            ("alpha", "beta", "gamma"),
            RunPolicy(),
        )

        self.assertFalse(plan["question_utf8_valid"])
        self.assertFalse(plan["within_limits"])
        with self.assertRaisesRegex(ValueError, "cannot be encoded as UTF-8"):
            require_workload_within_limits(plan)


if __name__ == "__main__":
    unittest.main()
