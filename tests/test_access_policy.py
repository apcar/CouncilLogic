from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.access_policy import (  # noqa: E402
    AuthorizationRequest,
    CouncilAction,
    DecisionCode,
    Mandate,
    PolicyCore,
    ReconciliationRequest,
    Tenant,
)


NOW = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)


class PolicyCoreReservationTests(unittest.TestCase):
    def test_settled_reservation_cannot_be_replayed_as_a_new_lease(self) -> None:
        core = PolicyCore(clock=lambda: NOW)
        mandate = Mandate(
            mandate_id="mandate-1",
            owner_principal_id="personal-laptop-human",
            principal_id="personal-laptop-human",
            tenant=Tenant.PERSONAL,
            allowed_actions=frozenset(
                {CouncilAction.PROVIDER_INVOKE.value}
            ),
            allowed_providers=frozenset({"mock-1"}),
            allowed_models=frozenset({"mock-model-1"}),
            per_run_limit=Decimal("10"),
            daily_limit=Decimal("10"),
            monthly_limit=Decimal("10"),
            issued_at=NOW - timedelta(hours=1),
            expires_at=NOW + timedelta(hours=1),
        )
        core.register_mandate(mandate)
        request = AuthorizationRequest(
            principal_id="personal-laptop-human",
            tenant=Tenant.PERSONAL,
            mandate_id=mandate.mandate_id,
            action=CouncilAction.PROVIDER_INVOKE.value,
            run_id="run-1",
            reservation_id="reservation-1",
            provider="mock-1",
            model="mock-model-1",
            maximum_cost=Decimal("1"),
        )

        first = core.authorize_and_reserve(request)
        reserved_replay = core.authorize_and_reserve(request)
        core.reconcile(
            ReconciliationRequest(
                reservation_id=request.reservation_id,
                principal_id=request.principal_id,
                tenant=request.tenant,
                actual_cost=Decimal("1"),
            )
        )
        settled_replay = core.authorize_and_reserve(request)

        self.assertTrue(first.allowed)
        self.assertTrue(reserved_replay.allowed)
        self.assertFalse(settled_replay.allowed)
        self.assertEqual(
            settled_replay.code,
            DecisionCode.RESERVATION_SETTLED,
        )


if __name__ == "__main__":
    unittest.main()
