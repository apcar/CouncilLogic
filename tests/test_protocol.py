from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.protocol import (  # noqa: E402
    PROTOCOL_ID,
    PROTOCOL_VERSION,
    aggregate_juries,
    jury_json_schema,
    jury_prompts,
    jury_repair_prompts,
    parse_jury,
    parse_proposal,
    proposal_json_schema,
    proposal_prompts,
    protocol_hash,
    synthesis_prompts,
)


def judgment(
    winner: str | None,
    ranking: list[str],
    *,
    abstain: bool = False,
    disagreements: list[str] | None = None,
    verification: list[str] | None = None,
) -> dict[str, object]:
    return {
        "winner": winner,
        "ranking": ranking,
        "confidence": "medium",
        "abstain": abstain,
        "rationale": "Candidate evidence was compared directly.",
        "material_disagreements": disagreements or [],
        "verification_needed": verification or [],
    }


def proposal() -> dict[str, object]:
    return {
        "outcome": "Use the bounded workload runner.",
        "evidence_and_reasoning": ["It limits downstream prompt growth."],
        "uncertainty": ["Live-provider variance remains."],
        "verification_needed": ["Run a private soak test."],
    }


class ProposalParsingTests(unittest.TestCase):
    def test_parses_valid_bounded_proposal(self) -> None:
        value = proposal()

        parsed = parse_proposal(json.dumps(value))

        self.assertEqual(parsed, value)

    def test_rejects_oversized_or_extra_proposal_fields(self) -> None:
        oversized = proposal()
        oversized["outcome"] = "x" * 601
        extra = proposal()
        extra["provider"] = "untrusted"

        with self.assertRaisesRegex(ValueError, "at most 600"):
            parse_proposal(json.dumps(oversized))
        with self.assertRaisesRegex(ValueError, "unexpected keys"):
            parse_proposal(json.dumps(extra))

    def test_rejects_serialized_overflow_and_unpaired_surrogate(self) -> None:
        escaped = proposal()
        escaped["outcome"] = '"' * 400
        surrogate = proposal()
        surrogate["outcome"] = "\ud800"

        with self.assertRaisesRegex(
            ValueError, "serialized JSON characters"
        ):
            parse_proposal(json.dumps(escaped))
        with self.assertRaisesRegex(
            ValueError, "unpaired Unicode surrogates"
        ):
            parse_proposal(json.dumps(surrogate))


