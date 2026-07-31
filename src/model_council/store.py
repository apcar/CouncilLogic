from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import os
import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlsplit

from model_council.models import ProviderError, ProviderResponse


CURRENT_SCHEMA_VERSION = 1
DEFAULT_DATABASE_NAME = "council.sqlite3"
_DATABASE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_TERMINAL_RUN_STATUSES = {"succeeded", "completed", "partial", "failed", "cancelled"}
_RAW_CREDENTIAL_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "bws_access_token",
    "authorization",
    "proxy_authorization",
    "password",
    "passwd",
    "client_secret",
    "private_key",
    "secret",
    "secret_value",
    "credential",
    "credentials",
    "bearer",
    "bearer_token",
}
_CREDENTIAL_QUERY_KEYS = {
    "key",
    "api_key",
    "apikey",
    "access_token",
    "token",
    "password",
    "client_secret",
}
SERVICE_MANAGED_MARKER = ".council-service-managed"


def service_managed_data_dir(path: str | os.PathLike[str]) -> bool:
    """Detect service ownership without migrating or otherwise writing."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if candidate.suffix.lower() in _DATABASE_SUFFIXES:
        data_dir = candidate.parent
        db_path = candidate
    else:
        data_dir = candidate
        db_path = candidate / DEFAULT_DATABASE_NAME
    marker = data_dir / SERVICE_MANAGED_MARKER
    if marker.exists() or marker.is_symlink():
        return True
    if not db_path.exists() or db_path.is_symlink():
        return False
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'service_principals'
            """
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _jsonable(value.to_dict())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    raise TypeError(f"value is not JSON serializable: {type(value).__name__}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_json(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _normalized_key(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _assert_endpoint_has_no_credentials(endpoint: str, path: str) -> None:
    parsed = urlsplit(endpoint)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"raw credentials are forbidden at {path}")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if _normalized_key(key) in _CREDENTIAL_QUERY_KEYS:
            raise ValueError(f"credential-bearing endpoint is forbidden at {path}")


