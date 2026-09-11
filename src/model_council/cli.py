from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Sequence

from .config import (
    AppConfig,
    load_config,
    mock_config,
    validate_provider_config,
    validate_run_policy,
    validate_synthesis_providers,
)
from .engine import CouncilEngine
from .models import ProviderConfig, RunPolicy
from .providers.factory import create_provider
from .run_lock import ServiceLock
from .secrets import SecretResolver, default_secret_resolver
from .store import CouncilStore, service_managed_data_dir
from .version import PACKAGE_VERSION
from .workload import estimate_workload, require_workload_within_limits


_RUN_POLICY_OVERRIDES = (
    "proposal_quorum",
    "jury_quorum",
    "min_lineages",
    "max_calls",
    "max_parallel_calls",
    "deadline_seconds",
    "max_question_chars",
    "max_stage_prompt_chars",
    "jury_repair_attempts",
    "truncation_retries",
    "max_recovery_output_tokens",
)


def _add_question_arguments(parser: argparse.ArgumentParser) -> None:
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--question", help="Question for the council")
    input_group.add_argument("--file", help="UTF-8 file containing the question")


def _add_run_selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--providers",
        help="Comma-separated provider names; defaults to all configured providers",
    )
    synthesis_group = parser.add_mutually_exclusive_group()
    synthesis_group.add_argument(
        "--synthesis-provider",
        help="Use one provider for synthesis with no fallback",
    )
    synthesis_group.add_argument(
        "--synthesis-providers",
        help="Comma-separated synthesis providers in exact fallback order",
    )
    parser.add_argument("--proposal-quorum", type=int)
    parser.add_argument("--jury-quorum", type=int)
    parser.add_argument("--min-lineages", type=int)
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-parallel-calls", type=int)
    parser.add_argument("--deadline-seconds", type=float)
    parser.add_argument("--max-question-chars", type=int)
    parser.add_argument("--max-stage-prompt-chars", type=int)
    parser.add_argument("--jury-repair-attempts", type=int)
    parser.add_argument("--truncation-retries", type=int)
    parser.add_argument("--max-recovery-output-tokens", type=int)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="council",
        description="Run an auditable council of heterogeneous language models.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {PACKAGE_VERSION}",
    )
    parser.add_argument("--config", help="Path to a council TOML configuration")
    parser.add_argument(
        "--data-dir",
        help="Override the local council data directory",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use four deterministic local mock lineages; no API calls",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor", help="Check configuration, credential presence, and storage"
    )
    doctor.add_argument(
        "--json", action="store_true", help="Print machine-readable output"
    )

    providers = subparsers.add_parser(
        "providers", help="Show locked provider, model, and lineage configuration"
    )
    providers.add_argument("--json", action="store_true")

    plan = subparsers.add_parser(
        "plan",
        help="Project a council workload without credentials, storage, or API calls",
    )
    _add_question_arguments(plan)
    _add_run_selection_arguments(plan)
    plan.add_argument("--json", action="store_true")

    run = subparsers.add_parser("run", help="Start a new council run")
    _add_question_arguments(run)
    _add_run_selection_arguments(run)
    run.add_argument("--idempotency-key")
    run.add_argument("--json", action="store_true")

    resume = subparsers.add_parser("resume", help="Resume a partial or failed run")
    resume.add_argument("run_id")
    resume.add_argument("--json", action="store_true")

    inspect = subparsers.add_parser("inspect", help="Inspect one persisted run")
    inspect.add_argument("run_id")
    inspect.add_argument("--json", action="store_true")

    listing = subparsers.add_parser("list", help="List persisted runs")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--json", action="store_true")

    export = subparsers.add_parser("export", help="Export a run")
    export.add_argument("run_id")
    export.add_argument(
        "--format", choices=("markdown", "json"), default="markdown"
    )
    export.add_argument("--output", required=True)

    return parser


def _app_config(args: argparse.Namespace) -> AppConfig:
    if args.mock:
        config = mock_config(args.data_dir)
    else:
        config = load_config(args.config)
        if args.data_dir:
            config = replace(config, data_dir=Path(args.data_dir).expanduser())
    return config


