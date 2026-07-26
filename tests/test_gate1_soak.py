from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "gate1_mock_soak.py"


class Gate1MockSoakTests(unittest.TestCase):
    def test_small_mock_soak_and_safe_explicit_directory(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--jobs",
                "4",
                "--submission-workers",
                "4",
                "--service-workers",
                "2",
                "--max-queue",
                "4",
                "--timeout-seconds",
                "30",
            ],
            cwd=PROJECT_ROOT,
            env={
                **os.environ,
                "PYTHONPATH": str(PROJECT_ROOT / "src"),
            },
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["mode"], "mock-only")
        self.assertEqual(summary["jobs_accepted"], 4)
        self.assertEqual(summary["terminal_statuses"], {"completed": 4})
        self.assertEqual(summary["reservations"], 36)
        self.assertEqual(summary["reconciled_logical_units"], 36)
        self.assertEqual(summary["unresolved_reservations"], 0)
        self.assertEqual(summary["integrity_check"], "ok")
        self.assertEqual(summary["foreign_key_violations"], 0)
        self.assertTrue(summary["service_lock_fenced_second_writer"])

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "not-dedicated"
            directory.mkdir()
            sentinel = directory / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            refused = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--data-dir",
                    str(directory),
                    "--jobs",
                    "1",
                ],
                cwd=PROJECT_ROOT,
                env={
                    **os.environ,
                    "PYTHONPATH": str(PROJECT_ROOT / "src"),
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(refused.returncode, 1)
            self.assertIn("dedicated and empty", refused.stderr)
            self.assertEqual(
                sentinel.read_text(encoding="utf-8"),
                "preserve",
            )


if __name__ == "__main__":
    unittest.main()
