from __future__ import annotations

import concurrent.futures
import hashlib
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.models import (  # noqa: E402
    ErrorCategory,
    ProviderError,
    ProviderResponse,
    Usage,
)
from model_council.store import CouncilStore  # noqa: E402


PROVIDERS = [
    {
        "name": "alpha",
        "model": "alpha-1",
        "lineage": "lab-alpha",
        "secret_name": "ALPHA_API_KEY",
        "endpoint": "https://alpha.invalid/v1",
        "max_attempts": 3,
    },
    {
        "name": "beta",
        "model": "beta-2",
        "lineage": "lab-beta",
        "secret_name": "BETA_API_KEY",
        "endpoint": "https://beta.invalid/v1",
        "max_attempts": 2,
    },
]
POLICY = {
    "proposal_quorum": 2,
    "jury_quorum": 2,
    "min_lineages": 2,
    "max_calls": 10,
}


class CouncilStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "private-data"
        self.store = CouncilStore(self.data_dir)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def create_run(self, *, key: str | None = None, question: str = "What is true?") -> str:
        return self.store.create_run(
            question,
            "beta",
            "1.0",
            "protocol-sha256",
            PROVIDERS,
            POLICY,
            idempotency_key=key,
        )

    def test_permissions_wal_foreign_keys_and_schema_version(self) -> None:
        self.assertEqual(stat.S_IMODE(self.data_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.db_path.stat().st_mode), 0o600)

        with self.store.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0],
                1,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO events(
                        run_id,event_type,payload_json,payload_sha256,created_at
                    ) VALUES ('missing','x','{}','hash','now')
                    """
                )

    def test_run_idempotency_decoding_result_and_status(self) -> None:
        first = self.create_run(key="stable-key")
        second = self.create_run(key="stable-key")
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "idempotency"):
            self.create_run(key="stable-key", question="A different request")

        self.store.save_result(first, {"answer": "bounded", "votes": [1, 2]})
        self.store.set_run_status(first, "succeeded")
        value = self.store.get_run(first)
        assert value is not None
        self.assertEqual(value["provider_configs"], PROVIDERS)
        self.assertEqual(value["policy"], POLICY)
        self.assertEqual(value["result"], {"answer": "bounded", "votes": [1, 2]})
        self.assertEqual(value["status"], "succeeded")
        self.assertIsNotNone(value["finished_at"])
        self.assertEqual(len(value["question_sha256"]), 64)
        self.assertEqual(len(value["result_sha256"]), 64)
        self.assertEqual(self.store.list_runs(limit=1)[0]["id"], first)

    def test_concurrent_idempotent_run_creation(self) -> None:
        def create(_: int) -> str:
            return self.create_run(key="concurrent-run")

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            run_ids = list(executor.map(create, range(40)))
        self.assertEqual(len(set(run_ids)), 1)
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_concurrent_unique_invocation_and_success_reuse(self) -> None:
        run_id = self.create_run()

        def start(_: int) -> str:
            return self.store.start_invocation(
                run_id,
                "proposal",
                "alpha",
                "alpha-1",
                "lab-alpha",
                "Audit this question.",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
            invocation_ids = list(executor.map(start, range(40)))
        self.assertEqual(len(set(invocation_ids)), 1)
        self.assertEqual(self.store.count_calls(run_id), 1)

        invocation_id = invocation_ids[0]
        response = ProviderResponse(
            content="A cautious answer.",
            resolved_model="alpha-1-2026-07",
            request_id="req-123",
            usage=Usage(
                input_tokens=11,
                output_tokens=5,
                total_tokens=16,
                cached_input_tokens=2,
                reasoning_tokens=1,
            ),
            latency_ms=842,
            attempts=2,
            finish_reason="stop",
            metadata={"region": "us"},
        )
        self.store.finish_invocation_success(invocation_id, response)
        reused = start(999)
        self.assertEqual(reused, invocation_id)
        self.assertEqual(self.store.count_calls(run_id), 1)

        stored = self.store.get_successful_invocation(run_id, "proposal", "alpha")
        assert stored is not None
        self.assertEqual(stored["response_text"], response.content)
        self.assertEqual(
            stored["response_sha256"],
            hashlib.sha256(response.content.encode()).hexdigest(),
        )
        self.assertEqual(stored["request_id"], "req-123")
        self.assertEqual(stored["usage"]["reasoning_tokens"], 1)
        self.assertEqual(stored["latency_ms"], 842)
        self.assertEqual(stored["attempts"], 2)
        self.assertEqual(stored["finish_reason"], "stop")
        self.assertEqual(stored["metadata"], {"region": "us"})
        self.assertEqual(stored["error_category"], None)

        # Exact repeat is idempotent; a conflicting final response is rejected.
        self.store.finish_invocation_success(invocation_id, response)
        with self.assertRaisesRegex(ValueError, "different successful response"):
            self.store.finish_invocation_success(
                invocation_id,
                ProviderResponse(
                    content="Different",
                    resolved_model="alpha-1-2026-07",
                    request_id="req-123",
                    usage=Usage(),
                    latency_ms=1,
                    attempts=1,
                ),
            )

    def test_failure_serialization_and_restart(self) -> None:
        run_id = self.create_run()
        invocation_id = self.store.start_invocation(
            run_id,
            "jury",
            "beta",
            "beta-2",
            "lab-beta",
            "Challenge the proposals.",
        )
        failure = ProviderError(
            "provider timed out after bounded retries",
            category=ErrorCategory.TIMEOUT,
            retryable=True,
            status_code=504,
            request_id="failure-req",
            attempts=3,
            ambiguous=True,
            client_request_id="client-failure-req",
            elapsed_ms=150023,
            transport_phase="request_in_flight",
            timeout_subtype="socket_or_os_timeout",
        )
        self.store.finish_invocation_failure(invocation_id, failure)
        stored = self.store.list_invocations(run_id)[0]
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["request_id"], "failure-req")
        self.assertEqual(stored["attempts"], 3)
        self.assertEqual(stored["error_category"], "timeout")
        self.assertEqual(stored["error_message"], str(failure))
        self.assertTrue(stored["error_retryable"])
        self.assertEqual(stored["error_status_code"], 504)
        self.assertTrue(stored["error_ambiguous"])
        self.assertEqual(stored["error"]["category"], "timeout")
        self.assertEqual(
            stored["error"]["client_request_id"],
            "client-failure-req",
        )
        self.assertEqual(stored["error"]["elapsed_ms"], 150023)
        self.assertEqual(
            stored["error"]["transport_phase"],
            "request_in_flight",
        )
        self.assertEqual(
            stored["error"]["timeout_subtype"],
            "socket_or_os_timeout",
        )
        self.assertIsNone(
            self.store.get_successful_invocation(run_id, "jury", "beta")
        )

        # A failed logical slot may be retried without creating a second call row.
        restarted = self.store.start_invocation(
            run_id,
            "jury",
            "beta",
            "beta-2",
            "lab-beta",
            "Challenge the proposals.",
        )
        self.assertEqual(restarted, invocation_id)
        self.assertEqual(self.store.count_calls(run_id), 2)
        self.assertEqual(self.store.list_invocations(run_id)[0]["status"], "running")
        self.assertEqual(self.store.list_invocations(run_id)[0]["call_count"], 2)
        retry_events = [
            event
            for event in self.store.list_events(run_id)
            if event["event_type"] == "provider_retry_started"
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(
            retry_events[0]["payload"]["retry_call_count"],
            2,
        )
        self.assertEqual(
            retry_events[0]["payload"]["prior_failure"],
            failure.to_dict(),
        )

    def test_events_are_concurrent_and_reopen_cleanly(self) -> None:
        run_id = self.create_run(key="reopen")

        def append(index: int) -> int:
            return self.store.append_event(run_id, "progress", {"index": index})

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            event_ids = list(executor.map(append, range(30)))
        self.assertEqual(len(set(event_ids)), 30)
        self.assertEqual(len(self.store.list_events(run_id)), 30)

        invocation_id = self.store.start_invocation(
            run_id, "proposal", "alpha", "alpha-1", "lab-alpha", "Prompt"
        )
        self.store.finish_invocation_success(
            invocation_id,
            ProviderResponse(
                content="Persisted",
                resolved_model="alpha-1",
                request_id=None,
                usage=Usage(total_tokens=3),
                latency_ms=10,
                attempts=1,
            ),
        )
        self.store.save_result(run_id, {"synthesis": "Persisted"})

        reopened = CouncilStore(db_path=self.store.db_path)
        try:
            self.assertEqual(reopened.get_run(run_id)["result"]["synthesis"], "Persisted")
            self.assertEqual(len(reopened.list_events(run_id)), 30)
            self.assertEqual(
                reopened.get_successful_invocation(run_id, "proposal", "alpha")[
                    "response_text"
                ],
                "Persisted",
            )
            self.assertEqual(stat.S_IMODE(reopened.db_path.stat().st_mode), 0o600)
        finally:
            reopened.close()

    def test_raw_credentials_are_rejected(self) -> None:
        compromised = [dict(PROVIDERS[0], api_key="do-not-store-this")]
        with self.assertRaisesRegex(ValueError, "raw credential"):
            self.store.create_run(
                "Question",
                "beta",
                "1",
                "hash",
                compromised,
                POLICY,
            )
        with self.assertRaisesRegex(ValueError, "credential-bearing endpoint"):
            self.store.create_run(
                "Question",
                "beta",
                "1",
                "hash",
                [dict(PROVIDERS[0], endpoint="https://example.invalid?key=secret")],
                POLICY,
            )
        self.assertNotIn(b"do-not-store-this", self.store.db_path.read_bytes())

    def test_missing_rows_and_conflicting_invocation_input_fail_closed(self) -> None:
        with self.assertRaises(KeyError):
            self.store.set_run_status("missing", "failed", "not found")
        with self.assertRaises(KeyError):
            self.store.start_invocation(
                "missing", "proposal", "alpha", "alpha-1", "lab-alpha", "Prompt"
            )

        run_id = self.create_run()
        self.store.start_invocation(
            run_id, "proposal", "alpha", "alpha-1", "lab-alpha", "Prompt one"
        )
        with self.assertRaisesRegex(ValueError, "different input"):
            self.store.start_invocation(
                run_id,
                "proposal",
                "alpha",
                "alpha-1",
                "lab-alpha",
                "Prompt two",
            )


if __name__ == "__main__":
    unittest.main()