class JuryParsingTests(unittest.TestCase):
    def test_parses_valid_json(self) -> None:
        value = judgment("A", ["A", "B"])

        parsed = parse_jury(json.dumps(value), ["A", "B"])

        self.assertEqual(parsed, value)

    def test_parses_fenced_json(self) -> None:
        value = judgment("B", ["B", "A"], verification=["Check the source."])
        response = "```json\n" + json.dumps(value, indent=2) + "\n```"

        parsed = parse_jury(response, ["A", "B"])

        self.assertEqual(parsed["winner"], "B")
        self.assertEqual(parsed["verification_needed"], ["Check the source."])

    def test_rejects_malformed_candidate_labels(self) -> None:
        unknown = judgment("C", ["C", "A"])
        duplicate = judgment("A", ["A", "A"])

        with self.assertRaisesRegex(ValueError, "allowed candidate label"):
            parse_jury(json.dumps(unknown), ["A", "B"])
        with self.assertRaisesRegex(ValueError, "every allowed candidate"):
            parse_jury(json.dumps(duplicate), ["A", "B"])

    def test_accepts_strict_abstention(self) -> None:
        value = judgment(
            None,
            [],
            abstain=True,
            disagreements=["The candidates rely on incompatible assumptions."],
            verification=["Obtain the missing primary source."],
        )

        parsed = parse_jury(json.dumps(value), ["A", "B"])

        self.assertTrue(parsed["abstain"])
        self.assertIsNone(parsed["winner"])
        self.assertEqual(parsed["ranking"], [])

    def test_rejects_inconsistent_abstention_and_extra_keys(self) -> None:
        inconsistent = judgment("A", ["A", "B"], abstain=True)
        extra = judgment("A", ["A", "B"])
        extra["provider"] = "untrusted"

        with self.assertRaisesRegex(ValueError, "winner must be null"):
            parse_jury(json.dumps(inconsistent), ["A", "B"])
        with self.assertRaisesRegex(ValueError, "unexpected keys"):
            parse_jury(json.dumps(extra), ["A", "B"])

    def test_rejects_serialized_jury_string_overflow(self) -> None:
        escaped = judgment("A", ["A", "B"])
        escaped["material_disagreements"] = ["\x00" * 100]

        with self.assertRaisesRegex(
            ValueError, "serialized JSON characters"
        ):
            parse_jury(json.dumps(escaped), ["A", "B"])

    def test_jury_rationale_accepts_1000_and_rejects_1001_characters(
        self,
    ) -> None:
        boundary = judgment("A", ["A", "B"])
        boundary["rationale"] = "x" * 1_000
        oversized = dict(boundary)
        oversized["rationale"] = "x" * 1_001

        self.assertEqual(
            len(parse_jury(json.dumps(boundary), ["A", "B"])["rationale"]),
            1_000,
        )
        with self.assertRaisesRegex(ValueError, "at most 1000"):
            parse_jury(json.dumps(oversized), ["A", "B"])

    def test_jury_repair_prompt_locks_the_existing_decision(self) -> None:
        oversized = judgment("B", ["B", "A"])
        oversized["rationale"] = "x" * 1_001

        system, user, decision = jury_repair_prompts(
            json.dumps(oversized),
            "rationale must be at most 1000 characters",
            ["A", "B"],
        )

        self.assertEqual(
            decision,
            {
                "winner": "B",
                "ranking": ["B", "A"],
                "confidence": "medium",
                "abstain": False,
            },
        )
        self.assertIn("not a new evaluation", system)
        self.assertIn("must remain exactly as provided", system)
        self.assertIn("Hard limits are 1000 characters", system)
        self.assertIn("BEGIN_UNTRUSTED_JURY_REPAIR_JSON", user)
        self.assertIn('"winner": "B"', user)
        with self.assertRaisesRegex(ValueError, "at most 6000"):
            jury_repair_prompts(
                "x" * 6_001,
                "validation error",
                ["A", "B"],
            )


class AggregationTests(unittest.TestCase):
    def test_reports_tie_abstention_invalid_and_disagreement(self) -> None:
        juries = [
            judgment(
                "A",
                ["A", "B"],
                disagreements=["Evidence quality differs."],
            ),
            judgment(
                "B",
                ["B", "A"],
                disagreements=["Evidence quality differs."],
            ),
            judgment(
                None,
                [],
                abstain=True,
                verification=["Verify the disputed date."],
            ),
            {"winner": "A"},
        ]

        aggregate = aggregate_juries(juries, ["A", "B"])

        self.assertTrue(aggregate["tie"])
        self.assertIsNone(aggregate["winner"])
        self.assertEqual(aggregate["outcome"], "tie")
        self.assertEqual(aggregate["borda_points"], {"A": 1, "B": 1})
        self.assertEqual(aggregate["win_counts"], {"A": 1, "B": 1})
        self.assertEqual(aggregate["abstentions"], 1)
        self.assertEqual(aggregate["invalid_judgments"], 1)
        self.assertEqual(aggregate["tied_candidates"], ["A", "B"])
        self.assertTrue(aggregate["has_material_disagreement"])
        self.assertEqual(
            aggregate["material_disagreements"], ["Evidence quality differs."]
        )
        self.assertEqual(
            aggregate["verification_needed"], ["Verify the disputed date."]
        )

    def test_aggregation_is_independent_of_candidate_label_input_order(self) -> None:
        juries = [
            judgment("A", ["A", "B", "C"]),
            judgment("A", ["A", "C", "B"]),
            judgment("B", ["B", "A", "C"]),
        ]

        forward = aggregate_juries(juries, ["A", "B", "C"])
        reverse = aggregate_juries(juries, ["C", "B", "A"])

        fields = (
            "winner",
            "ranking",
            "borda_points",
            "win_counts",
            "tie",
            "tied_candidates",
            "consensus",
        )
        for field in fields:
            self.assertEqual(forward[field], reverse[field], field)
        self.assertEqual(forward["winner"], "A")


