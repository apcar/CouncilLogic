from __future__ import annotations

from abc import ABC, abstractmethod

from model_council.models import ProviderConfig, ProviderResponse


class Provider(ABC):
    def __init__(self, config: ProviderConfig, api_key: str) -> None:
        if not api_key:
            raise ValueError(f"Missing credential for provider {config.name}")
        self.config = config
        self._api_key = api_key

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(name={self.config.name!r}, "
            f"model={self.config.model!r}, api_key='[REDACTED]')"
        )

    @abstractmethod
    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        stage: str,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ProviderResponse:
        """Generate one response without logging or persisting the credential."""

