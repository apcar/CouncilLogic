from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import COUNCIL_STAGES, ProviderConfig, RunPolicy


DEFAULT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1/responses",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "gemini": (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/{model}:generateContent"
    ),
    "mistral": "https://api.mistral.ai/v1/chat/completions",
    "xai": "https://api.x.ai/v1/responses",
    "qwen": (
        "https://dashscope-intl.aliyuncs.com/"
        "compatible-mode/v1/chat/completions"
    ),
    "cohere": "https://api.cohere.ai/v2/chat",
    "upstage": "https://api.upstage.ai/v1/chat/completions",
}

DEFAULT_MODELS = {
    "openai": "gpt-5.6-sol",
    "anthropic": "claude-sonnet-4-6",
    "gemini": "gemini-3.6-flash",
    "mistral": "mistral-medium-3-5",
    "xai": "grok-4.5",
    "qwen": "qwen3.7-max",
    "cohere": "command-a-plus-05-2026",
    "upstage": "solar-pro3-260323",
}

DEFAULT_SECRETS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "xai": "XAI_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "cohere": "COHERE_API_KEY",
    "upstage": "UPSTAGE_API_KEY",
}

DEFAULT_LINEAGES = {
    "openai": "openai-gpt",
    "anthropic": "anthropic-claude",
    "gemini": "google-gemini",
    "mistral": "mistral",
    "xai": "xai-grok",
    "qwen": "alibaba-qwen",
    "cohere": "cohere-command",
    "upstage": "upstage-solar",
}

DEFAULT_STAGE_MAX_OUTPUT_TOKENS = {
    "proposal": 3_200,
    "jury": 2_200,
    "synthesis": 3_200,
}

DEFAULT_STAGE_TIMEOUT_SECONDS = {
    "proposal": 150.0,
    "jury": 120.0,
    "synthesis": 180.0,
}

ALLOWED_ENDPOINT_HOSTS = {
    "openai": {"api.openai.com"},
    "anthropic": {"api.anthropic.com"},
    "gemini": {"generativelanguage.googleapis.com"},
    "mistral": {"api.mistral.ai"},
    "xai": {"api.x.ai"},
    "qwen": {
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
        "dashscope.aliyuncs.com",
        "cn-hongkong.dashscope.aliyuncs.com",
    },
    "cohere": {"api.cohere.ai"},
    "upstage": {"api.upstage.ai"},
    "mock": {"localhost", "127.0.0.1"},
}

DEFAULT_LIVE_PROVIDER_NAMES = (
    "openai",
    "anthropic",
    "gemini",
    "mistral",
    "xai",
    "qwen",
    "cohere",
)
OPTIONAL_PROVIDER_NAMES = ("upstage",)
KNOWN_PROVIDER_NAMES = DEFAULT_LIVE_PROVIDER_NAMES + OPTIONAL_PROVIDER_NAMES
_PROVIDERS_ADDED_AFTER_V0_2 = frozenset(
    {"xai", "qwen", "cohere", "upstage"}
)
_SECRET_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class AppConfig:
    providers: tuple[ProviderConfig, ...]
    policy: RunPolicy
    synthesis_provider: str
    data_dir: Path


