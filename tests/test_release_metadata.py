from __future__ import annotations

from pathlib import Path
import re
import tomllib
import unittest

from model_council import __version__
from model_council.protocol import PROTOCOL_ID, PROTOCOL_VERSION
from model_council.service import CouncilRequestHandler
from model_council.version import MOCK_SERVICE_PROFILE_VERSION, PACKAGE_VERSION


ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_public_release_identifiers_are_consistent(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
        citation = (ROOT / "CITATION.cff").read_text("utf-8")
        readme = (ROOT / "README.md").read_text("utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text("utf-8")

        self.assertEqual(PACKAGE_VERSION, "0.3.0a1")
        self.assertEqual(__version__, PACKAGE_VERSION)
        self.assertEqual(project["project"]["version"], PACKAGE_VERSION)
        self.assertRegex(
            citation,
            rf"(?m)^version: {re.escape(PACKAGE_VERSION)}$",
        )
        self.assertIn(f"public alpha (`{PACKAGE_VERSION}`)", readme)
        self.assertIn(f"## [{PACKAGE_VERSION}]", changelog)

    def test_independently_versioned_surfaces_remain_explicit(self) -> None:
        self.assertEqual(PROTOCOL_ID, "independent-jury")
        self.assertEqual(PROTOCOL_VERSION, "1.2.1-beta")
        self.assertEqual(MOCK_SERVICE_PROFILE_VERSION, "0.2.0a1")
        self.assertEqual(
            CouncilRequestHandler.server_version,
            f"CouncilService/{MOCK_SERVICE_PROFILE_VERSION}",
        )


if __name__ == "__main__":
    unittest.main()
