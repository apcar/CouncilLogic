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
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from .approval import is_paid, plan_record, verify_token
from .config import AppConfig
from .run_lock import ServiceLock

SCHEMA_VERSION = "1"
INTERFACE_VERSION = SCHEMA_VERSION
AGENT_ACTIONS = ("capabilities", "plan", "run", "start", "status", "inspect", "list")
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
_SELECTION_FIELDS = (
    "question",
    "providers",
    "synthesis_providers",
    "deadline_seconds",
    "call_timeout_seconds",
)
ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "capabilities": frozenset(),
    "plan": frozenset(_SELECTION_FIELDS),
    "run": frozenset(
        (*_SELECTION_FIELDS, "approved_by", "approval_token", "request_id")
    ),
    "start": frozenset(
        (*_SELECTION_FIELDS, "approved_by", "approval_token", "request_id")
    ),
    "status": frozenset({"run_id"}),
    "inspect": frozenset({"run_id"}),
    "list": frozenset({"limit"}),
}
# Fields that do not change what is run; a retry may differ in them.
_NON_PAYLOAD_FIELDS = frozenset(
    {"schema_version", "action", "approved_by", "approval_token", "request_id"}
)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class AgentRequestError(ValueError):
    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(message)
        self.code = code


def _positive_number(request: dict[str, Any], name: str) -> float | None:
    value = request.get(name)
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise AgentRequestError(f"{name} must be a positive number")
    return float(value)


def _selection_namespace(request: dict[str, Any]) -> argparse.Namespace:
    providers = request.get("providers")
    synthesis = request.get("synthesis_providers")
    for name, value in (("providers", providers), ("synthesis_providers", synthesis)):
        if value is not None and (
            not isinstance(value, list)
            or not value
            or not all(isinstance(v, str) for v in value)
        ):
            raise AgentRequestError(f"{name} must be a non-empty list of provider names")
    namespace = argparse.Namespace(
        providers=",".join(providers) if providers else None,
        synthesis_provider=None,
        synthesis_providers=",".join(synthesis) if synthesis else None,
        config=None,
    )
    from .cli import _RUN_POLICY_OVERRIDES

    for field in _RUN_POLICY_OVERRIDES:
        setattr(namespace, field, None)
    namespace.deadline_seconds = _positive_number(request, "deadline_seconds")
    namespace.call_timeout_seconds = _positive_number(request, "call_timeout_seconds")
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


def payload_sha256(request: dict[str, Any]) -> str:
    payload = {k: v for k, v in request.items() if k not in _NON_PAYLOAD_FIELDS}
    if isinstance(payload.get("question"), str):
        payload["question"] = payload["question"].strip()
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def guarded_create(
    config: AppConfig,
    question: str,
    *,
    approved_by: str | None,
    approval_token: str | None,
    idempotency_key: str | None = None,
    request_id: str | None = None,
    request_payload_sha256: str | None = None,
) -> tuple[Any, str, dict[str, Any] | None]:
    """Check approval, create the run and bind the request; do not execute."""

    from .cli import _engine, _store

    plan = plan_record(config, question)
    approval: dict[str, Any] | None = None
    token_sha256: str | None = None
    store = _store(config)
    if plan["approval_required"]:
        token_sha256 = verify_token(config.data_dir, plan, approved_by, approval_token)
        approval = store.consume_run_approval(token_sha256, plan["plan_hash"])
    engine = _engine(config)
    run_id = engine.create_run(question, idempotency_key=idempotency_key)
    if request_id is not None and request_payload_sha256 is not None:
        store.bind_agent_request(request_id, request_payload_sha256, run_id)
    if token_sha256 is not None and approval is not None:
        store.attach_run_approval(token_sha256, run_id)
        store.append_event(
            run_id,
            "human_run_approval",
            {
                "approved_by": approval["approved_by"],
                "plan_hash": approval["plan_hash"],
                "max_calls": approval["max_calls"],
                "approved_at": approval["approved_at"],
                "token_sha256": token_sha256,
            },
        )
    return engine, run_id, approval