def default_data_dir() -> Path:
    configured = os.environ.get("MODEL_COUNCIL_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "model-council"


def _default_provider(name: str) -> ProviderConfig:
    model_override = os.environ.get(f"MODEL_COUNCIL_{name.upper()}_MODEL")
    stage_output_tokens = dict(DEFAULT_STAGE_MAX_OUTPUT_TOKENS)
    stage_timeout_seconds = dict(DEFAULT_STAGE_TIMEOUT_SECONDS)
    extra: dict[str, Any] = {}
    if name == "gemini":
        # Gemini 3 Flash defaults to medium thinking. Thinking tokens count
        # against the generation budget, which made bounded council artifacts
        # truncate before visible output completed.
        stage_output_tokens = {
            stage: 4_096 for stage in COUNCIL_STAGES
        }
        extra["thinking_level"] = "low"
    elif name == "qwen":
        # Qwen's JSON-object mode is less reliable when thinking is enabled.
        # Keep the bounded council protocol deterministic unless an operator
        # explicitly opts into and budgets thinking in a reviewed config.
        extra["enable_thinking"] = False
        # Live canaries show that Qwen's long council prompts can legitimately
        # run past the generic stage limits. Keep them bounded, but leave
        # enough room for a complete response.
        stage_timeout_seconds = {
            "proposal": 300.0,
            "jury": 300.0,
            "synthesis": 360.0,
        }
    elif name == "cohere":
        # Command A+ enables thinking by default. Bound it so the 2,200-token
        # jury allowance still leaves more than Cohere's recommended 1,000
        # tokens for the visible response.
        extra["thinking"] = {"token_budget": 800}
        # Cohere cannot decoder-enforce CouncilLogic's array-item bounds.
        # Zero-temperature sampling reduces count drift in structured artifacts.
        extra["temperature"] = 0
    elif name == "upstage":
        # Solar Pro 3 medium/high reasoning reserves at least 4,096/8,192
        # tokens. Low disables reasoning and fits the bounded council stages.
        extra["reasoning_effort"] = "low"
    return ProviderConfig(
        name=name,
        model=model_override or DEFAULT_MODELS[name],
        lineage=DEFAULT_LINEAGES[name],
        secret_name=DEFAULT_SECRETS[name],
        endpoint=DEFAULT_ENDPOINTS[name],
        stage_max_output_tokens=stage_output_tokens,
        stage_timeout_seconds=stage_timeout_seconds,
        extra=extra,
    )


def default_config() -> AppConfig:
    return AppConfig(
        providers=tuple(
            _default_provider(name)
            for name in DEFAULT_LIVE_PROVIDER_NAMES
        ),
        # Seven clean participants require 15 calls. Preserve the previous
        # five-call recovery margin rather than making the larger default
        # topology consume the entire application-level budget.
        policy=RunPolicy(max_calls=20, jury_repair_attempts=1),
        synthesis_provider="openai",
        data_dir=default_data_dir(),
    )


def _validate_endpoint(config: ProviderConfig) -> None:
    rendered = config.endpoint.replace("{model}", config.model)
    parsed = urlparse(rendered)
    provider_kind = "mock" if config.name.startswith("mock") else config.name
    if parsed.scheme != "https" and provider_kind != "mock":
        raise ValueError(f"{config.name}: provider endpoint must use HTTPS")
    allowed = ALLOWED_ENDPOINT_HOSTS.get(provider_kind)
    if not allowed or parsed.hostname not in allowed:
        raise ValueError(
            f"{config.name}: endpoint host {parsed.hostname!r} is not allowlisted"
        )
    if parsed.port not in (None, 443) and provider_kind != "mock":
        raise ValueError(f"{config.name}: endpoint port must be 443")
    if parsed.username or parsed.password:
        raise ValueError(f"{config.name}: endpoint must not contain credentials")


def validate_provider_config(config: ProviderConfig) -> None:
    for field_name in ("name", "model", "lineage", "secret_name", "endpoint"):
        value = getattr(config, field_name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"{config.name or 'provider'}: {field_name} must be non-empty"
            )
    if not _SECRET_NAME_RE.fullmatch(config.secret_name):
        raise ValueError(
            f"{config.name}: secret_name must be an environment-style identifier"
        )
    if config.max_output_tokens < 1:
        raise ValueError(f"{config.name}: max_output_tokens must be positive")
    if config.timeout_seconds <= 0:
        raise ValueError(f"{config.name}: timeout_seconds must be positive")
    if not 1 <= config.max_attempts <= 10:
        raise ValueError(f"{config.name}: max_attempts must be between 1 and 10")
    for field_name, values in (
        ("stage_max_output_tokens", config.stage_max_output_tokens),
        ("stage_timeout_seconds", config.stage_timeout_seconds),
    ):
        unknown = set(values) - COUNCIL_STAGES
        if unknown:
            raise ValueError(
                f"{config.name}: {field_name} has unknown stages: "
                f"{', '.join(sorted(unknown))}"
            )
        for stage, value in values.items():
            if value <= 0:
                raise ValueError(
                    f"{config.name}: {field_name}.{stage} must be positive"
                )
    _validate_endpoint(config)


def validate_run_policy(
    policy: RunPolicy,
    providers: tuple[ProviderConfig, ...] | list[ProviderConfig],
) -> None:
    if not providers:
        raise ValueError("At least one provider must be enabled")
    for field_name in ("proposal_quorum", "jury_quorum", "min_lineages"):
        if getattr(policy, field_name) < 1:
            raise ValueError(f"{field_name} must be positive")
    if policy.deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be positive")
    if policy.max_parallel_calls < 1:
        raise ValueError("max_parallel_calls must be positive")
    if policy.max_question_chars < 1:
        raise ValueError("max_question_chars must be positive")
    if policy.max_stage_prompt_chars < 1:
        raise ValueError("max_stage_prompt_chars must be positive")
    if not 0 <= policy.jury_repair_attempts <= 1:
        raise ValueError("jury_repair_attempts must be 0 or 1")
    if not 0 <= policy.truncation_retries <= 1:
        raise ValueError("truncation_retries must be 0 or 1")
    if policy.max_recovery_output_tokens < 1:
        raise ValueError("max_recovery_output_tokens must be positive")
    if policy.proposal_quorum > len(providers):
        raise ValueError("Proposal quorum exceeds enabled provider count")
    if policy.jury_quorum > len(providers):
        raise ValueError("Jury quorum exceeds enabled provider count")
    lineage_count = len({provider.lineage for provider in providers})
    if policy.min_lineages > lineage_count:
        raise ValueError("Configured providers do not meet minimum lineage diversity")
    required_calls = len(providers) * 2 + 1
    if policy.max_calls < required_calls:
        raise ValueError(
            "Max calls is too small for proposals, juries, and synthesis"
        )


def load_config(path: str | Path | None = None) -> AppConfig:
    base = default_config()
    if path is None:
        for provider in base.providers:
            validate_provider_config(provider)
        validate_run_policy(base.policy, base.providers)
        return base

    config_path = Path(path).expanduser()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    run_raw = dict(raw.get("run") or {})
    policy_raw = dict(raw.get("policy") or {})
    provider_raw = dict(raw.get("providers") or {})

    providers: list[ProviderConfig] = []
    defaults = {
        name: _default_provider(name)
        for name in KNOWN_PROVIDER_NAMES
    }
    for name, default in defaults.items():
        # A file-backed configuration written before a provider was added must
        # not silently acquire another credential requirement or billable
        # participant. New configurations opt in by including its section.
        if name in _PROVIDERS_ADDED_AFTER_V0_2 and name not in provider_raw:
            continue
        override: dict[str, Any] = dict(provider_raw.get(name) or {})
        provider = ProviderConfig(
            name=name,
            model=str(override.get("model", default.model)),
            lineage=str(override.get("lineage", default.lineage)),
            secret_name=str(override.get("secret_name", default.secret_name)),
            endpoint=str(override.get("endpoint", default.endpoint)),
            max_output_tokens=int(
                override.get("max_output_tokens", default.max_output_tokens)
            ),
            timeout_seconds=float(
                override.get("timeout_seconds", default.timeout_seconds)
            ),
            max_attempts=int(override.get("max_attempts", default.max_attempts)),
            enabled=bool(override.get("enabled", default.enabled)),
            stage_max_output_tokens={
                str(stage): int(limit)
                for stage, limit in dict(
                    override.get(
                        "stage_max_output_tokens",
                        default.stage_max_output_tokens,
                    )
                    or {}
                ).items()
            },
            stage_timeout_seconds={
                str(stage): float(limit)
                for stage, limit in dict(
                    override.get(
                        "stage_timeout_seconds",
                        default.stage_timeout_seconds,
                    )
                    or {}
                ).items()
            },
            extra={
                **default.extra,
                **dict(override.get("extra") or {}),
            },
        )
        if provider.enabled:
            validate_provider_config(provider)
            providers.append(provider)

    # File-backed configurations retain the pre-expansion policy defaults.
    # This avoids silently increasing the billable recovery ceiling for an
    # existing operator config; new seven-provider files should set 20.
    policy = RunPolicy.from_dict({**RunPolicy().to_dict(), **policy_raw})
    synthesis_provider = str(
        run_raw.get("synthesis_provider", base.synthesis_provider)
    )
    data_dir = Path(run_raw.get("data_dir", str(base.data_dir))).expanduser()

    names = {provider.name for provider in providers}
    if synthesis_provider not in names:
        raise ValueError("Configured synthesis provider is not enabled")
    validate_run_policy(policy, providers)

    return AppConfig(
        providers=tuple(providers),
        policy=policy,
        synthesis_provider=synthesis_provider,
        data_dir=data_dir,
    )


def mock_config(data_dir: str | Path | None = None) -> AppConfig:
    providers = tuple(
        ProviderConfig(
            name=f"mock-{index}",
            model=f"mock-model-{index}",
            lineage=f"mock-lineage-{index}",
            secret_name=f"MOCK_KEY_{index}",
            endpoint="http://localhost/mock",
            max_attempts=1,
            extra={"seed": index},
        )
        for index in range(1, 5)
    )
    config = AppConfig(
        providers=providers,
        policy=RunPolicy(),
        synthesis_provider="mock-1",
        data_dir=Path(data_dir).expanduser() if data_dir else default_data_dir(),
    )
    for provider in config.providers:
        validate_provider_config(provider)
    validate_run_policy(config.policy, config.providers)
    return config
