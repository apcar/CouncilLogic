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
    jury_prompts,
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
            for event in self.store.list_events(run_id)
            if event["event_type"] == "truncation_recovery"
        ]

        proposal_system, proposal_user = proposal_prompts(question)
        proposal_prompts_by_provider = {
            name: (proposal_system, proposal_user) for name in self.providers
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
            )
            return self._finish(run_id, result)

        jury_prompts_by_provider: dict[str, tuple[str, str]] = {}
        jury_mappings: dict[str, dict[str, str]] = {}
        for juror_name in self.providers:
            mapping = self._jury_mapping(run_id, juror_name, list(proposals))
            candidates = {
                label: proposal_artifacts[provider_name]
                for label, provider_name in mapping.items()
            }
            jury_mappings[juror_name] = mapping
            jury_prompts_by_provider[juror_name] = jury_prompts(question, candidates)

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

        jury_records: list[dict[str, Any]] = []
        for juror_name, response in jury_responses.items():
            mapping = jury_mappings[juror_name]
            try:
                parsed = parse_jury(response.content, list(mapping))
                canonical = self._canonicalize_jury(parsed, mapping)
                canonical.update(
                    {
                        "juror": juror_name,
                        "juror_model": response.resolved_model,
                        "mapping": mapping,
                        "valid": True,
                    }
                )
            except (TypeError, ValueError) as exc:
                canonical = {
                    "juror": juror_name,
                    "juror_model": response.resolved_model,
                    "mapping": mapping,
                    "valid": False,
                    "error": str(exc),
                }
                failures.append(
                    self._failure_payload(
                        "jury",
                        juror_name,
                        ProviderError(
                            f"Jury artifact was invalid: {exc}",
                            category=ErrorCategory.INVALID_RESPONSE,
                            retryable=False,
                            request_id=response.request_id,
                            attempts=response.attempts,
                            ambiguous=False,
                        ),
                    )
                )
            jury_records.append(canonical)

        valid_juries = [jury for jury in jury_records if jury.get("valid")]
        if len(valid_juries) < self.policy.jury_quorum:
            warnings.append(
                f"Jury quorum not met: {len(valid_juries)}/{self.policy.jury_quorum}"
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
            )
            return self._finish(run_id, result)

        aggregate = aggregate_juries(
            [self._judgment_payload(jury) for jury in valid_juries],
            list(proposals),
        )

        answer: str | None = None
        if time.monotonic() < deadline:
            synthesis_name = self.synthesis_provider
            synth_mapping = {
                chr(ord("A") + index): provider_name
                for index, provider_name in enumerate(sorted(proposals))
            }
            synth_candidates = {
                label: proposal_artifacts[provider_name]
                for label, provider_name in synth_mapping.items()
            }
            anonymous_aggregate = self._anonymize_aggregate(
                aggregate, synth_mapping
            )
            anonymous_juries = [
                self._anonymize_jury(jury, synth_mapping) for jury in valid_juries
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
        )
        return self._finish(run_id, result)

    def _run_parallel_stage(
        self,
        *,
        run_id: str,
        stage: str,
        prompts: dict[str, tuple[str, str]],
        deadline: float,
        allow_truncation_recovery: bool = True,
        output_overrides: dict[str, int] | None = None,
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
        existing_records = {
            (record["stage"], record["provider"]): record
            for record in self.store.list_invocations(run_id)
        }
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
                and self._is_length_failure_record(record)
                and int(record.get("call_count") or 1)
                >= 1 + self.policy.truncation_retries
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
        with ThreadPoolExecutor(max_workers=len(allowed_names)) as executor:
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
                    provider.config.output_tokens_for(stage),
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
                    provider.config.timeout_for(stage),
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
                        stage=stage,
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
        current = {
            name: provider.config.to_dict()
            for name, provider in self.providers.items()
        }
        expected = {
            str(item["name"]): ProviderConfig.from_dict(item).to_dict()
            for item in locked
        }
        if current != expected:
            raise ValueError(
                "Current provider configuration differs from the run lock"
            )

    def _validate_policy_lock(self, locked: dict[str, Any]) -> None:
        expected = dict(locked)
        synthesis_provider = expected.pop(
            "synthesis_provider", self.synthesis_provider
        )
        if (
            expected != self.policy.to_dict()
            or synthesis_provider != self.synthesis_provider
        ):
            raise ValueError(
                "Current policy or synthesis provider differs from the run lock"
            )

    @staticmethod
    def _jury_mapping(
        run_id: str, juror_name: str, provider_names: list[str]
    ) -> dict[str, str]:
        seed_bytes = hashlib.sha256(
            f"{run_id}:{juror_name}:jury-order-v1".encode()
        ).digest()
        rng = random.Random(int.from_bytes(seed_bytes[:8], "big"))
        shuffled = list(provider_names)
        rng.shuffle(shuffled)
        return {
            chr(ord("A") + index): provider_name
            for index, provider_name in enumerate(shuffled)
        }

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
    ) -> dict[str, Any]:
        valid_jury_count = sum(
            1 for jury in jury_records if jury.get("valid")
        )
        completion_quality = (
            "clean"
            if status == "completed" and not failures and not recoveries
            else "degraded"
        )
        result_warnings = list(warnings)
        if status == "completed" and completion_quality == "degraded":
            result_warnings.append(
                "Council completed with degraded provider execution; "
                "inspect failures and recoveries."
            )
        actual_stage_prompt_chars: dict[str, int] = {}
        for invocation in self.store.list_invocations(run_id):
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
            "recoveries": recoveries,
            "warnings": result_warnings,
            "membership": {
                "requested_providers": len(self.providers),
                "successful_proposals": len(proposals),
                "valid_juries": valid_jury_count,
                "provider_stage_failures": len(failures),
                "recovered_truncations": sum(
                    1
                    for recovery in recoveries
                    if recovery.get("status") == "recovered"
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
