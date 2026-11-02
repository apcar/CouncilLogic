from __future__ import annotations

from urllib.parse import urlparse

from model_council.models import ProviderConfig

from .anthropic import AnthropicProvider
from .base import Provider
from .cohere import CohereProvider
from .gemini import GeminiProvider
from .mistral import MistralProvider
from .mock import MockProvider
from .openai import OpenAIProvider
from .openrouter import OpenRouterProvider
from .qwen import QwenProvider
from .upstage import UpstageProvider
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
        "qwen": QwenProvider,
        "cohere": CohereProvider,
        "upstage": UpstageProvider,
    }
    if is_openrouter(config):
        if not api_key:
            raise ValueError(f"Missing credential for provider {config.name}")
        return OpenRouterProvider(config, api_key)
    provider_type = providers.get(config.name)
    if provider_type is None:
        raise ValueError(f"Unsupported provider: {config.name}")
    if not api_key:
        raise ValueError(f"Missing credential for provider {config.name}")
    return provider_type(config, api_key)


OPENROUTER_HOST = "openrouter.ai"


def is_openrouter(config: ProviderConfig) -> bool:
    """A seat is routed through OpenRouter when its endpoint is OpenRouter's."""

    return urlparse(config.endpoint).hostname == OPENROUTER_HOST
