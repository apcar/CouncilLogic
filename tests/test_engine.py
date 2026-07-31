from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tempfile
import time
import unittest


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
            match = re.search(
                r"BEGIN_UNTRUSTED_EVALUATION_JSON\n(.*?)\n"
                r"END_UNTRUSTED_EVALUATION_JSON",
                user_prompt,
                re.DOTALL,
            )
            if not match:
                raise AssertionError("jury evaluation payload was not present")
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
                        "Candidates use different synthetic wording."
                    ],
                    "verification_needed": ["Run the deterministic fixture."],
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
