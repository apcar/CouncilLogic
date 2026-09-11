from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.engine import CouncilEngine  # noqa: E402
from model_council.models import (  # noqa: E402
    ErrorCategory,
    ProviderConfig,
    ProviderError,
    ProviderResponse,
    RunPolicy,
    Usage,
)
from model_council.providers.base import Provider  # noqa: E402
from model_council.protocol import (  # noqa: E402
    PROTOCOL_ID,
    PROTOCOL_VERSION,
    proposal_prompts,
    protocol_hash,
)
from model_council.store import CouncilStore  # noqa: E402


class FakeProvider(Provider):
    def __init__(
        self,
        name: str,
        *,
        fail_stages: set[str] | None = None,
        finish_reasons: dict[str, str | None] | None = None,
        truncate_once_stages: set[str] | None = None,
    ) -> None:
        super().__init__(
            ProviderConfig(
                name=name,
                model=f"{name}-model-1",
                lineage=f"{name}-lineage",
                secret_name=f"{name.upper()}_KEY",
                endpoint=f"https://{name}.example.test/v1",
                max_attempts=1,
            ),
            "test-key-never-persist",
        )
        self.fail_stages = fail_stages or set()
        self.finish_reasons = finish_reasons or {}
        self.truncate_once_stages = truncate_once_stages or set()
        self.calls: list[str] = []
        self.call_limits: list[dict[str, object]] = []

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        stage: str,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ProviderResponse:
        self.calls.append(stage)
        self.call_limits.append(
            {
                "stage": stage,
                "max_output_tokens": max_output_tokens,
                "timeout_seconds": timeout_seconds,
            }
        )
        if stage in self.fail_stages:
            raise ProviderError(
                "synthetic outage",
                category=ErrorCategory.PROVIDER_SERVER,
                retryable=True,
                status_code=503,
            )
        if stage == "proposal":
            content = json.dumps(
                {
                    "outcome": (
                        f"{self.config.name} proposes the tested answer."
                    ),
                    "evidence_and_reasoning": ["Synthetic fixture."],
                    "uncertainty": ["Low."],
                    "verification_needed": ["Run the test."],
                }
            )
        elif stage == "jury":
            repair_match = re.search(
                r"BEGIN_UNTRUSTED_JURY_REPAIR_JSON\n(.*?)\n"
                r"END_UNTRUSTED_JURY_REPAIR_JSON",
                user_prompt,
                re.DOTALL,
            )
            if repair_match:
                repair_payload = json.loads(repair_match.group(1))
                decision = repair_payload["immutable_decision"]
                content = json.dumps(
                    {
                        **decision,
                        "rationale": "The original judgment was preserved.",
                        "material_disagreements": [],
                        "verification_needed": [
                            "Run the deterministic fixture."
                        ],
                    }
                )
            else:
                match = re.search(
                    r"BEGIN_UNTRUSTED_EVALUATION_JSON\n(.*?)\n"
                    r"END_UNTRUSTED_EVALUATION_JSON",
                    user_prompt,
                    re.DOTALL,
                )
                if not match:
                    raise AssertionError(
                        "jury evaluation payload was not present"
                    )
                candidates = json.loads(match.group(1))["candidates"]
                labels = sorted(
                    candidates,
                    key=lambda label: (
                        "alpha proposes"
                        not in candidates[label].get("outcome", ""),
                        candidates[label].get("outcome", ""),
                    ),
                )
                content = json.dumps(
                    {
                        "winner": labels[0],
                        "ranking": labels,
                        "confidence": "medium",
                        "abstain": False,
                        "rationale": "The first candidate is best supported.",
                        "material_disagreements": [
                            (
                                f"{labels[0]} uses different wording from "
                                f"{labels[-1]}."
                            )
                        ],
                        "verification_needed": [
                            "Run the deterministic fixture."
                        ],
                    }
                )
        elif stage == "synthesis":
            content = (
                "## Outcome\nThe deterministic council completed.\n"
                "## Consensus\nThe fixture is internally consistent.\n"
                "## Dissent\nSynthetic wording differs.\n"
                "## Verification needed\nRun the tests."
            )
        else:
            raise AssertionError(stage)
        finish_reason = self.finish_reasons.get(stage, "stop")
        if (
            stage in self.truncate_once_stages
            and self.calls.count(stage) == 1
        ):
            finish_reason = "length"
        return ProviderResponse(
            content=content,
            resolved_model=self.config.model,
            request_id=f"request-{self.config.name}-{stage}",
            usage=Usage(input_tokens=10, output_tokens=20, total_tokens=30),
            latency_ms=5,
            attempts=1,
            finish_reason=finish_reason,
        )


class OversizedJuryProvider(FakeProvider):
    def __init__(
        self,
        name: str,
        *,
        change_repair_decision: bool = False,
        fail_repair: bool = False,
        ambiguous_repair: bool = False,
        repair_resolved_model: str | None = None,
    ) -> None:
        super().__init__(name)
        self.change_repair_decision = change_repair_decision
        self.fail_repair = fail_repair
        self.ambiguous_repair = ambiguous_repair
        self.repair_resolved_model = repair_resolved_model

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        stage: str,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ProviderResponse:
        is_repair = "BEGIN_UNTRUSTED_JURY_REPAIR_JSON" in user_prompt
        if is_repair and (self.fail_repair or self.ambiguous_repair):
            self.calls.append(stage)
            self.call_limits.append(
                {
                    "stage": stage,
                    "max_output_tokens": max_output_tokens,
                    "timeout_seconds": timeout_seconds,
                }
            )
            raise ProviderError(
                (
                    "synthetic ambiguous repair outcome"
                    if self.ambiguous_repair
                    else "synthetic repair outage"
                ),
                category=ErrorCategory.PROVIDER_SERVER,
                retryable=True,
                status_code=503,
                ambiguous=self.ambiguous_repair,
            )
        response = super().generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            stage=stage,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        if stage != "jury":
            return response
        value = json.loads(response.content)
        if is_repair:
            if self.change_repair_decision:
                value["ranking"] = list(reversed(value["ranking"]))
                value["winner"] = value["ranking"][0]
        else:
            value["rationale"] = "x" * 1_001
        return ProviderResponse(
            content=json.dumps(value),
            resolved_model=(
                self.repair_resolved_model
                if is_repair and self.repair_resolved_model is not None
                else response.resolved_model
            ),
            request_id=response.request_id,
            usage=response.usage,
            latency_ms=response.latency_ms,
            attempts=response.attempts,
            finish_reason=response.finish_reason,
            metadata=response.metadata,
        )


class ActiveCallTracker:
    def __init__(self) -> None:
        self.active = 0
        self.maximum = 0
        self.lock = threading.Lock()

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)

    def leave(self) -> None:
        with self.lock:
            self.active -= 1


class TrackedProvider(FakeProvider):
    def __init__(self, name: str, tracker: ActiveCallTracker) -> None:
        super().__init__(name)
        self.tracker = tracker

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        stage: str,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ProviderResponse:
        self.tracker.enter()
        try:
            # Sleeping releases the interpreter lock so the executor reaches
            # the configured amount of overlap deterministically.
            time.sleep(0.02)
            return super().generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                stage=stage,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
            )
        finally:
            self.tracker.leave()


class ManualClock:
    def __init__(self, initial: float = 0.0) -> None:
        self.value = initial
        self.lock = threading.Lock()

    def monotonic(self) -> float:
        with self.lock:
            return self.value

    def advance_to(self, value: float) -> None:
        with self.lock:
            self.value = max(self.value, value)


class _TestCallLease:
    def reconcile(self, actual_units: int) -> None:
        return

    def release(self) -> None:
        return


class RepairDeadlineGate:
    def __init__(self, clock: ManualClock, deadline: float) -> None:
        self.clock = clock
        self.deadline = deadline
        self.advanced = False
        self.repair_attempts: list[int] = []

    def reserve(
        self,
        *,
        stage: str,
        attempt: int,
        **_kwargs: object,
    ) -> _TestCallLease:
        if stage == "jury_repair":
            self.repair_attempts.append(attempt)
            if not self.advanced:
                self.clock.advance_to(self.deadline)
                self.advanced = True
        return _TestCallLease()


class OneShotDenyGate:
    def __init__(self, stage: str, provider: str) -> None:
        self.stage = stage
        self.provider = provider
        self.denied = False
        self.attempts: list[tuple[str, str, int]] = []

    def reserve(
        self,
        *,
        stage: str,
        provider: ProviderConfig,
        attempt: int,
        **_kwargs: object,
    ) -> _TestCallLease:
        self.attempts.append((stage, provider.name, attempt))
        if (
            stage == self.stage
            and provider.name == self.provider
            and not self.denied
        ):
            self.denied = True
            raise ProviderError(
                "synthetic service gate denial before provider dispatch",
                category=ErrorCategory.BUDGET,
                retryable=False,
                request_id="synthetic-gate-denial",
                ambiguous=False,
            )
        return _TestCallLease()


class DeadlineAdvancingProvider(FakeProvider):
    def __init__(self, name: str, clock: ManualClock, value: float) -> None:
        super().__init__(name)
        self.clock = clock
        self.value = value

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        stage: str,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ProviderResponse:
        response = super().generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            stage=stage,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )
        self.clock.advance_to(self.value)
        return response


class StageDelayProvider(FakeProvider):
    def __init__(self, name: str, proposal_delay: float) -> None:
        super().__init__(name)
        self.proposal_delay = proposal_delay

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        stage: str,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ProviderResponse:
        if stage == "proposal":
            time.sleep(self.proposal_delay)
        return super().generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            stage=stage,
            max_output_tokens=max_output_tokens,
            timeout_seconds=timeout_seconds,
        )


class AmbiguousSynthesisProvider(FakeProvider):
    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        stage: str,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ProviderResponse:
        if stage != "synthesis":
            return super().generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                stage=stage,
                max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
            )
        self.calls.append(stage)
        self.call_limits.append(
            {
                "stage": stage,
                "max_output_tokens": max_output_tokens,
                "timeout_seconds": timeout_seconds,
            }
        )
        raise ProviderError(
            "synthetic ambiguous synthesis timeout",
            category=ErrorCategory.TIMEOUT,
            retryable=True,
            request_id="ambiguous-synthesis-request",
            ambiguous=True,
        )


