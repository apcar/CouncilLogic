from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass, replace
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
for _hosts_name in list(ALLOWED_ENDPOINT_HOSTS):
    if _hosts_name != "mock":
        ALLOWED_ENDPOINT_HOSTS[_hosts_name] = (
            ALLOWED_ENDPOINT_HOSTS[_hosts_name] | {"openrouter.ai"}
        )

OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_SECRET = "OPENROUTER_API_KEY"
# OpenRouter model ids for each heterogeneous seat. Lineages are unchanged, so
# routing through one key keeps the same seven independent model families.
# Verified against the public list on 2026-11-01. Verify ids at https://openrouter.ai/models and override per seat with
# ``openrouter_model`` when a newer release is listed.
DEFAULT_OPENROUTER_MODELS = {
    "openai": "openai/gpt-5.6-sol",
    "anthropic": "anthropic/claude-sonnet-5.5",
    "gemini": "google/gemini-3.8-flash",
    "mistral": "mistralai/mistral-medium-3-5",
    "xai": "x-ai/grok-4.7",
    "qwen": "qwen/qwen3.8-max-0902",
    "cohere": "cohere/command-a-plus",
    "upstage": "upstage/solar-pro4",
}
# Live 7-seat trial (2026-11-01): reasoning models spent most of the bounded
# jury budget on hidden reasoning (Cohere 2,320 of 2,821 tokens) and hit
# finish_reason=length. Bound reasoning per seat via OpenRouter's unified
# ``reasoning`` parameter to leave more room for visible output. Truncation
# can still require bounded recovery.
DEFAULT_OPENROUTER_EXTRA: dict[str, dict[str, Any]] = {
    "cohere": {"reasoning": {"max_tokens": 800}},
    "qwen": {"reasoning": {"max_tokens": 800}},
    "xai": {"reasoning": {"effort": "low"}},
    "gemini": {"reasoning": {"effort": "low"}},
}
ROUTES = ("direct", "openrouter")

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
    synthesis_fallbacks: tuple[str, ...] = ()

    @property
    def synthesis_providers(self) -> tuple[str, ...]:
        return (self.synthesis_provider, *self.synthesis_fallbacks)


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


# OpenRouter model ids are "<vendor>/<model>"; the vendor prefix identifies
# the model family, which must match the seat's declared lineage.
OPENROUTER_PREFIX_LINEAGES = {
    "openai": "openai-gpt",
    "anthropic": "anthropic-claude",
    "google": "google-gemini",
    "mistralai": "mistral",
    "x-ai": "xai-grok",
    "qwen": "alibaba-qwen",
    "cohere": "cohere-command",
    "upstage": "upstage-solar",
}


# Direct provider seat -> the OpenRouter vendor prefix for the same models.
DIRECT_PROVIDER_PREFIXES = {
    "openai": "openai",
    "anthropic": "anthropic",
    "gemini": "google",
    "mistral": "mistralai",
    "xai": "x-ai",
    "qwen": "qwen",
    "cohere": "cohere",
    "upstage": "upstage",
}


def model_identity(provider: ProviderConfig) -> str:
    """Route-independent model identity used for duplicate detection.

    Direct ``gpt-5.6-sol`` and OpenRouter ``openai/gpt-5.6-sol`` normalize to
    the same vendor-qualified identity, compared case-insensitively. Preserve
    the namespace: different vendors can use the same short model name.
    """

    model = provider.model.strip().lower()
    if _is_openrouter_endpoint(provider.endpoint):
        return model
    prefix = DIRECT_PROVIDER_PREFIXES.get(provider.name, provider.name)
    if model.startswith(prefix + "/"):
        model = model[len(prefix) + 1:]
    if model.startswith("models/"):
        model = model[len("models/"):]
    return f"{prefix}/{model}"


