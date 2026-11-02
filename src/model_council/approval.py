"""Plan fingerprints and human approval tokens for paid runs.

A paid run (any non-mock provider) must present a token that a human issued
with ``council approve`` for the exact plan the run would execute. The token
is an HMAC over the plan hash, the approver's name and the call ceiling, keyed
by a random per-data-directory secret that only the local CLI reads. Tokens are
single use: ``council run`` consumes the token when the run is created.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import secrets
from pathlib import Path
from typing import Any

from .config import AppConfig, lineage_report
from .protocol import protocol_hash
from .workload import estimate_workload

TOKEN_PREFIX = "cla1."
APPROVAL_KEY_FILE = "approval.key"
CHARS_PER_TOKEN_ESTIMATE = 4


def is_paid(config: AppConfig) -> bool:
    return any(not provider.name.startswith("mock") for provider in config.providers)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def cost_estimate(config: AppConfig, workload: dict[str, Any]) -> dict[str, Any]:
    """Upper-bound call and token estimate. No currency: prices change."""

    mandatory = int(workload.get("mandatory_calls") or 0)
    recovery = int(workload.get("recovery_call_capacity") or 0)
    max_calls = min(config.policy.max_calls, mandatory + recovery)
    stage_chars = workload.get("stage_prompt_chars") or {}
    largest_prompt = max((int(v) for v in stage_chars.values()), default=0)
    output_caps = [
        provider.output_tokens_for(stage)
        for provider in config.providers
        for stage in ("proposal", "jury", "synthesis")
    ]
    largest_output = max(output_caps, default=0)
    return {
        "paid": is_paid(config),
        "provider_count": len(config.providers),
        "providers": [
            {"name": p.name, "model": p.model} for p in config.providers
        ],
        "healthy_run_calls": 2 * len(config.providers) + 1,
        "mandatory_reserved_calls": mandatory,
        "max_calls": max_calls,
        "max_input_tokens_upper_bound": math.ceil(
            max_calls * largest_prompt / CHARS_PER_TOKEN_ESTIMATE
        ),
        "max_output_tokens_upper_bound": max_calls * largest_output,
        "currency_estimate": None,
        "note": (
            "Upper bounds from protocol prompt sizes and configured output "
            "caps (about 4 characters per token). Multiply by each "
            "provider's current price; CouncilLogic does not track prices."
        ),
    }


def _provider_material(provider: Any) -> dict[str, Any]:
    material = provider.to_dict()
    material["secret_name"] = str(provider.secret_name)
    return material


def plan_record(config: AppConfig, question: str) -> dict[str, Any]:
    """Return the workload plan, cost estimate and the plan hash binding both."""

    workload = estimate_workload(
        question,
        (provider.name for provider in config.providers),
        config.policy,
        synthesis_providers=config.synthesis_providers,
    )
    estimate = cost_estimate(config, workload)
    material = {
        "question_sha256": hashlib.sha256(question.encode("utf-8")).hexdigest(),
        # Bind every execution-relevant provider field: model, lineage,
        # endpoint/route, credential NAME (never its value), attempts, output
        # caps, timeouts and provider extras (reasoning, temperature, routing
        # preferences). Any change forces a fresh human approval.
        "providers": [_provider_material(p) for p in config.providers],
        "protocol_hash": protocol_hash(),
        "diversity": lineage_report(config.providers),
        "synthesis_providers": list(config.synthesis_providers),
        "policy": config.policy.to_dict(),
        "workload": workload,
        "estimate": estimate,
    }
    plan_hash = hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()
    return {
        "plan_hash": plan_hash,
        "question_sha256": material["question_sha256"],
        "estimate": estimate,
        "workload": workload,
        "approval_required": estimate["paid"],
        "protocol_hash": material["protocol_hash"],
        "diversity": material["diversity"],
    }


def _key_path(data_dir: Path) -> Path:
    return Path(data_dir) / APPROVAL_KEY_FILE


def _load_key(data_dir: Path, *, create: bool) -> bytes | None:
    path = _key_path(data_dir)
    if path.exists():
        return bytes.fromhex(path.read_text("ascii").strip())
    if not create:
        return None
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = secrets.token_bytes(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(key.hex())
    return key


def _mac(
    key: bytes, plan_hash: str, approved_by: str, max_calls: int, nonce: str = ""
) -> str:
    message = f"{plan_hash}\n{approved_by}\n{max_calls}".encode("utf-8")
    if nonce:
        message += f"\n{nonce}".encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _clean_name(approved_by: str) -> str:
    name = (approved_by or "").strip()
    if not name or len(name) > 120 or "\n" in name:
        raise ValueError("--approved-by must be a non-empty name (max 120 chars)")
    return name


def issue_token(
    data_dir: Path, plan: dict[str, Any], approved_by: str
) -> str:
    key = _load_key(data_dir, create=True)
    assert key is not None
    name = _clean_name(approved_by)
    # A fresh nonce per approval keeps tokens single-use without making an
    # identical plan permanently un-approvable after its first run.
    nonce = secrets.token_hex(8)
    return TOKEN_PREFIX + nonce + "." + _mac(
        key, plan["plan_hash"], name, int(plan["estimate"]["max_calls"]), nonce
    )


def verify_token(
    data_dir: Path, plan: dict[str, Any], approved_by: str | None, token: str | None
) -> str:
    """Return the token's sha256 when valid for this exact plan; else raise."""

    if not approved_by or not token:
        raise PermissionError(
            "Paid run refused: human approval is required. Run "
            "`council plan --json`, have a human run `council approve` for "
            "that plan, then pass --approved-by NAME --approval-token TOKEN."
        )
    name = _clean_name(approved_by)
    key = _load_key(data_dir, create=False)
    if key is None or not token.startswith(TOKEN_PREFIX):
        raise PermissionError("Paid run refused: approval token is not valid here")
    body = token[len(TOKEN_PREFIX):]
    nonce, _, mac = body.rpartition(".")
    if nonce and not (len(nonce) == 16 and all(c in "0123456789abcdef" for c in nonce)):
        raise PermissionError("Paid run refused: approval token is not valid here")
    expected = _mac(
        key, plan["plan_hash"], name, int(plan["estimate"]["max_calls"]), nonce
    )
    if not hmac.compare_digest(expected, mac):
        raise PermissionError(
            "Paid run refused: approval token does not match this plan "
            "(question, providers, policy or estimate differ) or approver"
        )
    return hashlib.sha256(token.encode("ascii")).hexdigest()
