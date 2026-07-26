from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.access_policy import (  # noqa: E402
    CouncilAction,
    Tenant,
)
from model_council.service_store import (  # noqa: E402
    CallReservationCode,
    CallReservationRequest,
    RunBindingCode,
    ServicePolicy,
    ServiceStore,
)


NOW = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
TOKEN = "service-store-test-token-" + ("x" * 40)


class ServiceStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "council.sqlite3"
        self.store = ServiceStore(self.db_path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _policy(
        self,
        *,
        max_inflight: int = 20,
        daily: int = 20,
        monthly: int = 100,
    ) -> ServicePolicy:
        policy = ServicePolicy(
            policy_id="test-policy",
            principal_id="personal-laptop-human",
            tenant=Tenant.PERSONAL,
            allowed_actions=frozenset(
                {CouncilAction.PROVIDER_INVOKE.value}
            ),
            allowed_providers=frozenset({"mock-1"}),
            allowed_models=frozenset({"mock-model-1"}),
            max_inflight=max_inflight,
            daily_call_units=daily,
            monthly_call_units=monthly,
            issued_at=NOW - timedelta(days=1),
            expires_at=NOW + timedelta(days=1),
        )
        self.store.put_policy(policy)
        return policy

    def _bind(self, run_id: str = "run-1") -> None:
        binding = self.store.bind_run(
            principal_id="personal-laptop-human",
            idempotency_key=f"key-{run_id}",
            run_id=run_id,
            payload_hash=f"hash-{run_id}",
            at=NOW,
        )
        self.assertEqual(binding.code, RunBindingCode.CREATED)

    def _reservation(
        self,
        index: int,
        *,
        run_id: str = "run-1",
    ) -> CallReservationRequest:
        return CallReservationRequest(
            reservation_id=f"reservation-{index}",
            policy_id="test-policy",
            principal_id="personal-laptop-human",
            tenant=Tenant.PERSONAL,
            run_id=run_id,
            action=CouncilAction.PROVIDER_INVOKE.value,
            provider="mock-1",
            model="mock-model-1",
        )

    def test_tokens_are_one_way_and_work_principal_stays_disabled(self) -> None:
        self.store.put_bearer_token(
            token_id="human-token",
            principal_id="personal-laptop-human",
            bearer_token=TOKEN,
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        )
        authenticated = self.store.authenticate_bearer(TOKEN, at=NOW)
        self.assertEqual(
            authenticated.principal.principal_id,  # type: ignore[union-attr]
            "personal-laptop-human",
        )
        self.assertIsNone(
            self.store.authenticate_bearer(
                "service-store-test-token-" + ("y" * 40),
                at=NOW,
            )
        )
        raw_database = self.db_path.read_bytes()
        self.assertNotIn(TOKEN.encode(), raw_database)

        self.store.put_bearer_token(
            token_id="work-token",
            principal_id="work-laptop-human",
            bearer_token="disabled-work-token-" + ("z" * 40),
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        )
        self.assertIsNone(
            self.store.authenticate_bearer(
                "disabled-work-token-" + ("z" * 40),
                at=NOW,
            )
        )
        with self.assertRaisesRegex(ValueError, "32-512"):
            self.store.put_bearer_token(
                token_id="oversized-token",
                principal_id="personal-laptop-human",
                bearer_token="x" * 513,
                issued_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(hours=1),
            )

    def test_owner_scoped_idempotency_is_durable_and_conflict_safe(self) -> None:
        first = self.store.bind_run(
            principal_id="personal-laptop-human",
            idempotency_key="same-key",
            run_id="human-run",
            payload_hash="same-hash",
            at=NOW,
        )
        replay = ServiceStore(self.db_path).bind_run(
            principal_id="personal-laptop-human",
            idempotency_key="same-key",
            run_id="discarded-run-id",
            payload_hash="same-hash",
            at=NOW,
        )
        conflict = self.store.bind_run(
            principal_id="personal-laptop-human",
            idempotency_key="same-key",
            run_id="discarded-run-id-2",
            payload_hash="different-hash",
            at=NOW,
        )
        other_owner = self.store.bind_run(
            principal_id="personal-laptop-agent",
            idempotency_key="same-key",
            run_id="agent-run",
            payload_hash="same-hash",
            at=NOW,
        )

        self.assertEqual(first.code, RunBindingCode.CREATED)
        self.assertEqual(replay.code, RunBindingCode.REPLAY)
        self.assertEqual(replay.run_id, "human-run")
        self.assertEqual(conflict.code, RunBindingCode.IDEMPOTENCY_CONFLICT)
        self.assertEqual(other_owner.code, RunBindingCode.CREATED)

    def test_concurrency_cannot_race_past_daily_call_unit_limit(self) -> None:
        self._policy(daily=5)
        self._bind()

        def reserve(index: int) -> CallReservationCode:
            return self.store.reserve_call(
                self._reservation(index),
                at=NOW,
            ).code

        with ThreadPoolExecutor(max_workers=20) as executor:
            codes = list(executor.map(reserve, range(20)))

        self.assertEqual(codes.count(CallReservationCode.RESERVED), 5)
        self.assertEqual(
            codes.count(CallReservationCode.DAILY_LIMIT_EXCEEDED),
            15,
        )

    def test_max_inflight_and_reconciliation_are_durable(self) -> None:
        self._policy(max_inflight=1, daily=10)
        self._bind()
        first = self.store.reserve_call(self._reservation(1), at=NOW)
        denied = self.store.reserve_call(self._reservation(2), at=NOW)
        reconciled = self.store.reconcile_call(
            reservation_id="reservation-1",
            principal_id="personal-laptop-human",
            actual_units=1,
            at=NOW,
        )
        after_restart = ServiceStore(self.db_path).reserve_call(
            self._reservation(2),
            at=NOW,
        )

        self.assertEqual(first.code, CallReservationCode.RESERVED)
        self.assertEqual(
            denied.code,
            CallReservationCode.MAX_INFLIGHT_EXCEEDED,
        )
        self.assertTrue(reconciled.accepted)
        self.assertEqual(after_restart.code, CallReservationCode.RESERVED)

    def test_action_scoped_policy_and_revocation_survive_restart(self) -> None:
        policy = self._policy()
        invoke = self.store.authorize_action(
            policy_id=policy.policy_id,
            principal_id=policy.principal_id,
            tenant=policy.tenant,
            action=CouncilAction.PROVIDER_INVOKE.value,
            at=NOW,
        )
        create = self.store.authorize_action(
            policy_id=policy.policy_id,
            principal_id=policy.principal_id,
            tenant=policy.tenant,
            action=CouncilAction.RUN_CREATE.value,
            at=NOW,
        )
        revoked = self.store.revoke_policy(policy.policy_id, at=NOW)
        reopened = ServiceStore(self.db_path)
        preserved = reopened.put_policy(policy)
        after_restart = reopened.authorize_action(
            policy_id=policy.policy_id,
            principal_id=policy.principal_id,
            tenant=policy.tenant,
            action=CouncilAction.PROVIDER_INVOKE.value,
            at=NOW,
        )

        self.assertTrue(invoke.allowed)
        self.assertEqual(create.code, CallReservationCode.ACTION_DENIED)
        self.assertEqual(preserved.revoked_at, revoked.revoked_at)
        self.assertEqual(
            after_restart.code,
            CallReservationCode.POLICY_REVOKED,
        )

    def test_token_rotation_and_principal_revocation_are_atomic(self) -> None:
        replacement = "replacement-service-token-" + ("r" * 40)
        self.store.rotate_bearer_token(
            token_id="active-token",
            principal_id="personal-laptop-human",
            bearer_token=TOKEN,
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        )
        self.store.rotate_bearer_token(
            token_id="active-token",
            principal_id="personal-laptop-human",
            bearer_token=replacement,
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        )
        self.assertIsNone(self.store.authenticate_bearer(TOKEN, at=NOW))
        self.assertIsNotNone(
            self.store.authenticate_bearer(replacement, at=NOW)
        )
        self.assertEqual(
            self.store.revoke_principal_tokens(
                "personal-laptop-human",
                at=NOW,
            ),
            1,
        )
        self.assertIsNone(
            self.store.authenticate_bearer(replacement, at=NOW)
        )
        with self.assertRaisesRegex(ValueError, "retired"):
            self.store.rotate_bearer_token(
                token_id="active-token",
                principal_id="personal-laptop-human",
                bearer_token=replacement,
                issued_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(hours=1),
            )
        self.assertIsNone(
            self.store.authenticate_bearer(replacement, at=NOW)
        )

    def test_retired_token_tombstones_block_rollback_and_cross_principal_reuse(
        self,
    ) -> None:
        token_a = "rollback-token-a-" + ("a" * 40)
        token_b = "rollback-token-b-" + ("b" * 40)
        token_c = "rollback-token-c-" + ("c" * 40)
        token_d = "rollback-token-d-" + ("d" * 40)
        agent_token = "isolated-agent-token-" + ("e" * 40)

        self.store.rotate_bearer_token(
            token_id="human-current",
            principal_id="personal-laptop-human",
            bearer_token=token_a,
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        )
        self.assertEqual(
            self.store.revoke_principal_tokens(
                "personal-laptop-human",
                at=NOW,
            ),
            1,
        )
        self.store.rotate_bearer_token(
            token_id="human-current",
            principal_id="personal-laptop-human",
            bearer_token=token_b,
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        )

        reopened = ServiceStore(self.db_path)
        with self.assertRaisesRegex(ValueError, "retired"):
            reopened.rotate_bearer_token(
                token_id="human-current",
                principal_id="personal-laptop-human",
                bearer_token=token_a,
                issued_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(hours=1),
            )
        self.assertIsNone(reopened.authenticate_bearer(token_a, at=NOW))
        self.assertIsNotNone(reopened.authenticate_bearer(token_b, at=NOW))
        with self.assertRaisesRegex(ValueError, "retired"):
            reopened.put_bearer_token(
                token_id="stale-bootstrap",
                principal_id="personal-laptop-human",
                bearer_token=token_a,
                issued_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(hours=1),
            )

        reopened.rotate_bearer_token(
            token_id="agent-current",
            principal_id="personal-laptop-agent",
            bearer_token=agent_token,
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        )
        with self.assertRaisesRegex(ValueError, "retired"):
            reopened.rotate_bearer_token(
                token_id="agent-current",
                principal_id="personal-laptop-agent",
                bearer_token=token_a,
                issued_at=NOW - timedelta(hours=1),
                expires_at=NOW + timedelta(hours=1),
            )
        self.assertIsNotNone(
            reopened.authenticate_bearer(agent_token, at=NOW)
        )
        self.assertIsNotNone(reopened.authenticate_bearer(token_b, at=NOW))

        reopened.rotate_bearer_token(
            token_id="human-current",
            principal_id="personal-laptop-human",
            bearer_token=token_c,
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        )
        reopened.revoke_principal_tokens(
            "personal-laptop-human",
            at=NOW,
        )
        reopened.rotate_bearer_token(
            token_id="human-current",
            principal_id="personal-laptop-human",
            bearer_token=token_d,
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        )
        for retired in (token_a, token_b, token_c):
            with self.subTest(retired=retired[:16]):
                with self.assertRaisesRegex(ValueError, "retired"):
                    reopened.rotate_bearer_token(
                        token_id="human-current",
                        principal_id="personal-laptop-human",
                        bearer_token=retired,
                        issued_at=NOW - timedelta(hours=1),
                        expires_at=NOW + timedelta(hours=1),
                    )
                self.assertIsNotNone(
                    reopened.authenticate_bearer(token_d, at=NOW)
                )

        with reopened._read() as connection:
            tombstones = connection.execute(
                """
                SELECT COUNT(*)
                FROM service_bearer_token_tombstones
                WHERE principal_id = 'personal-laptop-human'
                """
            ).fetchone()[0]
        self.assertEqual(tombstones, 3)
        raw_database = self.db_path.read_bytes()
        for token in (token_a, token_b, token_c, token_d, agent_token):
            self.assertNotIn(token.encode(), raw_database)

    def test_symlinked_database_path_is_rejected_before_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "target.sqlite3"
            target.touch()
            link = directory / "service.sqlite3"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "regular file"):
                ServiceStore(link)


if __name__ == "__main__":
    unittest.main()
