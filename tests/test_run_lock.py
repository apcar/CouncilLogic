from __future__ import annotations

from pathlib import Path
import stat
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.run_lock import RunLock  # noqa: E402


class RunLockTests(unittest.TestCase):
    def test_lock_is_exclusive_and_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = RunLock(temporary, "run-one")
            second = RunLock(temporary, "run-one")

            with first:
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    with second:
                        pass

            with second:
                self.assertEqual(
                    stat.S_IMODE(second.path.stat().st_mode),
                    0o600,
                )


if __name__ == "__main__":
    unittest.main()