def _assert_no_credentials(value: Any, path: str = "$") -> None:
    """Reject credential-bearing fields while allowing references such as secret_name.

    Prompt and response text are intentionally opaque audit material. Callers must
    enforce the same no-secret boundary before those strings reach the store.
    """

    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            normalized = _normalized_key(key_text)
            child_path = f"{path}.{key_text}"
            if normalized in _RAW_CREDENTIAL_KEYS:
                raise ValueError(f"raw credential field is forbidden at {child_path}")
            if normalized == "endpoint" and isinstance(item, str):
                _assert_endpoint_has_no_credentials(item, child_path)
            _assert_no_credentials(item, child_path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_credentials(item, f"{path}[{index}]")


def _required_text(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


class CouncilStore:
    """Durable, thread-safe SQLite record of council runs and provider calls.

    Every public operation opens its own SQLite connection. SQLite WAL mode,
    immediate write transactions, a busy timeout, and database uniqueness
    constraints provide coordination across threads and processes.
    """

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        db_path: str | os.PathLike[str] | None = None,
    ) -> None:
        if path is not None and db_path is not None:
            raise ValueError("pass either path or db_path, not both")

        supplied = db_path if db_path is not None else path
        if supplied is None:
            supplied = os.environ.get(
                "MODEL_COUNCIL_DATA_DIR",
                str(Path.home() / ".local" / "share" / "model-council"),
            )
        candidate = Path(supplied).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate

        explicit_database = db_path is not None or candidate.suffix.lower() in _DATABASE_SUFFIXES
        if explicit_database:
            self.db_path = candidate
            self.data_dir = candidate.parent
        else:
            self.data_dir = candidate
            self.db_path = candidate / DEFAULT_DATABASE_NAME

        self._prepare_filesystem()
        self._migrate()

    def _prepare_filesystem(self) -> None:
        if self.data_dir.is_symlink():
            raise ValueError("data directory must not be a symlink")
        if self.data_dir.exists() and not self.data_dir.is_dir():
            raise ValueError("data directory path is not a directory")
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)

        try:
            metadata = self.db_path.lstat()
        except FileNotFoundError:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(self.db_path, flags, 0o600)
            except FileExistsError:
                metadata = self.db_path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("database path is not a regular file") from None
            else:
                os.close(descriptor)
        else:
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("database path is not a regular file")
        os.chmod(self.db_path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise RuntimeError("SQLite foreign-key enforcement is unavailable")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Expose a correctly configured connection for diagnostics."""

        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
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

    def _migrate(self) -> None:
        connection = self._connect()
        try:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise RuntimeError(f"could not enable SQLite WAL mode: {mode}")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("BEGIN IMMEDIATE")
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {current} is newer than supported "
                    f"{CURRENT_SCHEMA_VERSION}"
                )
            if current < 1:
                self._migration_1(connection)
                connection.execute("PRAGMA user_version = 1")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        os.chmod(self.db_path, 0o600)

    @staticmethod
    def _migration_1(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                question TEXT NOT NULL,
                question_sha256 TEXT NOT NULL,
                protocol_id TEXT NOT NULL,
                protocol_version TEXT NOT NULL,
                protocol_hash TEXT NOT NULL,
                provider_configs_json TEXT NOT NULL,
                policy_json TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                idempotency_key TEXT UNIQUE,
                result_json TEXT,
                result_sha256 TEXT,
                error_json TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS invocations (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                lineage TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
                prompt_text TEXT NOT NULL,
                prompt_sha256 TEXT NOT NULL,
                response_text TEXT,
                response_sha256 TEXT,
                response_json TEXT,
                resolved_model TEXT,
                request_id TEXT,
                usage_json TEXT,
                latency_ms INTEGER,
                attempts INTEGER,
                finish_reason TEXT,
                metadata_json TEXT,
                error_category TEXT,
                error_message TEXT,
                error_retryable INTEGER,
                error_status_code INTEGER,
                error_ambiguous INTEGER,
                error_json TEXT,
                call_count INTEGER NOT NULL DEFAULT 1,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                UNIQUE (run_id, stage, provider)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at DESC)",
            """
            CREATE INDEX IF NOT EXISTS idx_invocations_run_status
            ON invocations(run_id, status)
            """,
            "CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id, id)",
        )
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (?, ?)",
            (1, _utc_now()),
        )

    def create_run(
        self,
        question: str,
        protocol_id: str,
        protocol_version: str,
        protocol_hash: str,
        provider_configs: list[dict[str, Any]],
        policy: dict[str, Any],
        idempotency_key: str | None = None,
        run_id: str | None = None,
    ) -> str:
        question = _required_text("question", question)
        protocol_id = _required_text("protocol_id", protocol_id)
        protocol_version = _required_text("protocol_version", protocol_version)
        protocol_hash = _required_text("protocol_hash", protocol_hash)
        if not isinstance(provider_configs, list):
            raise TypeError("provider_configs must be a list")
        if not isinstance(policy, dict) and not hasattr(policy, "to_dict"):
            raise TypeError("policy must be a dict-like model")
        configs_value = _jsonable(provider_configs)
        policy_value = _jsonable(policy)
        _assert_no_credentials(configs_value, "$.provider_configs")
        _assert_no_credentials(policy_value, "$.policy")

        configs_json = _canonical_json(configs_value)
        policy_json = _canonical_json(policy_value)
        request_value = {
            "question": question,
            "protocol_id": protocol_id,
            "protocol_version": protocol_version,
            "protocol_hash": protocol_hash,
            "provider_configs": configs_value,
            "policy": policy_value,
        }
        request_sha256 = _sha256_text(_canonical_json(request_value))
        if idempotency_key is not None:
            idempotency_key = _required_text("idempotency_key", idempotency_key)

        explicit_run_id = run_id is not None
        run_id = str(uuid.uuid4()) if run_id is None else _required_text(
            "run_id", run_id
        )
        now = _utc_now()
        with self._write_transaction() as connection:
            existing_by_id = connection.execute(
                "SELECT id, request_sha256 FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if existing_by_id is not None:
                if existing_by_id["request_sha256"] != request_sha256:
                    raise ValueError("run_id was already used for another request")
                return str(existing_by_id["id"])
            if idempotency_key is not None:
                existing = connection.execute(
                    "SELECT id, request_sha256 FROM runs WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if existing is not None:
                    if existing["request_sha256"] != request_sha256:
                        raise ValueError("idempotency key was already used for another request")
                    if explicit_run_id and str(existing["id"]) != run_id:
                        raise ValueError(
                            "idempotency key is already bound to another run_id"
                        )
                    return str(existing["id"])
            connection.execute(
                """
                INSERT INTO runs (
                    id, created_at, updated_at, status, question, question_sha256,
                    protocol_id, protocol_version, protocol_hash,
                    provider_configs_json, policy_json, request_sha256, idempotency_key
                ) VALUES (?, ?, ?, 'created', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    now,
                    now,
                    question,
                    _sha256_text(question),
                    protocol_id,
                    protocol_version,
                    protocol_hash,
                    configs_json,
                    policy_json,
                    request_sha256,
                    idempotency_key,
                ),
            )
        return run_id

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
            "status": row["status"],
            "question": row["question"],
            "question_sha256": row["question_sha256"],
            "protocol_id": row["protocol_id"],
            "protocol_version": row["protocol_version"],
            "protocol_hash": row["protocol_hash"],
            "provider_configs": _decode_json(row["provider_configs_json"], []),
            "policy": _decode_json(row["policy_json"], {}),
            "request_sha256": row["request_sha256"],
            "idempotency_key": row["idempotency_key"],
            "result": _decode_json(row["result_json"], None),
            "result_sha256": row["result_sha256"],
            "error": _decode_json(row["error_json"], None),
        }

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return None if row is None else self._run_from_row(row)

    def list_runs(self, limit: int | None = None) -> list[dict[str, Any]]:
        parameters: tuple[Any, ...] = ()
        query = "SELECT * FROM runs ORDER BY created_at DESC, id DESC"
        if limit is not None:
            if not isinstance(limit, int) or limit < 1:
                raise ValueError("limit must be a positive integer")
            query += " LIMIT ?"
            parameters = (limit,)
        with self.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._run_from_row(row) for row in rows]

    @staticmethod
    def _error_value(error: Any) -> Any:
        if error is None:
            return None
        if isinstance(error, ProviderError):
            return error.to_dict()
        if isinstance(error, dict):
            return _jsonable(error)
        if isinstance(error, BaseException):
            return {"message": str(error), "type": type(error).__name__}
        return {"message": str(error)}

    def set_run_status(self, run_id: str, status: str, error: Any = None) -> None:
        status = _required_text("status", status)
        error_value = self._error_value(error)
        _assert_no_credentials(error_value, "$.error")
        error_json = None if error_value is None else _canonical_json(error_value)
        now = _utc_now()
        finished_at = now if status in _TERMINAL_RUN_STATUSES else None
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET status = ?, error_json = ?, updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, error_json, now, finished_at, run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown run: {run_id}")

    def save_result(self, run_id: str, result: Any) -> None:
        result_value = _jsonable(result)
        _assert_no_credentials(result_value, "$.result")
        result_json = _canonical_json(result_value)
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET result_json = ?, result_sha256 = ?, updated_at = ?
                WHERE id = ?
                """,
                (result_json, _sha256_text(result_json), _utc_now(), run_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown run: {run_id}")

    def finish_run(self, run_id: str, result: Any) -> None:
        """Atomically persist a terminal result, status, and audit event."""

        result_value = _jsonable(result)
        if not isinstance(result_value, dict):
            raise TypeError("terminal result must be an object")
        status = _required_text("result.status", result_value.get("status"))
        if status not in _TERMINAL_RUN_STATUSES:
            raise ValueError("result status must be terminal")
        _assert_no_credentials(result_value, "$.result")
        result_json = _canonical_json(result_value)
        event_value = {
            "status": status,
            "failure_count": len(result_value.get("failures") or []),
        }
        event_json = _canonical_json(event_value)
        now = _utc_now()
        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE runs
                SET result_json = ?, result_sha256 = ?, status = ?,
                    error_json = NULL, updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    result_json,
                    _sha256_text(result_json),
                    status,
                    now,
                    now,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown run: {run_id}")
            connection.execute(
                """
                INSERT INTO events (
                    run_id, event_type, payload_json, payload_sha256, created_at
                ) VALUES (?, 'run_finished', ?, ?, ?)
                """,
                (
                    run_id,
                    event_json,
                    _sha256_text(event_json),
                    now,
                ),
            )

    def append_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> int:
        event_type = _required_text("event_type", event_type)
        if not isinstance(payload, dict):
            raise TypeError("event payload must be a dict")
        payload_value = _jsonable(payload)
        _assert_no_credentials(payload_value, "$.event")
        payload_json = _canonical_json(payload_value)
        with self._write_transaction() as connection:
            if connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
                raise KeyError(f"unknown run: {run_id}")
            cursor = connection.execute(
                """
                INSERT INTO events (
                    run_id, event_type, payload_json, payload_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    event_type,
                    payload_json,
                    _sha256_text(payload_json),
                    _utc_now(),
                ),
            )
        return int(cursor.lastrowid)

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "event_type": row["event_type"],
                "payload": _decode_json(row["payload_json"], {}),
                "payload_sha256": row["payload_sha256"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def start_invocation(
        self,
        run_id: str,
        stage: str,
        provider: str,
        model: str,
        lineage: str,
        prompt: str,
    ) -> str:
        stage = _required_text("stage", stage)
        provider = _required_text("provider", provider)
        model = _required_text("model", model)
        lineage = _required_text("lineage", lineage)
        prompt = _required_text("prompt", prompt)
        prompt_sha256 = _sha256_text(prompt)
        now = _utc_now()

        with self._write_transaction() as connection:
            if connection.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
                raise KeyError(f"unknown run: {run_id}")
            existing = connection.execute(
                """
                SELECT * FROM invocations
                WHERE run_id = ? AND stage = ? AND provider = ?
                """,
                (run_id, stage, provider),
            ).fetchone()
            if existing is not None:
                if (
                    existing["model"] != model
                    or existing["lineage"] != lineage
                    or existing["prompt_sha256"] != prompt_sha256
                ):
                    raise ValueError(
                        "stage/provider invocation already exists with different input"
                    )
                invocation_id = str(existing["id"])
                if existing["status"] in {"running", "succeeded"}:
                    return invocation_id
                prior_failure = _decode_json(
                    existing["error_json"],
                    {
                        "message": existing["error_message"],
                        "category": existing["error_category"],
                        "retryable": bool(existing["error_retryable"]),
                        "status_code": existing["error_status_code"],
                        "request_id": existing["request_id"],
                        "attempts": existing["attempts"],
                        "ambiguous": bool(existing["error_ambiguous"]),
                    },
                )
                retry_kind = (
                    "truncation"
                    if str(prior_failure.get("message") or "").endswith(
                        "(finish_reason=length)"
                    )
                    else "application"
                )
                retry_event = {
                    "stage": stage,
                    "provider": provider,
                    "retry_call_count": int(existing["call_count"]) + 1,
                    "retry_kind": retry_kind,
                    "prior_failure": prior_failure,
                }
                retry_event_json = _canonical_json(retry_event)
                connection.execute(
                    """
                    INSERT INTO events (
                        run_id, event_type, payload_json, payload_sha256,
                        created_at
                    ) VALUES (?, 'provider_retry_started', ?, ?, ?)
                    """,
                    (
                        run_id,
                        retry_event_json,
                        _sha256_text(retry_event_json),
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE invocations
                    SET status = 'running', response_text = NULL,
                        response_sha256 = NULL, response_json = NULL,
                        resolved_model = NULL, request_id = NULL,
                        usage_json = NULL, latency_ms = NULL, attempts = NULL,
                        finish_reason = NULL, metadata_json = NULL,
                        error_category = NULL, error_message = NULL,
                        error_retryable = NULL, error_status_code = NULL,
                        error_ambiguous = NULL, error_json = NULL,
                        call_count = call_count + 1,
                        started_at = ?, updated_at = ?, finished_at = NULL
                    WHERE id = ?
                    """,
                    (now, now, invocation_id),
                )
                return invocation_id

            invocation_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO invocations (
                    id, run_id, stage, provider, model, lineage, status,
                    prompt_text, prompt_sha256, started_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    invocation_id,
                    run_id,
                    stage,
                    provider,
                    model,
                    lineage,
                    prompt,
                    prompt_sha256,
                    now,
                    now,
                ),
            )
        return invocation_id

    def finish_invocation_success(
        self,
        invocation_id: str,
        response: ProviderResponse,
    ) -> None:
        if not isinstance(response, ProviderResponse):
            raise TypeError("response must be ProviderResponse")
        response_value = response.to_dict()
        _assert_no_credentials(response_value.get("metadata", {}), "$.response.metadata")
        response_json = _canonical_json(response_value)
        usage_json = _canonical_json(response.usage.to_dict())
        metadata_json = _canonical_json(response.metadata)
        response_sha256 = _sha256_text(response.content)
        now = _utc_now()

        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT status, response_json FROM invocations WHERE id = ?",
                (invocation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown invocation: {invocation_id}")
            if row["status"] == "succeeded":
                if row["response_json"] != response_json:
                    raise ValueError("invocation already has a different successful response")
                return
            if row["status"] != "running":
                raise RuntimeError("failed invocation must be restarted before success")
            connection.execute(
                """
                UPDATE invocations
                SET status = 'succeeded', response_text = ?, response_sha256 = ?,
                    response_json = ?, resolved_model = ?, request_id = ?,
                    usage_json = ?, latency_ms = ?, attempts = ?,
                    finish_reason = ?, metadata_json = ?,
                    error_category = NULL, error_message = NULL,
                    error_retryable = NULL, error_status_code = NULL,
                    error_ambiguous = NULL, error_json = NULL,
                    updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    response.content,
                    response_sha256,
                    response_json,
                    response.resolved_model,
                    response.request_id,
                    usage_json,
                    response.latency_ms,
                    response.attempts,
                    response.finish_reason,
                    metadata_json,
                    now,
                    now,
                    invocation_id,
                ),
            )

    def finish_invocation_failure(
        self,
        invocation_id: str,
        error: ProviderError,
    ) -> None:
        if not isinstance(error, ProviderError):
            raise TypeError("error must be ProviderError")
        error_value = error.to_dict()
        _assert_no_credentials(error_value, "$.provider_error")
        error_json = _canonical_json(error_value)
        now = _utc_now()

        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT status, error_json FROM invocations WHERE id = ?",
                (invocation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown invocation: {invocation_id}")
            if row["status"] == "failed":
                if row["error_json"] != error_json:
                    raise ValueError("invocation already has a different failure")
                return
            if row["status"] != "running":
                raise RuntimeError("successful invocation cannot be overwritten by failure")
            connection.execute(
                """
                UPDATE invocations
                SET status = 'failed', response_text = NULL,
                    response_sha256 = NULL, response_json = NULL,
                    resolved_model = NULL, request_id = ?,
                    usage_json = NULL, latency_ms = NULL, attempts = ?,
                    finish_reason = NULL, metadata_json = NULL,
                    error_category = ?, error_message = ?,
                    error_retryable = ?, error_status_code = ?,
                    error_ambiguous = ?, error_json = ?,
                    updated_at = ?, finished_at = ?
                WHERE id = ?
                """,
                (
                    error.request_id,
                    error.attempts,
                    error.category.value,
                    str(error),
                    int(error.retryable),
                    error.status_code,
                    int(error.ambiguous),
                    error_json,
                    now,
                    now,
                    invocation_id,
                ),
            )

    @staticmethod
    def _invocation_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "invocation_id": row["id"],
            "run_id": row["run_id"],
            "stage": row["stage"],
            "provider": row["provider"],
            "model": row["model"],
            "lineage": row["lineage"],
            "status": row["status"],
            "prompt_text": row["prompt_text"],
            "prompt_sha256": row["prompt_sha256"],
            "response_text": row["response_text"],
            "response_sha256": row["response_sha256"],
            "response": _decode_json(row["response_json"], None),
            "resolved_model": row["resolved_model"],
            "request_id": row["request_id"],
            "usage": _decode_json(row["usage_json"], {}),
            "latency_ms": row["latency_ms"],
            "attempts": row["attempts"],
            "finish_reason": row["finish_reason"],
            "metadata": _decode_json(row["metadata_json"], {}),
            "error_category": row["error_category"],
            "error_message": row["error_message"],
            "error_retryable": (
                None if row["error_retryable"] is None else bool(row["error_retryable"])
            ),
            "error_status_code": row["error_status_code"],
            "error_ambiguous": (
                None if row["error_ambiguous"] is None else bool(row["error_ambiguous"])
            ),
            "error": _decode_json(row["error_json"], None),
            "call_count": row["call_count"],
            "started_at": row["started_at"],
            "updated_at": row["updated_at"],
            "finished_at": row["finished_at"],
        }

    def get_successful_invocation(
        self,
        run_id: str,
        stage: str,
        provider: str,
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM invocations
                WHERE run_id = ? AND stage = ? AND provider = ?
                  AND status = 'succeeded'
                """,
                (run_id, stage, provider),
            ).fetchone()
        return None if row is None else self._invocation_from_row(row)

    def get_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM invocations WHERE id = ?",
                (invocation_id,),
            ).fetchone()
        return None if row is None else self._invocation_from_row(row)

    def list_invocations(self, run_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM invocations
                WHERE run_id = ?
                ORDER BY started_at, id
                """,
                (run_id,),
            ).fetchall()
        return [self._invocation_from_row(row) for row in rows]

    def count_calls(self, run_id: str) -> int:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(call_count), 0) FROM invocations WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0])

    def close(self) -> None:
        """Compatibility no-op: the store intentionally keeps no shared connection."""
