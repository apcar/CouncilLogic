"""Agent-facing JSON interface for CouncilLogic.

Agents may plan, run (paid runs only with a human-issued approval token),
inspect and list. Two actions are deliberately unreachable from here:

* ``decide``: the final decision is a human act. If an agent could write the
  decision record, "a human makes the final decision" would be false.
* ``approve``: approving spend is a human act. An agent that could approve
  its own plan would make the approval gate meaningless.

Both stay CLI-only commands that a person runs in a terminal.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
from pathlib import Path
from typing import Any

from .approval import is_paid, plan_record, verify_token
from .config import AppConfig

INTERFACE_VERSION = "1"
AGENT_ACTIONS = ("capabilities", "plan", "run", "inspect", "list")
HUMAN_ONLY_ACTIONS = {
    "decide": (
        "The decision record is human-only. Present the council result to "
        "your human; they run `council decide RUN_ID --accept|--reject|--defer "
        "--note TEXT` themselves."
    ),
    "approve": (
        "Approving a paid run is human-only. Give your human the plan hash "
        "and estimate from the `plan` action; they run `council approve` in a "
        "terminal and hand back --approved-by NAME and the token."
    ),
}
AWAITING = "Awaiting human decision"


class AgentRequestError(ValueError):
    pass


def _selection_namespace(request: dict[str, Any]) -> argparse.Namespace:
    providers = request.get("providers")
    synthesis = request.get("synthesis_providers")
    for name, value in (("providers", providers), ("synthesis_providers", synthesis)):
        if value is not None and (
            not isinstance(value, list) or not all(isinstance(v, str) for v in value)
        ):
            raise AgentRequestError(f"{name} must be a list of provider names")
    namespace = argparse.Namespace(
        providers=",".join(providers) if providers else None,
        synthesis_provider=None,
        synthesis_providers=",".join(synthesis) if synthesis else None,
        config=None,
    )
    from .cli import _RUN_POLICY_OVERRIDES

    for field in _RUN_POLICY_OVERRIDES:
        setattr(namespace, field, None)
    return namespace


def _question(request: dict[str, Any]) -> str:
    question = request.get("question")
    if not isinstance(question, str) or not question.strip():
        raise AgentRequestError("question must be a non-empty string")
    return question.strip()


def select_config(base: AppConfig, request: dict[str, Any], config_path: str | None) -> AppConfig:
    from .cli import _select_run_config

    namespace = _selection_namespace(request)
    namespace.config = config_path
    return _select_run_config(base, namespace)


def guarded_run(
    config: AppConfig,
    question: str,
    *,
    approved_by: str | None,
    approval_token: str | None,
    idempotency_key: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run the council, enforcing human approval for any paid provider."""

    from .cli import _engine, _store

    plan = plan_record(config, question)
    approval: dict[str, Any] | None = None
    token_sha256: str | None = None
    store = _store(config)
    if plan["approval_required"]:
        token_sha256 = verify_token(config.data_dir, plan, approved_by, approval_token)
        approval = store.consume_run_approval(token_sha256, plan["plan_hash"])
    result = _engine(config).run(question, idempotency_key=idempotency_key)
    if token_sha256 is not None and approval is not None:
        store.attach_run_approval(token_sha256, result["run_id"])
        store.append_event(
            result["run_id"],
            "human_run_approval",
            {
                "approved_by": approval["approved_by"],
                "plan_hash": approval["plan_hash"],
                "max_calls": approval["max_calls"],
                "approved_at": approval["approved_at"],
                "token_sha256": token_sha256,
            },
        )
    return result, approval


def _run_summary(result: dict[str, Any], decisions: list[dict[str, Any]]) -> dict[str, Any]:
    from .cli import _council_verdict_lines

    run_id = result["run_id"]
    latest = decisions[-1] if decisions else None
    return {
        "run_id": run_id,
        "status": result.get("status"),
        "completion_quality": result.get("completion_quality"),
        "verdict": _council_verdict_lines(result),
        "aggregate": result.get("aggregate"),
        "self_votes": result.get("self_votes"),
        "synthesis_check": result.get("synthesis_check"),
        "answer": result.get("answer"),
        "warnings": result.get("warnings") or [],
        "human_decision_status": latest["decision"] if latest else "awaiting",
        "human_decisions": decisions,
        "next_step": (
            f"{AWAITING}. This result is advice, not a decision. Show it to "
            f"your human; only they can record `council decide {run_id} "
            "--accept|--reject|--defer --note TEXT`."
            if latest is None
            else f"Human decision recorded: {latest['decision']}."
        ),
    }


