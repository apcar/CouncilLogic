from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import hashlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.cli import main as cli_main  # noqa: E402
from model_council.config import default_config, mock_config  # noqa: E402
from model_council.remote import (  # noqa: E402
    RemoteCouncilClient,
    RemoteCouncilError,
)
from model_council.run_lock import ServiceLock  # noqa: E402
from model_council.service import (  # noqa: E402
    CouncilApplication,
    CouncilHTTPServer,
    DurableCallGate,
    MAX_BODY_BYTES,
    ServiceInputError,
    main as service_main,
)
from model_council.models import ProviderError  # noqa: E402
from model_council.service_store import ServiceStore  # noqa: E402
from model_council.store import CouncilStore  # noqa: E402


HUMAN_TOKEN = "human-principal-token-" + ("a" * 40)
AGENT_TOKEN = "agent-principal-token-" + ("b" * 40)
WORK_TOKEN = "work-principal-token-" + ("c" * 40)


class ServiceFixture:
    def __init__(
        self,
        directory: Path,
        *,
        max_workers: int = 2,
        max_queue: int = 8,
    ) -> None:
        self.application = CouncilApplication(
            directory,
            bearer_tokens={
                "personal-laptop-human": HUMAN_TOKEN,
                "personal-laptop-agent": AGENT_TOKEN,
                "work-laptop-human": WORK_TOKEN,
            },
            max_workers=max_workers,
            max_queue=max_queue,
        )
        self.server = CouncilHTTPServer(
            ("127.0.0.1", 0),
            self.application,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.application.close(wait=True)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, object]]:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_port,
            timeout=3,
        )
        connection.request(
            method,
            path,
            body=body,
            headers=headers or {},
        )
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, json.loads(raw)


class CouncilServiceHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.fixture = ServiceFixture(self.directory)

    def tearDown(self) -> None:
        self.fixture.close()
        self.temporary.cleanup()

    def _client(self, token: str) -> RemoteCouncilClient:
        return RemoteCouncilClient(self.fixture.base_url, token)

    def test_authentication_owner_scoped_reads_and_disabled_work(self) -> None:
        human = self._client(HUMAN_TOKEN)
        agent = self._client(AGENT_TOKEN)
        created = human.create_run(
            "Keep this run private to its owner.",
            idempotency_key="owner-boundary-1",
        )

        with self.assertRaises(RemoteCouncilError) as cross_read:
            agent.get_run(created["run_id"])
        self.assertEqual(cross_read.exception.status, 404)
        self.assertEqual(cross_read.exception.code, "run_not_found")

        status, unauthenticated = self.fixture.request(
            "GET",
            f"/v1/runs/{created['run_id']}",
        )
        self.assertEqual(status, 401)
        self.assertEqual(
            unauthenticated["error"]["code"],  # type: ignore[index]
            "unauthorized",
        )

        with self.assertRaises(RemoteCouncilError) as disabled_work:
            self._client(WORK_TOKEN).create_run(
                "Synthetic work request",
                idempotency_key="disabled-work-1",
            )
        self.assertEqual(disabled_work.exception.status, 401)

    def test_idempotency_is_owner_scoped_and_conflicts_fail(self) -> None:
        human = self._client(HUMAN_TOKEN)
        agent = self._client(AGENT_TOKEN)

        first = human.create_run(
            "Exact retry",
            idempotency_key="shared-key",
        )
        replay = human.create_run(
            "Exact retry",
            idempotency_key="shared-key",
        )
        other_owner = agent.create_run(
            "Exact retry",
            idempotency_key="shared-key",
        )

        self.assertEqual(first["run_id"], replay["run_id"])
        self.assertTrue(replay["replayed"])
        self.assertNotEqual(first["run_id"], other_owner["run_id"])
        with self.assertRaises(RemoteCouncilError) as conflict:
            human.create_run(
                "Changed request",
                idempotency_key="shared-key",
            )
        self.assertEqual(conflict.exception.status, 409)
        self.assertEqual(conflict.exception.code, "idempotency_conflict")

    def test_mock_run_completes_with_durable_reconciled_call_units(self) -> None:
        client = self._client(HUMAN_TOKEN)
        created = client.create_run(
            "Complete the offline council.",
            idempotency_key="durable-units-1",
        )
        completed = client.wait(
            created["run_id"],
            timeout_seconds=5,
            poll_seconds=0.01,
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["result"]["status"], "completed")

        with closing(
            sqlite3.connect(
                self.fixture.application.council_store.db_path
            )
        ) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*), SUM(actual_units)
                FROM service_call_reservations
                WHERE run_id = ? AND state = 'reconciled'
                """,
                (created["run_id"],),
            ).fetchone()
        self.assertEqual(row, (9, 9))

    def test_strict_json_content_type_duplicate_keys_and_body_limit(self) -> None:
        authorization = {"Authorization": f"Bearer {HUMAN_TOKEN}"}
        status, unsupported = self.fixture.request(
            "POST",
            "/v1/runs",
            body=b'{"question":"hello"}',
            headers={
                **authorization,
                "Content-Type": "text/plain",
                "Idempotency-Key": "strict-1",
            },
        )
        self.assertEqual(status, 415)
        self.assertEqual(
            unsupported["error"]["code"],  # type: ignore[index]
            "unsupported_media_type",
        )

        duplicate = b'{"question":"first","question":"second"}'
        status, malformed = self.fixture.request(
            "POST",
            "/v1/runs",
            body=duplicate,
            headers={
                **authorization,
                "Content-Type": "application/json",
                "Idempotency-Key": "strict-2",
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            malformed["error"]["code"],  # type: ignore[index]
            "invalid_json",
        )

        oversized = b"{" + (b" " * MAX_BODY_BYTES) + b"}"
        status, too_large = self.fixture.request(
            "POST",
            "/v1/runs",
            body=oversized,
            headers={
                **authorization,
                "Content-Type": "application/json",
                "Idempotency-Key": "strict-3",
            },
        )
        self.assertEqual(status, 413)
        self.assertEqual(
            too_large["error"]["code"],  # type: ignore[index]
            "body_too_large",
        )

    def test_question_and_idempotency_limits_fail_before_run_creation(
        self,
    ) -> None:
        authorization = {"Authorization": f"Bearer {HUMAN_TOKEN}"}
        multibyte = json.dumps(
            {"question": "é" * 20_000},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        status, question_error = self.fixture.request(
            "POST",
            "/v1/runs",
            body=multibyte,
            headers={
                **authorization,
                "Content-Type": "application/json",
                "Idempotency-Key": "question-byte-limit",
            },
        )
        self.assertEqual(status, 413)
        self.assertEqual(
            question_error["error"]["code"],  # type: ignore[index]
            "question_too_large",
        )

        status, key_error = self.fixture.request(
            "POST",
            "/v1/runs",
            body=b'{"question":"valid"}',
            headers={
                **authorization,
                "Content-Type": "application/json",
                "Idempotency-Key": "k" * 129,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(
            key_error["error"]["code"],  # type: ignore[index]
            "invalid_idempotency_key",
        )
        self.assertEqual(
            self.fixture.application.council_store.list_runs(),
            [],
        )

    def test_health_discloses_no_principal_or_run_data(self) -> None:
        status, payload = self.fixture.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "mock")
        self.assertTrue(payload["ready"])
        self.assertNotIn("principal", json.dumps(payload))
        self.assertNotIn("run_id", payload)


class CouncilServiceLifecycleTests(unittest.TestCase):
    def test_idempotency_and_ownership_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = ServiceFixture(directory)
            client = RemoteCouncilClient(first.base_url, HUMAN_TOKEN)
            created = client.create_run(
                "Persist this binding.",
                idempotency_key="restart-1",
            )
            client.wait(
                created["run_id"],
                timeout_seconds=5,
                poll_seconds=0.01,
            )
            first.close()

            second = ServiceFixture(directory)
            try:
                replayed = RemoteCouncilClient(
                    second.base_url,
                    HUMAN_TOKEN,
                ).create_run(
                    "Persist this binding.",
                    idempotency_key="restart-1",
                )
                self.assertEqual(replayed["run_id"], created["run_id"])
                self.assertTrue(replayed["replayed"])
            finally:
                second.close()

    def test_bounded_queue_rejects_without_creating_a_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
                max_workers=1,
                max_queue=0,
            )
            started = threading.Event()
            release = threading.Event()

            def block_run(principal: object, run_id: str) -> None:
                started.set()
                release.wait(timeout=3)

            application._execute_run = block_run  # type: ignore[method-assign]
            principal = application.authenticate(f"Bearer {HUMAN_TOKEN}")
            self.assertIsNotNone(principal)
            first = application.create_run(
                principal,  # type: ignore[arg-type]
                question="Occupy the sole worker.",
                idempotency_key="queue-first",
            )
            self.assertTrue(started.wait(timeout=1))
            with self.assertRaises(ServiceInputError) as full:
                application.create_run(
                    principal,  # type: ignore[arg-type]
                    question="This must not be accepted.",
                    idempotency_key="queue-second",
                )
            self.assertEqual(full.exception.code, "queue_full")
            self.assertIsNone(
                application.service_store.find_run_binding(
                    principal_id="personal-laptop-human",
                    idempotency_key="queue-second",
                    payload_hash="unused",
                )
            )
            self.assertEqual(first.payload["status"], "queued")
            release.set()
            application.close(wait=True)

    def test_concurrent_exact_retries_create_only_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
                max_workers=1,
                max_queue=0,
            )
            started = threading.Event()
            release = threading.Event()

            def block_run(principal: object, run_id: str) -> None:
                started.set()
                release.wait(timeout=3)

            application._execute_run = block_run  # type: ignore[method-assign]
            try:
                principal = application.authenticate(f"Bearer {HUMAN_TOKEN}")
                self.assertIsNotNone(principal)

                def create(_: int) -> object:
                    return application.create_run(
                        principal,  # type: ignore[arg-type]
                        question="One logical request.",
                        idempotency_key="concurrent-retry",
                    )

                with ThreadPoolExecutor(max_workers=8) as executor:
                    results = list(executor.map(create, range(8)))
                self.assertTrue(started.wait(timeout=1))
                run_ids = {
                    result.payload["run_id"]  # type: ignore[attr-defined]
                    for result in results
                }
                self.assertEqual(len(run_ids), 1)
                self.assertEqual(
                    len(application.council_store.list_runs()),
                    1,
                )
                self.assertEqual(
                    sum(
                        not result.payload[  # type: ignore[attr-defined]
                            "replayed"
                        ]
                        for result in results
                    ),
                    1,
                )
            finally:
                release.set()
                application.close(wait=True)

    def test_orphaned_binding_is_repaired_after_interrupted_creation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
            )
            try:
                principal = application.authenticate(f"Bearer {HUMAN_TOKEN}")
                self.assertIsNotNone(principal)
                orphan_run_id = str(uuid.uuid4())
                question = "Recover an interrupted creation."
                canonical = json.dumps(
                    {"question": question},
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                payload_hash = hashlib.sha256(
                    canonical.encode("utf-8")
                ).hexdigest()
                application.service_store.bind_run(
                    principal_id="personal-laptop-human",
                    idempotency_key="orphan-recovery",
                    run_id=orphan_run_id,
                    payload_hash=payload_hash,
                )

                created = application.create_run(
                    principal,  # type: ignore[arg-type]
                    question=question,
                    idempotency_key="orphan-recovery",
                )
                self.assertEqual(
                    created.payload["run_id"],
                    orphan_run_id,
                )
                self.assertEqual(created.status, 202)
            finally:
                application.close(wait=True)

    def test_replayed_reservation_cannot_authorize_duplicate_execution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
            )
            try:
                principal = application.authenticate(f"Bearer {HUMAN_TOKEN}")
                self.assertIsNotNone(principal)
                run_id = str(uuid.uuid4())
                application.service_store.bind_run(
                    principal_id="personal-laptop-human",
                    idempotency_key="reservation-replay",
                    run_id=run_id,
                    payload_hash="reservation-replay-hash",
                )
                gate = DurableCallGate(
                    store=application.service_store,
                    principal=principal,  # type: ignore[arg-type]
                    policy_id=application._policy_ids[
                        "personal-laptop-human"
                    ],
                )
                provider = application.config.providers[0]
                lease = gate.reserve(
                    run_id=run_id,
                    stage="proposal",
                    provider=provider,
                    attempt=1,
                )
                with self.assertRaises(ProviderError) as replay:
                    gate.reserve(
                        run_id=run_id,
                        stage="proposal",
                        provider=provider,
                        attempt=1,
                    )
                self.assertTrue(replay.exception.ambiguous)
                lease.reconcile(1)
            finally:
                application.close(wait=True)

    def test_service_refuses_live_provider_configuration_and_non_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            live = default_config()
            live = replace(live, data_dir=Path(temporary))
            with self.assertRaisesRegex(ValueError, "mock-only"):
                CouncilApplication(temporary, config=live)

            application = CouncilApplication(temporary)
            try:
                with self.assertRaisesRegex(ValueError, "loopback"):
                    CouncilHTTPServer(("0.0.0.0", 0), application)
            finally:
                application.close()

    def test_fifty_same_key_submissions_make_one_run_and_nine_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
                max_workers=2,
                max_queue=4,
            )
            try:
                principal = application.authenticate(f"Bearer {HUMAN_TOKEN}")
                self.assertIsNotNone(principal)

                def create(_: int) -> object:
                    return application.create_run(
                        principal,  # type: ignore[arg-type]
                        question="Fifty retries are one logical request.",
                        idempotency_key="fifty-same-key",
                    )

                with ThreadPoolExecutor(max_workers=50) as executor:
                    results = list(executor.map(create, range(50)))
                run_ids = {
                    result.payload["run_id"]  # type: ignore[attr-defined]
                    for result in results
                }
                self.assertEqual(len(run_ids), 1)
                run_id = run_ids.pop()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    run = application.council_store.get_run(run_id)
                    if run and run["status"] == "completed":
                        break
                    time.sleep(0.01)
                self.assertEqual(
                    application.council_store.get_run(run_id)["status"],
                    "completed",
                )
                with closing(
                    sqlite3.connect(application.council_store.db_path)
                ) as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM runs"
                        ).fetchone()[0],
                        1,
                    )
                    self.assertEqual(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM service_call_reservations
                            WHERE run_id = ?
                            """,
                            (run_id,),
                        ).fetchone()[0],
                        9,
                    )
            finally:
                application.close()

    def test_startup_recovers_queued_job_and_running_call_is_uncertain(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
                recover_jobs=False,
            )
            principal = first.authenticate(f"Bearer {HUMAN_TOKEN}")
            self.assertIsNotNone(principal)
            run_id = str(uuid.uuid4())
            question = "Recover without blindly retrying an uncertain call."
            first.service_store.bind_run(
                principal_id="personal-laptop-human",
                idempotency_key="crash-recovery",
                run_id=run_id,
                payload_hash=hashlib.sha256(
                    json.dumps(
                        {"question": question},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            )
            engine = first._new_engine(principal)  # type: ignore[arg-type]
            engine.create_run(question, run_id=run_id)
            first.council_store.set_run_status(run_id, "running")
            provider = first.config.providers[0]
            invocation_id = first.council_store.start_invocation(
                run_id=run_id,
                stage="proposal",
                provider=provider.name,
                model=provider.model,
                lineage=provider.lineage,
                prompt="synthetic crash boundary",
            )
            gate = DurableCallGate(
                store=first.service_store,
                principal=principal,  # type: ignore[arg-type]
                policy_id=first._policy_ids["personal-laptop-human"],
            )
            gate.reserve(
                run_id=run_id,
                stage="proposal",
                provider=provider,
                attempt=1,
            )
            first.close()

            second = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    run = second.council_store.get_run(run_id)
                    if run and run["status"] in {
                        "completed",
                        "partial",
                        "failed",
                    }:
                        break
                    time.sleep(0.01)
                invocation = second.council_store.get_invocation(
                    invocation_id
                )
                self.assertEqual(invocation["status"], "failed")
                self.assertTrue(invocation["error_ambiguous"])
                self.assertEqual(invocation["call_count"], 1)
                with closing(
                    sqlite3.connect(second.council_store.db_path)
                ) as connection:
                    state = connection.execute(
                        """
                        SELECT state FROM service_call_reservations
                        WHERE run_id = ? AND provider = ?
                          AND action = 'provider:invoke'
                        ORDER BY reserved_at LIMIT 1
                        """,
                        (run_id, provider.name),
                    ).fetchone()[0]
                self.assertEqual(state, "reserved")
            finally:
                second.close()

    def test_second_process_service_lock_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = CouncilApplication(temporary)
            try:
                script = (
                    "from model_council.service import CouncilApplication\n"
                    "import sys\n"
                    "try:\n"
                    "    CouncilApplication(sys.argv[1])\n"
                    "except RuntimeError as exc:\n"
                    "    print(exc)\n"
                    "    raise SystemExit(0)\n"
                    "raise SystemExit(1)\n"
                )
                result = subprocess.run(
                    [sys.executable, "-c", script, temporary],
                    cwd=PROJECT_ROOT,
                    env={
                        **os.environ,
                        "PYTHONPATH": str(PROJECT_ROOT / "src"),
                    },
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
                self.assertIn("already owns", result.stdout)
            finally:
                application.close()

    def test_revoked_policy_survives_restart_and_denies_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
            )
            policy_id = first._policy_ids["personal-laptop-human"]
            principal = first.authenticate(f"Bearer {HUMAN_TOKEN}")
            self.assertIsNotNone(principal)
            created = first.create_run(
                principal,  # type: ignore[arg-type]
                question="Create before revocation.",
                idempotency_key="before-revocation",
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                run = first.council_store.get_run(created.payload["run_id"])
                if run and run["status"] == "completed":
                    break
                time.sleep(0.01)
            revoked = first.service_store.revoke_policy(policy_id)
            first.close()

            second = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
            )
            try:
                principal = second.authenticate(f"Bearer {HUMAN_TOKEN}")
                self.assertIsNotNone(principal)
                with self.assertRaises(ServiceInputError) as denied:
                    second.create_run(
                        principal,  # type: ignore[arg-type]
                        question="Revoked policy must stay revoked.",
                        idempotency_key="revoked-policy",
                    )
                self.assertEqual(denied.exception.code, "policy_revoked")
                with self.assertRaises(ServiceInputError) as read_denied:
                    second.get_run(
                        principal,  # type: ignore[arg-type]
                        created.payload["run_id"],
                    )
                self.assertEqual(
                    read_denied.exception.code,
                    "policy_revoked",
                )
                persisted = second.service_store.put_policy(
                    replace(revoked, revoked_at=None)
                )
                self.assertEqual(persisted.revoked_at, revoked.revoked_at)
            finally:
                second.close()

    def test_local_cli_refuses_service_managed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = CouncilApplication(temporary)
            application.close()
            commands = (
                ["doctor"],
                ["run", "--question", "blocked"],
                ["resume", str(uuid.uuid4())],
                ["inspect", str(uuid.uuid4())],
                ["list"],
                [
                    "export",
                    str(uuid.uuid4()),
                    "--output",
                    str(Path(temporary) / "blocked.json"),
                ],
            )
            for command in commands:
                with self.subTest(command=command):
                    with mock.patch("sys.stderr"):
                        status = cli_main(
                            [
                                "--mock",
                                "--data-dir",
                                temporary,
                                *command,
                            ]
                        )
                    self.assertEqual(status, 2)

    def test_service_rejects_store_containing_live_mode_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CouncilStore(temporary)
            live = default_config()
            store.create_run(
                question="No network call is made.",
                protocol_id="test",
                protocol_version="1",
                protocol_hash="hash",
                provider_configs=[
                    provider.to_dict() for provider in live.providers
                ],
                policy=live.policy.to_dict(),
            )
            with self.assertRaisesRegex(ValueError, "live-mode"):
                CouncilApplication(temporary)

    def test_service_rejects_preexisting_unowned_mock_run_without_marking(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = CouncilStore(temporary)
            config = mock_config(temporary)
            store.create_run(
                question="Created by the local CLI before service startup.",
                protocol_id="test",
                protocol_version="1",
                protocol_hash="hash",
                provider_configs=[
                    provider.to_dict() for provider in config.providers
                ],
                policy=config.policy.to_dict(),
            )
            marker = Path(temporary) / ".council-service-managed"
            with self.assertRaisesRegex(ValueError, "unowned CLI store"):
                CouncilApplication(temporary)
            self.assertFalse(marker.exists())
            with closing(sqlite3.connect(store.db_path)) as connection:
                self.assertIsNone(
                    connection.execute(
                        """
                        SELECT 1 FROM sqlite_master
                        WHERE type = 'table'
                          AND name = 'service_principals'
                        """
                    ).fetchone()
                )

    def test_cli_first_common_lock_race_cannot_convert_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = mock_config(temporary)
            with ServiceLock(temporary):
                store = CouncilStore(temporary)
                store.create_run(
                    question="The CLI won the common directory lock.",
                    protocol_id="test",
                    protocol_version="1",
                    protocol_hash="hash",
                    provider_configs=[
                        provider.to_dict() for provider in config.providers
                    ],
                    policy=config.policy.to_dict(),
                )
                with self.assertRaisesRegex(RuntimeError, "already owns"):
                    CouncilApplication(temporary)

            with self.assertRaisesRegex(ValueError, "unowned CLI store"):
                CouncilApplication(temporary)
            self.assertFalse(
                (Path(temporary) / ".council-service-managed").exists()
            )

    def test_injected_council_and_service_stores_must_share_database(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            council_store = CouncilStore(first)
            service_store = ServiceStore(Path(second) / "council.sqlite3")
            marker = Path(first) / ".council-service-managed"
            with mock.patch(
                "model_council.service.ServiceLock.acquire"
            ) as acquire:
                with self.assertRaisesRegex(ValueError, "same database path"):
                    CouncilApplication(
                        first,
                        council_store=council_store,
                        service_store=service_store,
                    )
            acquire.assert_not_called()
            self.assertFalse(marker.exists())

    def test_same_principal_runs_are_serialized_and_account_exactly(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
                max_workers=2,
                max_queue=2,
            )
            started = threading.Event()
            release = threading.Event()
            counter_lock = threading.Lock()
            started_calls = 0

            def slow_wrapper(original: object) -> object:
                def generate(**kwargs: object) -> object:
                    nonlocal started_calls
                    with counter_lock:
                        started_calls += 1
                        if started_calls >= 4:
                            started.set()
                    release.wait(timeout=3)
                    return original(**kwargs)  # type: ignore[operator]

                return generate

            for provider in application._providers.values():
                provider.generate = slow_wrapper(  # type: ignore[method-assign]
                    provider.generate
                )
            try:
                principal = application.authenticate(f"Bearer {HUMAN_TOKEN}")
                self.assertIsNotNone(principal)
                first = application.create_run(
                    principal,  # type: ignore[arg-type]
                    question="First slow run.",
                    idempotency_key="same-principal-slow-1",
                )
                self.assertTrue(started.wait(timeout=1))
                second = application.create_run(
                    principal,  # type: ignore[arg-type]
                    question="Second slow run.",
                    idempotency_key="same-principal-slow-2",
                )
                release.set()

                for run_id in (
                    first.payload["run_id"],
                    second.payload["run_id"],
                ):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        run = application.council_store.get_run(run_id)
                        if run and run["status"] in {
                            "completed",
                            "partial",
                            "failed",
                        }:
                            break
                        time.sleep(0.01)
                    self.assertEqual(
                        application.council_store.get_run(run_id)["status"],
                        "completed",
                    )

                with closing(
                    sqlite3.connect(application.council_store.db_path)
                ) as connection:
                    rows = connection.execute(
                        """
                        SELECT run_id, COUNT(*), SUM(actual_units)
                        FROM service_call_reservations
                        WHERE run_id IN (?, ?) AND state = 'reconciled'
                        GROUP BY run_id
                        ORDER BY run_id
                        """,
                        (
                            first.payload["run_id"],
                            second.payload["run_id"],
                        ),
                    ).fetchall()
                self.assertEqual(
                    sorted((count, units) for _, count, units in rows),
                    [(9, 9), (9, 9)],
                )
            finally:
                release.set()
                application.close()

    def test_ambiguous_provider_keeps_one_reservation_and_is_suppressed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
            )
            target = "mock-4"
            calls = 0

            def ambiguous_generate(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                raise ProviderError(
                    "Synthetic ambiguous transport outcome",
                    ambiguous=True,
                )

            application._providers[target].generate = (  # type: ignore[method-assign]
                ambiguous_generate
            )
            try:
                principal = application.authenticate(f"Bearer {HUMAN_TOKEN}")
                self.assertIsNotNone(principal)
                created = application.create_run(
                    principal,  # type: ignore[arg-type]
                    question="Suppress an uncertain provider in later stages.",
                    idempotency_key="ambiguous-provider-suppression",
                )
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    run = application.council_store.get_run(
                        created.payload["run_id"]
                    )
                    if run and run["status"] in {
                        "completed",
                        "partial",
                        "failed",
                    }:
                        break
                    time.sleep(0.01)
                run_id = created.payload["run_id"]
                self.assertEqual(
                    application.council_store.get_run(run_id)["status"],
                    "completed",
                )
                self.assertEqual(calls, 1)
                invocations = [
                    invocation
                    for invocation in application.council_store.list_invocations(
                        run_id
                    )
                    if invocation["provider"] == target
                ]
                self.assertEqual(len(invocations), 1)
                self.assertTrue(invocations[0]["error_ambiguous"])
                with closing(
                    sqlite3.connect(application.council_store.db_path)
                ) as connection:
                    reservations = connection.execute(
                        """
                        SELECT state, actual_units
                        FROM service_call_reservations
                        WHERE run_id = ? AND provider = ?
                        """,
                        (run_id, target),
                    ).fetchall()
                self.assertEqual(reservations, [("reserved", None)])

                application._new_engine(  # type: ignore[arg-type]
                    principal
                ).resume(run_id)
                self.assertEqual(calls, 1)
            finally:
                application.close()

    def test_unexpected_provider_exception_is_ambiguous_and_not_retried(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
            )
            target = "mock-4"
            calls = 0

            def unexpected_generate(**kwargs: object) -> object:
                nonlocal calls
                calls += 1
                raise RuntimeError("Synthetic unexpected provider crash")

            application._providers[target].generate = (  # type: ignore[method-assign]
                unexpected_generate
            )
            try:
                principal = application.authenticate(f"Bearer {HUMAN_TOKEN}")
                self.assertIsNotNone(principal)
                created = application.create_run(
                    principal,  # type: ignore[arg-type]
                    question="Quarantine an unexpected provider exception.",
                    idempotency_key="unexpected-provider-ambiguous",
                )
                run_id = created.payload["run_id"]
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    run = application.council_store.get_run(run_id)
                    if run and run["status"] in {
                        "completed",
                        "partial",
                        "failed",
                    }:
                        break
                    time.sleep(0.01)

                self.assertEqual(
                    application.council_store.get_run(run_id)["status"],
                    "completed",
                )
                self.assertEqual(calls, 1)
                invocations = [
                    invocation
                    for invocation in application.council_store.list_invocations(
                        run_id
                    )
                    if invocation["provider"] == target
                ]
                self.assertEqual(len(invocations), 1)
                self.assertTrue(invocations[0]["error_ambiguous"])
                with closing(
                    sqlite3.connect(application.council_store.db_path)
                ) as connection:
                    reservations = connection.execute(
                        """
                        SELECT state, actual_units
                        FROM service_call_reservations
                        WHERE run_id = ? AND provider = ?
                        """,
                        (run_id, target),
                    ).fetchall()
                self.assertEqual(reservations, [("reserved", None)])

                application._new_engine(  # type: ignore[arg-type]
                    principal
                ).resume(run_id)
                self.assertEqual(calls, 1)
            finally:
                application.close()

    def test_operator_can_revoke_one_principals_tokens_and_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": HUMAN_TOKEN},
            )
            application.close()
            with mock.patch("sys.stdout"):
                status = service_main(
                    [
                        "--data-dir",
                        temporary,
                        "--revoke-token",
                        "personal-laptop-human",
                    ]
                )
            self.assertEqual(status, 0)
            store = ServiceStore(Path(temporary) / "council.sqlite3")
            self.assertIsNone(store.authenticate_bearer(HUMAN_TOKEN))

    def test_stale_bootstrap_token_cannot_replace_current_token(self) -> None:
        token_a = "bootstrap-rollback-token-a-" + ("a" * 40)
        token_b = "bootstrap-rollback-token-b-" + ("b" * 40)
        with tempfile.TemporaryDirectory() as temporary:
            first = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": token_a},
            )
            first.close()
            store = ServiceStore(Path(temporary) / "council.sqlite3")
            self.assertEqual(
                store.revoke_principal_tokens("personal-laptop-human"),
                1,
            )

            second = CouncilApplication(
                temporary,
                bearer_tokens={"personal-laptop-human": token_b},
            )
            second.close()
            with self.assertRaisesRegex(ValueError, "retired"):
                CouncilApplication(
                    temporary,
                    bearer_tokens={"personal-laptop-human": token_a},
                )

            reopened = ServiceStore(
                Path(temporary) / "council.sqlite3"
            )
            self.assertIsNone(reopened.authenticate_bearer(token_a))
            authenticated = reopened.authenticate_bearer(token_b)
            self.assertIsNotNone(authenticated)
            self.assertEqual(
                authenticated.principal.principal_id,  # type: ignore[union-attr]
                "personal-laptop-human",
            )

    def test_post_errors_close_connection_and_partial_body_times_out(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch(
                "model_council.service."
                "DEFAULT_CONNECTION_TIMEOUT_SECONDS",
                0.1,
            ):
                fixture = ServiceFixture(Path(temporary))
                connection: socket.socket | None = None
                try:
                    connection = socket.create_connection(
                        ("127.0.0.1", fixture.server.server_port),
                        timeout=2,
                    )
                    connection.sendall(
                        b"POST /v1/runs HTTP/1.1\r\n"
                        b"Host: 127.0.0.1\r\n"
                        b"Content-Type: application/json\r\n"
                        b"Content-Length: 10\r\n"
                        b"Idempotency-Key: partial\r\n"
                        + (
                            f"Authorization: Bearer "
                            f"{HUMAN_TOKEN}\r\n\r\n"
                        ).encode()
                        + b"{"
                    )
                    response = b""
                    while True:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        response += chunk
                    self.assertIn(b"500 Internal Server Error", response)
                    self.assertIn(b"Connection: close", response)
                finally:
                    if connection is not None:
                        connection.close()
                    fixture.close()

    def test_six_principals_exist_and_work_principals_remain_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = CouncilApplication(temporary)
            try:
                with closing(
                    sqlite3.connect(application.council_store.db_path)
                ) as connection:
                    rows = connection.execute(
                        """
                        SELECT principal_id, enabled
                        FROM service_principals
                        ORDER BY principal_id
                        """
                    ).fetchall()
                self.assertEqual(len(rows), 6)
                states = dict(rows)
                self.assertEqual(states["work-laptop-human"], 0)
                self.assertEqual(states["work-laptop-agent"], 0)
                self.assertEqual(states["personal-laptop-human"], 1)
            finally:
                application.close()


if __name__ == "__main__":
    unittest.main()
