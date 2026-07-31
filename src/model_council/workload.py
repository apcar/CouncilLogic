"""Deterministic workload planning for bounded council runs."""

from __future__ import annotations

from typing import Any, Iterable

from .models import RunPolicy
from .protocol import (
    JURY_LIST_ITEM_MAX_CHARS,
    JURY_LIST_MAX_ITEMS,
    PROTOCOL_ID,
    PROTOCOL_VERSION,
    PROPOSAL_OUTCOME_MAX_CHARS,
    PROPOSAL_REASON_MAX_CHARS,
    PROPOSAL_REASON_MAX_ITEMS,
    PROPOSAL_UNCERTAINTY_MAX_CHARS,
    PROPOSAL_UNCERTAINTY_MAX_ITEMS,
    PROPOSAL_VERIFICATION_MAX_CHARS,
    PROPOSAL_VERIFICATION_MAX_ITEMS,
    candidate_label,
    jury_prompts,
    proposal_prompts,
    synthesis_prompts,
)


def combined_prompt_chars(prompts: tuple[str, str]) -> int:
    system_prompt, user_prompt = prompts
    return len(f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}")


def _filled(prefix: str, length: int) -> str:
    if len(prefix) >= length:
        return prefix[:length]
    return prefix + ("x" * (length - len(prefix)))


def maximum_proposal_artifact(label: str) -> dict[str, Any]:
    """Return a deterministic artifact at every protocol size boundary."""

    return {
        "outcome": _filled(
            f"{label} outcome: ",
            PROPOSAL_OUTCOME_MAX_CHARS,
        ),
        "evidence_and_reasoning": [
            _filled(
                f"{label} reason {index + 1}: ",
                PROPOSAL_REASON_MAX_CHARS,
            )
            for index in range(PROPOSAL_REASON_MAX_ITEMS)
        ],
        "uncertainty": [
            _filled(
                f"{label} uncertainty {index + 1}: ",
                PROPOSAL_UNCERTAINTY_MAX_CHARS,
            )
            for index in range(PROPOSAL_UNCERTAINTY_MAX_ITEMS)
        ],
        "verification_needed": [
            _filled(
                f"{label} verification {index + 1}: ",
                PROPOSAL_VERIFICATION_MAX_CHARS,
            )
            for index in range(PROPOSAL_VERIFICATION_MAX_ITEMS)
        ],
    }


def estimate_workload(
    question: str,
    provider_names: Iterable[str],
    policy: RunPolicy,
) -> dict[str, Any]:
    """Project worst-case stage prompt growth before provider execution."""

    providers = tuple(provider_names)
    labels = [candidate_label(index) for index in range(len(providers))]
    artifacts = {
        label: maximum_proposal_artifact(label) for label in labels
    }
    proposal_chars = combined_prompt_chars(proposal_prompts(question))
    jury_chars = combined_prompt_chars(jury_prompts(question, artifacts))

    reported_count = len(providers) * JURY_LIST_MAX_ITEMS
    aggregate = {
        "protocol_id": PROTOCOL_ID,
        "protocol_version": PROTOCOL_VERSION,
        "winner": None,
        "outcome": "tie" if labels else "invalid",
        "consensus": "divided",
        "ranking": labels,
        "borda_points": {
            label: len(providers) * max(0, len(labels) - 1)
            for label in labels
        },
        "win_counts": {label: len(providers) for label in labels},
        "tie": bool(labels),
        "tied_candidates": labels,
        "valid_judgments": len(providers),
        "counted_judgments": len(providers),
        "abstentions": 0,
        "invalid_judgments": 0,
        "invalid_judgment_reasons": [],
        "has_material_disagreement": True,
        "material_disagreements": [
            _filled(
                f"disagreement {index + 1}: ",
                JURY_LIST_ITEM_MAX_CHARS,
            )
            for index in range(reported_count)
        ],
        "verification_needed": [
            _filled(
                f"verification {index + 1}: ",
                JURY_LIST_ITEM_MAX_CHARS,
            )
            for index in range(reported_count)
        ],
    }
    juries = [
        {
            "winner": labels[index % len(labels)] if labels else None,
            "ranking": labels,
            "confidence": "medium",
            "abstain": False,
        }
        for index in range(len(providers))
    ]
    synthesis_chars = combined_prompt_chars(
        synthesis_prompts(
            question,
            artifacts,
            aggregate,
            juries,
        )
    )
    stage_prompt_chars = {
        "proposal": proposal_chars,
        "jury": jury_chars,
        "synthesis": synthesis_chars,
    }
    exceeded_stages = sorted(
        stage
        for stage, chars in stage_prompt_chars.items()
        if chars > policy.max_stage_prompt_chars
    )
    return {
        "question_chars": len(question),
        "provider_count": len(providers),
        "stage_prompt_chars": stage_prompt_chars,
        "max_question_chars": policy.max_question_chars,
        "max_stage_prompt_chars": policy.max_stage_prompt_chars,
        "question_limit_exceeded": (
            len(question) > policy.max_question_chars
        ),
        "prompt_limit_exceeded_stages": exceeded_stages,
        "within_limits": (
            len(question) <= policy.max_question_chars
            and not exceeded_stages
        ),
        "estimate_basis": "protocol-character-upper-bound",
    }


def require_workload_within_limits(plan: dict[str, Any]) -> None:
    if plan["question_limit_exceeded"]:
        raise ValueError(
            "Question is too large for the configured council workload: "
            f"{plan['question_chars']} characters exceeds "
            f"{plan['max_question_chars']}"
        )
    exceeded = plan["prompt_limit_exceeded_stages"]
    if exceeded:
        details = ", ".join(
            f"{stage}={plan['stage_prompt_chars'][stage]}"
            for stage in exceeded
        )
        raise ValueError(
            "Projected council prompt growth exceeds "
            f"max_stage_prompt_chars={plan['max_stage_prompt_chars']}: "
            f"{details}"
        )