def _select_run_config(
    config: AppConfig, args: argparse.Namespace
) -> AppConfig:
    providers = list(config.providers)
    provider_selection = getattr(args, "providers", None)
    if provider_selection:
        requested = [
            item.strip()
            for item in provider_selection.split(",")
            if item.strip()
        ]
        if len(set(requested)) != len(requested):
            raise ValueError("Selected providers must not contain duplicates")
        available = {provider.name: provider for provider in providers}
        unknown = [name for name in requested if name not in available]
        if unknown:
            raise ValueError(f"Unknown providers: {', '.join(unknown)}")
        providers = [available[name] for name in requested]

    policy_values = config.policy.to_dict()
    for field in _RUN_POLICY_OVERRIDES:
        value = getattr(args, field, None)
        if value is not None:
            policy_values[field] = value
    policy = type(config.policy).from_dict(policy_values)
    ordered_override = getattr(args, "synthesis_providers", None)
    singular_override = getattr(args, "synthesis_provider", None)
    if ordered_override is not None:
        synthesis_chain = tuple(
            item.strip()
            for item in ordered_override.split(",")
            if item.strip()
        )
        if not synthesis_chain:
            raise ValueError("At least one synthesis provider is required")
    elif singular_override is not None:
        synthesis_chain = (singular_override,)
    elif provider_selection and getattr(args, "config", None) is None:
        selected_names = {provider.name for provider in providers}
        if config.synthesis_provider not in selected_names:
            raise ValueError(
                "Synthesis provider must be one of the selected providers"
            )
        # The no-config fallback chain is an implicit default. Preserve the
        # configured primary for compatibility with existing --providers
        # commands, but do not force default fallbacks that the command
        # explicitly removed from its participant roster. File-backed chains
        # and explicit CLI chains remain exact and fail closed below.
        synthesis_chain = tuple(
            name
            for name in config.synthesis_providers
            if name in selected_names
        )
    else:
        synthesis_chain = config.synthesis_providers
    synthesis_provider = synthesis_chain[0]
    synthesis_fallbacks = synthesis_chain[1:]
    validated_chain = validate_synthesis_providers(
        synthesis_provider,
        synthesis_fallbacks,
        providers,
    )
    validate_run_policy(
        policy,
        providers,
        synthesis_provider_count=len(validated_chain),
    )
    return replace(
        config,
        providers=tuple(providers),
        policy=policy,
        synthesis_provider=synthesis_provider,
        synthesis_fallbacks=synthesis_fallbacks,
    )


def _question_from_args(args: argparse.Namespace) -> str:
    if args.file:
        question = Path(args.file).read_text(encoding="utf-8")
    else:
        question = args.question
    clean_question = question.strip()
    if not clean_question:
        raise ValueError("Question cannot be empty")
    return clean_question


def _locked_resume_config(config: AppConfig, run_id: str) -> AppConfig:
    run = _store(config).get_run(run_id)
    if run is None:
        raise KeyError(f"Unknown run: {run_id}")
    providers = tuple(
        ProviderConfig.from_dict(value) for value in run["provider_configs"]
    )
    requested_mock = all(
        provider.name.startswith("mock") for provider in config.providers
    )
    stored_mock = all(provider.name.startswith("mock") for provider in providers)
    if requested_mock != stored_mock:
        raise ValueError(
            "Stored run mode differs from the requested deployment mode"
        )
    for provider in providers:
        validate_provider_config(provider)
    policy = RunPolicy.from_dict(run["policy"])
    synthesis_provider = str(
        run["policy"].get("synthesis_provider") or config.synthesis_provider
    )
    synthesis_fallbacks_value = run["policy"].get("synthesis_fallbacks", [])
    if not isinstance(synthesis_fallbacks_value, list):
        raise ValueError("Stored synthesis fallback lock is malformed")
    synthesis_fallbacks = tuple(synthesis_fallbacks_value)
    synthesis_chain = validate_synthesis_providers(
        synthesis_provider,
        synthesis_fallbacks,
        providers,
    )
    validate_run_policy(
        policy,
        providers,
        synthesis_provider_count=len(synthesis_chain),
    )
    return replace(
        config,
        providers=providers,
        policy=policy,
        synthesis_provider=synthesis_provider,
        synthesis_fallbacks=synthesis_fallbacks,
    )


def _store(config: AppConfig) -> CouncilStore:
    return CouncilStore(config.data_dir)


def _refuse_service_managed_cli(config: AppConfig) -> None:
    if service_managed_data_dir(config.data_dir):
        raise RuntimeError(
            "This data directory is managed by council-service; "
            "use council-remote instead of the local CLI"
        )


