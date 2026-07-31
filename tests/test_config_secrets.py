from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.config import (  # noqa: E402
    DEFAULT_MODELS,
    default_config,
    load_config,
)
from model_council.secrets import (  # noqa: E402
    ChainedSecretResolver,
    CommandSecretResolver,
    EnvironmentSecretResolver,
)


class ConfigurationTests(unittest.TestCase):
    def test_defaults_pin_seven_current_lineages(self) -> None:
        config = default_config()

        self.assertEqual(
            [provider.name for provider in config.providers],
            [
                "openai",
                "anthropic",
                "gemini",
                "mistral",
                "xai",
                "qwen",
                "cohere",
            ],
        )
        self.assertEqual(
            len({provider.lineage for provider in config.providers}),
            7,
        )
        self.assertEqual(
            [provider.model for provider in config.providers],
            [
                DEFAULT_MODELS["openai"],
                DEFAULT_MODELS["anthropic"],
                DEFAULT_MODELS["gemini"],
                DEFAULT_MODELS["mistral"],
                DEFAULT_MODELS["xai"],
                DEFAULT_MODELS["qwen"],
                DEFAULT_MODELS["cohere"],
            ],
        )
        self.assertEqual(config.policy.max_calls, 20)
        self.assertEqual(config.policy.deadline_seconds, 480.0)
        for provider in config.providers:
            self.assertEqual(
                set(provider.stage_max_output_tokens),
                {"proposal", "jury", "synthesis"},
            )
            self.assertEqual(
                set(provider.stage_timeout_seconds),
                {"proposal", "jury", "synthesis"},
            )
        gemini = next(
            provider
            for provider in config.providers
            if provider.name == "gemini"
        )
        self.assertEqual(gemini.extra["thinking_level"], "low")
        self.assertEqual(gemini.output_tokens_for("proposal"), 4096)
        qwen = next(
            provider
            for provider in config.providers
            if provider.name == "qwen"
        )
        self.assertFalse(qwen.extra["enable_thinking"])
        cohere = next(
            provider
            for provider in config.providers
            if provider.name == "cohere"
        )
        self.assertEqual(
            cohere.extra["thinking"],
            {"token_budget": 800},
        )
        self.assertNotIn(
            "upstage",
            {provider.name for provider in config.providers},
        )

    def test_default_qwen_uses_singapore_international_route(self) -> None:
        qwen = next(
            provider
            for provider in default_config().providers
            if provider.name == "qwen"
        )

        self.assertEqual(qwen.model, "qwen3.7-max")
        self.assertEqual(
            qwen.endpoint,
            (
                "https://dashscope-intl.aliyuncs.com/"
                "compatible-mode/v1/chat/completions"
            ),
        )

    def test_legacy_file_config_does_not_silently_enable_new_providers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text("[run]\nsynthesis_provider = \"openai\"\n")

            config = load_config(path)

            self.assertEqual(
                [provider.name for provider in config.providers],
                ["openai", "anthropic", "gemini", "mistral"],
            )
            self.assertEqual(config.policy.max_calls, 16)

    def test_example_config_activates_seven_and_parks_upstage(self) -> None:
        config = load_config(PROJECT_ROOT / "council.example.toml")

        self.assertEqual(
            [provider.name for provider in config.providers],
            [
                "openai",
                "anthropic",
                "gemini",
                "mistral",
                "xai",
                "qwen",
                "cohere",
            ],
        )
        self.assertEqual(config.policy.max_calls, 20)

    def test_example_config_can_enable_full_eight_provider_bench(self) -> None:
        example = (PROJECT_ROOT / "council.example.toml").read_text(
            encoding="utf-8"
        )
        enabled = example.replace("enabled = false", "enabled = true", 1)
        self.assertNotEqual(enabled, example)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(enabled, encoding="utf-8")

            config = load_config(path)

        self.assertEqual(
            [provider.name for provider in config.providers],
            [
                "openai",
                "anthropic",
                "gemini",
                "mistral",
                "xai",
                "qwen",
                "cohere",
                "upstage",
            ],
        )
        self.assertEqual(config.policy.max_calls, 20)

    def test_file_config_explicitly_enables_xai(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                "[providers.xai]\nmodel = \"grok-4.5\"\n",
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual(
                [provider.name for provider in config.providers],
                ["openai", "anthropic", "gemini", "mistral", "xai"],
            )

    def test_file_config_explicitly_enables_qwen_and_cohere(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[policy]
max_calls = 20

[providers.qwen]
model = "qwen3.7-max"

[providers.cohere]
model = "command-a-plus-05-2026"
""",
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual(
                [provider.name for provider in config.providers],
                [
                    "openai",
                    "anthropic",
                    "gemini",
                    "mistral",
                    "qwen",
                    "cohere",
                ],
            )
            self.assertEqual(config.policy.max_calls, 20)

    def test_file_config_can_enable_optional_upstage_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[policy]
max_calls = 20

[providers.upstage]
enabled = true
""",
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual(
                [provider.name for provider in config.providers],
                ["openai", "anthropic", "gemini", "mistral", "upstage"],
            )
            upstage = config.providers[-1]
            self.assertEqual(upstage.model, "solar-pro3-260323")
            self.assertEqual(upstage.secret_name, "UPSTAGE_API_KEY")
            self.assertEqual(
                upstage.extra["reasoning_effort"],
                "low",
            )

    def test_rejects_non_allowlisted_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[providers.openai]
endpoint = "https://attacker.example/v1/responses"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not allowlisted"):
                load_config(path)

    def test_rejects_non_allowlisted_xai_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[providers.xai]
endpoint = "https://attacker.example/v1/responses"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not allowlisted"):
                load_config(path)

    def test_rejects_non_allowlisted_qwen_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[providers.qwen]
endpoint = "https://attacker.example/v1/chat/completions"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not allowlisted"):
                load_config(path)

    def test_rejects_non_allowlisted_cohere_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[providers.cohere]
endpoint = "https://attacker.example/v2/chat"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not allowlisted"):
                load_config(path)

    def test_rejects_non_allowlisted_upstage_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[providers.upstage]
endpoint = "https://attacker.example/v1/chat/completions"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "not allowlisted"):
                load_config(path)

    def test_rejects_nonstandard_provider_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[providers.openai]
endpoint = "https://api.openai.com:4443/v1/responses"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "port"):
                load_config(path)

    def test_rejects_unsafe_numeric_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[providers.openai]
