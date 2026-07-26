from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from threading import RLock
from typing import Callable, Iterable


class Tenant(StrEnum):
    PERSONAL = "personal"
    WORK = "work"


class PrincipalKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"


class CouncilAction(StrEnum):
    RUN_CREATE = "run:create"
    RUN_READ = "run:read"
    RUN_CANCEL = "run:cancel"
    RUN_RESUME = "run:resume"
    RUN_EXPORT = "run:export"
    PROVIDER_INVOKE = "provider:invoke"
    MANDATE_APPROVE = "mandate:approve"
    AGENT_REVOKE = "agent:revoke"


class DecisionCode(StrEnum):
    ALLOWED = "allowed"
    UNKNOWN_PRINCIPAL = "unknown_principal"
    PRINCIPAL_DISABLED = "principal_disabled"
    UNKNOWN_MANDATE = "unknown_mandate"
    MANDATE_REVOKED = "mandate_revoked"
    MANDATE_NOT_ACTIVE = "mandate_not_active"
    MANDATE_EXPIRED = "mandate_expired"
    TENANT_MISMATCH = "tenant_mismatch"
    SUBJECT_MISMATCH = "subject_mismatch"
    OWNER_INVALID = "owner_invalid"
    ACTION_DENIED = "action_denied"
    PROVIDER_REQUIRED = "provider_required"
    PROVIDER_DENIED = "provider_denied"
    MODEL_REQUIRED = "model_required"
    MODEL_DENIED = "model_denied"
    INVALID_AMOUNT = "invalid_amount"
    RESERVATION_CONFLICT = "reservation_conflict"
    RESERVATION_SETTLED = "reservation_settled"
    PER_RUN_LIMIT_EXCEEDED = "per_run_limit_exceeded"
    DAILY_LIMIT_EXCEEDED = "daily_limit_exceeded"
    MONTHLY_LIMIT_EXCEEDED = "monthly_limit_exceeded"


class ReconciliationCode(StrEnum):
    RECONCILED = "reconciled"
    IDEMPOTENT = "idempotent"
    RESERVATION_NOT_FOUND = "reservation_not_found"
    RESERVATION_RELEASED = "reservation_released"
    RECONCILIATION_CONFLICT = "reconciliation_conflict"
    INVALID_AMOUNT = "invalid_amount"


@dataclass(frozen=True)
class Principal:
    principal_id: str
    tenant: Tenant
    kind: PrincipalKind
    enabled: bool = True

    def __post_init__(self) -> None:
        _require_identifier(self.principal_id, "principal_id")
        object.__setattr__(self, "tenant", Tenant(self.tenant))
        object.__setattr__(self, "kind", PrincipalKind(self.kind))


def six_principals(*, enable_work: bool = False) -> tuple[Principal, ...]:
    """Return the fixed four-device, six-principal deployment catalog."""

    return (
        Principal("mini-a-agent", Tenant.PERSONAL, PrincipalKind.AGENT),
        Principal("mini-b-agent", Tenant.PERSONAL, PrincipalKind.AGENT),
        Principal(
            "personal-laptop-human",
            Tenant.PERSONAL,
            PrincipalKind.HUMAN,
        ),
        Principal(
            "personal-laptop-agent",
            Tenant.PERSONAL,
            PrincipalKind.AGENT,
        ),
        Principal(
            "work-laptop-human",
            Tenant.WORK,
            PrincipalKind.HUMAN,
            enabled=enable_work,
        ),
        Principal(
            "work-laptop-agent",
            Tenant.WORK,
            PrincipalKind.AGENT,
            enabled=enable_work,
        ),
    )


