"""Deterministic workload planning for bounded council runs."""

from __future__ import annotations

import json
from typing import Any, Iterable

from .models import RunPolicy
from .protocol import (
    JURY_LIST_ITEM_MAX_CHARS,
    JURY_LIST_MAX_ITEMS,
    JURY_REPAIR_ERROR_MAX_CHARS,
    JURY_REPAIR_INPUT_MAX_CHARS,
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
    jury_repair_prompts,
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


def _is_utf8_encodable(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


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


def _stage_prompt_chars(
    question: str,
    providers: tuple[str, ...],
    policy: RunPolicy,
) -> dict[str, int]:
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
    if policy.jury_repair_attempts:
        # Repair accepts arbitrary surrounding text around one extractable
        # decision-valid object. A NUL expands to six characters when the raw
        # response is embedded in the outer JSON payload, so fill every byte
        # outside the smallest useful object with NULs for a true upper bound.
        repair_value = {
            "winner": None,
            "ranking": [],
            "confidence": "low",
            "abstain": True,
            "rationale": "",
            "material_disagreements": [],
            "verification_needed": [],
        }
        repair_object = json.dumps(
            repair_value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        repair_response = (
            "\0" * (JURY_REPAIR_INPUT_MAX_CHARS - len(repair_object))
            + repair_object
        )
        repair_system, repair_user, _decision = jury_repair_prompts(
            repair_response,
            "\0" * JURY_REPAIR_ERROR_MAX_CHARS,
            labels,
        )
        stage_prompt_chars["jury_repair"] = combined_prompt_chars(
            (repair_system, repair_user)
        )
    return stage_prompt_chars


def _plain_question_capacity(
    question: str,
    providers: tuple[str, ...],
    policy: RunPolicy,
    current_stage_chars: dict[str, int],
) -> tuple[int | None, int | None, list[str]]:
    """Return exact capacity for appended unescaped ASCII question text.

    Prompt payloads JSON-escape arbitrary question content, so character count
    alone cannot describe every possible future question. A plain ASCII
    character has deterministic one-character growth. Verify that growth
    against both a plain fixture and the supplied question before reporting
    the capacity.
    """

    plain_one = _stage_prompt_chars("x", providers, policy)
    plain_two = _stage_prompt_chars("xx", providers, policy)
    appended = _stage_prompt_chars(question + "x", providers, policy)
    if not (
        plain_one.keys() == plain_two.keys() == current_stage_chars.keys()
        and appended.keys() == current_stage_chars.keys()
    ):
        return None, None, []

    growth = {
        stage: plain_two[stage] - plain_one[stage]
        for stage in plain_one
    }
    if any(
        amount < 0
        or appended[stage] - current_stage_chars[stage] != amount
        for stage, amount in growth.items()
    ):
        return None, None, []

    stage_capacities: dict[str, int | None] = {}
    for stage, amount in growth.items():
        if amount == 0:
            stage_capacities[stage] = (
                None
                if plain_one[stage] <= policy.max_stage_prompt_chars
                else 0
            )
            continue
        fixed_chars = plain_one[stage] - amount
        stage_capacities[stage] = max(
            0,
            (policy.max_stage_prompt_chars - fixed_chars) // amount,
        )

    bounded_stage_capacities = [
        capacity
        for capacity in stage_capacities.values()
        if capacity is not None
    ]
    effective_maximum = min(
        [policy.max_question_chars, *bounded_stage_capacities]
    )
    limiting_stages = sorted(
        stage
        for stage, capacity in stage_capacities.items()
        if capacity is not None and capacity == effective_maximum
    )

    question_headroom = max(0, policy.max_question_chars - len(question))
    for stage, amount in growth.items():
        current_chars = current_stage_chars[stage]
        if current_chars > policy.max_stage_prompt_chars:
            question_headroom = 0
        elif amount:
            question_headroom = min(
                question_headroom,
                (policy.max_stage_prompt_chars - current_chars) // amount,
            )
    return effective_maximum, max(0, question_headroom), limiting_stages


def estimate_workload(
    question: str,
    provider_names: Iterable[str],
    policy: RunPolicy,
    *,
    synthesis_providers: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Project worst-case stage prompt growth before provider execution."""

    providers = tuple(provider_names)
    synthesizers = tuple(
        synthesis_providers
        if synthesis_providers is not None
        else providers[:1]
    )
    question_utf8_valid = _is_utf8_encodable(question)
    stage_prompt_chars = _stage_prompt_chars(question, providers, policy)
    exceeded_stages = sorted(
        stage
        for stage, chars in stage_prompt_chars.items()
        if chars > policy.max_stage_prompt_chars
    )
    (
        effective_plain_question_max_chars,
        plain_question_headroom_chars,
        limiting_stages,
    ) = _plain_question_capacity(
        question,
        providers,
        policy,
        stage_prompt_chars,
    )
    return {
        "question_chars": len(question),
        "question_utf8_valid": question_utf8_valid,
        "provider_count": len(providers),
        "providers": list(providers),
        "synthesis_provider": synthesizers[0] if synthesizers else None,
        "synthesis_providers": list(synthesizers),
        "mandatory_calls": len(providers) * 2 + len(synthesizers),
        "recovery_call_capacity": max(
            0,
            policy.max_calls - len(providers) * 2 - len(synthesizers),
        ),
        "stage_prompt_chars": stage_prompt_chars,
        "max_question_chars": policy.max_question_chars,
        "max_stage_prompt_chars": policy.max_stage_prompt_chars,
        "limits": {
            "question_chars": policy.max_question_chars,
            "stage_prompt_chars": policy.max_stage_prompt_chars,
        },
        "question_limit_exceeded": (
            len(question) > policy.max_question_chars
        ),
        "prompt_limit_exceeded_stages": exceeded_stages,
        "limiting_stages": limiting_stages,
        "within_limits": (
            question_utf8_valid
            and len(question) <= policy.max_question_chars
            and not exceeded_stages
        ),
        "effective_plain_question_max_chars": (
            effective_plain_question_max_chars
        ),
        "plain_question_headroom_chars": plain_question_headroom_chars,
        "plain_question_basis": "unescaped ASCII characters",
        "estimate_basis": "protocol-character-upper-bound",
    }


def require_workload_within_limits(plan: dict[str, Any]) -> None:
    if plan.get("question_utf8_valid") is False:
        raise ValueError(
            "Question contains Unicode surrogate code points and cannot "
            "be encoded as UTF-8"
        )
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