def capabilities() -> dict[str, Any]:
    return {
        "interface_version": INTERFACE_VERSION,
        "actions": list(AGENT_ACTIONS),
        "human_only": HUMAN_ONLY_ACTIONS,
        "rules": [
            "Use for big, contested, hard-to-reverse decisions.",
            "Always call plan first; a paid run needs a human-issued approval "
            "token bound to that plan's hash and estimate.",
            "Every run ends in 'Awaiting human decision'. Never present a "
            "council result as the decision.",
        ],
    }


def handle_request(
    request: Any,
    base_config: AppConfig,
    *,
    config_path: str | None = None,
) -> dict[str, Any]:
    """Handle one agent request. Never raises for bad input; returns ok=false."""

    try:
        if not isinstance(request, dict):
            raise AgentRequestError("request must be a JSON object")
        action = request.get("action")
        if action in HUMAN_ONLY_ACTIONS:
            return {
                "ok": False,
                "action": action,
                "error": "human_only",
                "message": HUMAN_ONLY_ACTIONS[action],
            }
        if action not in AGENT_ACTIONS:
            raise AgentRequestError(
                f"unknown action {action!r}; use one of {', '.join(AGENT_ACTIONS)}"
            )
        if action == "capabilities":
            return {"ok": True, "action": action, **capabilities()}
        if action == "plan":
            config = select_config(base_config, request, config_path)
            plan = plan_record(config, _question(request))
            return {
                "ok": True,
                "action": action,
                **plan,
                "next_step": (
                    "Ask your human to review this estimate and run: council "
                    "approve --plan-hash " + plan["plan_hash"] + " --approved-by "
                    "NAME with the same question and provider selection."
                    if plan["approval_required"]
                    else "Mock/offline plan: no approval needed; call run."
                ),
            }
        from .cli import _store
        from .run_lock import ServiceLock

        lock = ServiceLock(base_config.data_dir)
        lock.acquire()
        try:
            from .cli import _refuse_service_managed_cli

            _refuse_service_managed_cli(base_config)
            if action == "run":
                config = select_config(base_config, request, config_path)
                result, approval = guarded_run(
                    config,
                    _question(request),
                    approved_by=request.get("approved_by"),
                    approval_token=request.get("approval_token"),
                    idempotency_key=request.get("idempotency_key"),
                )
                summary = _run_summary(result, [])
                summary["approval"] = approval
                summary["paid"] = is_paid(config)
                return {"ok": True, "action": action, **summary}
            store = _store(base_config)
            if action == "inspect":
                run_id = request.get("run_id")
                if not isinstance(run_id, str):
                    raise AgentRequestError("run_id is required")
                run = store.get_run(run_id)
                if run is None:
                    raise AgentRequestError(f"unknown run: {run_id}")
                if not run.get("result"):
                    return {
                        "ok": True,
                        "action": action,
                        "run_id": run_id,
                        "status": run["status"],
                        "human_decision_status": "awaiting",
                    }
                return {
                    "ok": True,
                    "action": action,
                    **_run_summary(run["result"], store.list_human_decisions(run_id)),
                }
            limit = request.get("limit", 20)
            if not isinstance(limit, int) or limit < 1:
                raise AgentRequestError("limit must be a positive integer")
            runs = store.list_runs(limit=limit)
            return {
                "ok": True,
                "action": action,
                "runs": [
                    {
                        "run_id": run["id"],
                        "status": run["status"],
                        "created_at": run["created_at"],
                        "question_sha256": hashlib.sha256(
                            run["question"].encode("utf-8")
                        ).hexdigest(),
                    }
                    for run in runs
                ],
            }
        finally:
            lock.release()
    except PermissionError as exc:
        return {"ok": False, "error": "approval_required", "message": str(exc)}
    except (AgentRequestError, KeyError, ValueError, RuntimeError, OSError) as exc:
        return {"ok": False, "error": "bad_request", "message": str(exc)}


def config_for(mock: bool, data_dir: str | None, config_path: str | None) -> AppConfig:
    from .config import load_config, mock_config

    if mock:
        return mock_config(data_dir)
    config = load_config(config_path)
    if data_dir:
        config = replace(config, data_dir=Path(data_dir).expanduser())
    return config
