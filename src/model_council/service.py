from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import threading
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit
import uuid

from .access_policy import CouncilAction, Principal, Tenant, six_principals
from .config import AppConfig, mock_config
from .engine import CallLease, CouncilEngine
from .models import ErrorCategory, ProviderConfig, ProviderError
from .providers.factory import create_provider
from .remote import read_private_token
from .run_lock import ServiceLock
from .service_store import (
    CallReconciliationCode,
    CallReservationCode,
    CallReservationRequest,
    RunBindingCode,
    ServicePolicy,
    ServiceStore,
)
from .store import CouncilStore
from .version import MOCK_SERVICE_PROFILE_VERSION


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_WORKERS = 2
DEFAULT_MAX_QUEUE = 8
DEFAULT_CONNECTION_TIMEOUT_SECONDS = 5.0
MAX_BODY_BYTES = 64 * 1024
MAX_QUESTION_BYTES = 32 * 1024
MAX_QUESTION_CHARS = 32 * 1024
MAX_IDEMPOTENCY_KEY_BYTES = 128
MAX_AUTHORIZATION_BYTES = 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_PATH_BYTES = 512
_IDEMPOTENCY_RE = re.compile(r"^[\x21-\x7e]+$")
_RUN_PATH_RE = re.compile(
    r"^/v1/runs/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_POLICY_ISSUED_AT = datetime(2020, 1, 1, tzinfo=timezone.utc)
_POLICY_EXPIRES_AT = datetime(2100, 1, 1, tzinfo=timezone.utc)
_TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled"}
_SERVICE_MARKER = ".council-service-managed"


class ServiceInputError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CreateRunResult:
    status: int
    payload: dict[str, Any]


class _DurableCallLease(CallLease):
    def __init__(
        self,
        *,
        store: ServiceStore,
        reservation_id: str,
        principal_id: str,
    ) -> None:
        self._store = store
        self._reservation_id = reservation_id
        self._principal_id = principal_id

    def reconcile(self, actual_units: int) -> None:
        result = self._store.reconcile_call(
            reservation_id=self._reservation_id,
            principal_id=self._principal_id,
            actual_units=actual_units,
        )
        if not result.accepted or result.code not in {
            CallReconciliationCode.RECONCILED,
            CallReconciliationCode.IDEMPOTENT,
        }:
            raise RuntimeError(
                f"call-unit reconciliation failed: {result.code.value}"
            )

    def release(self) -> None:
        if not self._store.release_call(
            reservation_id=self._reservation_id,
            principal_id=self._principal_id,
        ):
            raise RuntimeError("call-unit reservation could not be released")


class DurableCallGate:
    """Adapt one principal's durable service policy to the engine call gate."""

    def __init__(
        self,
        *,
        store: ServiceStore,
        principal: Principal,
        policy_id: str,
    ) -> None:
        self._store = store
        self._principal = principal
        self._policy_id = policy_id

    def reserve(
        self,
        *,
        run_id: str,
        stage: str,
        provider: ProviderConfig,
        attempt: int,
    ) -> CallLease:
        reservation_id = _call_reservation_id(
            self._principal.principal_id,
            run_id,
            stage,
            provider.name,
            attempt,
        )
        result = self._store.reserve_call(
            CallReservationRequest(
                reservation_id=reservation_id,
                policy_id=self._policy_id,
                principal_id=self._principal.principal_id,
                tenant=self._principal.tenant,
                run_id=run_id,
                action=CouncilAction.PROVIDER_INVOKE.value,
                provider=provider.name,
                model=provider.model,
                units=1,
            )
        )
        if result.code is CallReservationCode.REPLAY:
            raise ProviderError(
                "Council call reservation already exists; refusing a "
                "duplicate provider execution",
                category=ErrorCategory.BUDGET,
                retryable=False,
                ambiguous=True,
            )
        if not result.allowed:
            category = (
                ErrorCategory.BUDGET
                if result.code
                in {
                    CallReservationCode.MAX_INFLIGHT_EXCEEDED,
                    CallReservationCode.DAILY_LIMIT_EXCEEDED,
                    CallReservationCode.MONTHLY_LIMIT_EXCEEDED,
                }
                else ErrorCategory.PERMISSION
            )
            raise ProviderError(
                f"Council policy denied provider invocation: "
                f"{result.code.value}",
                category=category,
                retryable=False,
                ambiguous=False,
            )
        return _DurableCallLease(
            store=self._store,
            reservation_id=reservation_id,
            principal_id=self._principal.principal_id,
        )


class CouncilApplication:
    """Mock-only asynchronous service application with bounded outstanding work."""

    def __init__(
        self,
        data_dir: str | os.PathLike[str],
        *,
        config: AppConfig | None = None,
        council_store: CouncilStore | None = None,
        service_store: ServiceStore | None = None,
        bearer_tokens: Mapping[str, str] | None = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        max_queue: int = DEFAULT_MAX_QUEUE,
        daily_call_units: int = 1200,
        monthly_call_units: int = 10_000,
        recover_jobs: bool = True,
    ) -> None:
        for name, value in (
            ("max_workers", max_workers),
            ("max_queue", max_queue),
            ("daily_call_units", daily_call_units),
            ("monthly_call_units", monthly_call_units),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < (0 if name == "max_queue" else 1)
            ):
                qualifier = "non-negative" if name == "max_queue" else "positive"
                raise ValueError(f"{name} must be a {qualifier} integer")

        if (
            council_store is not None
            and service_store is not None
            and _canonical_path(council_store.db_path)
            != _canonical_path(service_store.db_path)
        ):
            raise ValueError(
                "CouncilStore and ServiceStore must use the same database path"
            )
        lock_directory = (
            council_store.data_dir if council_store is not None else Path(data_dir)
        )
        self._service_lock = ServiceLock(lock_directory)
        self._service_lock.acquire()
        self._executor: ThreadPoolExecutor | None = None
        self._closed = False
        try:
            self.config = config or mock_config(data_dir)
            _validate_mock_only(self.config)
            self.council_store = council_store or CouncilStore(data_dir)
            self._reject_live_mode_runs()
            marker_existed = self._service_marker_exists()
            if self.council_store.list_runs() and not marker_existed:
                raise ValueError(
                    "Council service refuses a preexisting unowned CLI store"
                )
            self.service_store = service_store or ServiceStore(
                self.council_store.db_path
            )
            if _canonical_path(self.council_store.db_path) != _canonical_path(
                self.service_store.db_path
            ):
                raise ValueError(
                    "CouncilStore and ServiceStore must use the same "
                    "database path"
                )
            self._reject_unowned_runs()
            self._mark_service_managed()
            self.max_workers = max_workers
            self.max_queue = max_queue
            self._principals = {
                principal.principal_id: principal
                for principal in six_principals()
            }
            for principal_id in ("work-laptop-human", "work-laptop-agent"):
                self.service_store.set_principal_enabled(principal_id, False)

            self._providers = {
                provider_config.name: create_provider(provider_config)
                for provider_config in self.config.providers
            }
            self._policy_ids = self._install_mock_policies(
                daily_call_units=daily_call_units,
                monthly_call_units=monthly_call_units,
            )
            self._install_tokens(bearer_tokens or {})

            self._executor = ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="council-service",
            )
            self._slots = threading.BoundedSemaphore(
                max_workers + max_queue
            )
            self._state_lock = threading.RLock()
            self._principal_execution_locks = {
                principal_id: threading.Lock()
                for principal_id in self._principals
            }
            self._scheduled_run_ids: set[str] = set()
            self._pending_recovery: deque[tuple[Principal, str]] = deque()
            self._pending_run_ids: set[str] = set()
            if recover_jobs:
                self._recover_nonterminal_runs()
        except BaseException:
            if self._executor is not None:
                self._executor.shutdown(wait=True, cancel_futures=True)
            self._service_lock.release()
            raise

    def authenticate(self, authorization: str | None) -> Principal | None:
        if not isinstance(authorization, str):
            return None
        if len(authorization.encode("utf-8")) > MAX_AUTHORIZATION_BYTES:
            return None
        if not authorization.startswith("Bearer "):
            return None
        token = authorization[7:]
        if (
            len(token) < 32
            or len(token) > 512
            or any(character.isspace() for character in token)
        ):
            return None
        authenticated = self.service_store.authenticate_bearer(token)
        return None if authenticated is None else authenticated.principal

    def create_run(
        self,
        principal: Principal,
        *,
        question: str,
        idempotency_key: str,
    ) -> CreateRunResult:
        clean_question = _validate_question(question)
        clean_key = _validate_idempotency_key(idempotency_key)
        payload_hash = _request_hash(clean_question)

        with self._state_lock:
            if self._closed:
                raise ServiceInputError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "service_stopping",
                    "Council service is stopping",
                )
            self._authorize_action(principal, CouncilAction.RUN_CREATE)
            existing = self.service_store.find_run_binding(
                principal_id=principal.principal_id,
                idempotency_key=clean_key,
                payload_hash=payload_hash,
            )
            if existing is not None:
                if existing.code is RunBindingCode.IDEMPOTENCY_CONFLICT:
                    raise ServiceInputError(
                        HTTPStatus.CONFLICT,
                        "idempotency_conflict",
                        "Idempotency-Key was already used for another request",
                    )
                existing_run = self.council_store.get_run(existing.run_id)
                if existing_run is not None:
                    self._ensure_scheduled_locked(
                        principal,
                        existing.run_id,
                        str(existing_run["status"]),
                    )
                    return CreateRunResult(
                        HTTPStatus.OK,
                        self._public_run(existing.run_id, replayed=True),
                    )
                acquired = self._slots.acquire(blocking=False)
                if not acquired:
                    raise ServiceInputError(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        "queue_full",
                        "Council work queue is full",
                    )
                try:
                    self._new_engine(principal).create_run(
                        clean_question,
                        run_id=existing.run_id,
                    )
                    self.council_store.set_run_status(
                        existing.run_id,
                        "queued",
                    )
                    self.council_store.append_event(
                        existing.run_id,
                        "run_binding_repaired",
                        {"principal_id": principal.principal_id},
                    )
                    self._submit_acquired(
                        self._execute_run,
                        principal,
                        existing.run_id,
                    )
                except BaseException:
                    self._slots.release()
                    raise
                return CreateRunResult(
                    HTTPStatus.ACCEPTED,
                    {
                        "run_id": existing.run_id,
                        "status": "queued",
                        "replayed": True,
                    },
                )

            acquired = self._slots.acquire(blocking=False)
            if not acquired:
                raise ServiceInputError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "queue_full",
                    "Council work queue is full",
                )

            run_id = str(uuid.uuid4())
            binding_created = False
            try:
                binding = self.service_store.bind_run(
                    principal_id=principal.principal_id,
                    idempotency_key=clean_key,
                    run_id=run_id,
                    payload_hash=payload_hash,
                )
                if binding.code is RunBindingCode.IDEMPOTENCY_CONFLICT:
                    raise ServiceInputError(
                        HTTPStatus.CONFLICT,
                        "idempotency_conflict",
                        "Idempotency-Key was already used for another request",
                    )
                if binding.code is RunBindingCode.REPLAY:
                    payload = self._public_run(
                        binding.run_id,
                        replayed=True,
                    )
                    self._slots.release()
                    return CreateRunResult(
                        HTTPStatus.OK,
                        payload,
                    )
                if binding.code is not RunBindingCode.CREATED:
                    raise ServiceInputError(
                        HTTPStatus.FORBIDDEN,
                        "principal_disabled",
                        "Principal is not permitted to create runs",
                    )
                binding_created = True
                created_run_id = self._new_engine(principal).create_run(
                    clean_question,
                    run_id=run_id,
                )
                if created_run_id != run_id:
                    raise RuntimeError(
                        "Council store returned an unexpected run_id"
                    )
                self.council_store.set_run_status(run_id, "queued")
                self.council_store.append_event(
                    run_id,
                    "run_queued",
                    {"principal_id": principal.principal_id},
                )
                self._submit_acquired(self._execute_run, principal, run_id)
            except BaseException:
                if (
                    binding_created
                    and self.council_store.get_run(run_id) is None
                ):
                    self.service_store.abandon_run_binding(
                        principal_id=principal.principal_id,
                        idempotency_key=clean_key,
                        run_id=run_id,
                        payload_hash=payload_hash,
                    )
                if (
                    binding_created
                    and self.council_store.get_run(run_id) is not None
                ):
                    try:
                        self.council_store.set_run_status(
                            run_id,
                            "failed",
                            {"message": "Run dispatch failed safely"},
                        )
                    except Exception:
                        pass
                self._slots.release()
                raise
        return CreateRunResult(
            HTTPStatus.ACCEPTED,
            {"run_id": run_id, "status": "queued", "replayed": False},
        )

    def get_run(self, principal: Principal, run_id: str) -> dict[str, Any]:
        normalized = _validate_run_id(run_id)
        self._authorize_action(principal, CouncilAction.RUN_READ)
        if self.service_store.run_owner(normalized) != principal.principal_id:
            raise ServiceInputError(
                HTTPStatus.NOT_FOUND,
                "run_not_found",
                "Council run was not found",
            )
        return self._public_run(normalized)

    def close(self, *, wait: bool = True) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        assert self._executor is not None
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._service_lock.release()

    def _install_mock_policies(
        self,
        *,
        daily_call_units: int,
        monthly_call_units: int,
    ) -> dict[str, str]:
        if monthly_call_units < daily_call_units:
            raise ValueError(
                "monthly_call_units must be at least daily_call_units"
            )
        provider_names = frozenset(self._providers)
        model_names = frozenset(
            provider.config.model for provider in self._providers.values()
        )
        policy_ids: dict[str, str] = {}
        for principal in self._principals.values():
            policy_id = f"mock-default-{principal.principal_id}"
            self.service_store.put_policy(
                ServicePolicy(
                    policy_id=policy_id,
                    principal_id=principal.principal_id,
                    tenant=principal.tenant,
                    allowed_actions=frozenset(
                        {
                            CouncilAction.RUN_CREATE.value,
                            CouncilAction.RUN_READ.value,
                            CouncilAction.PROVIDER_INVOKE.value,
                        }
                    ),
                    allowed_providers=provider_names,
                    allowed_models=model_names,
                    max_inflight=len(self._providers),
                    daily_call_units=daily_call_units,
                    monthly_call_units=monthly_call_units,
                    issued_at=_POLICY_ISSUED_AT,
                    expires_at=_POLICY_EXPIRES_AT,
                )
            )
            policy_ids[principal.principal_id] = policy_id
        return policy_ids

    def _install_tokens(self, bearer_tokens: Mapping[str, str]) -> None:
        unknown = set(bearer_tokens) - set(self._principals)
        if unknown:
            raise ValueError(
                f"unknown bearer-token principals: {', '.join(sorted(unknown))}"
            )
        for principal_id, token in bearer_tokens.items():
            self.service_store.rotate_bearer_token(
                token_id=f"bootstrap-{principal_id}",
                principal_id=principal_id,
                bearer_token=token,
                issued_at=_POLICY_ISSUED_AT,
                expires_at=_POLICY_EXPIRES_AT,
            )

    def _new_engine(self, principal: Principal) -> CouncilEngine:
        return CouncilEngine(
            store=self.council_store,
            providers=self._providers,
            policy=self.config.policy,
            synthesis_provider=self.config.synthesis_provider,
            call_gate=DurableCallGate(
                store=self.service_store,
                principal=principal,
                policy_id=self._policy_ids[principal.principal_id],
            ),
        )

    def _submit_acquired(
        self,
        function: Callable[..., None],
        *args: object,
    ) -> Future[None]:
        principal = args[0]
        run_id = str(args[1])
        if not isinstance(principal, Principal):
            raise TypeError("scheduled run principal is invalid")
        if run_id in self._scheduled_run_ids:
            self._slots.release()
            raise RuntimeError("Council run is already scheduled")
        self._scheduled_run_ids.add(run_id)
        assert self._executor is not None
        try:
            future = self._executor.submit(function, *args)
        except BaseException:
            self._scheduled_run_ids.discard(run_id)
            raise

        def release_slot(_future: Future[None]) -> None:
            with self._state_lock:
                self._scheduled_run_ids.discard(run_id)
                self._slots.release()
                self._drain_pending_locked()

        future.add_done_callback(release_slot)
        return future

    def _authorize_action(
        self,
        principal: Principal,
        action: CouncilAction,
    ) -> None:
        result = self.service_store.authorize_action(
            policy_id=self._policy_ids[principal.principal_id],
            principal_id=principal.principal_id,
            tenant=principal.tenant,
            action=action.value,
        )
        if not result.allowed:
            raise ServiceInputError(
                HTTPStatus.FORBIDDEN,
                result.code.value,
                "Council policy denied this action",
            )

    def _recover_nonterminal_runs(self) -> None:
        with self._state_lock:
            for binding in self.service_store.list_run_bindings():
                run = self.council_store.get_run(binding.run_id)
                if run is None or str(run["status"]) in _TERMINAL_STATUSES:
                    continue
                principal = self._principals.get(binding.principal_id)
                if principal is None:
                    continue
                self._queue_recovery_locked(principal, binding.run_id)
            self._drain_pending_locked()

    def _ensure_scheduled_locked(
        self,
        principal: Principal,
        run_id: str,
        status: str,
    ) -> None:
        if status in _TERMINAL_STATUSES:
            return
        if (
            run_id not in self._scheduled_run_ids
            and run_id not in self._pending_run_ids
        ):
            self._queue_recovery_locked(principal, run_id)
            self._drain_pending_locked()

    def _queue_recovery_locked(
        self,
        principal: Principal,
        run_id: str,
    ) -> None:
        if (
            run_id in self._scheduled_run_ids
            or run_id in self._pending_run_ids
        ):
            return
        self._pending_recovery.append((principal, run_id))
        self._pending_run_ids.add(run_id)

    def _drain_pending_locked(self) -> None:
        while self._pending_recovery and not self._closed:
            if not self._slots.acquire(blocking=False):
                return
            principal, run_id = self._pending_recovery.popleft()
            self._pending_run_ids.discard(run_id)
            try:
                self._submit_acquired(
                    self._execute_run,
                    principal,
                    run_id,
                )
            except BaseException:
                self._slots.release()
                raise

    def revoke_principal_tokens(self, principal_id: str) -> int:
        if principal_id not in self._principals:
            raise KeyError(principal_id)
        return self.service_store.revoke_principal_tokens(principal_id)

    def _reject_live_mode_runs(self) -> None:
        for run in self.council_store.list_runs():
            providers = run.get("provider_configs") or []
            if not providers or any(
                not str(provider.get("name", "")).startswith("mock")
                for provider in providers
                if isinstance(provider, dict)
            ):
                raise ValueError(
                    "Council service refuses a store containing live-mode runs"
                )

    def _reject_unowned_runs(self) -> None:
        for run in self.council_store.list_runs():
            if self.service_store.run_owner(str(run["id"])) is None:
                raise ValueError(
                    "Council service refuses a preexisting unowned run"
                )

    def _service_marker_exists(self) -> bool:
        marker = self.council_store.data_dir / _SERVICE_MARKER
        return marker.exists() or marker.is_symlink()

    def _mark_service_managed(self) -> None:
        marker = self.council_store.data_dir / _SERVICE_MARKER
        if marker.is_symlink():
            raise ValueError("service marker must not be a symlink")
        flags = os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(marker, flags, 0o600)
        os.close(descriptor)
        os.chmod(marker, 0o600)

    def _execute_run(self, principal: Principal, run_id: str) -> None:
        with self._principal_execution_locks[principal.principal_id]:
            try:
                self._new_engine(principal).resume(run_id)
            except BaseException as error:
                safe_error = {
                    "message": "Council run failed safely",
                    "type": type(error).__name__,
                }
                try:
                    self.council_store.set_run_status(
                        run_id,
                        "failed",
                        safe_error,
                    )
                    self.council_store.append_event(
                        run_id,
                        "run_failed",
                        safe_error,
                    )
                except Exception:
                    return

    def _public_run(
        self,
        run_id: str,
        *,
        replayed: bool | None = None,
    ) -> dict[str, Any]:
        run = self.council_store.get_run(run_id)
        if run is None:
            raise ServiceInputError(
                HTTPStatus.NOT_FOUND,
                "run_not_found",
                "Council run was not found",
            )
        payload: dict[str, Any] = {
            "run_id": run_id,
            "status": run["status"],
        }
        if replayed is not None:
            payload["replayed"] = replayed
        if run["status"] in _TERMINAL_STATUSES:
            if isinstance(run.get("result"), dict):
                payload["result"] = run["result"]
            elif isinstance(run.get("error"), dict):
                payload["error"] = run["error"]
        return payload


class CouncilHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        server_address: tuple[str, int],
        application: CouncilApplication,
    ) -> None:
        _validate_loopback_host(server_address[0])
        try:
            address = ipaddress.ip_address(
                server_address[0].split("%", 1)[0]
            )
        except ValueError:
            address = None
        if address is not None and address.version == 6:
            self.address_family = socket.AF_INET6
        self.application = application
        super().__init__(server_address, CouncilRequestHandler)


class CouncilRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"CouncilService/{MOCK_SERVICE_PROFILE_VERSION}"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(DEFAULT_CONNECTION_TIMEOUT_SECONDS)

    @property
    def application(self) -> CouncilApplication:
        return self.server.application  # type: ignore[attr-defined,no-any-return]

    def do_GET(self) -> None:
        request_id = str(uuid.uuid4())
        try:
            self._validate_request_envelope()
            if self.path == "/healthz":
                self._require_empty_body()
                self._json(
                    HTTPStatus.OK,
                    {"ready": True, "mode": "mock"},
                    request_id,
                )
                return
            match = _RUN_PATH_RE.fullmatch(self.path)
            if match is None:
                raise ServiceInputError(
                    HTTPStatus.NOT_FOUND,
                    "not_found",
                    "Resource was not found",
                )
            self._require_empty_body()
            principal = self._authenticate()
            payload = self.application.get_run(principal, match.group(1))
            self._json(HTTPStatus.OK, payload, request_id)
        except ServiceInputError as error:
            self.close_connection = True
            self._error(error, request_id)
        except Exception:
            self.close_connection = True
            self._error(
                ServiceInputError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "Council request failed safely",
                ),
                request_id,
            )

    def do_POST(self) -> None:
        request_id = str(uuid.uuid4())
        try:
            self._validate_request_envelope()
            if self.path != "/v1/runs":
                raise ServiceInputError(
                    HTTPStatus.NOT_FOUND,
                    "not_found",
                    "Resource was not found",
                )
            principal = self._authenticate()
            idempotency_key = self._single_header(
                "Idempotency-Key",
                required=True,
            )
            payload = self._read_json_object()
            if set(payload) != {"question"}:
                raise ServiceInputError(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "JSON object must contain only question",
                )
            result = self.application.create_run(
                principal,
                question=payload["question"],
                idempotency_key=idempotency_key,
            )
            self._json(result.status, result.payload, request_id)
        except ServiceInputError as error:
            self.close_connection = True
            self._error(error, request_id)
        except Exception:
            self.close_connection = True
            self._error(
                ServiceInputError(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "internal_error",
                    "Council request failed safely",
                ),
                request_id,
            )

    def do_PUT(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        request_id = str(uuid.uuid4())
        self.close_connection = True
        self._error(
            ServiceInputError(
                HTTPStatus.METHOD_NOT_ALLOWED,
                "method_not_allowed",
                "HTTP method is not allowed",
            ),
            request_id,
            extra_headers={"Allow": "GET, POST"},
        )

    def _validate_request_envelope(self) -> None:
        try:
            peer = ipaddress.ip_address(
                str(self.client_address[0]).split("%", 1)[0]
            )
        except ValueError:
            peer = None
        if peer is None or not peer.is_loopback:
            raise ServiceInputError(
                HTTPStatus.FORBIDDEN,
                "loopback_required",
                "Council service accepts loopback clients only",
            )
        if len(self.path.encode("utf-8")) > MAX_PATH_BYTES:
            raise ServiceInputError(
                HTTPStatus.REQUEST_URI_TOO_LONG,
                "path_too_long",
                "Request path is too long",
            )
        header_bytes = sum(
            len(name.encode("utf-8")) + len(value.encode("utf-8")) + 4
            for name, value in self.headers.items()
        )
        if header_bytes > MAX_HEADER_BYTES:
            raise ServiceInputError(
                HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                "headers_too_large",
                "Request headers are too large",
            )

    def _authenticate(self) -> Principal:
        authorization = self._single_header(
            "Authorization",
            required=False,
        )
        principal = self.application.authenticate(authorization)
        if principal is None:
            raise ServiceInputError(
                HTTPStatus.UNAUTHORIZED,
                "unauthorized",
                "A valid principal bearer token is required",
            )
        return principal

    def _read_json_object(self) -> dict[str, Any]:
        transfer_encoding = self.headers.get_all("Transfer-Encoding") or []
        if transfer_encoding:
            raise ServiceInputError(
                HTTPStatus.BAD_REQUEST,
                "invalid_framing",
                "Transfer-Encoding is not accepted",
            )
        content_type = self._single_header("Content-Type", required=True)
        media_type, *parameters = [
            part.strip().lower() for part in content_type.split(";")
        ]
        if media_type != "application/json" or any(
            parameter != "charset=utf-8" for parameter in parameters
        ):
            raise ServiceInputError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/json with UTF-8",
            )
        raw_length = self._single_header("Content-Length", required=True)
        if not raw_length.isascii() or not raw_length.isdigit():
            raise ServiceInputError(
                HTTPStatus.BAD_REQUEST,
                "invalid_content_length",
                "Content-Length must be a decimal integer",
            )
        length = int(raw_length)
        if length < 1:
            raise ServiceInputError(
                HTTPStatus.BAD_REQUEST,
                "empty_body",
                "A JSON request body is required",
            )
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise ServiceInputError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "body_too_large",
                "Request body exceeds the service limit",
            )
        raw = self.rfile.read(length)
        if len(raw) != length:
            self.close_connection = True
            raise ServiceInputError(
                HTTPStatus.BAD_REQUEST,
                "incomplete_body",
                "Request body ended before Content-Length",
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ServiceInputError(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "Request body must be valid UTF-8 JSON",
            ) from None
        try:
            decoded = json.loads(
                text,
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
        except (ValueError, RecursionError):
            raise ServiceInputError(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "Request body must be a valid JSON object",
            ) from None
        if not isinstance(decoded, dict):
            raise ServiceInputError(
                HTTPStatus.BAD_REQUEST,
                "invalid_json",
                "Request body must be a JSON object",
            )
        return decoded

    def _require_empty_body(self) -> None:
        if self.headers.get_all("Transfer-Encoding"):
            self.close_connection = True
            raise ServiceInputError(
                HTTPStatus.BAD_REQUEST,
                "invalid_framing",
                "GET requests must not contain a body",
            )
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) > 1 or (
            lengths and (not lengths[0].isdigit() or int(lengths[0]) != 0)
        ):
            self.close_connection = True
            raise ServiceInputError(
                HTTPStatus.BAD_REQUEST,
                "unexpected_body",
                "GET requests must not contain a body",
            )

    def _single_header(self, name: str, *, required: bool) -> str | None:
        values = self.headers.get_all(name) or []
        if not values:
            if required:
                raise ServiceInputError(
                    HTTPStatus.BAD_REQUEST,
                    "missing_header",
                    f"{name} header is required",
                )
            return None
        if len(values) != 1 or "," in values[0]:
            raise ServiceInputError(
                HTTPStatus.BAD_REQUEST,
                "duplicate_header",
                f"{name} header must occur exactly once",
            )
        return values[0]

    def _json(
        self,
        status: int,
        payload: dict[str, Any],
        request_id: str,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        response = dict(payload)
        response["request_id"] = request_id
        body = json.dumps(
            response,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Request-ID", request_id)
        if self.close_connection:
            self.send_header("Connection", "close")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _error(
        self,
        error: ServiceInputError,
        request_id: str,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        headers = dict(extra_headers or {})
        if error.status == HTTPStatus.UNAUTHORIZED:
            headers["WWW-Authenticate"] = 'Bearer realm="council"'
        if error.code in {"queue_full", "run_initializing"}:
            headers["Retry-After"] = "1"
        self._json(
            error.status,
            {
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "request_id": request_id,
                }
            },
            request_id,
            extra_headers=headers,
        )

    def log_message(self, format: str, *args: object) -> None:
        return


def _validate_mock_only(config: AppConfig) -> None:
    if not config.providers:
        raise ValueError("mock service requires at least one provider")
    for provider in config.providers:
        parsed = urlsplit(provider.endpoint)
        if (
            not provider.name.startswith("mock")
            or parsed.scheme not in {"http", "https"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError(
                "council service is mock-only and refuses live providers"
            )
    if not config.synthesis_provider.startswith("mock"):
        raise ValueError("council service synthesis provider must be a mock")


def _validate_loopback_host(host: str) -> None:
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        if host != "localhost":
            raise ValueError("service host must be a loopback address") from None
        return
    if not address.is_loopback:
        raise ValueError("service host must be a loopback address")


def _canonical_path(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _validate_question(value: object) -> str:
    if not isinstance(value, str):
        raise ServiceInputError(
            HTTPStatus.BAD_REQUEST,
            "invalid_question",
            "question must be a string",
        )
    clean = value.strip()
    if not clean:
        raise ServiceInputError(
            HTTPStatus.BAD_REQUEST,
            "invalid_question",
            "question must not be empty",
        )
    if len(clean) > MAX_QUESTION_CHARS:
        raise ServiceInputError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "question_too_large",
            "question exceeds the service limit",
        )
    try:
        encoded = clean.encode("utf-8")
    except UnicodeEncodeError:
        encoded = b""
    if not encoded or len(encoded) > MAX_QUESTION_BYTES:
        raise ServiceInputError(
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "question_too_large",
            "question exceeds the service limit",
        )
    if any(
        ord(character) < 32 and character not in {"\t", "\n", "\r"}
        for character in clean
    ):
        raise ServiceInputError(
            HTTPStatus.BAD_REQUEST,
            "invalid_question",
            "question contains unsupported control characters",
        )
    return clean


def _validate_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ServiceInputError(
            HTTPStatus.BAD_REQUEST,
            "invalid_idempotency_key",
            "Idempotency-Key must be a non-empty visible ASCII value",
        )
    encoded = value.encode("utf-8")
    if (
        len(encoded) > MAX_IDEMPOTENCY_KEY_BYTES
        or _IDEMPOTENCY_RE.fullmatch(value) is None
    ):
        raise ServiceInputError(
            HTTPStatus.BAD_REQUEST,
            "invalid_idempotency_key",
            "Idempotency-Key must be at most 128 visible ASCII bytes",
        )
    return value


def _validate_run_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise ServiceInputError(
            HTTPStatus.NOT_FOUND,
            "run_not_found",
            "Council run was not found",
        ) from None


def _request_hash(question: str) -> str:
    canonical = json.dumps(
        {"question": question},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _call_reservation_id(
    principal_id: str,
    run_id: str,
    stage: str,
    provider: str,
    attempt: int,
) -> str:
    material = "\0".join(
        (principal_id, run_id, stage, provider, str(attempt))
    )
    return "call-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_token_file(value: str) -> tuple[str, str]:
    principal_id, separator, path = value.partition("=")
    if not separator or not principal_id or not path:
        raise ValueError("--token-file must use PRINCIPAL=PATH")
    return principal_id, read_private_token(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="council-service",
        description="Run the loopback-only deterministic mock Council service.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--data-dir",
        required=True,
        help="Private directory for the service SQLite database",
    )
    parser.add_argument(
        "--token-file",
        action="append",
        default=[],
        metavar="PRINCIPAL=PATH",
        help="Install a principal token from a private 0600 file",
    )
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--max-queue", type=int, default=DEFAULT_MAX_QUEUE)
    parser.add_argument(
        "--revoke-token",
        metavar="PRINCIPAL",
        help="Revoke all active bearer tokens for one principal and exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    application: CouncilApplication | None = None
    server: CouncilHTTPServer | None = None
    try:
        _validate_loopback_host(args.host)
        if not 0 <= args.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        tokens: dict[str, str] = {}
        for value in args.token_file:
            principal_id, token = _parse_token_file(value)
            if principal_id in tokens:
                raise ValueError(
                    f"duplicate --token-file principal: {principal_id}"
                )
            tokens[principal_id] = token
        if args.revoke_token and tokens:
            raise ValueError(
                "--revoke-token cannot be combined with --token-file"
            )
        application = CouncilApplication(
            Path(args.data_dir),
            bearer_tokens=tokens,
            max_workers=args.max_workers,
            max_queue=args.max_queue,
            recover_jobs=not bool(args.revoke_token),
        )
        if args.revoke_token:
            count = application.revoke_principal_tokens(args.revoke_token)
            print(
                f"revoked {count} active token(s) for {args.revoke_token}",
                flush=True,
            )
            return 0
        server = CouncilHTTPServer(
            (args.host, args.port),
            application,
        )
        host, port = server.server_address[:2]
        print(
            f"council-service mock-only listening on http://{host}:{port}",
            flush=True,
        )
        server.serve_forever(poll_interval=0.25)
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=os.sys.stderr)
        return 2
    finally:
        if server is not None:
            server.server_close()
        if application is not None:
            application.close(wait=True)


__all__ = [
    "CouncilApplication",
    "CouncilHTTPServer",
    "CouncilRequestHandler",
    "CreateRunResult",
    "DurableCallGate",
    "MAX_BODY_BYTES",
    "MAX_IDEMPOTENCY_KEY_BYTES",
    "MAX_QUESTION_BYTES",
    "ServiceInputError",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