@dataclass(frozen=True)
class Mandate:
    mandate_id: str
    owner_principal_id: str
    principal_id: str
    tenant: Tenant
    allowed_actions: frozenset[str]
    allowed_providers: frozenset[str]
    allowed_models: frozenset[str]
    per_run_limit: Decimal
    daily_limit: Decimal
    monthly_limit: Decimal
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.mandate_id, "mandate_id")
        _require_identifier(self.owner_principal_id, "owner_principal_id")
        _require_identifier(self.principal_id, "principal_id")
        object.__setattr__(self, "tenant", Tenant(self.tenant))
        object.__setattr__(
            self,
            "allowed_actions",
            _normalized_allowlist(self.allowed_actions, "allowed_actions"),
        )
        object.__setattr__(
            self,
            "allowed_providers",
            _normalized_allowlist(
                self.allowed_providers,
                "allowed_providers",
            ),
        )
        object.__setattr__(
            self,
            "allowed_models",
            _normalized_allowlist(self.allowed_models, "allowed_models"),
        )
        object.__setattr__(
            self,
            "per_run_limit",
            _money(self.per_run_limit, "per_run_limit"),
        )
        object.__setattr__(
            self,
            "daily_limit",
            _money(self.daily_limit, "daily_limit"),
        )
        object.__setattr__(
            self,
            "monthly_limit",
            _money(self.monthly_limit, "monthly_limit"),
        )
        object.__setattr__(
            self,
            "issued_at",
            _utc(self.issued_at, "issued_at"),
        )
        object.__setattr__(
            self,
            "expires_at",
            _utc(self.expires_at, "expires_at"),
        )
        if self.revoked_at is not None:
            object.__setattr__(
                self,
                "revoked_at",
                _utc(self.revoked_at, "revoked_at"),
            )
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be later than issued_at")

    def to_dict(self) -> dict[str, object]:
        return {
            "mandate_id": self.mandate_id,
            "owner_principal_id": self.owner_principal_id,
            "principal_id": self.principal_id,
            "tenant": self.tenant.value,
            "allowed_actions": sorted(self.allowed_actions),
            "allowed_providers": sorted(self.allowed_providers),
            "allowed_models": sorted(self.allowed_models),
            "per_run_limit": str(self.per_run_limit),
            "daily_limit": str(self.daily_limit),
            "monthly_limit": str(self.monthly_limit),
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "revoked_at": (
                self.revoked_at.isoformat()
                if self.revoked_at is not None
                else None
            ),
        }


@dataclass(frozen=True)
class AuthorizationRequest:
    principal_id: str
    tenant: Tenant
    mandate_id: str
    action: str
    run_id: str
    reservation_id: str
    provider: str | None = None
    model: str | None = None
    maximum_cost: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        for field_name in (
            "principal_id",
            "mandate_id",
            "action",
            "run_id",
            "reservation_id",
        ):
            _require_identifier(getattr(self, field_name), field_name)
        object.__setattr__(self, "tenant", Tenant(self.tenant))
        if self.provider is not None:
            _require_identifier(self.provider, "provider")
        if self.model is not None:
            _require_identifier(self.model, "model")
        object.__setattr__(
            self,
            "maximum_cost",
            _money(self.maximum_cost, "maximum_cost"),
        )


@dataclass(frozen=True)
class BudgetSnapshot:
    principal_id: str
    run_id: str
    at: datetime
    run_actual: Decimal
    run_reserved: Decimal
    daily_actual: Decimal
    daily_reserved: Decimal
    monthly_actual: Decimal
    monthly_reserved: Decimal
    per_run_limit: Decimal
    daily_limit: Decimal
    monthly_limit: Decimal

    @property
    def run_committed(self) -> Decimal:
        return self.run_actual + self.run_reserved

    @property
    def daily_committed(self) -> Decimal:
        return self.daily_actual + self.daily_reserved

    @property
    def monthly_committed(self) -> Decimal:
        return self.monthly_actual + self.monthly_reserved

    @property
    def run_remaining(self) -> Decimal:
        return max(Decimal("0"), self.per_run_limit - self.run_committed)

    @property
    def daily_remaining(self) -> Decimal:
        return max(Decimal("0"), self.daily_limit - self.daily_committed)

    @property
    def monthly_remaining(self) -> Decimal:
        return max(
            Decimal("0"),
            self.monthly_limit - self.monthly_committed,
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "principal_id": self.principal_id,
            "run_id": self.run_id,
            "at": self.at.isoformat(),
        }
        for name in (
            "run_actual",
            "run_reserved",
            "daily_actual",
            "daily_reserved",
            "monthly_actual",
            "monthly_reserved",
            "per_run_limit",
            "daily_limit",
            "monthly_limit",
            "run_committed",
            "daily_committed",
            "monthly_committed",
            "run_remaining",
            "daily_remaining",
            "monthly_remaining",
        ):
            result[name] = str(getattr(self, name))
        return result


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    code: DecisionCode
    principal_id: str
    tenant: Tenant
    mandate_id: str
    run_id: str
    reservation_id: str
    maximum_cost: Decimal
    budget: BudgetSnapshot | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "code": self.code.value,
            "principal_id": self.principal_id,
            "tenant": self.tenant.value,
            "mandate_id": self.mandate_id,
            "run_id": self.run_id,
            "reservation_id": self.reservation_id,
            "maximum_cost": str(self.maximum_cost),
            "budget": self.budget.to_dict() if self.budget else None,
        }