class PromptAndIdentityTests(unittest.TestCase):
    def test_proposal_schema_requires_every_protocol_field(self) -> None:
        schema = proposal_json_schema()

        self.assertEqual(
            set(schema["required"]),
            {
                "outcome",
                "evidence_and_reasoning",
                "uncertainty",
                "verification_needed",
            },
        )
        self.assertEqual(
            schema["properties"]["outcome"]["maxLength"],
            600,
        )
        self.assertIn(
            "600 characters",
            schema["properties"]["outcome"]["description"],
        )
        self.assertIn(
            "Must contain at most 4 items",
            schema["properties"]["evidence_and_reasoning"][
                "description"
            ],
        )
        self.assertIn(
            "350 characters",
            schema["properties"]["evidence_and_reasoning"]["items"][
                "description"
            ],
        )
        self.assertIn(
            "Must contain at most 3 items",
            schema["properties"]["uncertainty"]["description"],
        )
        self.assertIn(
            "280 characters",
            schema["properties"]["uncertainty"]["items"][
                "description"
            ],
        )
        self.assertIn(
            "Must contain at most 4 items",
            schema["properties"]["verification_needed"][
                "description"
            ],
        )
        self.assertIn(
            "280 characters",
            schema["properties"]["verification_needed"]["items"][
                "description"
            ],
        )
        self.assertFalse(schema["additionalProperties"])

    def test_jury_schema_requires_every_protocol_field(self) -> None:
        schema = jury_json_schema()

        self.assertEqual(set(schema["required"]), {
            "winner",
            "ranking",
            "confidence",
            "abstain",
            "rationale",
            "material_disagreements",
            "verification_needed",
        })
        self.assertIn(
            "1000 characters",
            schema["properties"]["rationale"]["description"],
        )
        self.assertEqual(
            schema["properties"]["rationale"]["maxLength"],
            1_000,
        )
        self.assertIn(
            "Must contain at most 4 items",
            schema["properties"]["material_disagreements"][
                "description"
            ],
        )
        self.assertIn(
            "280 characters",
            schema["properties"]["material_disagreements"]["items"][
                "description"
            ],
        )
        self.assertIn(
            "Must contain at most 4 items",
            schema["properties"]["verification_needed"][
                "description"
            ],
        )
        self.assertIn(
            "280 characters",
            schema["properties"]["verification_needed"]["items"][
                "description"
            ],
        )
        self.assertFalse(schema["additionalProperties"])

    def test_prompts_preserve_protocol_boundaries(self) -> None:
        proposal_system, proposal_user = proposal_prompts("Question?")
        jury_system, jury_user = jury_prompts(
            "Question?", {"B": "Second", "A": "First"}
        )
        synthesis_system, synthesis_user = synthesis_prompts(
            "Question?",
            {"A": "First", "B": "Second"},
            aggregate_juries([judgment("A", ["A", "B"])], ["A", "B"]),
            [judgment("A", ["A", "B"])],
        )

        self.assertIn('"evidence_and_reasoning"', proposal_system)
        self.assertIn("valid finished object", proposal_system)
        self.assertIn(
            "Target at most three evidence_and_reasoning and\n"
            "verification_needed items and at most two uncertainty items",
            proposal_system,
        )
        self.assertIn("rewrite it shorter before returning", proposal_system)
        self.assertIn("hard limits still apply", proposal_system)
        self.assertIn("untrusted question data", proposal_user)
        self.assertIn("metadata-blind", jury_system)
        self.assertIn('"winner"', jury_system)
        self.assertIn(
            "no more than 400 characters in rationale",
            jury_system,
        )
        self.assertIn("two items per array", jury_system)
        self.assertIn("140 characters per array item", jury_system)
        self.assertIn("rewrite it shorter before returning", jury_system)
        self.assertIn("Hard schema limits are at most 1000", jury_system)
        self.assertIn('"B": "Second"', jury_user)
        self.assertIn("not a new juror", synthesis_system)
        self.assertIn("## Dissent", synthesis_system)
        self.assertIn('"aggregate"', synthesis_user)
        self.assertIn('"jury_votes"', synthesis_user)
        self.assertNotIn('"rationale"', synthesis_user)

    def test_protocol_identity_and_stable_hash(self) -> None:
        self.assertEqual(PROTOCOL_ID, "independent-jury")
        self.assertEqual(PROTOCOL_VERSION, "1.2.0-beta")
        self.assertEqual(
            protocol_hash(),
            "75d54e3b70065da230715501b3bebb602e7cae7ab6c8185dd94bac15e1e6da4d",
        )


if __name__ == "__main__":
    unittest.main()
