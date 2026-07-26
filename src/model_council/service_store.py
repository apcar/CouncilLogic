from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterable, Iterator

from .access_policy import Principal, PrincipalKind, Tenant, six_principals


MAX_BEARER_TOKEN_CHARS = 512


class RunBindingCode(StrEnum):
    CREATED = "created"
    REPLAY = "replay"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    RUN_ID_CONFLICT = "run_id_conflict"
    PRINCIPAL_DISABLED = "principal_disabled"


class CallReservationCode(StrEnum):
    RESERVED = "reserved"
    REPLAY = "replay"
    RESERVATION_CONFLICT = "reservation_conflict"
    UNKNOWN_PRINCIPAL = "unknown_principal"
    PRINCIPAL_DISABLED = "principal_disabled"
    TENANT_MISMATCH = "tenant_mismatch"
    RUN_NOT_OWNED = "run_not_owned"
    UNKNOWN_POLICY = "unknown_policy"
    POLICY_REVOKED = "policy_revoked"
    POLICY_NOT_ACTIVE = "policy_not_active"
    POLICY_EXPIRED = "policy_expired"
    ACTION_DENIED = "action_denied"
    PROVIDER_DENIED = "provider_denied"
    MODEL_DENIED = "model_denied"
    MAX_INFLIGHT_EXCEEDED = "max_inflight_exceeded"
    DAILY_LIMIT_EXCEEDED = "daily_limit_exceeded"
    MONTHLY_LIMIT_EXCEEDED = "monthly_limit_exceeded"


class CallReconciliationCode(StrEnum):
    RECONCILED = "reconciled"
    IDEMPOTENT = "idempotent"
    NOT_FOUND = "not_found"
    RELEASED = "released"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ActionAuthorizationResult:
    allowed: bool
    code: CallReservationCode
    policy_id: str
    principal_id: str
    action: str


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    principal: Principal
    token_id: str