def _providers(
    config: AppConfig, resolver: SecretResolver
) -> dict[str, Any]:
    built: dict[str, Any] = {}
    missing: list[str] = []
    for provider_config in config.providers:
        if provider_config.name.startswith("mock"):
            key = "mock-only-sentinel"
        else:
            key = resolver.resolve(provider_config.secret_name)
        if not key:
            missing.append(
                f"{provider_config.name} ({provider_config.secret_name})"
            )
            continue
        built[provider_config.name] = create_provider(provider_config, key)
    if missing:
        raise RuntimeError(
            "Missing provider credentials: "
            + ", ".join(missing)
            + ". Use process environment variables or "
            + "MODEL_COUNCIL_SECRET_COMMAND; .env files are intentionally unsupported."
        )
    return built


def _engine(
    config: AppConfig, resolver: SecretResolver | None = None
) -> CouncilEngine:
    resolver = resolver or default_secret_resolver()
    return CouncilEngine(
        store=_store(config),
        providers=_providers(config, resolver),
        policy=config.policy,
        synthesis_provider=config.synthesis_provider,
        synthesis_fallbacks=config.synthesis_fallbacks,
    )


def _doctor(config: AppConfig, *, as_json: bool) -> int:
    resolver = default_secret_resolver()
    checks: list[dict[str, Any]] = []
    all_ready = True
    try:
        store = _store(config)
        store.list_runs(limit=1)
    except Exception as exc:
        checks.append(
            {
                "check": "storage",
                "ready": False,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )
        all_ready = False
    else:
        checks.append(
            {
                "check": "storage",
                "ready": True,
                "detail": str(config.data_dir),
            }
        )

    for provider in config.providers:
        if provider.name.startswith("mock"):
            source = "built-in mock"
        else:
            source = resolver.source_for(provider.secret_name)
        ready = source is not None
        all_ready = all_ready and ready
        checks.append(
            {
                "check": f"provider:{provider.name}",
                "ready": ready,
                "model": provider.model,
                "lineage": provider.lineage,
                "credential_source": source or "missing",
            }
        )

    payload = {
        "ready": all_ready,
        "mode": "mock" if any(
            provider.name.startswith("mock") for provider in config.providers
        ) else "live",
        "checks": checks,
        "note": "No network requests were made and no credential values were printed.",
    }
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("READY" if all_ready else "NOT READY")
        for check in checks:
            marker = "ok" if check["ready"] else "missing"
            detail = check.get("detail") or check.get("credential_source")
            print(f"  [{marker}] {check['check']}: {detail}")
        print(payload["note"])
    return 0 if all_ready else 2


def _print_workload_plan(plan: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return

    providers = ", ".join(plan["providers"])
    print(f"Providers ({plan['provider_count']}): {providers}")
    print(
        "Synthesis order: "
        + " -> ".join(plan["synthesis_providers"])
    )
    print(
        "Call plan: "
        f"{plan['mandatory_calls']} mandatory; "
        f"{plan['recovery_call_capacity']} recovery"
    )
    print(
        f"Question chars: {plan['question_chars']} / "
        f"{plan['max_question_chars']}"
    )
    print("Stage prompt chars:")
    exceeded = set(plan["prompt_limit_exceeded_stages"])
    for stage, chars in plan["stage_prompt_chars"].items():
        marker = " [exceeds limit]" if stage in exceeded else ""
        print(
            f"  {stage}: {chars} / "
            f"{plan['max_stage_prompt_chars']}{marker}"
        )
    print(f"Within limits: {'yes' if plan['within_limits'] else 'no'}")
    limiting = ", ".join(plan["limiting_stages"]) or "none"
    print(f"Limiting stages: {limiting}")
    maximum = plan["effective_plain_question_max_chars"]
    headroom = plan["plain_question_headroom_chars"]
    print(
        "Effective plain-question maximum: "
        f"{maximum if maximum is not None else 'unavailable'}"
    )
    print(
        "Plain-question headroom: "
        f"{headroom if headroom is not None else 'unavailable'}"
    )


def _print_result(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"Run: {result['run_id']}")
    print(f"Status: {result['status']}")
    print(
        f"Completion quality: "
        f"{result.get('completion_quality', 'unknown')}"
    )
    if result.get("answer"):
        print("\n" + result["answer"].strip())
    else:
        print("\nNo final synthesis was produced.")
    synthesis = result.get("synthesis") or {}
    if synthesis.get("configured_order"):
        selected = synthesis.get("selected_provider") or "none"
        print(f"\nSynthesizer selected: {selected}")
        print("Synthesis attempts:")
        for attempt in synthesis.get("attempts") or []:
            print(
                f"- {attempt['position']}. {attempt['provider']}: "
                f"{attempt['status']}"
            )
    if result.get("warnings"):
        print("\nWarnings:")
        for warning in result["warnings"]:
            print(f"- {warning}")
    if result.get("recoveries"):
        print("\nRecoveries:")
        for recovery in result["recoveries"]:
            kind = recovery.get("kind") or recovery.get("stage", "unknown")
            provider = recovery.get("provider", "unknown")
            status = recovery.get("status", "unknown")
            print(f"- {kind} / {provider}: {status}")
    if result.get("failures"):
        print("\nProvider failures:")
        for failure in result["failures"]:
            print(
                f"- {failure['stage']} / {failure['provider']}: "
                f"{failure['category']}"
            )


def _markdown_export(
    run: dict[str, Any],
    invocations: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> str:
    result = run.get("result") or {}
    lines = [
        "# CouncilLogic Run",
        "",
        f"- Run: `{run['id']}`",
        f"- Status: `{run['status']}`",
        (
            f"- Completion quality: "
            f"`{result.get('completion_quality', 'unknown')}`"
        ),
        f"- Protocol: `{run['protocol_id']}@{run['protocol_version']}`",
        f"- Protocol hash: `{run['protocol_hash']}`",
        "",
        "## Question",
        "",
        run["question"],
        "",
        "## Council answer",
        "",
        result.get("answer") or "_No final synthesis was produced._",
        "",
        "## Synthesis selection",
        "",
        "```json",
        json.dumps(result.get("synthesis"), indent=2, sort_keys=True),
        "```",
        "",
        "## Candidate namespace",
        "",
        "```json",
        json.dumps(
            result.get("candidate_namespace"),
            indent=2,
            sort_keys=True,
        ),
        "```",
        "",
        "## Aggregate",
        "",
        "```json",
        json.dumps(result.get("aggregate"), indent=2, sort_keys=True),
        "```",
        "",
        "## Warnings",
        "",
    ]
    warnings = result.get("warnings") or []
    lines.extend([f"- {warning}" for warning in warnings] or ["- None"])
    lines.extend(["", "## Recoveries", ""])
    result_recoveries = result.get("recoveries") or []
    if result_recoveries:
        lines.extend(
            [
                "```json",
                json.dumps(
                    result_recoveries,
                    indent=2,
                    sort_keys=True,
                ),
                "```",
            ]
        )
    else:
        lines.append("_None._")
    lines.extend(["", "## Invocation record", ""])
    for invocation in invocations:
        attempts = invocation.get("attempts")
        attempts_text = (
            str(attempts)
            if isinstance(attempts, int)
            and not isinstance(attempts, bool)
            and attempts >= 0
            else "unknown"
        )
        latency_ms = invocation.get("latency_ms")
        latency_text = (
            f"{latency_ms} ms"
            if isinstance(latency_ms, int)
            and not isinstance(latency_ms, bool)
            and latency_ms >= 0
            else "unknown"
        )
        invocation_lines = [
            f"### {invocation['stage']} — {invocation['provider']}",
            "",
            f"- Status: `{invocation['status']}`",
            f"- Model: `{invocation['model']}`",
            f"- Lineage: `{invocation['lineage']}`",
            f"- Attempts: `{attempts_text}`",
            f"- Latency: `{latency_text}`",
        ]
        if invocation["status"] == "failed":
            category = invocation.get("error_category")
            status_code = invocation.get("error_status_code")
            retryable = invocation.get("error_retryable")
            ambiguous = invocation.get("error_ambiguous")
            category_text = (
                category
                if isinstance(category, str) and category
                else "unknown"
            )
            status_code_text = (
                str(status_code)
                if isinstance(status_code, int)
                and not isinstance(status_code, bool)
                else "unknown"
            )
            retryable_text = (
                str(retryable).lower()
                if isinstance(retryable, bool)
                else "unknown"
            )
            ambiguous_text = (
                str(ambiguous).lower()
                if isinstance(ambiguous, bool)
                else "unknown"
            )
            invocation_lines.extend(
                [
                    f"- Failure category: `{category_text}`",
                    f"- HTTP status: `{status_code_text}`",
                    f"- Retryable: `{retryable_text}`",
                    f"- Ambiguous: `{ambiguous_text}`",
                ]
            )
        invocation_lines.extend(
            [
                "",
                invocation.get("response_text") or "_No response._",
                "",
            ]
        )
        lines.extend(invocation_lines)
    recovery_events = [
        event
        for event in events
        if event["event_type"]
        in {
            "truncated_response_preserved",
            "truncation_recovery",
            "jury_artifact_repair",
            "incomplete_response_preserved",
            "provider_call_not_dispatched",
            "synthesis_fallback_advanced",
        }
    ]
    lines.extend(["## Recovery audit events", ""])
    if recovery_events:
        lines.extend(
            [
                "```json",
                json.dumps(recovery_events, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    else:
        lines.extend(["_None._", ""])
    lines.extend(
        [
            "## Limitations",
            "",
            "- Model output is analysis, not independent evidence.",
            "- Metadata blinding cannot hide model writing style.",
            "- Consequential claims still require verification.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    data_lock: ServiceLock | None = None
    try:
        config = _app_config(args)
        if args.command in {
            "doctor",
            "run",
            "resume",
            "inspect",
            "list",
            "export",
        }:
            data_lock = ServiceLock(config.data_dir)
            data_lock.acquire()
            _refuse_service_managed_cli(config)
        if args.command == "doctor":
            return _doctor(config, as_json=args.json)
        if args.command == "providers":
            payload = [
                {
                    "provider": provider.name,
                    "model": provider.model,
                    "lineage": provider.lineage,
                    "endpoint": provider.endpoint,
                    "secret_name": provider.secret_name,
                }
                for provider in config.providers
            ]
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                for item in payload:
                    print(
                        f"{item['provider']}: {item['model']} "
                        f"({item['lineage']})"
                    )
            return 0
        if args.command == "plan":
            config = _select_run_config(config, args)
            question = _question_from_args(args)
            plan = estimate_workload(
                question,
                (provider.name for provider in config.providers),
                config.policy,
                synthesis_providers=config.synthesis_providers,
            )
            _print_workload_plan(plan, as_json=args.json)
            if not plan["within_limits"]:
                try:
                    require_workload_within_limits(plan)
                except ValueError as exc:
                    print(f"council: {exc}", file=sys.stderr)
                return 2
            return 0
        if args.command == "run":
            config = _select_run_config(config, args)
            question = _question_from_args(args)
            result = _engine(config).run(
                question, idempotency_key=args.idempotency_key
            )
            _print_result(result, as_json=args.json)
            return 0 if result["status"] == "completed" else 3
        if args.command == "resume":
            config = _locked_resume_config(config, args.run_id)
            result = _engine(config).resume(args.run_id)
            _print_result(result, as_json=args.json)
            return 0 if result["status"] == "completed" else 3

        store = _store(config)
        if args.command == "inspect":
            run = store.get_run(args.run_id)
            if run is None:
                raise KeyError(f"Unknown run: {args.run_id}")
            payload = {
                "run": run,
                "invocations": store.list_invocations(args.run_id),
                "events": store.list_events(args.run_id),
            }
            if args.json:
                print(json.dumps(payload, indent=2, sort_keys=True))
            else:
                print(f"Run: {run['id']}")
                print(f"Status: {run['status']}")
                print(
                    f"Protocol: {run['protocol_id']}@{run['protocol_version']}"
                )
                print(
                    f"Invocations: {len(payload['invocations'])}; "
                    f"result: {'present' if run.get('result') else 'not yet present'}"
                )
                if run.get("result"):
                    _print_result(run["result"], as_json=False)
            return 0
        if args.command == "list":
            runs = store.list_runs(limit=args.limit)
            if args.json:
                print(json.dumps(runs, indent=2, sort_keys=True))
            else:
                for run in runs:
                    print(
                        f"{run['id']}  {run['status']:<9}  "
                        f"{run['created_at']}"
                    )
            return 0
        if args.command == "export":
            run = store.get_run(args.run_id)
            if run is None:
                raise KeyError(f"Unknown run: {args.run_id}")
            invocations = store.list_invocations(args.run_id)
            events = store.list_events(args.run_id)
            output_path = Path(args.output).expanduser()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if args.format == "json":
                content = json.dumps(
                    {
                        "run": run,
                        "invocations": invocations,
                        "events": events,
                    },
                    indent=2,
                    sort_keys=True,
                )
            else:
                content = _markdown_export(run, invocations, events)
            _write_private_text(output_path, content + "\n")
            print(output_path)
            return 0
    except (KeyError, OSError, RuntimeError, ValueError) as exc:
        print(f"council: {exc}", file=sys.stderr)
        return 2
    finally:
        if data_lock is not None:
            data_lock.release()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