def _is_openrouter_endpoint(endpoint: str) -> bool:
    host = (urlparse(endpoint.replace("{model}", "m")).hostname or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def lineage_report(
    providers: tuple[ProviderConfig, ...] | list[ProviderConfig],
) -> dict[str, Any]:
    """Verify each seat's lineage against its model identity.

    OpenRouter seats derive their lineage from the model id prefix; a known
    prefix that disagrees with the declared lineage is a mismatch, an unknown
    prefix leaves diversity unverified. Two seats sharing one model identity
    are duplicates. Direct seats are trusted to their configured lineage.
    """

    mismatches: list[str] = []
    unverified: list[str] = []
    seen: dict[str, str] = {}
    duplicates: list[str] = []
    lineages: set[str] = set()
    for provider in providers:
        lineage = provider.lineage
        if not provider.name.startswith("mock"):
            identity = model_identity(provider)
            if _is_openrouter_endpoint(provider.endpoint):
                prefix, sep, _rest = provider.model.strip().lower().partition("/")
                derived = OPENROUTER_PREFIX_LINEAGES.get(prefix) if sep else None
                if derived is None:
                    unverified.append(provider.name)
                elif derived != provider.lineage:
                    mismatches.append(
                        f"{provider.name}: model {provider.model} is "
                        f"{derived}, not declared {provider.lineage}"
                    )
                    lineage = derived
            if identity in seen:
                duplicates.append(
                    f"{provider.name} and {seen[identity]} both use {provider.model}"
                )
            else:
                seen[identity] = provider.name
        lineages.add(lineage)
    status = "verified"
    if unverified:
        status = "unverified"
    if mismatches or duplicates:
        status = "invalid"
    return {
        "status": status,
        "lineage_count": len(lineages),
        "unverified_providers": unverified,
        "mismatches": mismatches,
        "duplicate_models": duplicates,
    }


def default_route() -> str:
    route = os.environ.get("MODEL_COUNCIL_ROUTE", "direct").strip().lower()
    if route not in ROUTES:
        raise ValueError("MODEL_COUNCIL_ROUTE must be 'direct' or 'openrouter'")
    return route


def route_via_openrouter(
    config: ProviderConfig, *, model: str | None = None
) -> ProviderConfig:
    """Return the same seat (name, lineage, budgets) sent through OpenRouter."""

    if config.name.startswith("mock"):
        return config
    env_model = os.environ.get(
        f"MODEL_COUNCIL_{config.name.upper()}_OPENROUTER_MODEL"
    )
    routed_model = (
        model
        or env_model
        or DEFAULT_OPENROUTER_MODELS.get(config.name)
        or config.model
    )
    # Direct-API tuning keys (thinking_level, enable_thinking, ...) do not apply
    # to OpenRouter's chat schema; keep only the portable sampling option.
    extra = {
        key: value
        for key, value in config.extra.items()
        if key in {"temperature", "reasoning", "provider"}
    }
    extra = {**DEFAULT_OPENROUTER_EXTRA.get(config.name, {}), **extra}
    return replace(
        config,
        model=routed_model,
        secret_name=OPENROUTER_SECRET,
        endpoint=OPENROUTER_ENDPOINT,
        extra=extra,
    )


def default_config() -> AppConfig:
    route = default_route()
    providers = tuple(
        _default_provider(name) for name in DEFAULT_LIVE_PROVIDER_NAMES
    )
    if route == "openrouter":
        providers = tuple(route_via_openrouter(p) for p in providers)
    return AppConfig(
        providers=providers,
        # Seven participants plus three ordered synthesis attempts reserve
        # 17 calls, leaving three application-level recovery calls.
        policy=RunPolicy(max_calls=20, jury_repair_attempts=1),
        synthesis_provider="openai",
        data_dir=default_data_dir(),
        synthesis_fallbacks=("anthropic", "gemini"),
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
    if (
        isinstance(config.timeout_seconds, bool)
        or not math.isfinite(config.timeout_seconds)
        or config.timeout_seconds <= 0
    ):
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
            if (
                value <= 0
                or (
                    field_name == "stage_timeout_seconds"
                    and (
                        isinstance(value, bool)
                        or not math.isfinite(value)
                    )
                )
            ):
                raise ValueError(
                    f"{config.name}: {field_name}.{stage} must be positive"
                )
    _validate_endpoint(config)


def validate_run_policy(
    policy: RunPolicy,
    providers: tuple[ProviderConfig, ...] | list[ProviderConfig],
    *,
    synthesis_provider_count: int = 1,
) -> None:
    if not providers:
        raise ValueError("At least one provider must be enabled")
    for field_name in ("proposal_quorum", "jury_quorum", "min_lineages"):
        if getattr(policy, field_name) < 1:
            raise ValueError(f"{field_name} must be positive")
    if (
        not math.isfinite(policy.deadline_seconds)
        or policy.deadline_seconds <= 0
    ):
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
    diversity = lineage_report(providers)
    if diversity["mismatches"]:
        raise ValueError(
            "OpenRouter model does not match seat lineage: "
            + "; ".join(diversity["mismatches"])
        )
    if diversity["duplicate_models"]:
        raise ValueError(
            "Duplicate model identity across seats: "
            + "; ".join(diversity["duplicate_models"])
        )
    lineage_count = diversity["lineage_count"]
    if policy.min_lineages > lineage_count:
        raise ValueError("Configured providers do not meet minimum lineage diversity")
    if synthesis_provider_count < 1:
        raise ValueError("At least one synthesis provider is required")
    required_calls = len(providers) * 2 + synthesis_provider_count
    if policy.max_calls < required_calls:
        raise ValueError(
            "Max calls is too small for proposals, juries, and configured "
            "synthesis attempts"
        )


def validate_synthesis_providers(
    synthesis_provider: str,
    synthesis_fallbacks: tuple[str, ...] | list[str],
    providers: tuple[ProviderConfig, ...] | list[ProviderConfig],
) -> tuple[str, ...]:
    chain = (synthesis_provider, *tuple(synthesis_fallbacks))
    if any(not isinstance(name, str) or not name.strip() for name in chain):
        raise ValueError("Synthesis provider names must be non-empty strings")
    if len(set(chain)) != len(chain):
        raise ValueError("Synthesis providers must not contain duplicates")
    available = {provider.name for provider in providers}
    unavailable = [name for name in chain if name not in available]
    if unavailable:
        raise ValueError(
            "Configured synthesis providers are not enabled: "
            + ", ".join(unavailable)
        )
    return chain


def load_config(path: str | Path | None = None) -> AppConfig:
    base = default_config()
    if path is None:
        for provider in base.providers:
            validate_provider_config(provider)
        validate_synthesis_providers(
            base.synthesis_provider,
            base.synthesis_fallbacks,
            base.providers,
        )
        validate_run_policy(
            base.policy,
            base.providers,
            synthesis_provider_count=len(base.synthesis_providers),
        )
        return base

    config_path = Path(path).expanduser()
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    run_raw = dict(raw.get("run") or {})
    policy_raw = dict(raw.get("policy") or {})
    provider_raw = dict(raw.get("providers") or {})

    run_route = str(
        run_raw.get("route", os.environ.get("MODEL_COUNCIL_ROUTE", "direct"))
    ).strip().lower()
    if run_route not in ROUTES:
        raise ValueError("run.route must be 'direct' or 'openrouter'")

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
        seat_route = str(override.get("route", run_route)).strip().lower()
        if seat_route not in ROUTES:
            raise ValueError(
                f"{name}: route must be 'direct' or 'openrouter'"
            )
        if seat_route == "openrouter":
            routed = route_via_openrouter(
                provider,
                model=(
                    str(override["openrouter_model"])
                    if "openrouter_model" in override
                    else None
                ),
            )
            provider = replace(
                routed,
                secret_name=str(
                    override.get("openrouter_secret_name", OPENROUTER_SECRET)
                ),
                extra={
                    **routed.extra,
                    **dict(override.get("openrouter_extra") or {}),
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
    raw_synthesis_fallbacks = run_raw.get("synthesis_fallbacks", [])
    if not isinstance(raw_synthesis_fallbacks, list):
        raise ValueError("run.synthesis_fallbacks must be an array")
    synthesis_fallbacks = tuple(raw_synthesis_fallbacks)
    data_dir = Path(run_raw.get("data_dir", str(base.data_dir))).expanduser()

    synthesis_chain = validate_synthesis_providers(
        synthesis_provider,
        synthesis_fallbacks,
        providers,
    )
    validate_run_policy(
        policy,
        providers,
        synthesis_provider_count=len(synthesis_chain),
    )

    return AppConfig(
        providers=tuple(providers),
        policy=policy,
        synthesis_provider=synthesis_provider,
        data_dir=data_dir,
        synthesis_fallbacks=synthesis_fallbacks,
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
    validate_synthesis_providers(
        config.synthesis_provider,
        config.synthesis_fallbacks,
        config.providers,
    )
    validate_run_policy(
        config.policy,
        config.providers,
        synthesis_provider_count=len(config.synthesis_providers),
    )
    return config