@dataclass(frozen=True)
class ReconciliationRequest:
    reservation_id: str
    principal_id: str
    tenant: Tenant
    actual_cost: Decimal

    def __post_init__(self) -> None:
        _require_identifier(self.reservation_id, "reservation_id")
        _require_identifier(self.principal_id, "principal_id")
        object.__setattr__(self, "tenant", Tenant(self.tenant))
        object.__setattr__(
            self,
            "actual_cost",
            _money(self.actual_cost, "actual_cost"),
        )


@dataclass(frozen=True)
class ReconciliationResult:
    accepted: bool
    code: ReconciliationCode
    reservation_id: str
    principal_id: str
    actual_cost: Decimal
    exceeded_reservation: bool = False
    exceeded_limit: bool = False
    budget: BudgetSnapshot | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "code": self.code.value,
            "reservation_id": self.reservation_id,
            "principal_id": self.principal_id,
            "actual_cost": str(self.actual_cost),
            "exceeded_reservation": self.exceeded_reservation,
            "exceeded_limit": self.exceeded_limit,
            "budget": self.budget.to_dict() if self.budget else None,
        }


class _ReservationState(StrEnum):
    RESERVED = "reserved"
    RECONCILED = "reconciled"
    RELEASED = "released"


@dataclass(frozen=True)
class _Reservation:
    request: AuthorizationRequest
    reserved_at: datetime
    decision: AuthorizationDecision
    state: _ReservationState = _ReservationState.RESERVED
    actual_cost: Decimal | None = None