def guarded_run(
    config: AppConfig,
    question: str,
    *,
    approved_by: str | None,
    approval_token: str | None,
    idempotency_key: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Run the council, enforcing human approval for any paid provider."""

    if not plan_record(config, question)["approval_required"]:
        from .cli import _engine

        return _engine(config).run(question, idempotency_key=idempotency_key), None
    engine, run_id, approval = guarded_create(
        config,
        question,
        approved_by=approved_by,
        approval_token=approval_token,
        idempotency_key=idempotency_key,
    )
    return engine.resume(run_id), approval


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
        "failures": [
            {k: f.get(k) for k in ("stage", "provider", "category")}
            for f in result.get("failures") or []
        ],
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


def run_status(store: Any, run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise AgentRequestError(f"unknown run: {run_id}", "not_found")
    progress = store.invocation_progress(run_id)
    max_calls = int((run.get("policy") or {}).get("max_calls") or 0)
    done = progress["succeeded"] + progress["failed"]
    reply: dict[str, Any] = {
        "run_id": run_id,
        "status": run["status"],
        "finished": bool(run.get("result")) and run["status"] != "running",
        "progress": {**progress, "finished_calls": done, "max_calls": max_calls},
    }
    if reply["finished"]:
        reply.update(_run_summary(run["result"], store.list_human_decisions(run_id)))
    else:
        reply["human_decision_status"] = "awaiting"
        reply["next_step"] = (
            "Run in progress; poll the status action. The result will end "
            f"'{AWAITING}'."
        )
    return reply


def capabilities() -> dict[str, Any]:
    return {
        "interface_version": INTERFACE_VERSION,
        "actions": list(AGENT_ACTIONS),
        "human_only": HUMAN_ONLY_ACTIONS,
        "rules": [
            "Use for big, contested, hard-to-reverse decisions.",
            "Always call plan first; a paid run needs a human-issued approval "
            "token bound to that plan's hash and estimate.",
            "Send a request_id with run/start; retries with the same id return "
            "the same run.",
            "Every run ends in 'Awaiting human decision'. Never present a "
            "council result as the decision.",
            "Use only these entry points; never read or write the data "
            "directory, database or approval key directly.",
        ],
    }


def spawn_worker(config_args: list[str], run_id: str, data_dir: Path) -> None:
    """Execute a created run in a detached process (``council resume``)."""

    package_parent = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [package_parent, *filter(None, [env.get("PYTHONPATH")])]
    )
    log_dir = Path(data_dir) / "worker-logs"
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    log = os.open(log_dir / f"{run_id}.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        subprocess.Popen(
            [sys.executable, "-m", "model_council.cli", *config_args, "resume", run_id, "--json"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(log)


def _validate(request: Any) -> str:
    if not isinstance(request, dict):
        raise AgentRequestError("request must be a JSON object")
    version = request.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise AgentRequestError(
            f"unsupported schema_version {version!r}; this server speaks {SCHEMA_VERSION!r}",
            "unsupported_schema_version",
        )
    action = request.get("action")
    if not isinstance(action, str):
        raise AgentRequestError("action is required")
    if action in HUMAN_ONLY_ACTIONS:
        return action
    if action not in AGENT_ACTIONS:
        raise AgentRequestError(
            f"unknown action {action!r}; use one of {', '.join(AGENT_ACTIONS)}"
        )
    unknown = sorted(set(request) - ALLOWED_FIELDS[action] - {"schema_version", "action"})
    if unknown:
        raise AgentRequestError(
            f"unknown field(s) for {action}: {', '.join(unknown)}", "unknown_field"
        )
    request_id = request.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id)
    ):
        raise AgentRequestError("request_id must match [A-Za-z0-9._:-]{1,128}")
    for name in ("approved_by", "approval_token", "run_id"):
        if name in request and not isinstance(request[name], str):
            raise AgentRequestError(f"{name} must be a string")
    if action in {"status", "inspect"} and "run_id" not in request:
        raise AgentRequestError("run_id is required")
    return action


def _envelope(action: str | None, body: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "action": action, **body}


def handle_request(
    request: Any,
    base_config: AppConfig,
    *,
    config_path: str | None = None,
    worker_args: list[str] | None = None,
) -> dict[str, Any]:
    """Handle one agent request. Never raises for bad input; returns ok=false."""

    action = request.get("action") if isinstance(request, dict) else None
    action = action if isinstance(action, str) else None
    try:
        action = _validate(request)
        if action in HUMAN_ONLY_ACTIONS:
            return _envelope(
                action,
                {"ok": False, "error": "human_only", "message": HUMAN_ONLY_ACTIONS[action]},
            )
        if action == "capabilities":
            return _envelope(action, {"ok": True, **capabilities()})
        if action == "plan":
            config = select_config(base_config, request, config_path)
            plan = plan_record(config, _question(request))
            return _envelope(
                action,
                {
                    "ok": True,
                    **plan,
                    "next_step": (
                        "Ask your human to review this estimate and run: council "
                        "approve --plan-hash " + plan["plan_hash"] + " --approved-by "
                        "NAME with the same question and provider selection."
                        if plan["approval_required"]
                        else "Mock/offline plan: no approval needed; call run or start."
                    ),
                },
            )
        from .cli import _store

        store = _store(base_config)
        if action in {"status", "inspect"}:
            return _envelope(action, {"ok": True, **run_status(store, request["run_id"])})
        if action == "list":
            limit = request.get("limit", 20)
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise AgentRequestError("limit must be a positive integer")
            runs = store.list_runs(limit=limit)
            return _envelope(
                action,
                {
                    "ok": True,
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
                },
            )
        # run / start: the only writing actions.
        request_id = request.get("request_id")
        digest = payload_sha256(request)
        if request_id is not None:
            existing = store.get_agent_request(request_id)
            if existing is not None:
                if existing["payload_sha256"] != digest:
                    raise AgentRequestError(
                        f"request_id {request_id!r} was already used with a "
                        "different payload; use a new request_id",
                        "request_id_conflict",
                    )
                return _envelope(
                    action,
                    {"ok": True, "replayed": True, **run_status(store, existing["run_id"])},
                )
        config = select_config(base_config, request, config_path)
        question = _question(request)
        lock = ServiceLock(base_config.data_dir)
        lock.acquire()
        try:
            from .cli import _refuse_service_managed_cli

            _refuse_service_managed_cli(base_config)
            engine, run_id, approval = guarded_create(
                config,
                question,
                approved_by=request.get("approved_by"),
                approval_token=request.get("approval_token"),
                idempotency_key=None if request_id is None else f"agent:{request_id}",
                request_id=request_id,
                request_payload_sha256=digest,
            )
            if action == "run":
                result = engine.resume(run_id)
        finally:
            lock.release()
        if action == "start":
            spawn_worker(worker_args or [], run_id, base_config.data_dir)
            reply = run_status(store, run_id)
            reply["next_step"] = (
                "Run started in the background; poll {\"action\": \"status\", "
                f"\"run_id\": \"{run_id}\"}}. The result will end '{AWAITING}'."
            )
        else:
            reply = _run_summary(result, [])
        reply.update({"approval": approval, "paid": is_paid(config), "replayed": False})
        return _envelope(action, {"ok": True, **reply})
    except PermissionError as exc:
        return _envelope(action, {"ok": False, "error": "approval_required", "message": str(exc)})
    except AgentRequestError as exc:
        return _envelope(action, {"ok": False, "error": exc.code, "message": str(exc)})
    except RuntimeError as exc:
        code = "busy" if "already owns" in str(exc) else "bad_request"
        return _envelope(action, {"ok": False, "error": code, "message": str(exc)})
    except (KeyError, ValueError, OSError) as exc:
        return _envelope(action, {"ok": False, "error": "bad_request", "message": str(exc)})


def config_for(mock: bool, data_dir: str | None, config_path: str | None) -> AppConfig:
    from .config import load_config, mock_config

    if mock:
        return mock_config(data_dir)
    config = load_config(config_path)
    if data_dir:
        config = replace(config, data_dir=Path(data_dir).expanduser())
    return config


def worker_args_for(mock: bool, data_dir: str | Path, config_path: str | None) -> list[str]:
    args = ["--data-dir", str(data_dir)]
    if mock:
        args.insert(0, "--mock")
    if config_path:
        args += ["--config", config_path]
    return args