class CouncilEngineTests(unittest.TestCase):
    def _engine(
        self,
        directory: Path,
        *,
        fail_provider: str | None = None,
    ) -> tuple[CouncilEngine, dict[str, FakeProvider], CouncilStore]:
        providers = {
            name: FakeProvider(
                name,
                fail_stages={"proposal", "jury"}
                if name == fail_provider
                else set(),
            )
            for name in ("alpha", "beta", "gamma")
        }
        store = CouncilStore(directory)
        engine = CouncilEngine(
            store=store,
            providers=providers,
            policy=RunPolicy(
                proposal_quorum=2,
                jury_quorum=2,
                min_lineages=2,
                max_calls=10,
                deadline_seconds=30,
            ),
            synthesis_provider="alpha",
        )
        return engine, providers, store

    def _ordered_engine(
        self,
        directory: Path,
        *,
        synthesis_failures: set[str] | None = None,
        max_calls: int = 9,
    ) -> tuple[CouncilEngine, dict[str, FakeProvider], CouncilStore]:
        synthesis_failures = synthesis_failures or set()
        providers = {
            name: FakeProvider(
                name,
                fail_stages=(
                    {"synthesis"}
                    if name in synthesis_failures
                    else set()
                ),
            )
            for name in ("alpha", "beta", "gamma")
        }
        store = CouncilStore(directory)
        engine = CouncilEngine(
            store=store,
            providers=providers,
            policy=RunPolicy(
                proposal_quorum=2,
                jury_quorum=2,
                min_lineages=2,
                max_calls=max_calls,
                deadline_seconds=30,
            ),
            synthesis_provider="alpha",
            synthesis_fallbacks=("beta", "gamma"),
        )
        return engine, providers, store

    def test_ordered_synthesis_primary_success_stops_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._ordered_engine(
                Path(temporary)
            )

            result = engine.run("Use the first successful synthesizer.")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["completion_quality"], "clean")
            self.assertEqual(store.count_calls(result["run_id"]), 7)
            self.assertEqual(
                result["synthesis"],
                {
                    "configured_order": ["alpha", "beta", "gamma"],
                    "selected_provider": "alpha",
                    "selected_position": 1,
                    "attempts": [
                        {
                            "position": 1,
                            "provider": "alpha",
                            "status": "succeeded",
                            "invocation_id": result["synthesis"]["attempts"][0][
                                "invocation_id"
                            ],
                            "resolved_model": "alpha-model-1",
                            "call_count": 1,
                        },
                        {
                            "position": 2,
                            "provider": "beta",
                            "status": "not_attempted",
                        },
                        {
                            "position": 3,
                            "provider": "gamma",
                            "status": "not_attempted",
                        },
                    ],
                },
            )
            self.assertEqual(
                [providers[name].calls.count("synthesis") for name in providers],
                [1, 0, 0],
            )
            self.assertFalse(
                any(
                    event["event_type"] == "synthesis_fallback_advanced"
                    for event in store.list_events(result["run_id"])
                )
            )

    def test_ordered_synthesis_uses_first_successful_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._ordered_engine(
                Path(temporary),
                synthesis_failures={"alpha"},
            )

            result = engine.run("Advance explicitly after synthesis failure.")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["completion_quality"], "degraded")
            self.assertEqual(store.count_calls(result["run_id"]), 8)
            self.assertEqual(
                (
                    result["synthesis"]["selected_provider"],
                    result["synthesis"]["selected_position"],
                ),
                ("beta", 2),
            )
            self.assertEqual(
                [
                    attempt["status"]
                    for attempt in result["synthesis"]["attempts"]
                ],
                ["failed", "succeeded", "not_attempted"],
            )
            self.assertEqual(
                result["synthesis"]["attempts"][0]["category"],
                ErrorCategory.PROVIDER_SERVER.value,
            )
            self.assertFalse(
                result["synthesis"]["attempts"][0]["ambiguous"]
            )
            self.assertEqual(
                [providers[name].calls.count("synthesis") for name in providers],
                [1, 1, 0],
            )
            fallback_events = [
                event["payload"]
                for event in store.list_events(result["run_id"])
                if event["event_type"] == "synthesis_fallback_advanced"
            ]
            synthesis_failure = next(
                failure
                for failure in result["failures"]
                if failure["stage"] == "synthesis"
                and failure["provider"] == "alpha"
            )
            self.assertEqual(
                fallback_events,
                [
                    {
                        "version": 1,
                        "from_position": 1,
                        "from_provider": "alpha",
                        "to_position": 2,
                        "to_provider": "beta",
                        "reason_category": (
                            ErrorCategory.PROVIDER_SERVER.value
                        ),
                        "ambiguous": False,
                        "failure": {
                            key: value
                            for key, value in synthesis_failure.items()
                            if key not in {"stage", "provider"}
                        },
                    }
                ],
            )
            self.assertTrue(
                any("fallback provider beta" in item for item in result["warnings"])
            )

    def test_ordered_synthesis_all_fail_preserves_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._ordered_engine(
                Path(temporary),
                synthesis_failures={"alpha", "beta", "gamma"},
            )

            result = engine.run("Preserve every failed synthesis attempt.")

            self.assertEqual(result["status"], "partial")
            self.assertIsNone(result["answer"])
            self.assertIsNone(result["synthesis"]["selected_provider"])
            self.assertIsNone(result["synthesis"]["selected_position"])
            self.assertEqual(
                [
                    attempt["status"]
                    for attempt in result["synthesis"]["attempts"]
                ],
                ["failed", "failed", "failed"],
            )
            self.assertEqual(
                [providers[name].calls.count("synthesis") for name in providers],
                [1, 1, 1],
            )
            self.assertEqual(store.count_calls(result["run_id"]), 9)
            self.assertEqual(
                len(
                    [
                        failure
                        for failure in result["failures"]
                        if failure["stage"] == "synthesis"
                    ]
                ),
                3,
            )
            fallback_events = [
                event["payload"]
                for event in store.list_events(result["run_id"])
                if event["event_type"] == "synthesis_fallback_advanced"
            ]
            self.assertEqual(
                [
                    (event["from_provider"], event["to_provider"])
                    for event in fallback_events
                ],
                [("alpha", "beta"), ("beta", "gamma")],
            )
            self.assertTrue(
                any(
                    "raw council record preserved" in warning
                    for warning in result["warnings"]
                )
            )

            calls_before_resume = {
                name: list(provider.calls)
                for name, provider in providers.items()
            }
            fallback_events_before = [
                event["payload"]
                for event in store.list_events(result["run_id"])
                if event["event_type"] == "synthesis_fallback_advanced"
            ]

            resumed = engine.resume(result["run_id"])

            self.assertEqual(resumed["failures"], result["failures"])
            self.assertEqual(resumed["synthesis"], result["synthesis"])
            self.assertEqual(
                {
                    name: provider.calls
                    for name, provider in providers.items()
                },
                calls_before_resume,
            )
            self.assertEqual(
                [
                    event["payload"]
                    for event in store.list_events(result["run_id"])
                    if event["event_type"]
                    == "synthesis_fallback_advanced"
                ],
                fallback_events_before,
            )

    def test_ambiguous_terminal_synthesis_failure_is_stable_on_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            providers: dict[str, FakeProvider] = {
                "alpha": AmbiguousSynthesisProvider("alpha"),
                "beta": FakeProvider("beta"),
                "gamma": FakeProvider("gamma"),
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=2,
                    min_lineages=2,
                    max_calls=7,
                    deadline_seconds=30,
                ),
                synthesis_provider="alpha",
            )

            first = engine.run("Preserve an ambiguous synthesis failure.")
            first_failure = next(
                failure
                for failure in first["failures"]
                if failure["stage"] == "synthesis"
            )
            calls_before_resume = list(providers["alpha"].calls)

            resumed = engine.resume(first["run_id"])
            resumed_failure = next(
                failure
                for failure in resumed["failures"]
                if failure["stage"] == "synthesis"
            )

            self.assertEqual(first["status"], "partial")
            self.assertEqual(resumed["status"], "partial")
            self.assertEqual(resumed_failure, first_failure)
            self.assertEqual(
                resumed["synthesis"]["attempts"][0]["category"],
                ErrorCategory.TIMEOUT.value,
            )
            self.assertEqual(providers["alpha"].calls, calls_before_resume)

    def test_undispatched_synthesis_retry_restores_prior_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            providers["alpha"].fail_stages.add("synthesis")
            first = engine.run(
                "Preserve a synthesis failure when its retry is denied."
            )
            first_failure = next(
                failure
                for failure in first["failures"]
                if failure["stage"] == "synthesis"
            )
            prior_invocation = next(
                invocation
                for invocation in store.list_invocations(first["run_id"])
                if invocation["stage"] == "synthesis"
            )
            providers["alpha"].fail_stages.remove("synthesis")
            gate = OneShotDenyGate("synthesis", "alpha")
            engine.call_gate = gate

            denied = engine.resume(first["run_id"])

            denied_failure = next(
                failure
                for failure in denied["failures"]
                if failure["stage"] == "synthesis"
            )
            restored_invocation = next(
                invocation
                for invocation in store.list_invocations(first["run_id"])
                if invocation["stage"] == "synthesis"
            )
            self.assertEqual(denied["status"], "partial")
            self.assertEqual(denied_failure, first_failure)
            self.assertEqual(restored_invocation, prior_invocation)
            self.assertEqual(providers["alpha"].calls.count("synthesis"), 1)
            self.assertFalse(
                any(
                    event["event_type"] == "provider_retry_started"
                    for event in store.list_events(first["run_id"])
                )
            )
            undispatched = [
                event["payload"]
                for event in store.list_events(first["run_id"])
                if event["event_type"] == "provider_call_not_dispatched"
            ]
            self.assertEqual(len(undispatched), 1)
            self.assertEqual(undispatched[0]["reservation_attempt"], 2)
            self.assertEqual(
                undispatched[0]["prior_failure"],
                {
                    key: value
                    for key, value in first_failure.items()
                    if key not in {"stage", "provider"}
                },
            )

            recovered = engine.resume(first["run_id"])

            self.assertEqual(recovered["status"], "completed")
            self.assertEqual(providers["alpha"].calls.count("synthesis"), 2)
            self.assertEqual(
                [
                    attempt
                    for stage, provider, attempt in gate.attempts
                    if stage == "synthesis" and provider == "alpha"
                ],
                [2, 3],
            )
            final_invocation = next(
                invocation
                for invocation in store.list_invocations(first["run_id"])
                if invocation["stage"] == "synthesis"
            )
            self.assertEqual(final_invocation["call_count"], 2)
            retry_events = [
                event["payload"]
                for event in store.list_events(first["run_id"])
                if event["event_type"] == "provider_retry_started"
            ]
            self.assertEqual(
                [event["retry_call_count"] for event in retry_events],
                [2],
            )

    def test_resume_preserves_no_dispatch_fallback_result_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            providers: dict[str, FakeProvider] = {
                "alpha": StageDelayProvider("alpha", 0.03),
                "beta": FakeProvider("beta"),
                "gamma": FakeProvider(
                    "gamma", fail_stages={"proposal"}
                ),
            }
            store = CouncilStore(Path(temporary))
            gate = OneShotDenyGate("synthesis", "alpha")
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=2,
                    min_lineages=2,
                    max_calls=8,
                    deadline_seconds=30,
                ),
                synthesis_provider="alpha",
                synthesis_fallbacks=("beta",),
                call_gate=gate,
            )
            run_id = engine.create_run(
                "Keep result ordering stable after interrupted finish."
            )
            captured: list[dict[str, object]] = []

            def crash_finish(
                _run_id: str, result: dict[str, object]
            ) -> dict[str, object]:
                captured.append(result)
                raise RuntimeError("synthetic interrupted finish")

            with patch.object(engine, "_finish", side_effect=crash_finish):
                with self.assertRaisesRegex(RuntimeError, "interrupted finish"):
                    engine.resume(run_id)

            self.assertEqual(len(captured), 1)
            first_result = captured[0]
            self.assertEqual(
                [proposal["provider"] for proposal in first_result["proposals"]],
                ["alpha", "beta"],
            )
            self.assertEqual(
                [failure["stage"] for failure in first_result["failures"]],
                ["proposal", "synthesis"],
            )
            fallback_event = next(
                event["payload"]
                for event in store.list_events(run_id)
                if event["event_type"] == "synthesis_fallback_advanced"
            )
            first_synthesis_failure = first_result["failures"][1]
            self.assertEqual(
                fallback_event["failure"],
                {
                    key: value
                    for key, value in first_synthesis_failure.items()
                    if key not in {"stage", "provider"}
                },
            )
            self.assertFalse(
                any(
                    invocation["stage"] == "synthesis"
                    and invocation["provider"] == "alpha"
                    for invocation in store.list_invocations(run_id)
                )
            )

            resumed = engine.resume(run_id)

            self.assertEqual(resumed, first_result)
            self.assertEqual(
                [failure["stage"] for failure in resumed["failures"]],
                ["proposal", "synthesis"],
            )
            self.assertEqual(
                [proposal["provider"] for proposal in resumed["proposals"]],
                ["alpha", "beta"],
            )

    def test_resume_reuses_persisted_fallback_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._ordered_engine(
                Path(temporary),
                synthesis_failures={"alpha"},
            )
            run_id = engine.create_run("Resume after fallback synthesis.")

            with patch.object(
                engine,
                "_finish",
                side_effect=RuntimeError("synthetic post-synthesis crash"),
            ):
                with self.assertRaisesRegex(RuntimeError, "post-synthesis"):
                    engine.resume(run_id)
            calls_before_resume = {
                name: list(provider.calls)
                for name, provider in providers.items()
            }
            fallback_events_before = [
                event["payload"]
                for event in store.list_events(run_id)
                if event["event_type"] == "synthesis_fallback_advanced"
            ]

            result = engine.resume(run_id)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                result["synthesis"]["selected_provider"],
                "beta",
            )
            self.assertEqual(
                {
                    name: provider.calls for name, provider in providers.items()
                },
                calls_before_resume,
            )
            fallback_events_after = [
                event["payload"]
                for event in store.list_events(run_id)
                if event["event_type"] == "synthesis_fallback_advanced"
            ]
            self.assertEqual(fallback_events_after, fallback_events_before)
            self.assertEqual(len(fallback_events_after), 1)
            self.assertEqual(
                {
                    invocation["provider"]: invocation["call_count"]
                    for invocation in store.list_invocations(run_id)
                    if invocation["stage"] == "synthesis"
                },
                {"alpha": 1, "beta": 1},
            )

    def test_resume_advances_after_crash_before_fallback_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._ordered_engine(
                Path(temporary),
                synthesis_failures={"alpha"},
            )
            run_id = engine.create_run(
                "Advance after a persisted failure without a transition."
            )

            with patch.object(
                engine,
                "_persist_synthesis_fallback_event",
                side_effect=RuntimeError("synthetic pre-transition crash"),
            ):
                with self.assertRaisesRegex(RuntimeError, "pre-transition"):
                    engine.resume(run_id)

            self.assertEqual(providers["alpha"].calls.count("synthesis"), 1)
            self.assertFalse(
                any(
                    event["event_type"] == "synthesis_fallback_advanced"
                    for event in store.list_events(run_id)
                )
            )

            result = engine.resume(run_id)

            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                result["synthesis"]["selected_provider"],
                "beta",
            )
            self.assertEqual(providers["alpha"].calls.count("synthesis"), 1)
            self.assertEqual(providers["beta"].calls.count("synthesis"), 1)
            fallback = next(
                event["payload"]
                for event in store.list_events(run_id)
                if event["event_type"] == "synthesis_fallback_advanced"
            )
            self.assertEqual(
                fallback["reason_category"],
                ErrorCategory.PROVIDER_SERVER.value,
            )

    def test_complete_run_and_resume_are_durable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))

            result = engine.run(
                "Does the deterministic fixture complete?",
                idempotency_key="fixture-1",
            )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["completion_quality"], "clean")
            self.assertIn("deterministic council completed", result["answer"])
            self.assertEqual(len(result["proposals"]), 3)
            self.assertTrue(
                all(proposal["artifact"] for proposal in result["proposals"])
            )
            self.assertEqual(len(result["juries"]), 3)
            self.assertIsNotNone(result["aggregate"]["winner"])
            mapping = result["aggregate"]["candidate_label_mapping"]
            self.assertEqual(
                result["candidate_namespace"][
                    "candidate_label_mapping"
                ],
                mapping,
            )
            self.assertEqual(
                {
                    json.dumps(jury["mapping"], sort_keys=True)
                    for jury in result["juries"]
                },
                {json.dumps(mapping, sort_keys=True)},
            )
            self.assertTrue(
                all(
                    set(jury["presentation_order"]) == set(mapping)
                    for jury in result["juries"]
                )
            )
            alpha_label = next(
                label
                for label, provider in mapping.items()
                if provider == "alpha"
            )
            self.assertTrue(
                any(
                    alpha_label in disagreement
                    for disagreement in result["aggregate"][
                        "material_disagreements"
                    ]
                )
            )
            self.assertTrue(result["workload"]["preflight"]["within_limits"])
            self.assertEqual(result["membership"]["successful_proposals"], 3)
            self.assertEqual(store.get_run(result["run_id"])["status"], "completed")
            calls_before = {
                name: list(provider.calls) for name, provider in providers.items()
            }

            resumed = engine.resume(result["run_id"])

            self.assertEqual(resumed, result)
            self.assertEqual(
                calls_before,
                {name: provider.calls for name, provider in providers.items()},
            )

    def test_invalid_jury_prose_is_repaired_once_without_changing_vote(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            providers: dict[str, FakeProvider] = {
                "alpha": OversizedJuryProvider(
                    "alpha", repair_resolved_model="repair-fallback-model"
                ),
                "beta": FakeProvider("beta"),
                "gamma": FakeProvider("gamma"),
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=2,
                    min_lineages=2,
                    max_calls=10,
                    deadline_seconds=30,
                    jury_repair_attempts=1,
                ),
                synthesis_provider="alpha",
            )

            result = engine.run("Repair only the invalid jury prose.")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["completion_quality"], "degraded")
            self.assertEqual(result["failures"], [])
            self.assertEqual(result["membership"]["valid_juries"], 3)
            self.assertEqual(
                result["membership"]["recovered_jury_repairs"], 1
            )
            self.assertEqual(
                result["membership"]["recovered_truncations"], 0
            )
            repair = next(
                item
                for item in result["recoveries"]
                if item.get("kind") == "jury_artifact_repair"
            )
            self.assertEqual(repair["provider"], "alpha")
            self.assertEqual(repair["status"], "recovered")
            self.assertTrue(repair["decision_preserved"])
            self.assertEqual(
                repair["repair_resolved_model"], "repair-fallback-model"
            )
            alpha_judgment = next(
                jury for jury in result["juries"] if jury["juror"] == "alpha"
            )
            self.assertEqual(
                alpha_judgment["juror_model"],
                providers["alpha"].config.model,
            )
            invocations = store.list_invocations(result["run_id"])
            alpha_jury = next(
                item
                for item in invocations
                if item["provider"] == "alpha"
                and item["stage"] == "jury"
            )
            alpha_repair = next(
                item
                for item in invocations
                if item["provider"] == "alpha"
                and item["stage"] == "jury_repair"
            )
            self.assertEqual(
                len(json.loads(alpha_jury["response_text"])["rationale"]),
                1_001,
            )
            self.assertLessEqual(
                len(json.loads(alpha_repair["response_text"])["rationale"]),
                1_000,
            )
            self.assertEqual(
                alpha_repair["resolved_model"], "repair-fallback-model"
            )
            self.assertEqual(store.count_calls(result["run_id"]), 8)
            calls_before = {
                name: list(provider.calls)
                for name, provider in providers.items()
            }

            resumed = engine.resume(result["run_id"])

            self.assertEqual(resumed, result)
            self.assertEqual(
                calls_before,
                {name: provider.calls for name, provider in providers.items()},
            )

    def test_successful_jury_repair_is_reused_after_interrupted_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            providers: dict[str, FakeProvider] = {
                "alpha": OversizedJuryProvider("alpha"),
                "beta": FakeProvider("beta"),
                "gamma": FakeProvider("gamma"),
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=2,
                    min_lineages=2,
                    max_calls=8,
                    deadline_seconds=30,
                    jury_repair_attempts=1,
                ),
                synthesis_provider="alpha",
            )
            original_lock = engine._lock_adjudication

            def interrupt_after_repair(*args: object, **kwargs: object) -> None:
                raise RuntimeError("synthetic interruption after repair")

            engine._lock_adjudication = interrupt_after_repair  # type: ignore[method-assign]
            with self.assertRaisesRegex(RuntimeError, "after repair"):
                engine.run("Reuse a completed repair after interruption.")
            run_id = store.list_runs()[0]["run_id"]
            self.assertEqual(store.count_calls(run_id), 7)
            self.assertEqual(providers["alpha"].calls.count("jury"), 2)
            engine._lock_adjudication = original_lock  # type: ignore[method-assign]

            resumed = engine.resume(run_id)

            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(resumed["membership"]["valid_juries"], 3)
            self.assertEqual(
                resumed["membership"]["recovered_jury_repairs"], 1
            )
            self.assertEqual(store.count_calls(run_id), 8)
            self.assertEqual(providers["alpha"].calls.count("jury"), 2)
            self.assertEqual(providers["alpha"].calls.count("synthesis"), 1)
            repair_events = [
                event
                for event in store.list_events(run_id)
                if event["event_type"] == "jury_artifact_repair"
            ]
            self.assertEqual(len(repair_events), 1)

    def test_undispatched_deadline_failure_can_repair_on_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            providers: dict[str, FakeProvider] = {
                "alpha": OversizedJuryProvider("alpha"),
                "beta": FakeProvider("beta"),
                "gamma": FakeProvider("gamma"),
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=2,
                    min_lineages=2,
                    max_calls=10,
                    deadline_seconds=30,
                    jury_repair_attempts=1,
                ),
                synthesis_provider="alpha",
            )
            run_id = engine.create_run("Resume an undispatched repair.")
            labels = [
                "CANDIDATE_01",
                "CANDIDATE_02",
                "CANDIDATE_03",
            ]
            mapping = {
                label: provider
                for label, provider in zip(labels, providers, strict=True)
            }
            invalid_response = ProviderResponse(
                content=json.dumps(
                    {
                        "winner": labels[0],
                        "ranking": labels,
                        "confidence": "medium",
                        "abstain": False,
                        "rationale": "x" * 1_001,
                        "material_disagreements": [],
                        "verification_needed": ["Run the fixture."],
                    }
                ),
                resolved_model=providers["alpha"].config.model,
                request_id="initial-alpha-jury",
                usage=Usage(
                    input_tokens=10,
                    output_tokens=20,
                    total_tokens=30,
                ),
                latency_ms=5,
                attempts=1,
                finish_reason="stop",
            )

            first_records, first_failures, first_recoveries = (
                engine._parse_and_repair_juries(
                    run_id=run_id,
                    candidate_mapping=mapping,
                    presentation_orders={"alpha": labels},
                    jury_responses={"alpha": invalid_response},
                    deadline=time.monotonic() - 1,
                )
            )

            self.assertFalse(first_records[0]["valid"])
            self.assertEqual(len(first_failures), 1)
            self.assertEqual(first_recoveries[0]["status"], "not_attempted")
            self.assertEqual(store.list_invocations(run_id), [])
            self.assertFalse(
                any(
                    event["event_type"] == "jury_artifact_repair"
                    for event in store.list_events(run_id)
                )
            )

            records, failures, recoveries = engine._parse_and_repair_juries(
                run_id=run_id,
                candidate_mapping=mapping,
                presentation_orders={"alpha": labels},
                jury_responses={"alpha": invalid_response},
                deadline=time.monotonic() + 5,
            )

            self.assertTrue(records[0]["valid"])
            self.assertEqual(failures, [])
            self.assertEqual(recoveries[0]["status"], "recovered")
            self.assertEqual(store.count_calls(run_id), 1)
            self.assertEqual(providers["alpha"].calls.count("jury"), 1)

    def test_post_reservation_deadline_releases_repair_for_resume(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clock = ManualClock()
            providers: dict[str, FakeProvider] = {
                "alpha": OversizedJuryProvider("alpha"),
                "beta": FakeProvider("beta"),
                "gamma": FakeProvider("gamma"),
            }
            store = CouncilStore(Path(temporary))
            gate = RepairDeadlineGate(clock, 30.0)
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=3,
                    min_lineages=2,
                    max_calls=8,
                    deadline_seconds=30,
                    jury_repair_attempts=1,
                ),
                synthesis_provider="alpha",
                call_gate=gate,
            )

            with patch(
                "model_council.engine.time.monotonic",
                side_effect=clock.monotonic,
            ):
                first = engine.run(
                    "Resume a repair blocked after its call reservation."
                )
                repair_audit = [
                    event
                    for event in store.list_events(first["run_id"])
                    if event["event_type"]
                    == "provider_call_not_dispatched"
                ]

                self.assertEqual(first["status"], "partial")
                self.assertEqual(store.count_calls(first["run_id"]), 6)
                self.assertEqual(
                    [
                        invocation
                        for invocation in store.list_invocations(
                            first["run_id"]
                        )
                        if invocation["stage"] == "jury_repair"
                    ],
                    [],
                )
                self.assertEqual(len(repair_audit), 1)
                self.assertEqual(
                    repair_audit[0]["payload"]["error"]["category"],
                    ErrorCategory.TIMEOUT.value,
                )
                self.assertEqual(providers["alpha"].calls.count("jury"), 1)

                resumed = engine.resume(first["run_id"])

            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(store.count_calls(first["run_id"]), 8)
            self.assertEqual(providers["alpha"].calls.count("jury"), 2)
            self.assertEqual(gate.repair_attempts, [1, 2])
            repair_invocation = next(
                invocation
                for invocation in store.list_invocations(first["run_id"])
                if invocation["stage"] == "jury_repair"
            )
            self.assertEqual(repair_invocation["status"], "succeeded")
            self.assertEqual(repair_invocation["call_count"], 1)

    def test_jury_repair_that_changes_vote_is_rejected_and_not_retried(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            providers: dict[str, FakeProvider] = {
                "alpha": OversizedJuryProvider(
                    "alpha", change_repair_decision=True
                ),
                "beta": FakeProvider("beta"),
                "gamma": FakeProvider("gamma"),
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=3,
                    min_lineages=2,
                    max_calls=10,
                    deadline_seconds=30,
                    jury_repair_attempts=1,
                ),
                synthesis_provider="alpha",
            )

            first = engine.run("Reject a repair that changes the vote.")

            self.assertEqual(first["status"], "partial")
            self.assertEqual(first["membership"]["valid_juries"], 2)
            self.assertEqual(providers["alpha"].calls.count("jury"), 2)
            self.assertIn(
                "repair changed immutable jury decision fields",
                next(
                    failure["message"]
                    for failure in first["failures"]
                    if failure["provider"] == "alpha"
                ),
            )

            resumed = engine.resume(first["run_id"])

            self.assertEqual(resumed["status"], "partial")
            self.assertEqual(providers["alpha"].calls.count("jury"), 2)
            self.assertEqual(
                len(
                    [
                        item
                        for item in store.list_invocations(first["run_id"])
                        if item["provider"] == "alpha"
                        and item["stage"] == "jury_repair"
                    ]
                ),
                1,
            )

    def test_failed_jury_repair_is_not_retried_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            providers: dict[str, FakeProvider] = {
                "alpha": OversizedJuryProvider(
                    "alpha", fail_repair=True
                ),
                "beta": FakeProvider("beta"),
                "gamma": FakeProvider("gamma"),
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=3,
                    min_lineages=2,
                    max_calls=10,
                    deadline_seconds=30,
                    jury_repair_attempts=1,
                ),
                synthesis_provider="alpha",
            )

            first = engine.run("Do not retry a failed jury repair.")
            self.assertEqual(first["status"], "partial")
            self.assertEqual(providers["alpha"].calls.count("jury"), 2)

            resumed = engine.resume(first["run_id"])

            self.assertEqual(resumed["status"], "partial")
            self.assertEqual(providers["alpha"].calls.count("jury"), 2)
            repair = next(
                item
                for item in resumed["recoveries"]
                if item.get("kind") == "jury_artifact_repair"
            )
            self.assertEqual(repair["status"], "failed")

    def test_ambiguous_jury_repair_outcome_is_stable_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            providers: dict[str, FakeProvider] = {
                "alpha": OversizedJuryProvider(
                    "alpha", ambiguous_repair=True
                ),
                "beta": FakeProvider("beta"),
                "gamma": FakeProvider("gamma"),
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=3,
                    min_lineages=2,
                    max_calls=10,
                    deadline_seconds=30,
                    jury_repair_attempts=1,
                ),
                synthesis_provider="alpha",
            )

            first = engine.run("Do not retry an ambiguous jury repair.")
            first_repair = next(
                item
                for item in first["recoveries"]
                if item.get("kind") == "jury_artifact_repair"
            )
            self.assertEqual(first["status"], "partial")
            self.assertEqual(first_repair["status"], "failed")
            self.assertEqual(providers["alpha"].calls.count("jury"), 2)

            resumed = engine.resume(first["run_id"])

            resumed_repair = next(
                item
                for item in resumed["recoveries"]
                if item.get("kind") == "jury_artifact_repair"
            )
            self.assertEqual(resumed["status"], "partial")
            self.assertEqual(resumed_repair, first_repair)
            self.assertEqual(providers["alpha"].calls.count("jury"), 2)
            self.assertEqual(
                len(
                    [
                        event
                        for event in store.list_events(first["run_id"])
                        if event["event_type"] == "jury_artifact_repair"
                    ]
                ),
                1,
            )

    def test_jury_repair_reserves_the_final_call_for_synthesis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            providers: dict[str, FakeProvider] = {
                "alpha": OversizedJuryProvider("alpha"),
                "beta": FakeProvider("beta"),
                "gamma": FakeProvider("gamma"),
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=2,
                    min_lineages=2,
                    max_calls=7,
                    deadline_seconds=30,
                    jury_repair_attempts=1,
                ),
                synthesis_provider="alpha",
            )

            result = engine.run("Reserve the synthesis call.")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(store.count_calls(result["run_id"]), 7)
            self.assertEqual(providers["alpha"].calls.count("jury"), 1)
            repair = next(
                item
                for item in result["recoveries"]
                if item.get("kind") == "jury_artifact_repair"
            )
            self.assertEqual(repair["status"], "not_attempted")
            self.assertEqual(
                repair["reason"], "call budget reserved for synthesis"
            )

    def test_truncation_recovery_reserves_final_call_for_synthesis(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            providers = {
                name: FakeProvider(
                    name,
                    truncate_once_stages=(
                        {"jury"} if name == "beta" else set()
                    ),
                )
                for name in ("alpha", "beta", "gamma")
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=2,
                    min_lineages=2,
                    max_calls=7,
                    deadline_seconds=30,
                ),
                synthesis_provider="alpha",
            )

            result = engine.run("Keep the final call for synthesis.")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["completion_quality"], "degraded")
            self.assertEqual(store.count_calls(result["run_id"]), 7)
            self.assertEqual(providers["beta"].calls.count("jury"), 1)
            self.assertEqual(providers["alpha"].calls.count("synthesis"), 1)
            recovery = next(
                item
                for item in result["recoveries"]
                if item.get("stage") == "jury"
                and item.get("provider") == "beta"
            )
            self.assertEqual(recovery["status"], "not_attempted")
            self.assertEqual(
                recovery["reason"],
                "call budget reserved for synthesis",
            )
            self.assertTrue(recovery["final_failure_recorded"])
            self.assertEqual(
                [
                    event
                    for event in store.list_events(result["run_id"])
                    if event["event_type"] == "provider_retry_started"
                    and event["payload"]["stage"] == "jury"
                    and event["payload"]["provider"] == "beta"
                ],
                [],
            )

    def test_proposal_recovery_reserves_jury_calls_and_synthesis(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            providers = {
                name: FakeProvider(
                    name,
                    truncate_once_stages=(
                        {"proposal"} if name == "beta" else set()
                    ),
                )
                for name in ("alpha", "beta", "gamma")
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=3,
                    min_lineages=2,
                    max_calls=7,
                    deadline_seconds=30,
                ),
                synthesis_provider="alpha",
            )

            result = engine.run("Reserve every mandatory downstream call.")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["membership"]["valid_juries"], 3)
            self.assertEqual(store.count_calls(result["run_id"]), 7)
            self.assertEqual(providers["beta"].calls.count("proposal"), 1)
            self.assertEqual(providers["alpha"].calls.count("synthesis"), 1)
            recovery = next(
                item
                for item in result["recoveries"]
                if item.get("stage") == "proposal"
                and item.get("provider") == "beta"
            )
            self.assertEqual(recovery["status"], "not_attempted")
            self.assertEqual(
                recovery["reason"],
                "call budget reserved for jury calls and synthesis",
            )

    def test_ordered_synthesis_reserves_fallback_before_length_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._ordered_engine(
                Path(temporary),
                max_calls=9,
            )
            providers["alpha"].truncate_once_stages.add("synthesis")

            result = engine.run("Keep the next synthesis slot available.")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                result["synthesis"]["selected_provider"],
                "beta",
            )
            self.assertEqual(store.count_calls(result["run_id"]), 8)
            self.assertEqual(
                [providers[name].calls.count("synthesis") for name in providers],
                [1, 1, 0],
            )
            recovery = next(
                item
                for item in result["recoveries"]
                if item.get("stage") == "synthesis"
                and item.get("provider") == "alpha"
            )
            self.assertEqual(recovery["status"], "not_attempted")
            self.assertEqual(
                recovery["reason"],
                "call budget reserved for synthesis",
            )
            self.assertEqual(
                [
                    event
                    for event in store.list_events(result["run_id"])
                    if event["event_type"] == "provider_retry_started"
                    and event["payload"]["stage"] == "synthesis"
                ],
                [],
            )

    def test_default_budget_limits_proposal_recovery_to_reported_capacity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            names = (
                "alpha",
                "beta",
                "gamma",
                "delta",
                "epsilon",
                "zeta",
                "eta",
            )
            providers = {
                name: FakeProvider(
                    name,
                    fail_stages=(
                        {"synthesis"}
                        if name in {"alpha", "beta"}
                        else set()
                    ),
                    truncate_once_stages={"proposal"},
                )
                for name in names
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=3,
                    jury_quorum=3,
                    min_lineages=3,
                    max_calls=20,
                    deadline_seconds=30,
                ),
                synthesis_provider="alpha",
                synthesis_fallbacks=("beta", "gamma"),
            )

            result = engine.run(
                "Allow only the three reported proposal recovery calls."
            )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(
                result["synthesis"]["selected_provider"],
                "gamma",
            )
            self.assertEqual(store.count_calls(result["run_id"]), 20)
            self.assertEqual(
                result["workload"]["preflight"]["mandatory_calls"],
                17,
            )
            self.assertEqual(
                result["workload"]["preflight"]["recovery_call_capacity"],
                3,
            )
            self.assertEqual(
                [providers[name].calls.count("proposal") for name in names],
                [2, 2, 2, 1, 1, 1, 1],
            )
            self.assertTrue(
                all(provider.calls.count("jury") == 1 for provider in providers.values())
            )
            self.assertEqual(
                [providers[name].calls.count("synthesis") for name in names],
                [1, 1, 1, 0, 0, 0, 0],
            )
            proposal_recoveries = [
                recovery
                for recovery in result["recoveries"]
                if recovery.get("stage") == "proposal"
            ]
            self.assertEqual(
                sum(
                    recovery["status"] == "recovered"
                    for recovery in proposal_recoveries
                ),
                3,
            )
            self.assertEqual(
                sum(
                    recovery["status"] == "not_attempted"
                    and recovery["reason"]
                    == "call budget reserved for jury calls and synthesis"
                    for recovery in proposal_recoveries
                ),
                4,
            )

    def test_candidate_namespace_is_stable_across_juries(self) -> None:
        mapping = {
            "CANDIDATE_01": "alpha",
            "CANDIDATE_02": "beta",
        }
        jury = {
            "winner": "CANDIDATE_01",
            "ranking": ["CANDIDATE_01", "CANDIDATE_02"],
            "confidence": "high",
            "abstain": False,
            "rationale": (
                "CANDIDATE_01 is better supported than CANDIDATE_02."
            ),
            "material_disagreements": [
                "CANDIDATE_01 revises; CANDIDATE_02 repositions."
            ],
            "verification_needed": [
                "Verify the evidence cited by CANDIDATE_01."
            ],
        }

        canonical = CouncilEngine._canonicalize_jury(jury, mapping)

        self.assertEqual(canonical["winner"], "alpha")
        self.assertEqual(canonical["ranking"], ["alpha", "beta"])
        self.assertEqual(
            canonical["rationale"],
            "CANDIDATE_01 is better supported than CANDIDATE_02.",
        )
        self.assertEqual(
            canonical["material_disagreements"],
            ["CANDIDATE_01 revises; CANDIDATE_02 repositions."],
        )
        self.assertEqual(
            canonical["verification_needed"],
            ["Verify the evidence cited by CANDIDATE_01."],
        )

        anonymous = CouncilEngine._anonymize_aggregate(
            {
                "winner": "alpha",
                "ranking": ["alpha", "beta"],
                "tied_candidates": [],
                "borda_points": {"alpha": 2, "beta": 1},
                "win_counts": {"alpha": 1, "beta": 0},
                "candidate_label_mapping": mapping,
                "material_disagreements": canonical[
                    "material_disagreements"
                ],
                "verification_needed": canonical[
                    "verification_needed"
                ],
            },
            mapping,
        )
        self.assertEqual(
            anonymous["material_disagreements"],
            ["CANDIDATE_01 revises; CANDIDATE_02 repositions."],
        )
        self.assertEqual(
            anonymous["verification_needed"],
            ["Verify the evidence cited by CANDIDATE_01."],
        )
        self.assertNotIn("candidate_label_mapping", anonymous)

    def test_mapping_and_presentation_orders_are_deterministic(self) -> None:
        mapping = CouncilEngine._candidate_mapping(
            "run-1", ["alpha", "beta", "gamma"]
        )
        reversed_input = CouncilEngine._candidate_mapping(
            "run-1", ["gamma", "beta", "alpha"]
        )

        self.assertEqual(mapping, reversed_input)
        self.assertEqual(
            set(mapping),
            {"CANDIDATE_01", "CANDIDATE_02", "CANDIDATE_03"},
        )
        self.assertEqual(set(mapping.values()), {"alpha", "beta", "gamma"})
        orders = [
            CouncilEngine._jury_presentation_order(
                "run-1", juror, list(mapping)
            )
            for juror in ("alpha", "beta", "gamma")
        ]
        self.assertTrue(
            all(set(order) == set(mapping) for order in orders)
        )
        self.assertGreater(len({tuple(order) for order in orders}), 1)
        self.assertEqual(
            orders[0],
            CouncilEngine._jury_presentation_order(
                "run-1", "alpha", list(reversed(mapping))
            ),
        )

    def test_candidate_membership_is_frozen_across_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(
                Path(temporary), fail_provider="gamma"
            )
            providers["alpha"].fail_stages.add("synthesis")

            first = engine.run("Freeze candidate membership across resume")

            self.assertEqual(first["status"], "partial")
            self.assertEqual(
                {
                    proposal["provider"]
                    for proposal in first["proposals"]
                },
                {"alpha", "beta"},
            )
            first_mapping = first["candidate_namespace"][
                "candidate_label_mapping"
            ]
            alpha_jury_calls = providers["alpha"].calls.count("jury")
            beta_jury_calls = providers["beta"].calls.count("jury")
            gamma_proposal_calls = providers["gamma"].calls.count(
                "proposal"
            )
            namespace_events = [
                event
                for event in store.list_events(first["run_id"])
                if event["event_type"] == "candidate_namespace_locked"
            ]
            self.assertEqual(len(namespace_events), 1)

            providers["gamma"].fail_stages.clear()
            providers["alpha"].fail_stages.remove("synthesis")
            resumed = engine.resume(first["run_id"])

            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(
                resumed["completion_quality"],
                "degraded",
            )
            self.assertEqual(
                resumed["candidate_namespace"][
                    "candidate_label_mapping"
                ],
                first_mapping,
            )
            self.assertEqual(
                {
                    proposal["provider"]
                    for proposal in resumed["proposals"]
                },
                {"alpha", "beta"},
            )
            self.assertEqual(
                providers["gamma"].calls.count("proposal"),
                gamma_proposal_calls,
            )
            self.assertEqual(
                providers["alpha"].calls.count("jury"),
                alpha_jury_calls,
            )
            self.assertEqual(
                providers["beta"].calls.count("jury"),
                beta_jury_calls,
            )
            self.assertEqual(providers["gamma"].calls.count("jury"), 1)
            self.assertEqual(
                providers["alpha"].calls.count("synthesis"),
                2,
            )
            self.assertEqual(
                {
                    (failure["stage"], failure["provider"])
                    for failure in resumed["failures"]
                },
                {
                    ("proposal", "gamma"),
                    ("jury", "gamma"),
                },
            )
            application_retries = [
                recovery
                for recovery in resumed["recoveries"]
                if recovery.get("kind") == "application_retry"
            ]
            self.assertEqual(len(application_retries), 1)
            self.assertEqual(
                (
                    application_retries[0]["stage"],
                    application_retries[0]["provider"],
                    application_retries[0]["status"],
                ),
                ("synthesis", "alpha", "recovered"),
            )
            self.assertEqual(
                application_retries[0]["prior_failure"]["category"],
                ErrorCategory.PROVIDER_SERVER.value,
            )
            self.assertEqual(
                len(
                    [
                        event
                        for event in store.list_events(first["run_id"])
                        if event["event_type"]
                        == "candidate_namespace_locked"
                    ]
                ),
                1,
            )

    def test_resumed_jury_retries_remain_auditable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            providers["beta"].fail_stages.add("jury")
            providers["gamma"].fail_stages.add("jury")

            first = engine.run("Audit jury retries across resume")

            self.assertEqual(first["status"], "partial")
            self.assertEqual(first["completion_quality"], "degraded")
            providers["beta"].fail_stages.clear()
            providers["gamma"].fail_stages.clear()

            resumed = engine.resume(first["run_id"])

            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(
                resumed["completion_quality"],
                "degraded",
            )
            self.assertEqual(resumed["failures"], [])
            application_retries = [
                recovery
                for recovery in resumed["recoveries"]
                if recovery.get("kind") == "application_retry"
            ]
            self.assertEqual(
                {
                    (
                        recovery["stage"],
                        recovery["provider"],
                        recovery["status"],
                    )
                    for recovery in application_retries
                },
                {
                    ("jury", "beta", "recovered"),
                    ("jury", "gamma", "recovered"),
                },
            )
            retry_events = [
                event
                for event in store.list_events(first["run_id"])
                if event["event_type"] == "provider_retry_started"
            ]
            self.assertEqual(len(retry_events), 2)
            self.assertEqual(
                {
                    (
                        event["payload"]["stage"],
                        event["payload"]["provider"],
                    )
                    for event in retry_events
                },
                {("jury", "beta"), ("jury", "gamma")},
            )

    def test_multi_retry_audit_preserves_each_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            run_id = engine.create_run("Audit a multi-retry chain")
            provider = providers["alpha"]
            invocation_id = store.start_invocation(
                run_id,
                "synthesis",
                "alpha",
                provider.config.model,
                provider.config.lineage,
                "stable synthesis prompt",
            )
            first_failure = ProviderError(
                "first synthetic outage",
                category=ErrorCategory.PROVIDER_SERVER,
                retryable=True,
                status_code=503,
            )
            second_failure = ProviderError(
                "second synthetic outage",
                category=ErrorCategory.PROVIDER_SERVER,
                retryable=True,
                status_code=503,
            )
            store.finish_invocation_failure(
                invocation_id, first_failure
            )
            self.assertEqual(
                store.start_invocation(
                    run_id,
                    "synthesis",
                    "alpha",
                    provider.config.model,
                    provider.config.lineage,
                    "stable synthesis prompt",
                ),
                invocation_id,
            )
            store.finish_invocation_failure(
                invocation_id, second_failure
            )
            self.assertEqual(
                store.start_invocation(
                    run_id,
                    "synthesis",
                    "alpha",
                    provider.config.model,
                    provider.config.lineage,
                    "stable synthesis prompt",
                ),
                invocation_id,
            )
            store.finish_invocation_success(
                invocation_id,
                ProviderResponse(
                    content="Recovered synthesis",
                    resolved_model=provider.config.model,
                    request_id="request-recovered",
                    usage=Usage(total_tokens=1),
                    latency_ms=1,
                    attempts=1,
                    finish_reason="stop",
                ),
            )
            recoveries = engine._provider_retry_recoveries(
                run_id,
                store.list_invocations(run_id),
            )

            self.assertEqual(
                [
                    (
                        recovery["retry_call_count"],
                        recovery["status"],
                    )
                    for recovery in recoveries
                ],
                [(2, "failed"), (3, "recovered")],
            )
            self.assertEqual(
                recoveries[0]["final_failure"]["message"],
                str(second_failure),
            )
            retry_events = [
                event
                for event in store.list_events(run_id)
                if event["event_type"] == "provider_retry_started"
            ]
            self.assertEqual(len(retry_events), 2)
            self.assertEqual(store.count_calls(run_id), 3)

    def test_parallel_provider_calls_never_exceed_policy_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tracker = ActiveCallTracker()
            providers = {
                name: TrackedProvider(name, tracker)
                for name in ("alpha", "beta", "gamma", "delta", "epsilon")
            }
            engine = CouncilEngine(
                store=CouncilStore(Path(temporary)),
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=3,
                    jury_quorum=3,
                    min_lineages=3,
                    max_calls=11,
                    max_parallel_calls=2,
                    deadline_seconds=30,
                ),
                synthesis_provider="alpha",
            )

            result = engine.run("Exercise the bounded worker pool.")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(tracker.maximum, 2)
            self.assertEqual(tracker.active, 0)

    def test_queued_provider_calls_recheck_deadline_before_dispatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clock = ManualClock()
            providers: dict[str, FakeProvider] = {
                "alpha": DeadlineAdvancingProvider(
                    "alpha",
                    clock,
                    10.0,
                ),
                "beta": FakeProvider("beta"),
                "gamma": FakeProvider("gamma"),
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=2,
                    min_lineages=2,
                    max_calls=7,
                    max_parallel_calls=1,
                    deadline_seconds=30,
                ),
                synthesis_provider="alpha",
            )
            question = "Do not dispatch queued calls after the deadline."
            run_id = engine.create_run(question)
            prompts = {
                name: proposal_prompts(question) for name in providers
            }

            with patch("model_council.engine.time", clock):
                successes, failures, recoveries = (
                    engine._run_parallel_stage(
                        run_id=run_id,
                        stage="proposal",
                        prompts=prompts,
                        deadline=5.0,
                    )
                )

                self.assertEqual(set(successes), {"alpha"})
                self.assertEqual(recoveries, [])
                self.assertEqual(
                    {failure["provider"] for failure in failures},
                    {"beta", "gamma"},
                )
                self.assertTrue(
                    all(
                        failure["category"]
                        == ErrorCategory.TIMEOUT.value
                        and not failure["ambiguous"]
                        for failure in failures
                    )
                )
                self.assertEqual(providers["beta"].calls, [])
                self.assertEqual(providers["gamma"].calls, [])
                self.assertEqual(
                    {
                        invocation["provider"]
                        for invocation in store.list_invocations(run_id)
                    },
                    {"alpha"},
                )

                resumed_successes, resumed_failures, _ = (
                    engine._run_parallel_stage(
                        run_id=run_id,
                        stage="proposal",
                        prompts=prompts,
                        deadline=15.0,
                    )
                )

            self.assertEqual(set(resumed_successes), set(providers))
            self.assertEqual(resumed_failures, [])
            self.assertEqual(providers["beta"].calls, ["proposal"])
            self.assertEqual(providers["gamma"].calls, ["proposal"])
            self.assertEqual(store.count_calls(run_id), 3)

    def test_partial_jury_run_exposes_and_validates_namespace_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            for provider in providers.values():
                provider.fail_stages.add("jury")

            result = engine.run("Preserve a namespace without valid juries")

            self.assertEqual(result["status"], "partial")
            self.assertIsNone(result["aggregate"])
            self.assertEqual(result["juries"], [])
            mapping = result["candidate_namespace"][
                "candidate_label_mapping"
            ]
            self.assertEqual(set(mapping.values()), set(providers))
            namespace_events = [
                event
                for event in store.list_events(result["run_id"])
                if event["event_type"] == "candidate_namespace_locked"
            ]
            self.assertEqual(len(namespace_events), 1)

            store.append_event(
                result["run_id"],
                "candidate_namespace_locked",
                namespace_events[0]["payload"],
            )
            with self.assertRaisesRegex(
                ValueError, "duplicate candidate namespace"
            ):
                engine.resume(result["run_id"])

    def test_duplicate_adjudication_lock_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, _providers, store = self._engine(Path(temporary))
            result = engine.run("Lock one adjudication record")
            adjudication_events = [
                event
                for event in store.list_events(result["run_id"])
                if event["event_type"] == "adjudication_locked"
            ]
            self.assertEqual(len(adjudication_events), 1)

            store.append_event(
                result["run_id"],
                "adjudication_locked",
                adjudication_events[0]["payload"],
            )
            with self.assertRaisesRegex(
                ValueError, "duplicate adjudication"
            ):
                engine.resume(result["run_id"])

    def test_one_provider_outage_preserves_explicit_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, _providers, _store = self._engine(
                Path(temporary), fail_provider="gamma"
            )

            result = engine.run("Can two healthy lineages form quorum?")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["completion_quality"], "degraded")
            self.assertEqual(len(result["proposals"]), 2)
            self.assertGreaterEqual(len(result["failures"]), 2)
            self.assertTrue(
                all(
                    failure["provider"] == "gamma"
                    for failure in result["failures"]
                )
            )

    def test_default_four_provider_topology_tolerates_one_outage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            providers = {
                name: FakeProvider(
                    name,
                    fail_stages=(
                        {"proposal", "jury"} if name == "delta" else set()
                    ),
                )
                for name in ("alpha", "beta", "gamma", "delta")
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(),
                synthesis_provider="alpha",
            )

            result = engine.run("Can three healthy lineages form quorum?")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(result["proposals"]), 3)
            self.assertEqual(
                len([jury for jury in result["juries"] if jury["valid"]]),
                3,
            )
            self.assertEqual(store.count_calls(result["run_id"]), 9)
            self.assertEqual(result["completion_quality"], "degraded")
            self.assertEqual(len(result["failures"]), 2)
            self.assertTrue(
                all(
                    failure["provider"] == "delta"
                    for failure in result["failures"]
                )
            )

    def test_seven_provider_topology_completes_in_fifteen_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            names = (
                "openai",
                "anthropic",
                "gemini",
                "mistral",
                "xai",
                "qwen",
                "cohere",
            )
            providers = {name: FakeProvider(name) for name in names}
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(max_calls=20),
                synthesis_provider="openai",
            )

            result = engine.run("Exercise the full seven-provider topology.")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["completion_quality"], "clean")
            self.assertEqual(len(result["proposals"]), 7)
            self.assertEqual(
                len([jury for jury in result["juries"] if jury["valid"]]),
                7,
            )
            self.assertEqual(store.count_calls(result["run_id"]), 15)
            self.assertEqual(result["workload"]["application_calls"], 15)
            self.assertEqual(
                result["workload"]["preflight"]["provider_count"],
                7,
            )
            self.assertEqual(result["failures"], [])
            self.assertEqual(result["recoveries"], [])

    def test_seven_provider_topology_preserves_quorum_at_four_outages(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unavailable = {"mistral", "xai", "qwen", "cohere"}
            providers = {
                name: FakeProvider(
                    name,
                    fail_stages=(
                        {"proposal", "jury"}
                        if name in unavailable
                        else set()
                    ),
                )
                for name in (
                    "openai",
                    "anthropic",
                    "gemini",
                    "mistral",
                    "xai",
                    "qwen",
                    "cohere",
                )
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(max_calls=20),
                synthesis_provider="openai",
            )

            result = engine.run("Exercise the seven-provider quorum boundary.")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["completion_quality"], "degraded")
            self.assertEqual(len(result["proposals"]), 3)
            self.assertEqual(
                len([jury for jury in result["juries"] if jury["valid"]]),
                3,
            )
            self.assertEqual(store.count_calls(result["run_id"]), 15)
            self.assertEqual(len(result["failures"]), 8)
            self.assertEqual(
                {failure["provider"] for failure in result["failures"]},
                unavailable,
            )

    def test_incomplete_synthesis_never_marks_run_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            providers["alpha"].finish_reasons["synthesis"] = "length"

            result = engine.run("Reject a truncated final synthesis")

            self.assertEqual(result["status"], "partial")
            self.assertIsNone(result["answer"])
            synthesis_failures = [
                failure
                for failure in result["failures"]
                if failure["stage"] == "synthesis"
            ]
            self.assertEqual(len(synthesis_failures), 1)
            self.assertEqual(
                synthesis_failures[0]["category"],
                ErrorCategory.INVALID_RESPONSE.value,
            )
            invocation = next(
                record
                for record in store.list_invocations(result["run_id"])
                if record["stage"] == "synthesis"
            )
            self.assertEqual(invocation["status"], "failed")
            self.assertEqual(invocation["call_count"], 2)
            preserved = [
                event
                for event in store.list_events(result["run_id"])
                if event["event_type"] == "truncated_response_preserved"
            ]
            self.assertEqual(len(preserved), 1)
            self.assertEqual(
                preserved[0]["payload"]["response"]["finish_reason"],
                "length",
            )
            calls_before_resume = providers["alpha"].calls.count("synthesis")

            resumed = engine.resume(result["run_id"])

            self.assertEqual(resumed["status"], "partial")
            self.assertEqual(
                [
                    recovery["status"]
                    for recovery in resumed["recoveries"]
                    if recovery["stage"] == "synthesis"
                ],
                ["failed"],
            )
            self.assertEqual(
                providers["alpha"].calls.count("synthesis"),
                calls_before_resume,
            )
            invocation = next(
                record
                for record in store.list_invocations(result["run_id"])
                if record["stage"] == "synthesis"
            )
            self.assertEqual(invocation["call_count"], 2)
            truncation_retry_events = [
                event
                for event in store.list_events(result["run_id"])
                if (
                    event["event_type"] == "provider_retry_started"
                    and event["payload"]["retry_kind"]
                    == "truncation"
                )
            ]
            self.assertEqual(len(truncation_retry_events), 1)

    def test_length_completion_recovers_once_with_larger_output_budget(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            providers["alpha"].truncate_once_stages.add("synthesis")

            result = engine.run("Recover a safely truncated final synthesis")

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["completion_quality"], "degraded")
            self.assertEqual(
                [
                    recovery["status"]
                    for recovery in result["recoveries"]
                    if recovery["stage"] == "synthesis"
                ],
                ["recovered"],
            )
            synthesis_limits = [
                int(call["max_output_tokens"])
                for call in providers["alpha"].call_limits
                if call["stage"] == "synthesis"
            ]
            self.assertEqual(synthesis_limits, [1800, 3600])
            invocation = next(
                record
                for record in store.list_invocations(result["run_id"])
                if record["stage"] == "synthesis"
            )
            self.assertEqual(invocation["status"], "succeeded")
            self.assertEqual(invocation["call_count"], 2)
            self.assertEqual(store.count_calls(result["run_id"]), 8)
            self.assertEqual(
                len(
                    [
                        event
                        for event in store.list_events(result["run_id"])
                        if event["event_type"] == "truncation_recovery"
                    ]
                ),
                1,
            )

    def test_deadline_before_truncation_retry_does_not_consume_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            clock = ManualClock()
            alpha = DeadlineAdvancingProvider("alpha", clock, 5.0)
            alpha.truncate_once_stages.add("proposal")
            providers: dict[str, FakeProvider] = {
                "alpha": alpha,
                "beta": FakeProvider("beta"),
                "gamma": FakeProvider("gamma"),
            }
            store = CouncilStore(Path(temporary))
            engine = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=2,
                    min_lineages=2,
                    max_calls=7,
                    deadline_seconds=30,
                ),
                synthesis_provider="alpha",
            )
            question = "Retry a truncated response only after dispatch opens."
            run_id = engine.create_run(question)
            prompts = {"alpha": proposal_prompts(question)}

            with patch(
                "model_council.engine.time.monotonic",
                side_effect=clock.monotonic,
            ):
                first_successes, first_failures, first_recoveries = (
                    engine._run_parallel_stage(
                        run_id=run_id,
                        stage="proposal",
                        prompts=prompts,
                        deadline=5.0,
                    )
                )

                self.assertEqual(first_successes, {})
                self.assertEqual(len(first_failures), 1)
                self.assertTrue(
                    first_failures[0]["message"].endswith(
                        "(finish_reason=length)"
                    )
                )
                self.assertEqual(
                    [recovery["status"] for recovery in first_recoveries],
                    ["not_dispatched"],
                )
                self.assertEqual(alpha.calls.count("proposal"), 1)
                self.assertEqual(store.count_calls(run_id), 1)
                self.assertFalse(
                    any(
                        event["event_type"] == "provider_retry_started"
                        for event in store.list_events(run_id)
                    )
                )

                resumed_successes, resumed_failures, _ = (
                    engine._run_parallel_stage(
                        run_id=run_id,
                        stage="proposal",
                        prompts=prompts,
                        deadline=15.0,
                    )
                )

            self.assertEqual(set(resumed_successes), {"alpha"})
            self.assertEqual(resumed_failures, [])
            self.assertEqual(alpha.calls.count("proposal"), 2)
            self.assertEqual(store.count_calls(run_id), 2)
            truncation_events = [
                event["payload"]
                for event in store.list_events(run_id)
                if event["event_type"] == "truncation_recovery"
            ]
            self.assertEqual(
                [event["status"] for event in truncation_events],
                ["not_dispatched"],
            )
            retry_recoveries = engine._provider_retry_recoveries(
                run_id,
                store.list_invocations(run_id),
            )
            self.assertEqual(
                [
                    recovery["status"]
                    for recovery in retry_recoveries
                    if recovery["kind"] == "truncation_retry"
                ],
                ["recovered"],
            )

    def test_application_retry_does_not_consume_truncation_recovery(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            providers["alpha"].fail_stages.add("synthesis")

            first = engine.run(
                "Recover an application failure and then a truncation"
            )

            self.assertEqual(first["status"], "partial")
            providers["alpha"].fail_stages.remove("synthesis")
            providers["alpha"].calls.clear()
            providers["alpha"].truncate_once_stages.add("synthesis")

            resumed = engine.resume(first["run_id"])

            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(
                resumed["completion_quality"],
                "degraded",
            )
            synthesis_invocation = next(
                invocation
                for invocation in store.list_invocations(first["run_id"])
                if invocation["stage"] == "synthesis"
            )
            self.assertEqual(synthesis_invocation["call_count"], 3)
            retry_events = [
                event["payload"]
                for event in store.list_events(first["run_id"])
                if event["event_type"] == "provider_retry_started"
                and event["payload"]["stage"] == "synthesis"
            ]
            self.assertEqual(
                [
                    (
                        event["retry_call_count"],
                        event["retry_kind"],
                    )
                    for event in retry_events
                ],
                [(2, "application"), (3, "truncation")],
            )
            self.assertEqual(
                [
                    (
                        recovery.get("kind", "truncation_recovery"),
                        recovery["status"],
                    )
                    for recovery in resumed["recoveries"]
                    if recovery["stage"] == "synthesis"
                ],
                [
                    ("truncation_recovery", "recovered"),
                    ("application_retry", "failed"),
                ],
            )

    def test_exhausted_truncation_recovery_is_not_reclassified(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            provider = providers["alpha"]
            base_generate = provider.generate
            synthesis_outcomes = ["length", "error"]

            def sequenced_generate(
                *,
                system_prompt: str,
                user_prompt: str,
                stage: str,
                max_output_tokens: int | None = None,
                timeout_seconds: float | None = None,
            ) -> ProviderResponse:
                if stage != "synthesis":
                    return base_generate(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        stage=stage,
                        max_output_tokens=max_output_tokens,
                        timeout_seconds=timeout_seconds,
                    )
                outcome = synthesis_outcomes.pop(0)
                if outcome == "error":
                    provider.fail_stages.add(stage)
                else:
                    provider.finish_reasons[stage] = outcome
                try:
                    return base_generate(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        stage=stage,
                        max_output_tokens=max_output_tokens,
                        timeout_seconds=timeout_seconds,
                    )
                finally:
                    provider.fail_stages.discard(stage)
                    provider.finish_reasons.pop(stage, None)

            provider.generate = sequenced_generate  # type: ignore[method-assign]

            first = engine.run(
                "Do not invent a second truncation recovery"
            )

            self.assertEqual(first["status"], "partial")
            synthesis_outcomes.append("length")
            resumed = engine.resume(first["run_id"])

            self.assertEqual(resumed["status"], "partial")
            synthesis_invocation = next(
                invocation
                for invocation in store.list_invocations(first["run_id"])
                if invocation["stage"] == "synthesis"
            )
            self.assertEqual(synthesis_invocation["call_count"], 3)
            retry_events = [
                event["payload"]
                for event in store.list_events(first["run_id"])
                if event["event_type"] == "provider_retry_started"
                and event["payload"]["stage"] == "synthesis"
            ]
            self.assertEqual(
                [
                    (
                        event["retry_call_count"],
                        event["retry_kind"],
                    )
                    for event in retry_events
                ],
                [(2, "truncation"), (3, "application")],
            )
            truncation_recoveries = [
                event
                for event in store.list_events(first["run_id"])
                if event["event_type"] == "truncation_recovery"
                and event["payload"]["stage"] == "synthesis"
            ]
            self.assertEqual(len(truncation_recoveries), 1)
            self.assertEqual(
                truncation_recoveries[0]["payload"]["status"],
                "failed",
            )
            self.assertEqual(
                len(
                    [
                        recovery
                        for recovery in resumed["recoveries"]
                        if (
                            recovery["stage"] == "synthesis"
                            and recovery.get("kind")
                            != "application_retry"
                        )
                    ]
                ),
                1,
            )

    def test_preflight_rejects_oversized_question_before_creating_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            bounded = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=2,
                    jury_quorum=2,
                    min_lineages=2,
                    max_calls=10,
                    deadline_seconds=30,
                    max_question_chars=10,
                ),
                synthesis_provider="alpha",
            )

            with self.assertRaisesRegex(ValueError, "Question is too large"):
                bounded.run("This question is longer than ten characters.")

            self.assertEqual(store.list_runs(), [])

    def test_preflight_rejects_unpaired_surrogate_before_creating_run(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))

            with self.assertRaisesRegex(
                ValueError,
                "cannot be encoded as UTF-8",
            ):
                engine.run("invalid \ud800 question")

            self.assertEqual(store.list_runs(), [])
            self.assertTrue(
                all(not provider.calls for provider in providers.values())
            )

    def test_idempotency_key_returns_the_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, _providers, _store = self._engine(Path(temporary))

            first = engine.run("Same request", idempotency_key="same-key")
            second = engine.run("Same request", idempotency_key="same-key")

            self.assertEqual(first["run_id"], second["run_id"])

    def test_lock_rejects_changed_provider_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            result = engine.run("Lock this run")
            changed = FakeProvider("alpha")
            changed.config = ProviderConfig(
                **{
                    **changed.config.to_dict(),
                    "model": "different-model",
                }
            )
            changed_engine = CouncilEngine(
                store=store,
                providers={**providers, "alpha": changed},
                policy=engine.policy,
                synthesis_provider="alpha",
            )

            with self.assertRaisesRegex(ValueError, "run lock"):
                changed_engine.resume(result["run_id"])

    def test_lock_rejects_changed_provider_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            result = engine.run("Lock provider repair priority")
            reordered = {
                name: providers[name]
                for name in ("gamma", "beta", "alpha")
            }
            changed_engine = CouncilEngine(
                store=store,
                providers=reordered,
                policy=engine.policy,
                synthesis_provider="alpha",
            )

            with self.assertRaisesRegex(ValueError, "order"):
                changed_engine.resume(result["run_id"])

    def test_lock_rejects_changed_policy_or_synthesis_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            result = engine.run("Lock the run policy")
            changed_policy = CouncilEngine(
                store=store,
                providers=providers,
                policy=RunPolicy(
                    proposal_quorum=3,
                    jury_quorum=2,
                    min_lineages=2,
                    max_calls=10,
                    deadline_seconds=30,
                ),
                synthesis_provider="alpha",
            )
            with self.assertRaisesRegex(ValueError, "policy"):
                changed_policy.resume(result["run_id"])

            changed_synthesizer = CouncilEngine(
                store=store,
                providers=providers,
                policy=engine.policy,
                synthesis_provider="beta",
            )
            with self.assertRaisesRegex(ValueError, "synthesis provider"):
                changed_synthesizer.resume(result["run_id"])

            changed_fallbacks = CouncilEngine(
                store=store,
                providers=providers,
                policy=engine.policy,
                synthesis_provider="alpha",
                synthesis_fallbacks=("beta",),
            )
            with self.assertRaisesRegex(ValueError, "provider chain"):
                changed_fallbacks.resume(result["run_id"])

    def test_lock_rejects_changed_synthesis_fallback_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._ordered_engine(
                Path(temporary)
            )
            result = engine.run("Lock the fallback order.")
            reordered = CouncilEngine(
                store=store,
                providers=providers,
                policy=engine.policy,
                synthesis_provider="alpha",
                synthesis_fallbacks=("gamma", "beta"),
            )

            with self.assertRaisesRegex(ValueError, "provider chain"):
                reordered.resume(result["run_id"])

    def test_policy_lock_defaults_missing_parallel_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            locked = engine.policy.to_dict()
            locked.pop("max_parallel_calls")
            locked["synthesis_provider"] = "alpha"

            engine._validate_policy_lock(locked)

            ordered = CouncilEngine(
                store=store,
                providers=providers,
                policy=engine.policy,
                synthesis_provider="alpha",
                synthesis_fallbacks=("beta",),
            )
            with self.assertRaisesRegex(ValueError, "provider chain"):
                ordered._validate_policy_lock(locked)

            malformed = dict(locked)
            malformed["synthesis_fallbacks"] = "beta"
            with self.assertRaisesRegex(ValueError, "malformed"):
                engine._validate_policy_lock(malformed)

            locked["unexpected_policy_field"] = True
            with self.assertRaisesRegex(ValueError, "unknown fields"):
                engine._validate_policy_lock(locked)

    def test_completed_resume_rejects_duplicate_fallback_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, _providers, store = self._ordered_engine(
                Path(temporary),
                synthesis_failures={"alpha"},
            )
            result = engine.run("Validate fallback audit before returning.")
            fallback_event = next(
                event
                for event in store.list_events(result["run_id"])
                if event["event_type"] == "synthesis_fallback_advanced"
            )
            store.append_event(
                result["run_id"],
                "synthesis_fallback_advanced",
                fallback_event["payload"],
            )

            with self.assertRaisesRegex(ValueError, "fallback audit"):
                engine.resume(result["run_id"])

    def test_completed_resume_rejects_fallback_from_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, _providers, store = self._ordered_engine(
                Path(temporary)
            )
            result = engine.run("Reject a forged fallback transition.")
            store.append_event(
                result["run_id"],
                "synthesis_fallback_advanced",
                {
                    "version": 1,
                    "from_position": 1,
                    "from_provider": "alpha",
                    "to_position": 2,
                    "to_provider": "beta",
                    "reason_category": ErrorCategory.PROVIDER_SERVER.value,
                    "ambiguous": False,
                    "failure": ProviderError(
                        "forged fallback failure",
                        category=ErrorCategory.PROVIDER_SERVER,
                        retryable=False,
                        ambiguous=False,
                    ).to_dict(),
                },
            )

            with self.assertRaisesRegex(ValueError, "invocation"):
                engine.resume(result["run_id"])

    def test_crash_left_running_call_is_marked_ambiguous_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            question = "Do not duplicate an ambiguous billable call"
            run_id = store.create_run(
                question=question,
                protocol_id=PROTOCOL_ID,
                protocol_version=PROTOCOL_VERSION,
                protocol_hash=protocol_hash(),
                provider_configs=[
                    provider.config.to_dict()
                    for provider in providers.values()
                ],
                policy=engine.policy.to_dict(),
            )
            system_prompt, user_prompt = proposal_prompts(question)
            store.start_invocation(
                run_id=run_id,
                stage="proposal",
                provider="alpha",
                model=providers["alpha"].config.model,
                lineage=providers["alpha"].config.lineage,
                prompt=f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}",
            )

            result = engine.resume(run_id)

            self.assertNotIn("proposal", providers["alpha"].calls)
            ambiguous = [
                failure
                for failure in result["failures"]
                if failure["provider"] == "alpha"
                and failure["stage"] == "proposal"
            ]
            self.assertEqual(len(ambiguous), 1)
            self.assertTrue(ambiguous[0]["ambiguous"])
            record = next(
                record
                for record in store.list_invocations(run_id)
                if record["provider"] == "alpha"
                and record["stage"] == "proposal"
            )
            self.assertEqual(record["status"], "failed")
            self.assertTrue(record["error_ambiguous"])

    def test_recovery_slot_precedence_reuses_converts_then_suppresses(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            engine, providers, store = self._engine(Path(temporary))
            question = "Exercise recovery precedence."
            run_id = engine.create_run(question)
            system_prompt, user_prompt = proposal_prompts(question)
            combined = (
                f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}"
            )

            alpha_id = store.start_invocation(
                run_id=run_id,
                stage="proposal",
                provider="alpha",
                model=providers["alpha"].config.model,
                lineage=providers["alpha"].config.lineage,
                prompt=combined,
            )
            alpha_response = providers["alpha"].generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                stage="proposal",
            )
            store.finish_invocation_success(alpha_id, alpha_response)
            providers["alpha"].calls.clear()
            alpha_later = store.start_invocation(
                run_id=run_id,
                stage="jury",
                provider="alpha",
                model=providers["alpha"].config.model,
                lineage=providers["alpha"].config.lineage,
                prompt="later ambiguous alpha jury",
            )
            store.finish_invocation_failure(
                alpha_later,
                ProviderError("ambiguous alpha jury", ambiguous=True),
            )

            beta_id = store.start_invocation(
                run_id=run_id,
                stage="proposal",
                provider="beta",
                model=providers["beta"].config.model,
                lineage=providers["beta"].config.lineage,
                prompt=combined,
            )
            beta_later = store.start_invocation(
                run_id=run_id,
                stage="jury",
                provider="beta",
                model=providers["beta"].config.model,
                lineage=providers["beta"].config.lineage,
                prompt="later ambiguous beta jury",
            )
            store.finish_invocation_failure(
                beta_later,
                ProviderError("ambiguous beta jury", ambiguous=True),
            )

            gamma_later = store.start_invocation(
                run_id=run_id,
                stage="jury",
                provider="gamma",
                model=providers["gamma"].config.model,
                lineage=providers["gamma"].config.lineage,
                prompt="later ambiguous gamma jury",
            )
            store.finish_invocation_failure(
                gamma_later,
                ProviderError("ambiguous gamma jury", ambiguous=True),
            )

            successes, failures, recoveries = engine._run_parallel_stage(
                run_id=run_id,
                stage="proposal",
                prompts={
                    name: (system_prompt, user_prompt)
                    for name in providers
                },
                deadline=time.monotonic() + 5,
            )

            self.assertEqual(set(successes), {"alpha"})
            self.assertEqual(recoveries, [])
            self.assertEqual(providers["alpha"].calls, [])
            self.assertEqual(providers["beta"].calls, [])
            self.assertEqual(providers["gamma"].calls, [])
            by_provider = {
                failure["provider"]: failure for failure in failures
            }
            self.assertIn("automatic retry refused", by_provider["beta"]["message"])
            self.assertIn(
                "later-stage invocation suppressed",
                by_provider["gamma"]["message"],
            )
            beta = store.get_invocation(beta_id)
            self.assertEqual(beta["status"], "failed")
            self.assertTrue(beta["error_ambiguous"])


if __name__ == "__main__":
    unittest.main()
