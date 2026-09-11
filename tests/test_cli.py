from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.cli import _markdown_export, _print_result, main  # noqa: E402
from model_council.store import CouncilStore  # noqa: E402


class CliTests(unittest.TestCase):
    def _call(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_human_result_surfaces_recovery(self) -> None:
        stdout = StringIO()
        with redirect_stdout(stdout):
            _print_result(
                {
                    "run_id": "repair-run",
                    "status": "completed",
                    "completion_quality": "degraded",
                    "answer": "Repaired answer.",
                    "recoveries": [
                        {
                            "kind": "jury_artifact_repair",
                            "provider": "alpha",
                            "status": "recovered",
                        }
                    ],
                    "warnings": [],
                    "failures": [],
                },
                as_json=False,
            )

        self.assertIn("Recoveries:", stdout.getvalue())
        self.assertIn(
            "jury_artifact_repair / alpha: recovered", stdout.getvalue()
        )

    def test_mock_doctor_run_inspect_and_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            common = ["--mock", "--data-dir", temporary]
            code, output, error = self._call([*common, "doctor", "--json"])
            self.assertEqual(code, 0, error)
            self.assertTrue(json.loads(output)["ready"])

            code, output, error = self._call(
                [
                    *common,
                    "run",
                    "--question",
                    "Exercise every private-beta stage.",
                    "--idempotency-key",
                    "cli-fixture",
                    "--synthesis-provider",
                    "mock-2",
                    "--proposal-quorum",
                    "4",
                    "--jury-quorum",
                    "4",
                    "--min-lineages",
                    "4",
                    "--max-calls",
                    "9",
                    "--max-parallel-calls",
                    "2",
                    "--jury-repair-attempts",
                    "1",
                    "--json",
                ]
            )
            self.assertEqual(code, 0, error)
            result = json.loads(output)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(result["proposals"]), 4)
            self.assertEqual(
                set(
                    result["aggregate"][
                        "candidate_label_mapping"
                    ].values()
                ),
                {"mock-1", "mock-2", "mock-3", "mock-4"},
            )
            run_id = result["run_id"]

            code, output, error = self._call(
                [*common, "resume", run_id, "--json"]
            )
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output), result)

            code, output, error = self._call(
                [*common, "inspect", run_id, "--json"]
            )
            self.assertEqual(code, 0, error)
            inspected = json.loads(output)
            self.assertEqual(inspected["run"]["id"], run_id)
            self.assertEqual(
                inspected["run"]["policy"]["max_parallel_calls"],
                2,
            )
            self.assertEqual(
                inspected["run"]["policy"]["jury_repair_attempts"],
                1,
            )
            self.assertEqual(len(inspected["invocations"]), 9)
            self.assertTrue(
                any(
                    event["event_type"] == "workload_preflight"
                    for event in inspected["events"]
                )
            )

            export_path = Path(temporary) / "council-export.md"
            code, _output, error = self._call(
                [
                    *common,
                    "export",
                    run_id,
                    "--output",
                    str(export_path),
                ]
            )
            self.assertEqual(code, 0, error)
            exported = export_path.read_text(encoding="utf-8")
            self.assertIn("# CouncilLogic Run", exported)
            self.assertIn("- Completion quality: `clean`", exported)
            self.assertIn("## Council answer", exported)
            self.assertIn("## Recovery audit events", exported)
            self.assertIn('"candidate_label_mapping"', exported)
            self.assertEqual(stat.S_IMODE(export_path.stat().st_mode), 0o600)

            json_export_path = Path(temporary) / "council-export.json"
            code, _output, error = self._call(
                [
                    *common,
                    "export",
                    run_id,
                    "--format",
                    "json",
                    "--output",
                    str(json_export_path),
                ]
            )
            self.assertEqual(code, 0, error)
            json_export = json.loads(
                json_export_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                json_export["run"]["result"]["aggregate"][
                    "candidate_label_mapping"
                ],
                result["aggregate"]["candidate_label_mapping"],
            )
            self.assertEqual(
                stat.S_IMODE(json_export_path.stat().st_mode),
                0o600,
            )

    def test_live_plan_is_credential_free_and_does_not_create_storage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unused_data_dir = Path(temporary) / "unused-council-data"
            with (
                patch(
                    "model_council.cli._store",
                    side_effect=AssertionError("plan constructed a store"),
                ),
                patch(
                    "model_council.cli.default_secret_resolver",
                    side_effect=AssertionError("plan resolved credentials"),
                ),
                patch(
                    "model_council.cli._engine",
                    side_effect=AssertionError("plan constructed an engine"),
                ),
            ):
                code, output, error = self._call(
                    [
                        "--data-dir",
                        str(unused_data_dir),
                        "plan",
                        "--question",
                        "Choose the safer implementation.",
                        "--providers",
                        "openai,anthropic,gemini",
                        "--synthesis-provider",
                        "gemini",
                        "--max-calls",
                        "7",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0, error)
            self.assertEqual(error, "")
            self.assertFalse(unused_data_dir.exists())
            plan = json.loads(output)
            self.assertEqual(
                plan["providers"], ["openai", "anthropic", "gemini"]
            )
            self.assertEqual(plan["synthesis_provider"], "gemini")
            self.assertEqual(plan["synthesis_providers"], ["gemini"])
            self.assertEqual(plan["mandatory_calls"], 7)
            self.assertEqual(plan["recovery_call_capacity"], 0)
            self.assertEqual(
                plan["limits"],
                {"question_chars": 30_000, "stage_prompt_chars": 60_000},
            )
            self.assertTrue(plan["within_limits"])
            self.assertIn("effective_plain_question_max_chars", plan)
            self.assertIn("plain_question_headroom_chars", plan)

    def test_plan_accepts_an_exact_ordered_synthesis_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unused_data_dir = Path(temporary) / "unused-council-data"
            with (
                patch(
                    "model_council.cli._store",
                    side_effect=AssertionError("plan constructed a store"),
                ),
                patch(
                    "model_council.cli.default_secret_resolver",
                    side_effect=AssertionError("plan resolved credentials"),
                ),
                patch(
                    "model_council.cli._engine",
                    side_effect=AssertionError("plan constructed an engine"),
                ),
            ):
                code, output, error = self._call(
                    [
                        "--data-dir",
                        str(unused_data_dir),
                        "plan",
                        "--question",
                        "Plan an ordered synthesis fallback.",
                        "--providers",
                        "openai,anthropic,gemini",
                        "--synthesis-providers",
                        "gemini,openai",
                        "--max-calls",
                        "8",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0, error)
            self.assertEqual(error, "")
            self.assertFalse(unused_data_dir.exists())
            plan = json.loads(output)
            self.assertEqual(plan["synthesis_provider"], "gemini")
            self.assertEqual(
                plan["synthesis_providers"],
                ["gemini", "openai"],
            )
            self.assertEqual(plan["mandatory_calls"], 8)
            self.assertEqual(plan["recovery_call_capacity"], 0)

    def test_no_config_plan_filters_implicit_fallbacks_to_selected_roster(
        self,
    ) -> None:
        code, output, error = self._call(
            [
                "plan",
                "--question",
                "Keep only selected implicit synthesis fallbacks.",
                "--providers",
                "openai,anthropic,mistral",
                "--max-calls",
                "8",
                "--json",
            ]
        )

        self.assertEqual(code, 0, error)
        self.assertEqual(error, "")
        plan = json.loads(output)
        self.assertEqual(
            plan["providers"],
            ["openai", "anthropic", "mistral"],
        )
        self.assertEqual(
            plan["synthesis_providers"],
            ["openai", "anthropic"],
        )
        self.assertEqual(plan["mandatory_calls"], 8)

    def test_no_config_run_filters_unselected_implicit_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected_configs = []

            class StubEngine:
                @staticmethod
                def run(
                    question: str,
                    *,
                    idempotency_key: str | None = None,
                ) -> dict[str, object]:
                    return {"status": "completed"}

            def capture_engine(config):
                selected_configs.append(config)
                return StubEngine()

            with patch(
                "model_council.cli._engine",
                side_effect=capture_engine,
            ):
                code, output, error = self._call(
                    [
                        "--data-dir",
                        temporary,
                        "run",
                        "--question",
                        "Preserve legacy provider selection.",
                        "--providers",
                        "openai,mistral,xai",
                        "--max-calls",
                        "7",
                        "--json",
                    ]
                )

        self.assertEqual(code, 0, error)
        self.assertEqual(error, "")
        self.assertEqual(json.loads(output), {"status": "completed"})
        self.assertEqual(len(selected_configs), 1)
        self.assertEqual(
            selected_configs[0].synthesis_providers,
            ("openai",),
        )

    def test_no_config_selection_still_requires_configured_primary(
        self,
    ) -> None:
        code, output, error = self._call(
            [
                "plan",
                "--question",
                "Reject a roster without the configured primary.",
                "--providers",
                "anthropic,gemini,mistral",
            ]
        )

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("Synthesis provider must be one", error)

    def test_file_backed_fallback_chain_remains_exact(self) -> None:
        code, output, error = self._call(
            [
                "--config",
                str(PROJECT_ROOT / "council.example.toml"),
                "plan",
                "--question",
                "Keep the configured fallback chain exact.",
                "--providers",
                "openai,anthropic,mistral",
            ]
        )

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn(
            "Configured synthesis providers are not enabled: gemini",
            error,
        )

    def test_duplicate_selected_providers_are_rejected(self) -> None:
        code, output, error = self._call(
            [
                "--mock",
                "plan",
                "--question",
                "Reject a duplicate participant.",
                "--providers",
                "mock-1,mock-1",
                "--synthesis-provider",
                "mock-1",
            ]
        )

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("must not contain duplicates", error)

    def test_singular_and_ordered_synthesis_options_are_exclusive(
        self,
    ) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main(
                    [
                        "--mock",
                        "plan",
                        "--question",
                        "Reject conflicting synthesis selectors.",
                        "--synthesis-provider",
                        "mock-1",
                        "--synthesis-providers",
                        "mock-1,mock-2",
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not allowed with argument", stderr.getvalue())

    def test_cli_rejects_nonfinite_deadline(self) -> None:
        code, output, error = self._call(
            [
                "--mock",
                "plan",
                "--question",
                "Keep deadline checks active.",
                "--deadline-seconds",
                "nan",
            ]
        )

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("deadline_seconds", error)

    def test_plan_rejection_prints_json_and_actionable_error(self) -> None:
        code, output, error = self._call(
            [
                "--mock",
                "plan",
                "--question",
                "A bounded question.",
                "--max-stage-prompt-chars",
                "10",
                "--json",
            ]
        )

        self.assertEqual(code, 2)
        plan = json.loads(output)
        self.assertFalse(plan["within_limits"])
        self.assertTrue(plan["prompt_limit_exceeded_stages"])
        self.assertIn("Projected council prompt growth exceeds", error)

    def test_live_doctor_reports_missing_credentials_without_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            keys = (
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "GEMINI_API_KEY",
                "MISTRAL_API_KEY",
                "XAI_API_KEY",
                "DASHSCOPE_API_KEY",
                "COHERE_API_KEY",
            )
            import os

            previous = {key: os.environ.pop(key, None) for key in keys}
            try:
                code, output, error = self._call(
                    ["--data-dir", temporary, "doctor", "--json"]
                )
            finally:
                for key, value in previous.items():
                    if value is not None:
                        os.environ[key] = value

            self.assertEqual(error, "")
            self.assertEqual(code, 2)
            payload = json.loads(output)
            self.assertFalse(payload["ready"])
            self.assertNotIn("sensitive", output.lower())
            self.assertEqual(CouncilStore(temporary).list_runs(), [])

    def test_partial_markdown_preserves_candidate_namespace(self) -> None:
        mapping = {
            "CANDIDATE_01": "alpha",
            "CANDIDATE_02": "beta",
        }
        rendered = _markdown_export(
            {
                "id": "partial-run",
                "status": "partial",
                "protocol_id": "independent-jury",
                "protocol_version": "test",
                "protocol_hash": "test-hash",
                "question": "Question",
                "result": {
                    "completion_quality": "degraded",
                    "answer": None,
                    "aggregate": None,
                    "candidate_namespace": {
                        "candidate_label_mapping": mapping,
                    },
                    "recoveries": [
                        {
                            "kind": "application_retry",
                            "stage": "jury",
                            "provider": "alpha",
                            "status": "recovered",
                        }
                    ],
                    "warnings": [],
                },
            },
            [],
            [
                {
                    "event_type": "jury_artifact_repair",
                    "payload": {
                        "kind": "jury_artifact_repair",
                        "provider": "alpha",
                        "status": "recovered",
                    },
                },
                {
                    "event_type": "provider_call_not_dispatched",
                    "payload": {
                        "stage": "jury_repair",
                        "provider": "alpha",
                        "error": {
                            "category": "timeout",
                            "ambiguous": False,
                        },
                    },
                },
            ],
        )

        self.assertIn("## Candidate namespace", rendered)
        self.assertIn('"CANDIDATE_01": "alpha"', rendered)
        self.assertIn("## Aggregate\n\n```json\nnull", rendered)
        self.assertIn("## Recoveries", rendered)
        self.assertIn('"kind": "application_retry"', rendered)
        self.assertIn('"status": "recovered"', rendered)
        self.assertIn("## Recovery audit events", rendered)
        self.assertIn('"event_type": "jury_artifact_repair"', rendered)
        self.assertIn(
            '"event_type": "provider_call_not_dispatched"',
            rendered,
        )

    def test_markdown_failed_invocation_preserves_unknown_latency_and_safe_error(self) -> None:
        rendered = _markdown_export(
            {
                "id": "failed-invocation-run",
                "status": "completed",
                "protocol_id": "independent-jury",
                "protocol_version": "test",
                "protocol_hash": "test-hash",
                "question": "Question",
                "result": {
                    "completion_quality": "degraded",
                    "answer": "Answer",
                    "aggregate": None,
                    "candidate_namespace": None,
                    "recoveries": [],
                    "warnings": [],
                },
            },
            [
                {
                    "stage": "proposal",
                    "provider": "mistral",
                    "status": "failed",
                    "model": "mistral-medium-3-5",
                    "lineage": "mistral",
                    "attempts": 3,
                    "latency_ms": None,
                    "error_category": "rate_limit",
                    "error_status_code": 429,
                    "error_retryable": True,
                    "error_ambiguous": False,
                    "error_message": "raw provider detail must stay out",
                    "response_text": None,
                }
            ],
            [],
        )

        self.assertIn("- Attempts: `3`", rendered)
        self.assertIn("- Latency: `unknown`", rendered)
        self.assertNotIn("- Latency: `0 ms`", rendered)
        self.assertIn("- Failure category: `rate_limit`", rendered)
        self.assertIn("- HTTP status: `429`", rendered)
        self.assertIn("- Retryable: `true`", rendered)
        self.assertIn("- Ambiguous: `false`", rendered)
        self.assertNotIn("raw provider detail", rendered)


if __name__ == "__main__":
    unittest.main()
