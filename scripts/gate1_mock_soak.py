#!/usr/bin/env python3
"""Mock-only Gate-1 durability and isolation soak verifier."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from typing import Any, Sequence
import uuid

from model_council.access_policy import Principal, six_principals
from model_council.service import CouncilApplication, ServiceInputError
from model_council.service_store import ServiceStore
from model_council.store import CouncilStore


TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled"}


class SoakFailure(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SoakFailure(message)


def _explicit_data_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.is_symlink():
        raise SoakFailure("explicit data directory must not be a symlink")
    if path.exists():
        if not path.is_dir():
            raise SoakFailure("explicit data path must be a directory")
        if any(path.iterdir()):
            raise SoakFailure(
                "explicit data directory must be dedicated and empty"
            )
    return path


def _tokens(principals: list[Principal]) -> dict[str, str]:
    return {
        principal.principal_id: (
            f"gate1-soak-{principal.principal_id}-"
            + secrets.token_urlsafe(32)
        )
        for principal in principals
    }


def run_soak(
    data_dir: Path,
    *,
    jobs: int,
    submission_workers: int,
    service_workers: int,
    max_queue: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    if jobs < 1:
        raise SoakFailure("jobs must be positive")
    if submission_workers < 1 or service_workers < 1:
        raise SoakFailure("worker counts must be positive")
    if max_queue < 0 or timeout_seconds <= 0:
        raise SoakFailure("queue and timeout settings are invalid")

    enabled = [principal for principal in six_principals() if principal.enabled]
    _require(len(enabled) == 4, "expected four enabled personal principals")
    token_map = _tokens(enabled)
    started = time.perf_counter()
    backpressure_retries = 0
    retry_lock = threading.Lock()
    application = CouncilApplication(
        data_dir,
        bearer_tokens=token_map,
        max_workers=service_workers,
        max_queue=max_queue,
    )
    try:
        _require(
            all(
                provider.name.startswith("mock")
                for provider in application.config.providers
            ),
            "service constructed a non-mock provider",
        )
        principals = {
            principal_id: application.authenticate(f"Bearer {token}")
            for principal_id, token in token_map.items()
        }
        _require(
            all(principal is not None for principal in principals.values()),
            "an enabled soak principal did not authenticate",
        )

        batch_id = uuid.uuid4().hex
        expected_owners: dict[str, str] = {}

        def submit(index: int) -> tuple[str, str]:
            nonlocal backpressure_retries
            principal_id = enabled[index % len(enabled)].principal_id
            principal = principals[principal_id]
            assert principal is not None
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    accepted = application.create_run(
                        principal,
                        question=(
                            "Gate-1 deterministic mock soak job "
                            f"{index:04d}."
                        ),
                        idempotency_key=(
                            f"gate1-soak-{batch_id}-{index:04d}"
                        ),
                    )
                    _require(
                        accepted.status in {200, 202},
                        f"job {index} returned HTTP {accepted.status}",
                    )
                    return str(accepted.payload["run_id"]), principal_id
                except ServiceInputError as error:
                    if (
                        error.code != "queue_full"
                        or time.monotonic() >= deadline
                    ):
                        raise
                    with retry_lock:
                        backpressure_retries += 1
                    time.sleep(0.005)

        with ThreadPoolExecutor(
            max_workers=submission_workers,
            thread_name_prefix="gate1-soak-submit",
        ) as executor:
            accepted = list(executor.map(submit, range(jobs)))
        for run_id, principal_id in accepted:
            _require(
                run_id not in expected_owners,
                f"duplicate accepted run_id: {run_id}",
            )
            expected_owners[run_id] = principal_id

        terminal: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + timeout_seconds
        while len(terminal) < jobs:
            for run_id, principal_id in expected_owners.items():
                if run_id in terminal:
                    continue
                principal = principals[principal_id]
                assert principal is not None
                payload = application.get_run(principal, run_id)
                if payload["status"] in TERMINAL_STATUSES:
                    terminal[run_id] = payload
            if len(terminal) == jobs:
                break
            if time.monotonic() >= deadline:
                raise SoakFailure(
                    f"timed out with {jobs - len(terminal)} nonterminal jobs"
                )
            time.sleep(0.01)

        statuses = Counter(str(payload["status"]) for payload in terminal.values())
        _require(
            statuses == Counter({"completed": jobs}),
            f"unexpected terminal statuses: {dict(statuses)}",
        )
        for run_id, payload in terminal.items():
            result = payload.get("result")
            _require(
                isinstance(result, dict)
                and result.get("run_id") == run_id
                and result.get("status") == "completed",
                f"run {run_id} lacks a valid completed result",
            )

        principal_ids = list(principals)
        for run_id, owner in expected_owners.items():
            _require(
                application.service_store.run_owner(run_id) == owner,
                f"run {run_id} has incorrect durable owner",
            )
            other_id = next(
                principal_id
                for principal_id in principal_ids
                if principal_id != owner
            )
            other = principals[other_id]
            assert other is not None
            try:
                application.get_run(other, run_id)
            except ServiceInputError as error:
                _require(
                    error.status == 404 and error.code == "run_not_found",
                    f"cross-principal read leaked run {run_id}",
                )
            else:
                raise SoakFailure(
                    f"cross-principal read exposed run {run_id}"
                )

        db_path = application.council_store.db_path
        with closing(sqlite3.connect(db_path)) as connection:
            run_count = connection.execute(
                "SELECT COUNT(*) FROM runs"
            ).fetchone()[0]
            binding_count = connection.execute(
                "SELECT COUNT(*) FROM service_run_bindings"
            ).fetchone()[0]
            orphan_bindings = connection.execute(
                """
                SELECT COUNT(*)
                FROM service_run_bindings AS b
                LEFT JOIN runs AS r ON r.id = b.run_id
                WHERE r.id IS NULL
                """
            ).fetchone()[0]
            unbound_runs = connection.execute(
                """
                SELECT COUNT(*)
                FROM runs AS r
                LEFT JOIN service_run_bindings AS b ON b.run_id = r.id
                WHERE b.run_id IS NULL
                """
            ).fetchone()[0]
            reservation_count, reconciled_count, units, unresolved = (
                connection.execute(
                    """
                    SELECT
                        COUNT(*),
                        SUM(CASE WHEN state = 'reconciled' THEN 1 ELSE 0 END),
                        COALESCE(SUM(actual_units), 0),
                        SUM(CASE WHEN state != 'reconciled' THEN 1 ELSE 0 END)
                    FROM service_call_reservations
                    """
                ).fetchone()
            )
            per_run = connection.execute(
                """
                SELECT run_id, COUNT(*), SUM(actual_units),
                       SUM(CASE WHEN state != 'reconciled' THEN 1 ELSE 0 END)
                FROM service_call_reservations
                GROUP BY run_id
                """
            ).fetchall()
            integrity = connection.execute(
                "PRAGMA integrity_check"
            ).fetchall()
            foreign_key_violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()

        _require(run_count == jobs, f"expected {jobs} runs, found {run_count}")
        _require(
            binding_count == jobs,
            f"expected {jobs} bindings, found {binding_count}",
        )
        _require(orphan_bindings == 0, "orphan run binding detected")
        _require(unbound_runs == 0, "service run lacks an ownership binding")
        _require(
            reservation_count == jobs * 9
            and reconciled_count == jobs * 9
            and units == jobs * 9
            and unresolved == 0,
            "global logical-invocation accounting is inconsistent",
        )
        _require(len(per_run) == jobs, "reservation rows omit a run")
        for run_id, count, actual_units, unresolved_count in per_run:
            _require(
                count == 9
                and actual_units == 9
                and unresolved_count == 0,
                f"run {run_id} does not have exactly 9 settled call units",
            )
        _require(integrity == [("ok",)], "SQLite integrity_check failed")
        _require(
            foreign_key_violations == [],
            "SQLite foreign_key_check found violations",
        )

        with tempfile.TemporaryDirectory(
            prefix="model-council-gate1-backup-"
        ) as backup_directory:
            backup_path = Path(backup_directory) / "council-backup.sqlite3"
            with closing(sqlite3.connect(db_path)) as source, closing(
                sqlite3.connect(backup_path)
            ) as destination:
                source.backup(destination)
            backup_store = CouncilStore(db_path=backup_path)
            backup_service_store = ServiceStore(backup_path)
            representative_ids = [
                accepted[0][0],
                accepted[len(accepted) // 2][0],
                accepted[-1][0],
            ]
            for run_id in representative_ids:
                run = backup_store.get_run(run_id)
                _require(
                    run is not None
                    and run["status"] == "completed"
                    and backup_service_store.run_owner(run_id)
                    == expected_owners[run_id],
                    f"backup verification failed for run {run_id}",
                )
            with closing(sqlite3.connect(backup_path)) as connection:
                backup_integrity = connection.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
                backup_foreign_keys = connection.execute(
                    "PRAGMA foreign_key_check"
                ).fetchall()
            _require(
                backup_integrity == [("ok",)] and backup_foreign_keys == [],
                "backup SQLite verification failed",
            )

        try:
            CouncilApplication(data_dir)
        except RuntimeError as error:
            _require(
                "already owns" in str(error),
                "second-writer refusal had an unexpected error",
            )
        else:
            raise SoakFailure("service lock did not fence a second writer")

        elapsed = time.perf_counter() - started
        return {
            "status": "ok",
            "mode": "mock-only",
            "jobs_requested": jobs,
            "jobs_accepted": len(accepted),
            "terminal_statuses": dict(statuses),
            "enabled_principals": principal_ids,
            "ownership_checks": jobs,
            "cross_principal_denials": jobs,
            "run_bindings": binding_count,
            "orphan_bindings": orphan_bindings,
            "unbound_runs": unbound_runs,
            "reservations": reservation_count,
            "reconciled_logical_units": units,
            "unresolved_reservations": unresolved,
            "backpressure_retries": backpressure_retries,
            "integrity_check": "ok",
            "foreign_key_violations": len(foreign_key_violations),
            "backup_representative_runs": 3,
            "backup_integrity_check": "ok",
            "service_lock_fenced_second_writer": True,
            "elapsed_seconds": round(elapsed, 3),
            "limitations": [
                "No provider credentials or provider network were used.",
                "Logical call units are not provider billing or money.",
                "This does not test active-active failover.",
            ],
        }
    finally:
        application.close(wait=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the mock-only Council Gate-1 SQLite soak verifier."
    )
    parser.add_argument(
        "--data-dir",
        help="Dedicated empty directory; defaults to an ephemeral directory",
    )
    parser.add_argument("--jobs", type=int, default=100)
    parser.add_argument("--submission-workers", type=int, default=8)
    parser.add_argument("--service-workers", type=int, default=4)
    parser.add_argument("--max-queue", type=int, default=16)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.data_dir:
            summary = run_soak(
                _explicit_data_dir(args.data_dir),
                jobs=args.jobs,
                submission_workers=args.submission_workers,
                service_workers=args.service_workers,
                max_queue=args.max_queue,
                timeout_seconds=args.timeout_seconds,
            )
        else:
            with tempfile.TemporaryDirectory(
                prefix="model-council-gate1-soak-"
            ) as temporary:
                summary = run_soak(
                    Path(temporary) / "service-data",
                    jobs=args.jobs,
                    submission_workers=args.submission_workers,
                    service_workers=args.service_workers,
                    max_queue=args.max_queue,
                    timeout_seconds=args.timeout_seconds,
                )
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"gate1-mock-soak: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
