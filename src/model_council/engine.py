from __future__ import annotations

import hashlib
import random
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, Protocol

from .models import (
    ErrorCategory,
    ProviderConfig,
    ProviderError,
    ProviderResponse,
    RunPolicy,
    Usage,
)
from .protocol import (
    PROTOCOL_ID,
    PROTOCOL_VERSION,
    aggregate_juries,
    candidate_label,
    jury_prompts,
    jury_repair_prompts,
    parse_jury,
    parse_proposal,
    proposal_prompts,
    protocol_hash,
    synthesis_prompts,
)
from .providers.base import Provider
from .run_lock import RunLock
from .store import CouncilStore
from .workload import (
    combined_prompt_chars,
    estimate_workload,
    require_workload_within_limits,
)


_CANDIDATE_NAMESPACE_EVENT = "candidate_namespace_locked"
_CANDIDATE_NAMESPACE_VERSION = 1
_ADJUDICATION_EVENT = "adjudication_locked"
_ADJUDICATION_VERSION = 1
_JURY_REPAIR_EVENT = "jury_artifact_repair"
_PROVIDER_RETRY_EVENT = "provider_retry_started"


class CallLease(Protocol):
    def reconcile(self, actual_units: int) -> None:
        """Commit actual call units after a provider attempt."""

    def release(self) -> None:
        """Release a reservation when provider execution never started."""


class CallGate(Protocol):
    def reserve(
        self,
        *,
        run_id: str,
        stage: str,
        provider: ProviderConfig,
        attempt: int,
    ) -> CallLease:
        """Authorize and durably reserve one application-level provider call."""