class PolicyCore:
    """Thread-safe in-memory authorization and budget policy core.

    Provider credentials and authentication tokens are intentionally outside
    this component. The server supplies the already-authenticated principal.
    """

    def __init__(
        self,
        principals: Iterable[Principal] | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        catalog = tuple(principals if principals is not None else six_principals())
        principal_map = {principal.principal_id: principal for principal in catalog}
        if len(principal_map) != len(catalog):
            raise ValueError("principal_id values must be unique")
        self._principals = principal_map
        self._mandates: dict[str, Mandate] = {}
        self._reservations: dict[str, _Reservation] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()

    def principals(self) -> tuple[Principal, ...]:
        with self._lock:
            return tuple(
                self._principals[key]
                for key in sorted(self._principals)
            )

    def set_principal_enabled(
        self,
        principal_id: str,
        enabled: bool,
    ) -> Principal:
        with self._lock:
            principal = self._principals.get(principal_id)
            if principal is None:
                raise KeyError(principal_id)
            updated = replace(principal, enabled=bool(enabled))
            self._principals[principal_id] = updated
            return updated

    def register_mandate(self, mandate: Mandate) -> Mandate:
        with self._lock:
            subject = self._principals.get(mandate.principal_id)
            if subject is None:
                raise ValueError("mandate subject is not a configured principal")
            owner = self._principals.get(mandate.owner_principal_id)
            if owner is None or owner.kind is not PrincipalKind.HUMAN:
                raise ValueError("mandate owner must be a configured human")
            if owner.tenant is not mandate.tenant:
                raise ValueError("mandate owner tenant does not match mandate")
            if subject.tenant is not mandate.tenant:
                raise ValueError("mandate subject tenant does not match mandate")
            existing = self._mandates.get(mandate.mandate_id)
            if existing is not None and existing != mandate:
                raise ValueError("mandate_id is already registered")
            self._mandates[mandate.mandate_id] = mandate
            return mandate

    def revoke_mandate(
        self,
        mandate_id: str,
        *,
        at: datetime | None = None,
    ) -> Mandate:
        with self._lock:
            mandate = self._mandates.get(mandate_id)
            if mandate is None:
                raise KeyError(mandate_id)
            revoked_at = self._now(at)
            if mandate.revoked_at is not None:
                return mandate
            revoked = replace(mandate, revoked_at=revoked_at)
            self._mandates[mandate_id] = revoked
            return revoked

    def check(
        self,
        request: AuthorizationRequest,
        *,
        at: datetime | None = None,
    ) -> AuthorizationDecision:
        """Evaluate policy without reserving money.

        A caller that intends to make a billable provider call must use
        ``authorize_and_reserve`` instead; a successful check is not a lease.
        """

        with self._lock:
            return self._evaluate_locked(request, self._now(at))

    def authorize_and_reserve(
        self,
        request: AuthorizationRequest,
        *,
        at: datetime | None = None,
    ) -> AuthorizationDecision:
        """Atomically authorize a request and reserve its maximum cost."""

        with self._lock:
            existing = self._reservations.get(request.reservation_id)
            if existing is not None:
                if (
                    existing.request == request
                    and existing.state is _ReservationState.RESERVED
                ):
                    return existing.decision
                if existing.request == request:
                    return self._decision(
                        request,
                        DecisionCode.RESERVATION_SETTLED,
                    )
                return self._decision(
                    request,
                    DecisionCode.RESERVATION_CONFLICT,
                )

            now = self._now(at)
            decision = self._evaluate_locked(request, now)
            if not decision.allowed:
                return decision

            self._reservations[request.reservation_id] = _Reservation(
                request=request,
                reserved_at=now,
                decision=decision,
            )
            return decision

    def reconcile(
        self,
        request: ReconciliationRequest,
        *,
        at: datetime | None = None,
    ) -> ReconciliationResult:
        """Replace a reservation with trusted provider-reported actual cost.

        Actual cost is always accounted for, even when it unexpectedly exceeds
        the reservation or a ceiling. The result flags the overrun and later
        authorizations fail closed against the updated ledger.
        """

        with self._lock:
            reservation = self._reservations.get(request.reservation_id)
            if (
                reservation is None
                or reservation.request.principal_id != request.principal_id
                or reservation.request.tenant is not request.tenant
            ):
                return ReconciliationResult(
                    accepted=False,
                    code=ReconciliationCode.RESERVATION_NOT_FOUND,
                    reservation_id=request.reservation_id,
                    principal_id=request.principal_id,
                    actual_cost=request.actual_cost,
                )
            if reservation.state is _ReservationState.RELEASED:
                return ReconciliationResult(
                    accepted=False,
                    code=ReconciliationCode.RESERVATION_RELEASED,
                    reservation_id=request.reservation_id,
                    principal_id=request.principal_id,
                    actual_cost=request.actual_cost,
                )
            if reservation.state is _ReservationState.RECONCILED:
                if reservation.actual_cost == request.actual_cost:
                    snapshot = self._snapshot_locked(
                        reservation.request,
                        self._now(at),
                    )
                    return ReconciliationResult(
                        accepted=True,
                        code=ReconciliationCode.IDEMPOTENT,
                        reservation_id=request.reservation_id,
                        principal_id=request.principal_id,
                        actual_cost=request.actual_cost,
                        exceeded_reservation=(
                            request.actual_cost
                            > reservation.request.maximum_cost
                        ),
                        exceeded_limit=self._exceeded(snapshot),
                        budget=snapshot,
                    )
                return ReconciliationResult(
                    accepted=False,
                    code=ReconciliationCode.RECONCILIATION_CONFLICT,
                    reservation_id=request.reservation_id,
                    principal_id=request.principal_id,
                    actual_cost=request.actual_cost,
                )

            reconciled = replace(
                reservation,
                state=_ReservationState.RECONCILED,
                actual_cost=request.actual_cost,
            )
            self._reservations[request.reservation_id] = reconciled
            snapshot = self._snapshot_locked(
                reservation.request,
                self._now(at),
            )
            return ReconciliationResult(
                accepted=True,
                code=ReconciliationCode.RECONCILED,
                reservation_id=request.reservation_id,
                principal_id=request.principal_id,
                actual_cost=request.actual_cost,
                exceeded_reservation=(
                    request.actual_cost > reservation.request.maximum_cost
                ),
                exceeded_limit=self._exceeded(snapshot),
                budget=snapshot,
            )

    def release_reservation(
        self,
        reservation_id: str,
        *,
        principal_id: str,
        tenant: Tenant,
    ) -> bool:
        """Release cost when the provider call is known not to have started."""

        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if (
                reservation is None
                or reservation.request.principal_id != principal_id
                or reservation.request.tenant is not Tenant(tenant)
                or reservation.state is not _ReservationState.RESERVED
            ):
                return False
            self._reservations[reservation_id] = replace(
                reservation,
                state=_ReservationState.RELEASED,
            )
            return True

    def budget_snapshot(
        self,
        mandate_id: str,
        *,
        run_id: str,
        at: datetime | None = None,
    ) -> BudgetSnapshot:
        with self._lock:
            mandate = self._mandates.get(mandate_id)
            if mandate is None:
                raise KeyError(mandate_id)
            synthetic = AuthorizationRequest(
                principal_id=mandate.principal_id,
                tenant=mandate.tenant,
                mandate_id=mandate.mandate_id,
                action=CouncilAction.RUN_READ,
                run_id=run_id,
                reservation_id="snapshot",
            )
            return self._snapshot_locked(synthetic, self._now(at))

    def _evaluate_locked(
        self,
        request: AuthorizationRequest,
        now: datetime,
    ) -> AuthorizationDecision:
        principal = self._principals.get(request.principal_id)
        if principal is None:
            return self._decision(request, DecisionCode.UNKNOWN_PRINCIPAL)
        if not principal.enabled:
            return self._decision(request, DecisionCode.PRINCIPAL_DISABLED)
        if principal.tenant is not request.tenant:
            return self._decision(request, DecisionCode.TENANT_MISMATCH)

        mandate = self._mandates.get(request.mandate_id)
        if mandate is None:
            return self._decision(request, DecisionCode.UNKNOWN_MANDATE)
        if mandate.principal_id != request.principal_id:
            return self._decision(request, DecisionCode.SUBJECT_MISMATCH)
        if mandate.tenant is not request.tenant:
            return self._decision(request, DecisionCode.TENANT_MISMATCH)
        owner = self._principals.get(mandate.owner_principal_id)
        if (
            owner is None
            or owner.kind is not PrincipalKind.HUMAN
            or not owner.enabled
            or owner.tenant is not request.tenant
        ):
            return self._decision(request, DecisionCode.OWNER_INVALID)
        if mandate.revoked_at is not None and now >= mandate.revoked_at:
            return self._decision(request, DecisionCode.MANDATE_REVOKED)
        if now < mandate.issued_at:
            return self._decision(request, DecisionCode.MANDATE_NOT_ACTIVE)
        if now >= mandate.expires_at:
            return self._decision(request, DecisionCode.MANDATE_EXPIRED)
        if request.action not in mandate.allowed_actions:
            return self._decision(request, DecisionCode.ACTION_DENIED)

        if request.action == CouncilAction.PROVIDER_INVOKE:
            if request.provider is None:
                return self._decision(request, DecisionCode.PROVIDER_REQUIRED)
            if request.model is None:
                return self._decision(request, DecisionCode.MODEL_REQUIRED)
        if (
            request.provider is not None
            and request.provider not in mandate.allowed_providers
        ):
            return self._decision(request, DecisionCode.PROVIDER_DENIED)
        if (
            request.model is not None
            and request.model not in mandate.allowed_models
        ):
            return self._decision(request, DecisionCode.MODEL_DENIED)

        snapshot = self._snapshot_locked(request, now)
        if snapshot.run_committed + request.maximum_cost > mandate.per_run_limit:
            return self._decision(
                request,
                DecisionCode.PER_RUN_LIMIT_EXCEEDED,
                snapshot,
            )
        if (
            snapshot.daily_committed + request.maximum_cost
            > mandate.daily_limit
        ):
            return self._decision(
                request,
                DecisionCode.DAILY_LIMIT_EXCEEDED,
                snapshot,
            )
        if (
            snapshot.monthly_committed + request.maximum_cost
            > mandate.monthly_limit
        ):
            return self._decision(
                request,
                DecisionCode.MONTHLY_LIMIT_EXCEEDED,
                snapshot,
            )

        projected = replace(
            snapshot,
            run_reserved=snapshot.run_reserved + request.maximum_cost,
            daily_reserved=snapshot.daily_reserved + request.maximum_cost,
            monthly_reserved=(
                snapshot.monthly_reserved + request.maximum_cost
            ),
        )
        return self._decision(
            request,
            DecisionCode.ALLOWED,
            projected,
        )

    def _snapshot_locked(
        self,
        request: AuthorizationRequest,
        now: datetime,
    ) -> BudgetSnapshot:
        mandate = self._mandates[request.mandate_id]
        run_actual = Decimal("0")
        run_reserved = Decimal("0")
        daily_actual = Decimal("0")
        daily_reserved = Decimal("0")
        monthly_actual = Decimal("0")
        monthly_reserved = Decimal("0")
        current_day = now.date()
        current_month = (now.year, now.month)

        for reservation in self._reservations.values():
            candidate = reservation.request
            if (
                candidate.principal_id != request.principal_id
                or candidate.tenant is not request.tenant
                or reservation.state is _ReservationState.RELEASED
            ):
                continue
            actual = (
                reservation.actual_cost
                if reservation.state is _ReservationState.RECONCILED
                else None
            )
            reserved = (
                candidate.maximum_cost
                if reservation.state is _ReservationState.RESERVED
                else None
            )
            if candidate.run_id == request.run_id:
                run_actual += actual or Decimal("0")
                run_reserved += reserved or Decimal("0")
            if reservation.reserved_at.date() == current_day:
                daily_actual += actual or Decimal("0")
                daily_reserved += reserved or Decimal("0")
            if (
                reservation.reserved_at.year,
                reservation.reserved_at.month,
            ) == current_month:
                monthly_actual += actual or Decimal("0")
                monthly_reserved += reserved or Decimal("0")

        return BudgetSnapshot(
            principal_id=request.principal_id,
            run_id=request.run_id,
            at=now,
            run_actual=run_actual,
            run_reserved=run_reserved,
            daily_actual=daily_actual,
            daily_reserved=daily_reserved,
            monthly_actual=monthly_actual,
            monthly_reserved=monthly_reserved,
            per_run_limit=mandate.per_run_limit,
            daily_limit=mandate.daily_limit,
            monthly_limit=mandate.monthly_limit,
        )

    @staticmethod
    def _exceeded(snapshot: BudgetSnapshot) -> bool:
        return (
            snapshot.run_committed > snapshot.per_run_limit
            or snapshot.daily_committed > snapshot.daily_limit
            or snapshot.monthly_committed > snapshot.monthly_limit
        )

    @staticmethod
    def _decision(
        request: AuthorizationRequest,
        code: DecisionCode,
        budget: BudgetSnapshot | None = None,
    ) -> AuthorizationDecision:
        return AuthorizationDecision(
            allowed=code is DecisionCode.ALLOWED,
            code=code,
            principal_id=request.principal_id,
            tenant=request.tenant,
            mandate_id=request.mandate_id,
            run_id=request.run_id,
            reservation_id=request.reservation_id,
            maximum_cost=request.maximum_cost,
            budget=budget,
        )

    def _now(self, at: datetime | None) -> datetime:
        return _utc(at if at is not None else self._clock(), "current time")


def _require_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not have surrounding whitespace")


def _normalized_allowlist(
    values: Iterable[str],
    field_name: str,
) -> frozenset[str]:
    normalized = frozenset(values)
    for value in normalized:
        _require_identifier(value, field_name)
    return normalized


def _money(value: Decimal, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field_name} must use Decimal, int, or a string")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a valid dollar amount") from error
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return result


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


__all__ = [
    "AuthorizationDecision",
    "AuthorizationRequest",
    "BudgetSnapshot",
    "CouncilAction",
    "DecisionCode",
    "Mandate",
    "PolicyCore",
    "Principal",
    "PrincipalKind",
    "ReconciliationCode",
    "ReconciliationRequest",
    "ReconciliationResult",
    "Tenant",
    "six_principals",
]
