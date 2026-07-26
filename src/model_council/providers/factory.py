from __future__ import annotations

from model_council.models import ProviderConfig

from .anthropic import AnthropicProvider
from .base import Provider
from .gemini import GeminiProvider
from .mistral import MistralProvider
from .mock import MockProvider
from .openai import OpenAIProvider
from .xai import XAIProvider


def create_provider(
    config: ProviderConfig,
    api_key: str | None = None,
) -> Provider:
    if config.name.startswith("mock"):
        return MockProvider(config, api_key or "mock-only-sentinel")
    providers: dict[str, type[Provider]] = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "gemini": GeminiProvider,
        "mistral": MistralProvider,
        "xai": XAIProvider,
    }
    provider_type = providers.get(config.name)
    if provider_type is None:
        raise ValueError(f"Unsupported provider: {config.name}")
    if not api_key:
        raise ValueError(f"Missing credential for provider {config.name}")
    return provider_type(config, api_key)