max_output_tokens = 0
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "max_output_tokens"):
                load_config(path)

    def test_rejects_unknown_stage_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[providers.openai.stage_max_output_tokens]
unknown = 100
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unknown stages"):
                load_config(path)

    def test_rejects_call_budget_below_complete_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.toml"
            path.write_text(
                """
[policy]
max_calls = 8
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Max calls"):
                load_config(path)


class SecretResolverTests(unittest.TestCase):
    def test_environment_resolver_never_requires_a_file(self) -> None:
        resolver = EnvironmentSecretResolver({"TEST_SECRET": "sensitive-value"})

        self.assertEqual(resolver.resolve("TEST_SECRET"), "sensitive-value")
        self.assertEqual(
            resolver.source_for("TEST_SECRET"), "process environment"
        )
        self.assertIsNone(resolver.resolve("MISSING"))

    def test_chain_uses_first_available_source(self) -> None:
        resolver = ChainedSecretResolver(
            (
                EnvironmentSecretResolver({}),
                EnvironmentSecretResolver({"KEY": "second"}),
            )
        )

        self.assertEqual(resolver.resolve("KEY"), "second")

    def test_external_secret_command_requires_absolute_executable(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute"):
            CommandSecretResolver.from_string("relative-helper")

        resolver = CommandSecretResolver.from_string("/usr/bin/false --quiet")
        self.assertEqual(resolver.command, ("/usr/bin/false", "--quiet"))


if __name__ == "__main__":
    unittest.main()