@dataclass(frozen=True)
class ServicePolicy:
    policy_id: str
    principal_id: str
    tenant: Tenant
    allowed_actions: frozenset[str]
    allowed_providers: frozenset[str]
    allowed_models: frozenset[str]
    max_inflight: int
    daily_call_units: int
    monthly_call_units: int
    issued_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("policy_id", "principal_id"):
            _text(getattr(self, name), name)
        object.__setattr__(self, "tenant", Tenant(self.tenant))
        for name in (
            "allowed_actions",
            "allowed_providers",
            "allowed_models",
        ):
            values = frozenset(getattr(self, name))
            for value in values:
                _text(value, name)
            object.__setattr__(self, name, values)
        for name in ("max_inflight", "daily_call_units", "monthly_call_units"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        issued_at = _utc(self.issued_at, "issued_at")
        expires_at = _utc(self.expires_at, "expires_at")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        if expires_at <= issued_at:
            raise ValueError("expires_at must be later than issued_at")
        if self.revoked_at is not None:
            object.__setattr__(
                self,
                "revoked_at",
                _utc(self.revoked_at, "revoked_at"),
            )


@dataclass(frozen=True)
class RunBinding:
    code: RunBindingCode
    principal_id: str
    run_id: str
    idempotency_key: str
    payload_hash: str

    @property
    def created(self) -> bool:
        return self.code is RunBindingCode.CREATED

    @property
    def replay(self) -> bool:
        return self.code is RunBindingCode.REPLAY

    @property
    def conflict(self) -> bool:
        return self.code in {
            RunBindingCode.IDEMPOTENCY_CONFLICT,
            RunBindingCode.RUN_ID_CONFLICT,
        }


@dataclass(frozen=True)
class CallReservationRequest:
    reservation_id: str
    policy_id: str
    principal_id: str
    tenant: Tenant
    run_id: str
    action: str
    provider: str
    model: str
    units: int = 1

    def __post_init__(self) -> None:
        for name in (
            "reservation_id",
            "policy_id",
            "principal_id",
            "run_id",
            "action",
            "provider",
            "model",
        ):
            _text(getattr(self, name), name)
        object.__setattr__(self, "tenant", Tenant(self.tenant))
        if isinstance(self.units, bool) or not isinstance(self.units, int):
            raise ValueError("units must be a positive integer")
        if self.units < 1:
            raise ValueError("units must be a positive integer")


@dataclass(frozen=True)
class CallReservationResult:
    allowed: bool
    code: CallReservationCode
    reservation_id: str
    principal_id: str
    run_id: str
    units: int
    inflight: int = 0
    daily_committed: int = 0
    monthly_committed: int = 0


@dataclass(frozen=True)
class CallReconciliationResult:
    accepted: bool
    code: CallReconciliationCode
    reservation_id: str
    principal_id: str
    actual_units: int
    exceeded_reservation: bool = False
    exceeded_daily_limit: bool = False
    exceeded_monthly_limit: bool = False


class ServiceStore:
    """Durable multi-principal service state stored in SQLite.

    The store can share the CouncilStore database file; its tables use the
    ``service_`` prefix. Every mutation uses ``BEGIN IMMEDIATE``, so budget and
    max-inflight checks remain atomic across threads and processes.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        principals: Iterable[Principal] | None = None,
    ) -> None:
        candidate = Path(db_path).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        self.db_path = candidate
        self.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.db_path.parent, 0o700)
        self._prepare_database()
        self._migrate()
        self.put_principals(
            principals if principals is not None else six_principals()
        )

    def _prepare_database(self) -> None:
        try:
            metadata = self.db_path.lstat()
        except FileNotFoundError:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.db_path, flags, 0o600)
            os.close(descriptor)
        else:
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("database path must be a regular file")
        os.chmod(self.db_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._write() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS service_principals (
                    principal_id TEXT PRIMARY KEY,
                    tenant TEXT NOT NULL CHECK (tenant IN ('personal', 'work')),
                    kind TEXT NOT NULL CHECK (kind IN ('human', 'agent')),
                    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS service_bearer_tokens (
                    token_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL
                        REFERENCES service_principals(principal_id),
                    token_hash BLOB NOT NULL UNIQUE,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS service_bearer_token_tombstones (
                    token_hash BLOB PRIMARY KEY,
                    principal_id TEXT NOT NULL
                        REFERENCES service_principals(principal_id),
                    token_id TEXT NOT NULL,
                    retired_at TEXT NOT NULL,
                    reason TEXT NOT NULL
                        CHECK (reason IN ('revoked', 'rotated'))
                );
                CREATE TABLE IF NOT EXISTS service_policies (
                    policy_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL
                        REFERENCES service_principals(principal_id),
                    tenant TEXT NOT NULL CHECK (tenant IN ('personal', 'work')),
                    actions_json TEXT NOT NULL,
                    providers_json TEXT NOT NULL,
                    models_json TEXT NOT NULL,
                    max_inflight INTEGER NOT NULL CHECK (max_inflight > 0),
                    daily_call_units INTEGER NOT NULL
                        CHECK (daily_call_units > 0),
                    monthly_call_units INTEGER NOT NULL
                        CHECK (monthly_call_units > 0),
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS service_run_bindings (
                    run_id TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL
                        REFERENCES service_principals(principal_id),
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (principal_id, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS service_call_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL
                        REFERENCES service_policies(policy_id),
                    principal_id TEXT NOT NULL
                        REFERENCES service_principals(principal_id),
                    run_id TEXT NOT NULL
                        REFERENCES service_run_bindings(run_id),
                    action TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    reserved_units INTEGER NOT NULL CHECK (reserved_units > 0),
                    actual_units INTEGER CHECK (actual_units >= 0),
                    state TEXT NOT NULL
                        CHECK (state IN ('reserved', 'reconciled', 'released')),
                    reserved_at TEXT NOT NULL,
                    budget_day TEXT NOT NULL,
                    budget_month TEXT NOT NULL,
                    reconciled_at TEXT
                );
                CREATE INDEX IF NOT EXISTS service_token_principal_idx
                    ON service_bearer_tokens(principal_id);
                CREATE INDEX IF NOT EXISTS service_token_tombstone_principal_idx
                    ON service_bearer_token_tombstones(principal_id);
                CREATE INDEX IF NOT EXISTS service_call_budget_idx
                    ON service_call_reservations(
                        principal_id, budget_month, budget_day, state
                    );
                INSERT OR IGNORE INTO service_bearer_token_tombstones (
                    token_hash, principal_id, token_id, retired_at, reason
                )
                SELECT
                    token_hash, principal_id, token_id, revoked_at, 'revoked'
                FROM service_bearer_tokens
                WHERE revoked_at IS NOT NULL;
                DELETE FROM service_bearer_tokens
                WHERE revoked_at IS NOT NULL;
                """
            )

    def put_principals(self, principals: Iterable[Principal]) -> None:
        with self._write() as connection:
            for principal in principals:
                existing = connection.execute(
                    """
                    SELECT tenant, kind
                    FROM service_principals
                    WHERE principal_id = ?
                    """,
                    (principal.principal_id,),
                ).fetchone()
                if existing is not None and (
                    existing["tenant"] != principal.tenant.value
                    or existing["kind"] != principal.kind.value
                ):
                    raise ValueError(
                        f"principal identity changed: {principal.principal_id}"
                    )
                connection.execute(
                    """
                    INSERT INTO service_principals (
                        principal_id, tenant, kind, enabled
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(principal_id) DO NOTHING
                    """,
                    (
                        principal.principal_id,
                        principal.tenant.value,
                        principal.kind.value,
                        int(principal.enabled),
                    ),
                )

    def set_principal_enabled(
        self,
        principal_id: str,
        enabled: bool,
    ) -> None:
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE service_principals SET enabled = ?
                WHERE principal_id = ?
                """,
                (int(bool(enabled)), principal_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(principal_id)

    def put_bearer_token(
        self,
        *,
        token_id: str,
        principal_id: str,
        bearer_token: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> None:
        _text(token_id, "token_id")
        _text(principal_id, "principal_id")
        _text(bearer_token, "bearer_token")
        if (
            len(bearer_token) < 32
            or len(bearer_token) > MAX_BEARER_TOKEN_CHARS
            or any(character.isspace() for character in bearer_token)
        ):
            raise ValueError(
                "bearer_token must be a 32-512 character high-entropy "
                "non-whitespace value"
            )
        issued = _utc(issued_at, "issued_at")
        expires = _utc(expires_at, "expires_at")
        if expires <= issued:
            raise ValueError("expires_at must be later than issued_at")
        digest = _token_digest(bearer_token)
        with self._write() as connection:
            if _token_is_retired(connection, digest):
                raise ValueError(
                    "bearer token verifier is retired and cannot be reused"
                )
            digest_owner = connection.execute(
                """
                SELECT token_id, principal_id
                FROM service_bearer_tokens
                WHERE token_hash = ?
                """,
                (digest,),
            ).fetchone()
            if digest_owner is not None and (
                digest_owner["token_id"] != token_id
                or digest_owner["principal_id"] != principal_id
            ):
                raise ValueError(
                    "bearer token verifier is already registered"
                )
            existing = connection.execute(
                """
                SELECT principal_id, token_hash, issued_at, expires_at
                FROM service_bearer_tokens WHERE token_id = ?
                """,
                (token_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["principal_id"] != principal_id
                    or not hmac.compare_digest(
                        bytes(existing["token_hash"]),
                        digest,
                    )
                    or existing["issued_at"] != _timestamp(issued)
                    or existing["expires_at"] != _timestamp(expires)
                ):
                    raise ValueError("token_id is already registered")
                return
            connection.execute(
                """
                INSERT INTO service_bearer_tokens (
                    token_id, principal_id, token_hash, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    principal_id,
                    digest,
                    _timestamp(issued),
                    _timestamp(expires),
                ),
            )

    def rotate_bearer_token(
        self,
        *,
        token_id: str,
        principal_id: str,
        bearer_token: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> None:
        """Atomically replace active verifiers without reusing retired hashes."""

        _text(token_id, "token_id")
        _text(principal_id, "principal_id")
        _text(bearer_token, "bearer_token")
        if (
            len(bearer_token) < 32
            or len(bearer_token) > MAX_BEARER_TOKEN_CHARS
            or any(character.isspace() for character in bearer_token)
        ):
            raise ValueError(
                "bearer_token must be a 32-512 character high-entropy "
                "non-whitespace value"
            )
        issued = _utc(issued_at, "issued_at")
        expires = _utc(expires_at, "expires_at")
        if expires <= issued:
            raise ValueError("expires_at must be later than issued_at")
        digest = _token_digest(bearer_token)
        with self._write() as connection:
            principal = connection.execute(
                """
                SELECT 1 FROM service_principals WHERE principal_id = ?
                """,
                (principal_id,),
            ).fetchone()
            if principal is None:
                raise KeyError(principal_id)
            if _token_is_retired(connection, digest):
                raise ValueError(
                    "bearer token verifier is retired and cannot be reused"
                )
            existing_rows = connection.execute(
                """
                SELECT token_id, principal_id, token_hash, revoked_at
                FROM service_bearer_tokens
                ORDER BY token_id
                """
            ).fetchall()
            matching = next(
                (
                    row
                    for row in existing_rows
                    if hmac.compare_digest(
                        bytes(row["token_hash"]),
                        digest,
                    )
                ),
                None,
            )
            if matching is not None:
                if (
                    matching["principal_id"] != principal_id
                    or matching["revoked_at"] is not None
                ):
                    raise ValueError(
                        "bearer token verifier is already registered"
                    )
                for row in existing_rows:
                    if (
                        row["principal_id"] == principal_id
                        and row["token_id"] != matching["token_id"]
                    ):
                        _retire_token_row(
                            connection,
                            row,
                            reason="rotated",
                        )
                connection.execute(
                    """
                    DELETE FROM service_bearer_tokens
                    WHERE principal_id = ? AND token_id != ?
                    """,
                    (principal_id, matching["token_id"]),
                )
                return
            principal_rows = [
                row
                for row in existing_rows
                if row["principal_id"] == principal_id
            ]
            for row in principal_rows:
                _retire_token_row(
                    connection,
                    row,
                    reason=(
                        "revoked"
                        if row["revoked_at"] is not None
                        else "rotated"
                    ),
                )
            connection.execute(
                """
                DELETE FROM service_bearer_tokens WHERE principal_id = ?
                """,
                (principal_id,),
            )
            connection.execute(
                """
                INSERT INTO service_bearer_tokens (
                    token_id, principal_id, token_hash, issued_at, expires_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    token_id,
                    principal_id,
                    digest,
                    _timestamp(issued),
                    _timestamp(expires),
                ),
            )

    def revoke_bearer_token(
        self,
        token_id: str,
        *,
        at: datetime | None = None,
    ) -> bool:
        now = _timestamp(_utc_now(at))
        with self._write() as connection:
            row = connection.execute(
                """
                SELECT token_id, principal_id, token_hash, revoked_at
                FROM service_bearer_tokens WHERE token_id = ?
                """,
                (token_id,),
            ).fetchone()
            if row is None:
                return False
            _retire_token_row(
                connection,
                row,
                reason="revoked",
                retired_at=now,
            )
            connection.execute(
                "DELETE FROM service_bearer_tokens WHERE token_id = ?",
                (token_id,),
            )
            return True

    def revoke_principal_tokens(
        self,
        principal_id: str,
        *,
        at: datetime | None = None,
    ) -> int:
        """Revoke all currently active tokens for one configured principal."""

        _text(principal_id, "principal_id")
        now = _timestamp(_utc_now(at))
        with self._write() as connection:
            principal = connection.execute(
                """
                SELECT 1 FROM service_principals WHERE principal_id = ?
                """,
                (principal_id,),
            ).fetchone()
            if principal is None:
                raise KeyError(principal_id)
            rows = connection.execute(
                """
                SELECT token_id, principal_id, token_hash, revoked_at
                FROM service_bearer_tokens
                WHERE principal_id = ?
                """,
                (principal_id,),
            ).fetchall()
            for row in rows:
                _retire_token_row(
                    connection,
                    row,
                    reason="revoked",
                    retired_at=now,
                )
            connection.execute(
                "DELETE FROM service_bearer_tokens WHERE principal_id = ?",
                (principal_id,),
            )
            return len(rows)

    def authenticate_bearer(
        self,
        bearer_token: str,
        *,
        at: datetime | None = None,
    ) -> AuthenticatedPrincipal | None:
        _text(bearer_token, "bearer_token")
        candidate = _token_digest(bearer_token)
        now = _utc_now(at)
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT
                    t.token_id, t.token_hash, t.issued_at, t.expires_at,
                    t.revoked_at, p.principal_id, p.tenant, p.kind, p.enabled
                FROM service_bearer_tokens AS t
                JOIN service_principals AS p
                  ON p.principal_id = t.principal_id
                ORDER BY t.token_id
                """
            ).fetchall()

        matched: sqlite3.Row | None = None
        for row in rows:
            equal = hmac.compare_digest(bytes(row["token_hash"]), candidate)
            if equal:
                matched = row
        if matched is None:
            return None
        if not bool(matched["enabled"]) or matched["revoked_at"] is not None:
            return None
        if now < _parse_timestamp(matched["issued_at"]):
            return None
        if now >= _parse_timestamp(matched["expires_at"]):
            return None
        return AuthenticatedPrincipal(
            principal=Principal(
                principal_id=matched["principal_id"],
                tenant=Tenant(matched["tenant"]),
                kind=PrincipalKind(matched["kind"]),
            ),
            token_id=matched["token_id"],
        )

    def put_policy(self, policy: ServicePolicy) -> ServicePolicy:
        with self._write() as connection:
            principal = connection.execute(
                """
                SELECT tenant FROM service_principals WHERE principal_id = ?
                """,
                (policy.principal_id,),
            ).fetchone()
            if principal is None:
                raise ValueError("policy principal is not configured")
            if principal["tenant"] != policy.tenant.value:
                raise ValueError("policy tenant does not match principal")
            existing = connection.execute(
                "SELECT * FROM service_policies WHERE policy_id = ?",
                (policy.policy_id,),
            ).fetchone()
            serialized = _policy_values(policy)
            if existing is not None:
                existing_policy = _policy_from_row(existing)
                if replace(existing_policy, revoked_at=None) != replace(
                    policy,
                    revoked_at=None,
                ):
                    raise ValueError("policy_id is already registered")
                return existing_policy
            connection.execute(
                """
                INSERT INTO service_policies (
                    policy_id, principal_id, tenant, actions_json,
                    providers_json, models_json, max_inflight,
                    daily_call_units, monthly_call_units, issued_at,
                    expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                serialized,
            )
            return policy

    def authorize_action(
        self,
        *,
        policy_id: str,
        principal_id: str,
        tenant: Tenant,
        action: str,
        at: datetime | None = None,
    ) -> ActionAuthorizationResult:
        """Authorize a non-provider service action against durable policy."""

        for name, value in (
            ("policy_id", policy_id),
            ("principal_id", principal_id),
            ("action", action),
        ):
            _text(value, name)
        normalized_tenant = Tenant(tenant)
        now = _utc_now(at)
        with self._read() as connection:
            principal = connection.execute(
                """
                SELECT tenant, enabled FROM service_principals
                WHERE principal_id = ?
                """,
                (principal_id,),
            ).fetchone()
            if principal is None:
                code = CallReservationCode.UNKNOWN_PRINCIPAL
            elif not bool(principal["enabled"]):
                code = CallReservationCode.PRINCIPAL_DISABLED
            elif principal["tenant"] != normalized_tenant.value:
                code = CallReservationCode.TENANT_MISMATCH
            else:
                row = connection.execute(
                    """
                    SELECT * FROM service_policies WHERE policy_id = ?
                    """,
                    (policy_id,),
                ).fetchone()
                if row is None or row["principal_id"] != principal_id:
                    code = CallReservationCode.UNKNOWN_POLICY
                else:
                    policy = _policy_from_row(row)
                    if policy.tenant is not normalized_tenant:
                        code = CallReservationCode.TENANT_MISMATCH
                    elif (
                        policy.revoked_at is not None
                        and now >= policy.revoked_at
                    ):
                        code = CallReservationCode.POLICY_REVOKED
                    elif now < policy.issued_at:
                        code = CallReservationCode.POLICY_NOT_ACTIVE
                    elif now >= policy.expires_at:
                        code = CallReservationCode.POLICY_EXPIRED
                    elif action not in policy.allowed_actions:
                        code = CallReservationCode.ACTION_DENIED
                    else:
                        code = CallReservationCode.RESERVED
        return ActionAuthorizationResult(
            allowed=code is CallReservationCode.RESERVED,
            code=code,
            policy_id=policy_id,
            principal_id=principal_id,
            action=action,
        )

    def revoke_policy(
        self,
        policy_id: str,
        *,
        at: datetime | None = None,
    ) -> ServicePolicy:
        now = _timestamp(_utc_now(at))
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE service_policies
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE policy_id = ?
                """,
                (now, policy_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(policy_id)
            row = connection.execute(
                "SELECT * FROM service_policies WHERE policy_id = ?",
                (policy_id,),
            ).fetchone()
            return _policy_from_row(row)

    def bind_run(
        self,
        *,
        principal_id: str,
        idempotency_key: str,
        run_id: str,
        payload_hash: str,
        at: datetime | None = None,
    ) -> RunBinding:
        for name, value in (
            ("principal_id", principal_id),
            ("idempotency_key", idempotency_key),
            ("run_id", run_id),
            ("payload_hash", payload_hash),
        ):
            _text(value, name)
        now = _timestamp(_utc_now(at))
        with self._write() as connection:
            principal = connection.execute(
                """
                SELECT enabled FROM service_principals WHERE principal_id = ?
                """,
                (principal_id,),
            ).fetchone()
            if principal is None or not bool(principal["enabled"]):
                return RunBinding(
                    RunBindingCode.PRINCIPAL_DISABLED,
                    principal_id,
                    run_id,
                    idempotency_key,
                    payload_hash,
                )
            existing = connection.execute(
                """
                SELECT run_id, payload_hash
                FROM service_run_bindings
                WHERE principal_id = ? AND idempotency_key = ?
                """,
                (principal_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                code = (
                    RunBindingCode.REPLAY
                    if existing["payload_hash"] == payload_hash
                    else RunBindingCode.IDEMPOTENCY_CONFLICT
                )
                return RunBinding(
                    code,
                    principal_id,
                    existing["run_id"],
                    idempotency_key,
                    existing["payload_hash"],
                )
            occupied = connection.execute(
                """
                SELECT principal_id, idempotency_key, payload_hash
                FROM service_run_bindings WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if occupied is not None:
                return RunBinding(
                    RunBindingCode.RUN_ID_CONFLICT,
                    occupied["principal_id"],
                    run_id,
                    occupied["idempotency_key"],
                    occupied["payload_hash"],
                )
            connection.execute(
                """
                INSERT INTO service_run_bindings (
                    run_id, principal_id, idempotency_key, payload_hash,
                    created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, principal_id, idempotency_key, payload_hash, now),
            )
            return RunBinding(
                RunBindingCode.CREATED,
                principal_id,
                run_id,
                idempotency_key,
                payload_hash,
            )

    def run_owner(self, run_id: str) -> str | None:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT principal_id FROM service_run_bindings WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return None if row is None else str(row["principal_id"])

    def list_run_bindings(self) -> tuple[RunBinding, ...]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT principal_id, run_id, idempotency_key, payload_hash
                FROM service_run_bindings
                ORDER BY created_at, run_id
                """
            ).fetchall()
        return tuple(
            RunBinding(
                RunBindingCode.REPLAY,
                str(row["principal_id"]),
                str(row["run_id"]),
                str(row["idempotency_key"]),
                str(row["payload_hash"]),
            )
            for row in rows
        )

    def find_run_binding(
        self,
        *,
        principal_id: str,
        idempotency_key: str,
        payload_hash: str,
    ) -> RunBinding | None:
        """Look up an owner-scoped idempotency binding without mutating it."""

        for name, value in (
            ("principal_id", principal_id),
            ("idempotency_key", idempotency_key),
            ("payload_hash", payload_hash),
        ):
            _text(value, name)
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT run_id, payload_hash
                FROM service_run_bindings
                WHERE principal_id = ? AND idempotency_key = ?
                """,
                (principal_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        code = (
            RunBindingCode.REPLAY
            if row["payload_hash"] == payload_hash
            else RunBindingCode.IDEMPOTENCY_CONFLICT
        )
        return RunBinding(
            code,
            principal_id,
            str(row["run_id"]),
            idempotency_key,
            str(row["payload_hash"]),
        )

    def abandon_run_binding(
        self,
        *,
        principal_id: str,
        idempotency_key: str,
        run_id: str,
        payload_hash: str,
    ) -> bool:
        """Remove a new binding if creation of its Council run fails."""

        with self._write() as connection:
            cursor = connection.execute(
                """
                DELETE FROM service_run_bindings
                WHERE principal_id = ? AND idempotency_key = ?
                  AND run_id = ? AND payload_hash = ?
                  AND NOT EXISTS (
                    SELECT 1 FROM service_call_reservations
                    WHERE service_call_reservations.run_id =
                          service_run_bindings.run_id
                  )
                """,
                (
                    principal_id,
                    idempotency_key,
                    run_id,
                    payload_hash,
                ),
            )
            return cursor.rowcount == 1

    def reserve_call(
        self,
        request: CallReservationRequest,
        *,
        at: datetime | None = None,
    ) -> CallReservationResult:
        now = _utc_now(at)
        with self._write() as connection:
            existing = connection.execute(
                """
                SELECT * FROM service_call_reservations
                WHERE reservation_id = ?
                """,
                (request.reservation_id,),
            ).fetchone()
            if existing is not None:
                if _reservation_matches(existing, request):
                    return self._call_result(
                        connection,
                        request,
                        CallReservationCode.REPLAY,
                        now,
                    )
                return CallReservationResult(
                    False,
                    CallReservationCode.RESERVATION_CONFLICT,
                    request.reservation_id,
                    request.principal_id,
                    request.run_id,
                    request.units,
                )

            principal = connection.execute(
                """
                SELECT tenant, enabled FROM service_principals
                WHERE principal_id = ?
                """,
                (request.principal_id,),
            ).fetchone()
            if principal is None:
                return self._denied(request, CallReservationCode.UNKNOWN_PRINCIPAL)
            if not bool(principal["enabled"]):
                return self._denied(
                    request,
                    CallReservationCode.PRINCIPAL_DISABLED,
                )
            if principal["tenant"] != request.tenant.value:
                return self._denied(request, CallReservationCode.TENANT_MISMATCH)

            owner = connection.execute(
                """
                SELECT principal_id FROM service_run_bindings WHERE run_id = ?
                """,
                (request.run_id,),
            ).fetchone()
            if owner is None or owner["principal_id"] != request.principal_id:
                return self._denied(request, CallReservationCode.RUN_NOT_OWNED)

            row = connection.execute(
                "SELECT * FROM service_policies WHERE policy_id = ?",
                (request.policy_id,),
            ).fetchone()
            if row is None or row["principal_id"] != request.principal_id:
                return self._denied(request, CallReservationCode.UNKNOWN_POLICY)
            policy = _policy_from_row(row)
            if policy.tenant is not request.tenant:
                return self._denied(request, CallReservationCode.TENANT_MISMATCH)
            if policy.revoked_at is not None and now >= policy.revoked_at:
                return self._denied(request, CallReservationCode.POLICY_REVOKED)
            if now < policy.issued_at:
                return self._denied(
                    request,
                    CallReservationCode.POLICY_NOT_ACTIVE,
                )
            if now >= policy.expires_at:
                return self._denied(request, CallReservationCode.POLICY_EXPIRED)
            if request.action not in policy.allowed_actions:
                return self._denied(request, CallReservationCode.ACTION_DENIED)
            if request.provider not in policy.allowed_providers:
                return self._denied(request, CallReservationCode.PROVIDER_DENIED)
            if request.model not in policy.allowed_models:
                return self._denied(request, CallReservationCode.MODEL_DENIED)

            inflight, daily, monthly = _usage(
                connection,
                request.principal_id,
                now,
            )
            if inflight >= policy.max_inflight:
                return self._denied(
                    request,
                    CallReservationCode.MAX_INFLIGHT_EXCEEDED,
                    inflight,
                    daily,
                    monthly,
                )
            if daily + request.units > policy.daily_call_units:
                return self._denied(
                    request,
                    CallReservationCode.DAILY_LIMIT_EXCEEDED,
                    inflight,
                    daily,
                    monthly,
                )
            if monthly + request.units > policy.monthly_call_units:
                return self._denied(
                    request,
                    CallReservationCode.MONTHLY_LIMIT_EXCEEDED,
                    inflight,
                    daily,
                    monthly,
                )

            connection.execute(
                """
                INSERT INTO service_call_reservations (
                    reservation_id, policy_id, principal_id, run_id,
                    action, provider, model, reserved_units, state,
                    reserved_at, budget_day, budget_month
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
                """,
                (
                    request.reservation_id,
                    request.policy_id,
                    request.principal_id,
                    request.run_id,
                    request.action,
                    request.provider,
                    request.model,
                    request.units,
                    _timestamp(now),
                    now.date().isoformat(),
                    f"{now.year:04d}-{now.month:02d}",
                ),
            )
            return CallReservationResult(
                True,
                CallReservationCode.RESERVED,
                request.reservation_id,
                request.principal_id,
                request.run_id,
                request.units,
                inflight + 1,
                daily + request.units,
                monthly + request.units,
            )

    def reconcile_call(
        self,
        *,
        reservation_id: str,
        principal_id: str,
        actual_units: int,
        at: datetime | None = None,
    ) -> CallReconciliationResult:
        _positive_or_zero(actual_units, "actual_units")
        now = _utc_now(at)
        with self._write() as connection:
            row = connection.execute(
                """
                SELECT r.*, p.daily_call_units, p.monthly_call_units
                FROM service_call_reservations AS r
                JOIN service_policies AS p ON p.policy_id = r.policy_id
                WHERE r.reservation_id = ? AND r.principal_id = ?
                """,
                (reservation_id, principal_id),
            ).fetchone()
            if row is None:
                return CallReconciliationResult(
                    False,
                    CallReconciliationCode.NOT_FOUND,
                    reservation_id,
                    principal_id,
                    actual_units,
                )
            if row["state"] == "released":
                return CallReconciliationResult(
                    False,
                    CallReconciliationCode.RELEASED,
                    reservation_id,
                    principal_id,
                    actual_units,
                )
            if row["state"] == "reconciled":
                if row["actual_units"] != actual_units:
                    return CallReconciliationResult(
                        False,
                        CallReconciliationCode.CONFLICT,
                        reservation_id,
                        principal_id,
                        actual_units,
                    )
                code = CallReconciliationCode.IDEMPOTENT
            else:
                connection.execute(
                    """
                    UPDATE service_call_reservations
                    SET state = 'reconciled', actual_units = ?,
                        reconciled_at = ?
                    WHERE reservation_id = ?
                    """,
                    (actual_units, _timestamp(now), reservation_id),
                )
                code = CallReconciliationCode.RECONCILED
            _, daily, monthly = _usage(connection, principal_id, now)
            return CallReconciliationResult(
                True,
                code,
                reservation_id,
                principal_id,
                actual_units,
                exceeded_reservation=actual_units > row["reserved_units"],
                exceeded_daily_limit=daily > row["daily_call_units"],
                exceeded_monthly_limit=monthly > row["monthly_call_units"],
            )

    def release_call(
        self,
        *,
        reservation_id: str,
        principal_id: str,
    ) -> bool:
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE service_call_reservations SET state = 'released'
                WHERE reservation_id = ? AND principal_id = ?
                  AND state = 'reserved'
                """,
                (reservation_id, principal_id),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _denied(
        request: CallReservationRequest,
        code: CallReservationCode,
        inflight: int = 0,
        daily: int = 0,
        monthly: int = 0,
    ) -> CallReservationResult:
        return CallReservationResult(
            False,
            code,
            request.reservation_id,
            request.principal_id,
            request.run_id,
            request.units,
            inflight,
            daily,
            monthly,
        )

    @staticmethod
    def _call_result(
        connection: sqlite3.Connection,
        request: CallReservationRequest,
        code: CallReservationCode,
        at: datetime,
    ) -> CallReservationResult:
        inflight, daily, monthly = _usage(
            connection,
            request.principal_id,
            at,
        )
        return CallReservationResult(
            True,
            code,
            request.reservation_id,
            request.principal_id,
            request.run_id,
            request.units,
            inflight,
            daily,
            monthly,
        )


def _policy_values(policy: ServicePolicy) -> tuple[object, ...]:
    return (
        policy.policy_id,
        policy.principal_id,
        policy.tenant.value,
        _json_set(policy.allowed_actions),
        _json_set(policy.allowed_providers),
        _json_set(policy.allowed_models),
        policy.max_inflight,
        policy.daily_call_units,
        policy.monthly_call_units,
        _timestamp(policy.issued_at),
        _timestamp(policy.expires_at),
        _timestamp(policy.revoked_at) if policy.revoked_at else None,
    )


def _policy_from_row(row: sqlite3.Row) -> ServicePolicy:
    return ServicePolicy(
        policy_id=row["policy_id"],
        principal_id=row["principal_id"],
        tenant=Tenant(row["tenant"]),
        allowed_actions=frozenset(json.loads(row["actions_json"])),
        allowed_providers=frozenset(json.loads(row["providers_json"])),
        allowed_models=frozenset(json.loads(row["models_json"])),
        max_inflight=row["max_inflight"],
        daily_call_units=row["daily_call_units"],
        monthly_call_units=row["monthly_call_units"],
        issued_at=_parse_timestamp(row["issued_at"]),
        expires_at=_parse_timestamp(row["expires_at"]),
        revoked_at=(
            _parse_timestamp(row["revoked_at"])
            if row["revoked_at"] is not None
            else None
        ),
    )


def _reservation_matches(
    row: sqlite3.Row,
    request: CallReservationRequest,
) -> bool:
    return (
        row["policy_id"] == request.policy_id
        and row["principal_id"] == request.principal_id
        and row["run_id"] == request.run_id
        and row["action"] == request.action
        and row["provider"] == request.provider
        and row["model"] == request.model
        and row["reserved_units"] == request.units
    )


def _usage(
    connection: sqlite3.Connection,
    principal_id: str,
    at: datetime,
) -> tuple[int, int, int]:
    day = at.date().isoformat()
    month = f"{at.year:04d}-{at.month:02d}"
    row = connection.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN state = 'reserved' THEN 1 ELSE 0 END), 0)
            AS inflight,
          COALESCE(SUM(CASE
            WHEN budget_day = ? AND state = 'reserved' THEN reserved_units
            WHEN budget_day = ? AND state = 'reconciled' THEN actual_units
            ELSE 0 END), 0) AS daily,
          COALESCE(SUM(CASE
            WHEN budget_month = ? AND state = 'reserved' THEN reserved_units
            WHEN budget_month = ? AND state = 'reconciled' THEN actual_units
            ELSE 0 END), 0) AS monthly
        FROM service_call_reservations
        WHERE principal_id = ?
        """,
        (day, day, month, month, principal_id),
    ).fetchone()
    return int(row["inflight"]), int(row["daily"]), int(row["monthly"])


def _json_set(values: frozenset[str]) -> str:
    return json.dumps(sorted(values), separators=(",", ":"))


def _token_is_retired(
    connection: sqlite3.Connection,
    digest: bytes,
) -> bool:
    return (
        connection.execute(
            """
            SELECT 1 FROM service_bearer_token_tombstones
            WHERE token_hash = ?
            """,
            (digest,),
        ).fetchone()
        is not None
    )


def _retire_token_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    reason: str,
    retired_at: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO service_bearer_token_tombstones (
            token_hash, principal_id, token_id, retired_at, reason
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            bytes(row["token_hash"]),
            str(row["principal_id"]),
            str(row["token_id"]),
            retired_at
            or row["revoked_at"]
            or _timestamp(_utc_now(None)),
            reason,
        ),
    )


def _token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")


def _positive_or_zero(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_now(value: datetime | None) -> datetime:
    return _utc(
        value if value is not None else datetime.now(timezone.utc),
        "current time",
    )


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


__all__ = [
    "ActionAuthorizationResult",
    "AuthenticatedPrincipal",
    "CallReconciliationCode",
    "CallReconciliationResult",
    "CallReservationCode",
    "CallReservationRequest",
    "CallReservationResult",
    "RunBinding",
    "RunBindingCode",
    "ServicePolicy",
    "ServiceStore",
]