class CouncilEngine:
    def __init__(
        self,
        *,
        store: CouncilStore,
        providers: dict[str, Provider],
        policy: RunPolicy,
        synthesis_provider: str,
        call_gate: CallGate | None = None,
    ) -> None:
        if not providers:
            raise ValueError("At least one provider is required")
        if synthesis_provider not in providers:
            raise ValueError("Synthesis provider is not available")
        self.store = store
        self.providers = providers
        self.policy = policy
        self.synthesis_provider = synthesis_provider
        self.call_gate = call_gate

    def run(
        self,
        question: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        run_id = self.create_run(question, idempotency_key=idempotency_key)
        return self.resume(run_id)

    def create_run(
        self,
        question: str,
        *,
        idempotency_key: str | None = None,
        run_id: str | None = None,
    ) -> str:
        """Create a durable run without starting provider work.

        The split is used by the service layer so it can durably bind an
        authenticated caller to a run before dispatching background work.
        """
        clean_question = question.strip()
        if not clean_question:
            raise ValueError("Question cannot be empty")
        workload_plan = estimate_workload(
            clean_question,
            self.providers,
            self.policy,
        )
        require_workload_within_limits(workload_plan)
        return self.store.create_run(
            question=clean_question,
            protocol_id=PROTOCOL_ID,
            protocol_version=PROTOCOL_VERSION,
            protocol_hash=protocol_hash(),
            provider_configs=[
                provider.config.to_dict() for provider in self.providers.values()
            ],
            policy={
                **self.policy.to_dict(),
                "synthesis_provider": self.synthesis_provider,
            },
            idempotency_key=idempotency_key,
            run_id=run_id,
        )

    def resume(self, run_id: str) -> dict[str, Any]:
        with RunLock(self.store.data_dir, run_id):
            return self._resume_locked(run_id)

    def _resume_locked(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"Unknown run: {run_id}")
        if run["protocol_id"] != PROTOCOL_ID:
            raise ValueError(f"Unsupported protocol: {run['protocol_id']}")
        if run["protocol_hash"] != protocol_hash():
            raise ValueError("Protocol hash differs from the immutable run lock")
        self._validate_provider_lock(run["provider_configs"])
        self._validate_policy_lock(run["policy"])
        existing_events = self.store.list_events(run_id)
        namespace_lock = self._load_candidate_namespace_lock(
            run_id, existing_events
        )
        adjudication_lock = self._load_adjudication_lock(
            run_id,
            namespace_lock,
            existing_events,
        )
        if run.get("status") == "completed" and run.get("result"):
            return run["result"]

        started = time.monotonic()
        deadline = started + self.policy.deadline_seconds
        self.store.set_run_status(run_id, "running")
        self.store.append_event(
            run_id,
            "run_started",
            {"protocol": f"{PROTOCOL_ID}@{PROTOCOL_VERSION}"},
        )

        question = run["question"]
        workload_plan = estimate_workload(
            question,
            self.providers,
            self.policy,
        )
        require_workload_within_limits(workload_plan)
        self.store.append_event(
            run_id,
            "workload_preflight",
            workload_plan,
        )
        failures: list[dict[str, Any]] = []
        warnings: list[str] = []
        recoveries: list[dict[str, Any]] = [
            dict(event["payload"])
            for event in existing_events
            if event["event_type"]
            in {"truncation_recovery", _JURY_REPAIR_EVENT}
        ]
        if namespace_lock is None:
            candidate_mapping: dict[str, str] | None = None
            proposal_provider_names = list(self.providers)
        else:
            candidate_mapping = dict(
                namespace_lock["candidate_label_mapping"]
            )
            proposal_provider_names = list(candidate_mapping.values())
            failures.extend(
                dict(failure)
                for failure in namespace_lock["proposal_failures"]
            )

        proposal_system, proposal_user = proposal_prompts(question)
        proposal_prompts_by_provider = {
            name: (proposal_system, proposal_user)
            for name in proposal_provider_names
        }
        proposals, proposal_failures, proposal_recoveries = (
            self._run_parallel_stage(
                run_id=run_id,
                stage="proposal",
                prompts=proposal_prompts_by_provider,
                deadline=deadline,
            )
        )
        failures.extend(proposal_failures)
        recoveries.extend(proposal_recoveries)

        proposal_artifacts: dict[str, dict[str, Any]] = {}
        for provider_name, response in list(proposals.items()):
            try:
                proposal_artifacts[provider_name] = parse_proposal(
                    response.content
                )
            except (TypeError, ValueError) as exc:
                failures.append(
                    self._failure_payload(
                        "proposal",
                        provider_name,
                        ProviderError(
                            f"Proposal artifact was invalid: {exc}",
                            category=ErrorCategory.INVALID_RESPONSE,
                            retryable=False,
                            request_id=response.request_id,
                            attempts=response.attempts,
                            ambiguous=False,
                        ),
                    )
                )
                proposals.pop(provider_name)

        if candidate_mapping is not None and set(proposals) != set(
            candidate_mapping.values()
        ):
            raise ValueError(
                "Frozen candidate membership could not be recovered "
                "from successful proposal records"
            )

        proposal_lineages = {
            self.providers[name].config.lineage for name in proposals
        }
        proposal_quorum_met = len(proposals) >= self.policy.proposal_quorum
        diversity_met = len(proposal_lineages) >= self.policy.min_lineages

        if not proposal_quorum_met or not diversity_met:
            if not proposal_quorum_met:
                warnings.append(
                    f"Proposal quorum not met: {len(proposals)}/"
                    f"{self.policy.proposal_quorum}"
                )
            if not diversity_met:
                warnings.append(
                    f"Lineage diversity not met: {len(proposal_lineages)}/"
                    f"{self.policy.min_lineages}"
                )
            result = self._build_result(
                run_id=run_id,
                question=question,
                status=(
                    "partial"
                    if proposals and self.policy.allow_partial
                    else "failed"
                ),
                answer=None,
                proposals=proposals,
                proposal_artifacts=proposal_artifacts,
                jury_records=[],
                aggregate=None,
                failures=failures,
                warnings=warnings,
                recoveries=recoveries,
                workload_plan=workload_plan,
                candidate_mapping=None,
            )
            return self._finish(run_id, result)

        if candidate_mapping is None:
            candidate_mapping = self._candidate_mapping(
                run_id, list(proposals)
            )
            namespace_lock = self._lock_candidate_namespace(
                run_id,
                candidate_mapping,
                failures,
            )
            candidate_mapping = dict(
                namespace_lock["candidate_label_mapping"]
            )
        if adjudication_lock is None:
            jury_prompts_by_provider: dict[str, tuple[str, str]] = {}
            jury_presentation_orders: dict[str, list[str]] = {}
            for juror_name in self.providers:
                presentation_order = self._jury_presentation_order(
                    run_id, juror_name, list(candidate_mapping)
                )
                candidates = {
                    label: proposal_artifacts[candidate_mapping[label]]
                    for label in presentation_order
                }
                jury_presentation_orders[juror_name] = presentation_order
                jury_prompts_by_provider[juror_name] = jury_prompts(
                    question, candidates
                )

            jury_responses, jury_failures, jury_recoveries = (
                self._run_parallel_stage(
                    run_id=run_id,
                    stage="jury",
                    prompts=jury_prompts_by_provider,
                    deadline=deadline,
                )
            )
            failures.extend(jury_failures)
            recoveries.extend(jury_recoveries)
            jury_stage_failures = list(jury_failures)

            (
                jury_records,
                artifact_failures,
                artifact_recoveries,
            ) = self._parse_and_repair_juries(
                run_id=run_id,
                candidate_mapping=candidate_mapping,
                presentation_orders=jury_presentation_orders,
                jury_responses=jury_responses,
                deadline=deadline,
            )
            failures.extend(artifact_failures)
            jury_stage_failures.extend(artifact_failures)
            for recovery in artifact_recoveries:
                if recovery not in recoveries:
                    recoveries.append(recovery)

            valid_juries = [
                jury for jury in jury_records if jury.get("valid")
            ]
            if len(valid_juries) < self.policy.jury_quorum:
                warnings.append(
                    "Jury quorum not met: "
                    f"{len(valid_juries)}/{self.policy.jury_quorum}"
                )
                result = self._build_result(
                    run_id=run_id,
                    question=question,
                    status="partial",
                    answer=None,
                    proposals=proposals,
                    proposal_artifacts=proposal_artifacts,
                    jury_records=jury_records,
                    aggregate=None,
                    failures=failures,
                    warnings=warnings,
                    recoveries=recoveries,
                    workload_plan=workload_plan,
                    candidate_mapping=candidate_mapping,
                )
                return self._finish(run_id, result)
        else:
            jury_records = [
                dict(jury)
                for jury in adjudication_lock["jury_records"]
            ]
            jury_stage_failures = [
                dict(failure)
                for failure in adjudication_lock["jury_failures"]
            ]
            failures.extend(jury_stage_failures)
            valid_juries = [
                jury for jury in jury_records if jury.get("valid")
            ]

        aggregate = aggregate_juries(
            [self._judgment_payload(jury) for jury in valid_juries],
            list(proposals),
        )
        aggregate["candidate_label_mapping"] = dict(candidate_mapping)
        if adjudication_lock is None:
            adjudication_lock = self._lock_adjudication(
                run_id,
                candidate_mapping,
                jury_records,
                jury_stage_failures,
            )

        answer: str | None = None
        if time.monotonic() < deadline:
            synthesis_name = self.synthesis_provider
            synth_candidates = {
                label: proposal_artifacts[provider_name]
                for label, provider_name in candidate_mapping.items()
            }
            anonymous_aggregate = self._anonymize_aggregate(
                aggregate, candidate_mapping
            )
            anonymous_juries = [
                self._anonymize_jury(jury, candidate_mapping)
                for jury in valid_juries
            ]
            synth_system, synth_user = synthesis_prompts(
                question,
                synth_candidates,
                anonymous_aggregate,
                anonymous_juries,
            )
            synth_responses, synth_failures, synthesis_recoveries = (
                self._run_parallel_stage(
                    run_id=run_id,
                    stage="synthesis",
                    prompts={synthesis_name: (synth_system, synth_user)},
                    deadline=deadline,
                )
            )
            failures.extend(synth_failures)
            recoveries.extend(synthesis_recoveries)
            if synthesis_name in synth_responses:
                answer = synth_responses[synthesis_name].content

        if answer is None:
            warnings.append("Synthesis did not complete; raw council record preserved")
        status = "completed" if answer is not None else "partial"
        result = self._build_result(
            run_id=run_id,
            question=question,
            status=status,
            answer=answer,
            proposals=proposals,
            proposal_artifacts=proposal_artifacts,
            jury_records=jury_records,
            aggregate=aggregate,
            failures=failures,
            warnings=warnings,
            recoveries=recoveries,
            workload_plan=workload_plan,
            candidate_mapping=candidate_mapping,
        )
        return self._finish(run_id, result)

    def _parse_and_repair_juries(
        self,
        *,
        run_id: str,
        candidate_mapping: dict[str, str],
        presentation_orders: dict[str, list[str]],
        jury_responses: dict[str, ProviderResponse],
        deadline: float,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        """Validate jury artifacts and make at most one prose-only repair.

        The original response remains in the durable ``jury`` invocation. A
        repair uses a separate ``jury_repair`` invocation and is accepted only
        when winner, ranking, confidence, and abstention are unchanged.
        """

        records: dict[str, dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []
        recoveries: list[dict[str, Any]] = []
        invalid: dict[str, dict[str, Any]] = {}
        labels = list(candidate_mapping)

        # Provider configuration order is the deterministic repair priority;
        # response insertion order depends on network completion timing.
        for juror_name in self.providers:
            response = jury_responses.get(juror_name)
            if response is None:
                continue
            presentation_order = presentation_orders[juror_name]
            try:
                parsed = parse_jury(response.content, labels)
            except (TypeError, ValueError) as exc:
                error = str(exc)
                records[juror_name] = self._invalid_jury_record(
                    juror_name,
                    response,
                    candidate_mapping,
                    presentation_order,
                    error,
                )
                initial_failure = self._jury_artifact_failure(
                    juror_name,
                    response,
                    error,
                )
                repair_prompt: tuple[str, str] | None = None
                immutable_decision: dict[str, Any] | None = None
                repair_prompt_error: str | None = None
                if self.policy.jury_repair_attempts:
                    try:
                        (
                            repair_system,
                            repair_user,
                            immutable_decision,
                        ) = jury_repair_prompts(
                            response.content,
                            error,
                            labels,
                        )
                        repair_prompt = (repair_system, repair_user)
                    except (TypeError, ValueError) as repair_exc:
                        repair_prompt_error = str(repair_exc)
                invalid[juror_name] = {
                    "response": response,
                    "presentation_order": presentation_order,
                    "initial_error": error,
                    "initial_failure": initial_failure,
                    "repair_prompt": repair_prompt,
                    "repair_prompt_error": repair_prompt_error,
                    "immutable_decision": immutable_decision,
                }
                continue

            records[juror_name] = self._valid_jury_record(
                juror_name,
                response,
                candidate_mapping,
                presentation_order,
                parsed,
            )

        eligible_names = [
            name
            for name in self.providers
            if name in invalid
            and invalid[name]["repair_prompt"] is not None
        ]
        existing_repair_providers = {
            str(record["provider"])
            for record in self.store.list_invocations(run_id)
            if record["stage"] == "jury_repair"
        }
        # Keep one call available for synthesis. Repairs consume only currently
        # unused call capacity. Persisted repair invocations do not need new
        # capacity and must always be selected on resume so their outcome
        # cannot disappear.
        new_repair_capacity = max(
            0,
            self.policy.max_calls - self.store.count_calls(run_id) - 1,
        )
        new_repair_names = [
            name
            for name in eligible_names
            if name not in existing_repair_providers
        ][:new_repair_capacity]
        selected_names = [
            name
            for name in eligible_names
            if name in existing_repair_providers
            or name in new_repair_names
        ]
        repair_prompts = {
            name: invalid[name]["repair_prompt"]
            for name in selected_names
        }
        repair_responses: dict[str, ProviderResponse] = {}
        repair_call_failures: list[dict[str, Any]] = []
        if repair_prompts:
            (
                repair_responses,
                repair_call_failures,
                _nested_recoveries,
            ) = self._run_parallel_stage(
                run_id=run_id,
                stage="jury_repair",
                provider_stage="jury",
                prompts=repair_prompts,
                deadline=deadline,
                allow_truncation_recovery=False,
                retry_failed_invocations=False,
                preserve_incomplete_responses=True,
            )
        repair_failure_by_provider = {
            str(failure["provider"]): failure
            for failure in repair_call_failures
        }
        repair_invocation_providers = {
            str(record["provider"])
            for record in self.store.list_invocations(run_id)
            if record["stage"] == "jury_repair"
        }

        for juror_name in self.providers:
            details = invalid.get(juror_name)
            if details is None:
                continue
            response = details["response"]
            initial_error = str(details["initial_error"])
            recovery: dict[str, Any] | None = None
            persist_recovery = True

            if not self.policy.jury_repair_attempts:
                failures.append(dict(details["initial_failure"]))
                continue
            if details["repair_prompt"] is None:
                failures.append(dict(details["initial_failure"]))
                recovery = {
                    "kind": "jury_artifact_repair",
                    "stage": "jury",
                    "repair_stage": "jury_repair",
                    "provider": juror_name,
                    "status": "not_attempted",
                    "initial_error": initial_error,
                    "reason": (
                        "immutable jury decision could not be recovered: "
                        f"{details['repair_prompt_error']}"
                    ),
                }
            elif juror_name not in selected_names:
                failures.append(dict(details["initial_failure"]))
                recovery = {
                    "kind": "jury_artifact_repair",
                    "stage": "jury",
                    "repair_stage": "jury_repair",
                    "provider": juror_name,
                    "status": "not_attempted",
                    "initial_error": initial_error,
                    "reason": "call budget reserved for synthesis",
                }
            elif juror_name in repair_responses:
                repair_response = repair_responses[juror_name]
                final_error: str | None = None
                try:
                    parsed = parse_jury(repair_response.content, labels)
                    repaired_decision = {
                        key: parsed[key]
                        for key in (
                            "winner",
                            "ranking",
                            "confidence",
                            "abstain",
                        )
                    }
                    if repaired_decision != details["immutable_decision"]:
                        raise ValueError(
                            "repair changed immutable jury decision fields"
                        )
                except (TypeError, ValueError) as exc:
                    final_error = str(exc)

                if final_error is None:
                    records[juror_name] = self._valid_jury_record(
                        juror_name,
                        response,
                        candidate_mapping,
                        details["presentation_order"],
                        parsed,
                    )
                    recovery = {
                        "kind": "jury_artifact_repair",
                        "stage": "jury",
                        "repair_stage": "jury_repair",
                        "provider": juror_name,
                        "status": "recovered",
                        "initial_error": initial_error,
                        "decision_preserved": True,
                        "repair_resolved_model": (
                            repair_response.resolved_model
                        ),
                    }
                else:
                    records[juror_name] = self._invalid_jury_record(
                        juror_name,
                        repair_response,
                        candidate_mapping,
                        details["presentation_order"],
                        final_error,
                    )
                    failure = self._jury_artifact_failure(
                        juror_name,
                        repair_response,
                        "repair remained invalid: " + final_error,
                    )
                    failure["attempt_stage"] = "jury_repair"
                    failures.append(failure)
                    recovery = {
                        "kind": "jury_artifact_repair",
                        "stage": "jury",
                        "repair_stage": "jury_repair",
                        "provider": juror_name,
                        "status": "failed",
                        "initial_error": initial_error,
                        "final_error": final_error,
                        "decision_preserved": False,
                    }
            else:
                repair_failure = repair_failure_by_provider.get(juror_name)
                repair_was_dispatched = (
                    juror_name in repair_invocation_providers
                )
                final_failure = dict(details["initial_failure"])
                if repair_failure is not None:
                    final_failure.update(
                        {
                            key: value
                            for key, value in repair_failure.items()
                            if key not in {"stage", "provider", "message"}
                        }
                    )
                    final_failure["message"] = (
                        "Jury artifact repair failed after an invalid "
                        f"response: {repair_failure.get('message')}"
                    )
                    final_failure["retryable"] = False
                    final_failure["attempt_stage"] = "jury_repair"
                failures.append(final_failure)
                recovery = {
                    "kind": "jury_artifact_repair",
                    "stage": "jury",
                    "repair_stage": "jury_repair",
                    "provider": juror_name,
                    "status": (
                        "failed"
                        if repair_was_dispatched
                        else "not_attempted"
                    ),
                    "initial_error": initial_error,
                    "final_error": (
                        None
                        if repair_failure is None
                        else repair_failure.get("message")
                    ),
                    "decision_preserved": False,
                }
                # A deadline or local budget check can fail before an
                # invocation is created. Keep that fact in the terminal
                # result, but do not freeze it as an event: an interrupted
                # run receives a fresh bounded deadline on resume and may
                # safely make its still-unused repair attempt.
                persist_recovery = repair_was_dispatched

            if recovery is not None:
                if persist_recovery:
                    recovery = self._persist_jury_repair_event(
                        run_id, recovery
                    )
                if recovery not in recoveries:
                    recoveries.append(recovery)

        return (
            [records[name] for name in self.providers if name in records],
            failures,
            recoveries,
        )

    @staticmethod
    def _valid_jury_record(
        juror_name: str,
        response: ProviderResponse,
        candidate_mapping: dict[str, str],
        presentation_order: list[str],
        parsed: dict[str, Any],
    ) -> dict[str, Any]:
        canonical = CouncilEngine._canonicalize_jury(
            parsed, candidate_mapping
        )
        canonical.update(
            {
                "juror": juror_name,
                "juror_model": response.resolved_model,
                "mapping": candidate_mapping,
                "presentation_order": presentation_order,
                "valid": True,
            }
        )
        return canonical

    @staticmethod
    def _invalid_jury_record(
        juror_name: str,
        response: ProviderResponse,
        candidate_mapping: dict[str, str],
        presentation_order: list[str],
        error: str,
    ) -> dict[str, Any]:
        return {
            "juror": juror_name,
            "juror_model": response.resolved_model,
            "mapping": candidate_mapping,
            "presentation_order": presentation_order,
            "valid": False,
            "error": error,
        }

    @staticmethod
    def _jury_artifact_failure(
        juror_name: str,
        response: ProviderResponse,
        error: str,
    ) -> dict[str, Any]:
        return CouncilEngine._failure_payload(
            "jury",
            juror_name,
            ProviderError(
                f"Jury artifact was invalid: {error}",
                category=ErrorCategory.INVALID_RESPONSE,
                retryable=False,
                request_id=response.request_id,
                attempts=response.attempts,
                ambiguous=False,
            ),
        )

    def _persist_jury_repair_event(
        self,
        run_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        matching = [
            event
            for event in self.store.list_events(run_id)
            if event["event_type"] == _JURY_REPAIR_EVENT
            and event.get("payload", {}).get("provider")
            == payload.get("provider")
        ]
        if len(matching) > 1:
            raise ValueError("Run contains duplicate jury repair events")
        if matching:
            existing = dict(matching[0]["payload"])
            if existing != payload:
                raise ValueError("Jury repair audit event is inconsistent")
            return existing
        self.store.append_event(run_id, _JURY_REPAIR_EVENT, payload)
        return dict(payload)

    def _run_parallel_stage(
        self,
        *,
        run_id: str,
        stage: str,
        prompts: dict[str, tuple[str, str]],
        deadline: float,
        allow_truncation_recovery: bool = True,
        output_overrides: dict[str, int] | None = None,
        provider_stage: str | None = None,
        retry_failed_invocations: bool = True,
        preserve_incomplete_responses: bool = False,
    ) -> tuple[
        dict[str, ProviderResponse],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        successes: dict[str, ProviderResponse] = {}
        failures: list[dict[str, Any]] = []
        recoveries: list[dict[str, Any]] = []
        work: dict[str, tuple[str, str]] = {}
        output_overrides = output_overrides or {}
        provider_stage = provider_stage or stage
        existing_records = {
            (record["stage"], record["provider"]): record
            for record in self.store.list_invocations(run_id)
        }
        truncation_retry_counts: dict[str, int] = {}
        for event in self.store.list_events(run_id):
            if event["event_type"] != _PROVIDER_RETRY_EVENT:
                continue
            payload = event.get("payload")
            if (
                isinstance(payload, dict)
                and payload.get("stage") == stage
                and payload.get("retry_kind") == "truncation"
                and isinstance(payload.get("provider"), str)
            ):
                provider_name = str(payload["provider"])
                truncation_retry_counts[provider_name] = (
                    truncation_retry_counts.get(provider_name, 0) + 1
                )
        ambiguous_providers = {
            str(record["provider"])
            for record in existing_records.values()
            if record["status"] == "running"
            or (
                record["status"] == "failed"
                and record.get("error_ambiguous")
            )
        }

        for provider_name, prompt_pair in prompts.items():
            existing = self.store.get_successful_invocation(
                run_id, stage, provider_name
            )
            if existing:
                response = self._response_from_record(existing)
                completion_error = self._completion_error(response)
                if completion_error is not None:
                    failures.append(
                        self._failure_payload(
                            stage, provider_name, completion_error
                        )
                    )
                else:
                    successes[provider_name] = response
                continue
            record = existing_records.get((stage, provider_name))
            if (
                record
                and record["status"] == "failed"
                and record.get("error_ambiguous")
                and not retry_failed_invocations
            ):
                stored_error = record.get("error")
                if not isinstance(stored_error, dict):
                    raise ValueError(
                        "Ambiguous repair failure record is malformed"
                    )
                failures.append(
                    {
                        "stage": stage,
                        "provider": provider_name,
                        **dict(stored_error),
                    }
                )
                continue
            if record and (
                record["status"] == "running"
                or (
                    record["status"] == "failed"
                    and record.get("error_ambiguous")
                )
            ):
                ambiguous = ProviderError(
                    "Prior invocation outcome is ambiguous; automatic retry refused",
                    category=ErrorCategory.UNKNOWN,
                    retryable=False,
                    request_id=record.get("request_id"),
                    attempts=int(record.get("attempts") or 1),
                    ambiguous=True,
                )
                if record["status"] == "running":
                    self.store.finish_invocation_failure(
                        record["id"], ambiguous
                    )
                failures.append(
                    self._failure_payload(stage, provider_name, ambiguous)
                )
                continue
            if (
                record
                and record["status"] == "failed"
                and not retry_failed_invocations
            ):
                stored_error = record.get("error")
                if isinstance(stored_error, dict):
                    failures.append(
                        {
                            "stage": stage,
                            "provider": provider_name,
                            **dict(stored_error),
                        }
                    )
                else:
                    failures.append(
                        self._failure_payload(
                            stage,
                            provider_name,
                            ProviderError(
                                "Prior repair invocation failed; automatic "
                                "retry refused",
                                category=ErrorCategory.INVALID_RESPONSE,
                                retryable=False,
                                request_id=record.get("request_id"),
                                attempts=int(record.get("attempts") or 1),
                                ambiguous=False,
                            ),
                        )
                    )
                continue
            if (
                record
                and self._is_length_failure_record(record)
                and truncation_retry_counts.get(provider_name, 0)
                >= self.policy.truncation_retries
            ):
                exhausted = ProviderError(
                    "Known output-length recovery is exhausted; "
                    "automatic retry refused (finish_reason=length)",
                    category=ErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                    request_id=record.get("request_id"),
                    attempts=int(record.get("attempts") or 1),
                    ambiguous=False,
                )
                failures.append(
                    self._failure_payload(stage, provider_name, exhausted)
                )
                continue
            if provider_name in ambiguous_providers:
                ambiguous = ProviderError(
                    "Prior provider outcome in this run is ambiguous; "
                    "later-stage invocation suppressed",
                    category=ErrorCategory.UNKNOWN,
                    retryable=False,
                    ambiguous=True,
                )
                failures.append(
                    self._failure_payload(stage, provider_name, ambiguous)
                )
                continue
            work[provider_name] = prompt_pair

        if not work:
            return successes, failures, recoveries
        if time.monotonic() >= deadline:
            for provider_name in work:
                failures.append(
                    self._failure_payload(
                        stage,
                        provider_name,
                        ProviderError(
                            "Run deadline exhausted",
                            category=ErrorCategory.TIMEOUT,
                            retryable=True,
                        ),
                    )
                )
            return successes, failures, recoveries

        for provider_name, prompt_pair in list(work.items()):
            prompt_chars = combined_prompt_chars(prompt_pair)
            if prompt_chars <= self.policy.max_stage_prompt_chars:
                continue
            failures.append(
                self._failure_payload(
                    stage,
                    provider_name,
                    ProviderError(
                        "Stage prompt exceeds configured workload limit: "
                        f"{prompt_chars} > "
                        f"{self.policy.max_stage_prompt_chars}",
                        category=ErrorCategory.BUDGET,
                        retryable=False,
                        ambiguous=False,
                    ),
                )
            )
            work.pop(provider_name)
        if not work:
            return successes, failures, recoveries

        calls_used = self.store.count_calls(run_id)
        remaining = max(0, self.policy.max_calls - calls_used)
        allowed_names = list(work)[:remaining]
        denied_names = list(work)[remaining:]
        for provider_name in denied_names:
            failures.append(
                self._failure_payload(
                    stage,
                    provider_name,
                    ProviderError(
                        "Run call budget exhausted",
                        category=ErrorCategory.BUDGET,
                        retryable=False,
                    ),
                )
            )
        if not allowed_names:
            return successes, failures, recoveries

        future_map: dict[
            Future[ProviderResponse],
            tuple[str, str, CallLease | None, int, float],
        ] = {}
        recoverable: dict[
            str,
            tuple[tuple[str, str], ProviderResponse, int],
        ] = {}
        prior_truncations = {
            provider_name
            for provider_name in allowed_names
            if self._is_length_failure_record(
                existing_records.get((stage, provider_name))
            )
        }
        max_workers = min(
            self.policy.max_parallel_calls,
            len(allowed_names),
        )
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for provider_name in allowed_names:
                system_prompt, user_prompt = work[provider_name]
                provider = self.providers[provider_name]
                combined_prompt = (
                    f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}"
                )
                invocation_id = self.store.start_invocation(
                    run_id=run_id,
                    stage=stage,
                    provider=provider_name,
                    model=provider.config.model,
                    lineage=provider.config.lineage,
                    prompt=combined_prompt,
                )
                invocation = self.store.get_invocation(invocation_id)
                attempt = int(
                    invocation.get("call_count") if invocation else 1
                )
                output_limit = output_overrides.get(
                    provider_name,
                    provider.config.output_tokens_for(provider_stage),
                )
                if (
                    provider_name in prior_truncations
                    and provider_name not in output_overrides
                ):
                    output_limit = min(
                        max(output_limit + 1_024, output_limit * 2),
                        self.policy.max_recovery_output_tokens,
                    )
                timeout_limit = min(
                    provider.config.timeout_for(provider_stage),
                    max(0.001, deadline - time.monotonic()),
                )
                lease: CallLease | None = None
                try:
                    if self.call_gate is not None:
                        lease = self.call_gate.reserve(
                            run_id=run_id,
                            stage=stage,
                            provider=provider.config,
                            attempt=attempt,
                        )
                    future = executor.submit(
                        provider.generate,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        stage=provider_stage,
                        max_output_tokens=output_limit,
                        timeout_seconds=timeout_limit,
                    )
                except ProviderError as exc:
                    if lease is not None:
                        lease.release()
                    self.store.finish_invocation_failure(invocation_id, exc)
                    failures.append(
                        self._failure_payload(stage, provider_name, exc)
                    )
                    continue
                except Exception as exc:
                    if lease is not None:
                        lease.release()
                    safe_error = ProviderError(
                        f"Provider dispatch failed safely: {type(exc).__name__}",
                        category=ErrorCategory.UNKNOWN,
                        retryable=False,
                    )
                    self.store.finish_invocation_failure(
                        invocation_id,
                        safe_error,
                    )
                    failures.append(
                        self._failure_payload(
                            stage,
                            provider_name,
                            safe_error,
                        )
                    )
                    continue
                future_map[future] = (
                    provider_name,
                    invocation_id,
                    lease,
                    output_limit,
                    timeout_limit,
                )

            for future in as_completed(future_map):
                (
                    provider_name,
                    invocation_id,
                    lease,
                    output_limit,
                    _timeout_limit,
                ) = future_map[future]
                response: ProviderResponse | None = None
                provider_error: ProviderError | None = None
                try:
                    response = future.result()
                except ProviderError as exc:
                    provider_error = exc
                except Exception as exc:  # defensive provider boundary
                    provider_error = ProviderError(
                        f"Unexpected provider failure: {type(exc).__name__}",
                        category=ErrorCategory.UNKNOWN,
                        retryable=False,
                        ambiguous=True,
                    )
                if (
                    lease is not None
                    and not (
                        provider_error is not None
                        and provider_error.ambiguous
                    )
                ):
                    try:
                        lease.reconcile(1)
                    except Exception as exc:
                        provider_error = ProviderError(
                            "Provider call completed but budget reconciliation "
                            f"failed safely: {type(exc).__name__}",
                            category=ErrorCategory.BUDGET,
                            retryable=False,
                            ambiguous=True,
                        )
                        response = None
                if provider_error is not None:
                    self.store.finish_invocation_failure(
                        invocation_id,
                        provider_error,
                    )
                    failures.append(
                        self._failure_payload(
                            stage,
                            provider_name,
                            provider_error,
                        )
                    )
                    continue
                completion_error = self._completion_error(response)
                if completion_error is not None:
                    if (
                        response is not None
                        and response.finish_reason == "length"
                        and allow_truncation_recovery
                        and self.policy.truncation_retries
                        and provider_name not in prior_truncations
                        and truncation_retry_counts.get(
                            provider_name, 0
                        )
                        < self.policy.truncation_retries
                    ):
                        self.store.append_event(
                            run_id,
                            "truncated_response_preserved",
                            {
                                "stage": stage,
                                "provider": provider_name,
                                "requested_max_output_tokens": output_limit,
                                "response": response.to_dict(),
                            },
                        )
                        self.store.finish_invocation_failure(
                            invocation_id,
                            completion_error,
                        )
                        recoverable[provider_name] = (
                            work[provider_name],
                            response,
                            output_limit,
                        )
                        continue
                    if response is not None and preserve_incomplete_responses:
                        self.store.append_event(
                            run_id,
                            "incomplete_response_preserved",
                            {
                                "stage": stage,
                                "provider": provider_name,
                                "response": response.to_dict(),
                            },
                        )
                    self.store.finish_invocation_failure(
                        invocation_id, completion_error
                    )
                    failures.append(
                        self._failure_payload(
                            stage, provider_name, completion_error
                        )
                    )
                else:
                    assert response is not None
                    self.store.finish_invocation_success(
                        invocation_id, response
                    )
                    successes[provider_name] = response

        if recoverable:
            recovery_prompts: dict[str, tuple[str, str]] = {}
            recovery_limits: dict[str, int] = {}
            for provider_name, (
                prompt_pair,
                initial_response,
                initial_limit,
            ) in recoverable.items():
                recovery_limit = min(
                    max(initial_limit + 1_024, initial_limit * 2),
                    self.policy.max_recovery_output_tokens,
                )
                if recovery_limit <= initial_limit:
                    error = self._completion_error(initial_response)
                    assert error is not None
                    failures.append(
                        self._failure_payload(
                            stage,
                            provider_name,
                            error,
                        )
                    )
                    recovery = {
                        "stage": stage,
                        "provider": provider_name,
                        "status": "not_attempted",
                        "reason": "recovery output limit unavailable",
                        "initial_max_output_tokens": initial_limit,
                    }
                    self.store.append_event(
                        run_id,
                        "truncation_recovery",
                        recovery,
                    )
                    recoveries.append(recovery)
                    continue
                recovery_prompts[provider_name] = prompt_pair
                recovery_limits[provider_name] = recovery_limit

            if recovery_prompts:
                (
                    recovered_successes,
                    recovery_failures,
                    nested_recoveries,
                ) = self._run_parallel_stage(
                    run_id=run_id,
                    stage=stage,
                    prompts=recovery_prompts,
                    deadline=deadline,
                    allow_truncation_recovery=False,
                    output_overrides=recovery_limits,
                    provider_stage=provider_stage,
                    retry_failed_invocations=retry_failed_invocations,
                    preserve_incomplete_responses=(
                        preserve_incomplete_responses
                    ),
                )
                successes.update(recovered_successes)
                failures.extend(recovery_failures)
                recoveries.extend(nested_recoveries)
                failed_recovery_providers = {
                    failure["provider"] for failure in recovery_failures
                }
                for provider_name, recovery_limit in recovery_limits.items():
                    recovery = {
                        "stage": stage,
                        "provider": provider_name,
                        "status": (
                            "recovered"
                            if provider_name in recovered_successes
                            else "failed"
                        ),
                        "initial_finish_reason": "length",
                        "initial_max_output_tokens": (
                            recoverable[provider_name][2]
                        ),
                        "recovery_max_output_tokens": recovery_limit,
                        "final_failure_recorded": (
                            provider_name in failed_recovery_providers
                        ),
                    }
                    self.store.append_event(
                        run_id,
                        "truncation_recovery",
                        recovery,
                    )
                    recoveries.append(recovery)
        return successes, failures, recoveries

    @staticmethod
    def _is_length_failure_record(
        record: dict[str, Any] | None,
    ) -> bool:
        return bool(
            record
            and record.get("status") == "failed"
            and not record.get("error_ambiguous")
            and (
                record.get("error_message")
                or ""
            ).endswith("(finish_reason=length)")
        )

    @staticmethod
    def _completion_error(response: Any) -> ProviderError | None:
        if not isinstance(response, ProviderResponse):
            return ProviderError(
                "Provider returned an invalid response object",
                category=ErrorCategory.INVALID_RESPONSE,
                retryable=False,
                ambiguous=False,
            )
        if response.finish_reason == "stop":
            return None
        category = (
            ErrorCategory.CONTENT_FILTER
            if response.finish_reason == "content_filter"
            else ErrorCategory.INVALID_RESPONSE
        )
        return ProviderError(
            "Provider response did not complete normally"
            f" (finish_reason={response.finish_reason or 'missing'})",
            category=category,
            retryable=False,
            request_id=response.request_id,
            attempts=response.attempts,
            ambiguous=False,
        )

    def _validate_provider_lock(self, locked: list[dict[str, Any]]) -> None:
        current = [
            provider.config.to_dict()
            for provider in self.providers.values()
        ]
        expected = [
            ProviderConfig.from_dict(item).to_dict()
            for item in locked
        ]
        if current != expected:
            raise ValueError(
                "Current provider configuration or order differs from the "
                "run lock"
            )

    def _validate_policy_lock(self, locked: dict[str, Any]) -> None:
        expected = dict(locked)
        synthesis_provider = expected.pop(
            "synthesis_provider", self.synthesis_provider
        )
        # Normalize older run locks through the parser so policy fields added
        # with backward-compatible defaults do not strand resumable runs.
        unknown_fields = set(expected) - set(RunPolicy().to_dict())
        if unknown_fields:
            raise ValueError("Run policy lock contains unknown fields")
        expected_policy = RunPolicy.from_dict(expected).to_dict()
        if (
            expected_policy != self.policy.to_dict()
            or synthesis_provider != self.synthesis_provider
        ):
            raise ValueError(
                "Current policy or synthesis provider differs from the run lock"
            )

    @staticmethod
    def _candidate_mapping(
        run_id: str, provider_names: list[str]
    ) -> dict[str, str]:
        seed_bytes = hashlib.sha256(
            f"{run_id}:candidate-label-map-v1".encode()
        ).digest()
        rng = random.Random(int.from_bytes(seed_bytes[:8], "big"))
        shuffled = sorted(provider_names)
        rng.shuffle(shuffled)
        return {
            candidate_label(index): provider_name
            for index, provider_name in enumerate(shuffled)
        }

    def _load_candidate_namespace_lock(
        self,
        run_id: str,
        events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        matching = [
            event
            for event in (
                self.store.list_events(run_id)
                if events is None
                else events
            )
            if event["event_type"] == _CANDIDATE_NAMESPACE_EVENT
        ]
        if not matching:
            return None
        if len(matching) != 1:
            raise ValueError(
                "Run contains duplicate candidate namespace locks"
            )
        payload = matching[0].get("payload")
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "candidate_label_mapping",
            "proposal_failures",
        }:
            raise ValueError("Candidate namespace lock is malformed")
        if payload["version"] != _CANDIDATE_NAMESPACE_VERSION:
            raise ValueError("Candidate namespace lock version is unsupported")

        raw_mapping = payload["candidate_label_mapping"]
        if not isinstance(raw_mapping, dict) or not raw_mapping:
            raise ValueError("Candidate namespace mapping is malformed")
        if any(
            not isinstance(label, str)
            or not isinstance(provider, str)
            for label, provider in raw_mapping.items()
        ):
            raise ValueError("Candidate namespace mapping is malformed")
        expected_labels = [
            candidate_label(index) for index in range(len(raw_mapping))
        ]
        if set(raw_mapping) != set(expected_labels):
            raise ValueError("Candidate namespace labels are malformed")
        mapping = {
            label: raw_mapping[label] for label in expected_labels
        }
        selected_providers = set(mapping.values())
        if (
            len(selected_providers) != len(mapping)
            or not selected_providers <= set(self.providers)
            or mapping
            != self._candidate_mapping(run_id, list(selected_providers))
        ):
            raise ValueError("Candidate namespace mapping is inconsistent")
        selected_lineages = {
            self.providers[name].config.lineage
            for name in selected_providers
        }
        if (
            len(selected_providers) < self.policy.proposal_quorum
            or len(selected_lineages) < self.policy.min_lineages
        ):
            raise ValueError("Candidate namespace does not satisfy quorum")

        raw_failures = payload["proposal_failures"]
        if not isinstance(raw_failures, list) or any(
            not isinstance(failure, dict) for failure in raw_failures
        ):
            raise ValueError("Frozen proposal failures are malformed")
        failures = [dict(failure) for failure in raw_failures]
        if any(
            failure.get("stage") != "proposal"
            or failure.get("provider") not in self.providers
            for failure in failures
        ):
            raise ValueError("Frozen proposal failures are inconsistent")
        failed_providers = {
            str(failure["provider"]) for failure in failures
        }
        if (
            failed_providers & selected_providers
            or failed_providers
            != set(self.providers) - selected_providers
        ):
            raise ValueError("Frozen proposal membership is inconsistent")
        return {
            "version": _CANDIDATE_NAMESPACE_VERSION,
            "candidate_label_mapping": mapping,
            "proposal_failures": failures,
        }

    def _lock_candidate_namespace(
        self,
        run_id: str,
        mapping: dict[str, str],
        proposal_failures: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.store.append_event(
            run_id,
            _CANDIDATE_NAMESPACE_EVENT,
            {
                "version": _CANDIDATE_NAMESPACE_VERSION,
                "candidate_label_mapping": dict(mapping),
                "proposal_failures": [
                    dict(failure) for failure in proposal_failures
                ],
            },
        )
        locked = self._load_candidate_namespace_lock(run_id)
        assert locked is not None
        return locked

    def _load_adjudication_lock(
        self,
        run_id: str,
        namespace_lock: dict[str, Any] | None,
        events: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        matching = [
            event
            for event in (
                self.store.list_events(run_id)
                if events is None
                else events
            )
            if event["event_type"] == _ADJUDICATION_EVENT
        ]
        if not matching:
            return None
        if namespace_lock is None:
            raise ValueError(
                "Adjudication lock exists without a candidate namespace"
            )
        if len(matching) != 1:
            raise ValueError("Run contains duplicate adjudication locks")
        payload = matching[0].get("payload")
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "candidate_label_mapping",
            "jury_records",
            "jury_failures",
        }:
            raise ValueError("Adjudication lock is malformed")
        if payload["version"] != _ADJUDICATION_VERSION:
            raise ValueError("Adjudication lock version is unsupported")

        mapping = namespace_lock["candidate_label_mapping"]
        if payload["candidate_label_mapping"] != mapping:
            raise ValueError(
                "Adjudication lock candidate namespace is inconsistent"
            )
        raw_records = payload["jury_records"]
        if not isinstance(raw_records, list) or any(
            not isinstance(record, dict) for record in raw_records
        ):
            raise ValueError("Locked jury records are malformed")
        jury_records = [dict(record) for record in raw_records]
        record_jurors: set[str] = set()
        for record in jury_records:
            juror = record.get("juror")
            presentation_order = record.get("presentation_order")
            if (
                not isinstance(juror, str)
                or juror not in self.providers
                or juror in record_jurors
                or record.get("mapping") != mapping
                or not isinstance(presentation_order, list)
                or len(presentation_order) != len(mapping)
                or set(presentation_order) != set(mapping)
                or not isinstance(record.get("valid"), bool)
            ):
                raise ValueError("Locked jury records are inconsistent")
            record_jurors.add(juror)

        raw_failures = payload["jury_failures"]
        if not isinstance(raw_failures, list) or any(
            not isinstance(failure, dict) for failure in raw_failures
        ):
            raise ValueError("Locked jury failures are malformed")
        jury_failures = [dict(failure) for failure in raw_failures]
        if any(
            failure.get("stage") != "jury"
            or failure.get("provider") not in self.providers
            for failure in jury_failures
        ):
            raise ValueError("Locked jury failures are inconsistent")
        failed_jurors = {
            str(failure["provider"]) for failure in jury_failures
        }
        valid_jurors = {
            str(record["juror"])
            for record in jury_records
            if record["valid"]
        }
        invalid_jurors = record_jurors - valid_jurors
        if (
            valid_jurors & failed_jurors
            or invalid_jurors - failed_jurors
            or record_jurors | failed_jurors != set(self.providers)
            or len(valid_jurors) < self.policy.jury_quorum
        ):
            raise ValueError("Locked jury membership is inconsistent")
        try:
            aggregate_juries(
                [
                    self._judgment_payload(record)
                    for record in jury_records
                    if record["valid"]
                ],
                list(mapping.values()),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Locked jury judgments are inconsistent"
            ) from exc
        return {
            "version": _ADJUDICATION_VERSION,
            "candidate_label_mapping": dict(mapping),
            "jury_records": jury_records,
            "jury_failures": jury_failures,
        }

    def _lock_adjudication(
        self,
        run_id: str,
        mapping: dict[str, str],
        jury_records: list[dict[str, Any]],
        jury_failures: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.store.append_event(
            run_id,
            _ADJUDICATION_EVENT,
            {
                "version": _ADJUDICATION_VERSION,
                "candidate_label_mapping": dict(mapping),
                "jury_records": [dict(record) for record in jury_records],
                "jury_failures": [
                    dict(failure) for failure in jury_failures
                ],
            },
        )
        namespace_lock = self._load_candidate_namespace_lock(run_id)
        locked = self._load_adjudication_lock(
            run_id, namespace_lock
        )
        assert locked is not None
        return locked

    def _provider_retry_recoveries(
        self,
        run_id: str,
        invocations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        grouped: dict[
            tuple[str, str], list[dict[str, Any]]
        ] = {}
        seen_slots: set[tuple[str, str, int]] = set()
        all_events = self.store.list_events(run_id)
        completed_truncation_slots = {
            (
                str(event["payload"].get("stage")),
                str(event["payload"].get("provider")),
            )
            for event in all_events
            if event["event_type"] == "truncation_recovery"
            and isinstance(event.get("payload"), dict)
        }
        for event in all_events:
            if event["event_type"] != _PROVIDER_RETRY_EVENT:
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict) or set(payload) != {
                "stage",
                "provider",
                "retry_call_count",
                "retry_kind",
                "prior_failure",
            }:
                raise ValueError("Provider retry audit event is malformed")
            stage = payload["stage"]
            provider = payload["provider"]
            retry_call_count = payload["retry_call_count"]
            prior_failure = payload["prior_failure"]
            if (
                not isinstance(stage, str)
                or not isinstance(provider, str)
                or provider not in self.providers
                or not isinstance(retry_call_count, int)
                or isinstance(retry_call_count, bool)
                or retry_call_count < 2
                or payload["retry_kind"]
                not in {"application", "truncation"}
                or not isinstance(prior_failure, dict)
            ):
                raise ValueError(
                    "Provider retry audit event is inconsistent"
                )
            slot = (stage, provider, retry_call_count)
            if slot in seen_slots:
                raise ValueError(
                    "Provider retry audit event is duplicated"
                )
            seen_slots.add(slot)
            grouped.setdefault((stage, provider), []).append(
                dict(payload)
            )

        invocation_by_slot = {
            (str(invocation["stage"]), str(invocation["provider"])): invocation
            for invocation in invocations
        }
        if any(
            int(invocation.get("call_count") or 1) > 1
            and slot not in grouped
            for slot, invocation in invocation_by_slot.items()
        ):
            raise ValueError("Provider retry audit is missing")
        recoveries: list[dict[str, Any]] = []
        for slot, retry_events in grouped.items():
            retry_events.sort(
                key=lambda event: int(event["retry_call_count"])
            )
            invocation = invocation_by_slot.get(slot)
            if invocation is None:
                raise ValueError(
                    "Provider retry audit has no invocation record"
                )
            retry_counts = [
                int(event["retry_call_count"])
                for event in retry_events
            ]
            if retry_counts != list(
                range(2, int(invocation["call_count"]) + 1)
            ):
                raise ValueError(
                    "Provider retry audit is incomplete"
                )
            for index, retry_event in enumerate(retry_events):
                is_truncation = (
                    retry_event["retry_kind"] == "truncation"
                )
                if is_truncation and slot in completed_truncation_slots:
                    continue
                next_event = (
                    retry_events[index + 1]
                    if index + 1 < len(retry_events)
                    else None
                )
                final_failure: dict[str, Any] | None = None
                if next_event is not None:
                    status = "failed"
                    final_failure = dict(next_event["prior_failure"])
                elif invocation["status"] == "succeeded":
                    status = "recovered"
                elif invocation["status"] == "failed":
                    status = (
                        "ambiguous"
                        if invocation.get("error_ambiguous")
                        else "failed"
                    )
                    if isinstance(invocation.get("error"), dict):
                        final_failure = dict(invocation["error"])
                else:
                    status = "in_progress"
                recovery = {
                    "kind": (
                        "truncation_retry"
                        if is_truncation
                        else "application_retry"
                    ),
                    "stage": retry_event["stage"],
                    "provider": retry_event["provider"],
                    "retry_call_count": retry_event[
                        "retry_call_count"
                    ],
                    "status": status,
                    "prior_failure": dict(
                        retry_event["prior_failure"]
                    ),
                }
                if final_failure is not None:
                    recovery["final_failure"] = final_failure
                recoveries.append(recovery)
        return recoveries

    @staticmethod
    def _jury_presentation_order(
        run_id: str, juror_name: str, candidate_labels: list[str]
    ) -> list[str]:
        seed_bytes = hashlib.sha256(
            f"{run_id}:{juror_name}:jury-presentation-v1".encode()
        ).digest()
        rng = random.Random(int.from_bytes(seed_bytes[:8], "big"))
        order = sorted(candidate_labels)
        rng.shuffle(order)
        return order

    @staticmethod
    def _canonicalize_jury(
        jury: dict[str, Any], mapping: dict[str, str]
    ) -> dict[str, Any]:
        canonical = dict(jury)
        canonical["ranking"] = [
            mapping[label] for label in jury.get("ranking", [])
        ]
        if jury.get("winner") is not None:
            canonical["winner"] = mapping[jury["winner"]]
        return canonical

    @staticmethod
    def _anonymize_aggregate(
        aggregate: dict[str, Any], mapping: dict[str, str]
    ) -> dict[str, Any]:
        reverse = {provider: label for label, provider in mapping.items()}
        anonymous = dict(aggregate)
        anonymous.pop("candidate_label_mapping", None)
        if aggregate.get("winner") in reverse:
            anonymous["winner"] = reverse[aggregate["winner"]]
        for field in ("ranking", "tied_candidates"):
            if field in aggregate:
                anonymous[field] = [
                    reverse.get(value, value) for value in aggregate[field]
                ]
        for field in ("borda_points", "win_counts", "mean_scores"):
            if field in aggregate:
                anonymous[field] = {
                    reverse.get(key, key): value
                    for key, value in aggregate[field].items()
                }
        return anonymous

    @staticmethod
    def _anonymize_jury(
        jury: dict[str, Any], mapping: dict[str, str]
    ) -> dict[str, Any]:
        reverse = {provider: label for label, provider in mapping.items()}
        anonymous = CouncilEngine._judgment_payload(jury)
        anonymous["ranking"] = [
            reverse.get(value, value) for value in jury.get("ranking", [])
        ]
        if jury.get("winner") is not None:
            anonymous["winner"] = reverse.get(
                jury["winner"], jury["winner"]
            )
        return anonymous

    @staticmethod
    def _judgment_payload(jury: dict[str, Any]) -> dict[str, Any]:
        return {
            key: jury[key]
            for key in (
                "winner",
                "ranking",
                "confidence",
                "abstain",
                "rationale",
                "material_disagreements",
                "verification_needed",
            )
        }

    @staticmethod
    def _response_from_record(record: dict[str, Any]) -> ProviderResponse:
        if isinstance(record.get("response"), dict):
            return ProviderResponse.from_dict(record["response"])
        return ProviderResponse(
            content=record["response_text"],
            resolved_model=record.get("resolved_model") or record["model"],
            request_id=record.get("request_id"),
            usage=Usage.from_dict(record.get("usage")),
            latency_ms=int(record.get("latency_ms") or 0),
            attempts=int(record.get("attempts") or 1),
            finish_reason=record.get("finish_reason"),
            metadata=dict(record.get("metadata") or {}),
        )

    @staticmethod
    def _failure_payload(
        stage: str, provider_name: str, error: ProviderError
    ) -> dict[str, Any]:
        return {
            "stage": stage,
            "provider": provider_name,
            **error.to_dict(),
        }

    def _build_result(
        self,
        *,
        run_id: str,
        question: str,
        status: str,
        answer: str | None,
        proposals: dict[str, ProviderResponse],
        proposal_artifacts: dict[str, dict[str, Any]],
        jury_records: list[dict[str, Any]],
        aggregate: dict[str, Any] | None,
        failures: list[dict[str, Any]],
        warnings: list[str],
        recoveries: list[dict[str, Any]],
        workload_plan: dict[str, Any],
        candidate_mapping: dict[str, str] | None,
    ) -> dict[str, Any]:
        invocations = self.store.list_invocations(run_id)
        result_recoveries = [
            *recoveries,
            *self._provider_retry_recoveries(
                run_id,
                invocations,
            ),
        ]
        valid_jury_count = sum(
            1 for jury in jury_records if jury.get("valid")
        )
        completion_quality = (
            "clean"
            if (
                status == "completed"
                and not failures
                and not result_recoveries
            )
            else "degraded"
        )
        result_warnings = list(warnings)
        if status == "completed" and completion_quality == "degraded":
            result_warnings.append(
                "Council completed with degraded provider execution; "
                "inspect failures and recoveries."
            )
        actual_stage_prompt_chars: dict[str, int] = {}
        for invocation in invocations:
            prompt_chars = len(invocation.get("prompt_text") or "")
            stage = str(invocation["stage"])
            actual_stage_prompt_chars[stage] = max(
                prompt_chars,
                actual_stage_prompt_chars.get(stage, 0),
            )
        return {
            "run_id": run_id,
            "status": status,
            "completion_quality": completion_quality,
            "protocol": {
                "id": PROTOCOL_ID,
                "version": PROTOCOL_VERSION,
                "hash": protocol_hash(),
            },
            "question_sha256": hashlib.sha256(question.encode()).hexdigest(),
            "answer": answer,
            "aggregate": aggregate,
            "candidate_namespace": (
                None
                if candidate_mapping is None
                else {
                    "candidate_label_mapping": dict(candidate_mapping),
                }
            ),
            "proposals": [
                {
                    "provider": name,
                    "model": response.resolved_model,
                    "lineage": self.providers[name].config.lineage,
                    "artifact": proposal_artifacts.get(name),
                    **response.to_dict(),
                }
                for name, response in proposals.items()
            ],
            "juries": jury_records,
            "failures": failures,
            "recoveries": result_recoveries,
            "warnings": result_warnings,
            "membership": {
                "requested_providers": len(self.providers),
                "successful_proposals": len(proposals),
                "valid_juries": valid_jury_count,
                "provider_stage_failures": len(failures),
                "recovered_truncations": sum(
                    1
                    for recovery in result_recoveries
                    if (
                        recovery.get("status") == "recovered"
                        and recovery.get("kind")
                        in {None, "truncation_recovery", "truncation_retry"}
                    )
                ),
                "recovered_jury_repairs": sum(
                    1
                    for recovery in result_recoveries
                    if (
                        recovery.get("status") == "recovered"
                        and recovery.get("kind")
                        == "jury_artifact_repair"
                    )
                ),
            },
            "workload": {
                "preflight": workload_plan,
                "actual_stage_prompt_chars": actual_stage_prompt_chars,
                "application_calls": self.store.count_calls(run_id),
            },
            "limitations": [
                "Model output is analysis, not independent evidence.",
                "Metadata blinding cannot hide model writing style.",
                "Consensus can still be wrong; verify consequential claims.",
            ],
        }

    def _finish(self, run_id: str, result: dict[str, Any]) -> dict[str, Any]:
        self.store.finish_run(run_id, result)
        return result
