from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import stat
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.cli import main  # noqa: E402
from model_council.store import CouncilStore  # noqa: E402


class CliTests(unittest.TestCase):
    def _call(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

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
                    "--json",
                ]
            )
            self.assertEqual(code, 0, error)
            result = json.loads(output)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(len(result["proposals"]), 4)
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
            self.assertEqual(len(inspected["invocations"]), 9)

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
            self.assertIn("# Model Council Run", exported)
            self.assertIn("## Council answer", exported)
            self.assertEqual(stat.S_IMODE(export_path.stat().st_mode), 0o600)

    def test_live_doctor_reports_missing_credentials_without_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            keys = (
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "GEMINI_API_KEY",
                "MISTRAL_API_KEY",
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


if __name__ == "__main__":
    unittest.main()
