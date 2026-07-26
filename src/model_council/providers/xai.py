from __future__ import annotations

from dataclasses import replace
from typing import Any

from model_council.models import ProviderResponse

from .http import JsonResponse, header_value
from .openai import OpenAIProvider, _integer


class XAIProvider(OpenAIProvider):
    """xAI's OpenAI-compatible Responses API for the Grok model family."""

    provider_label = "xAI"

    def _parse(
        self,
        result: JsonResponse,
        *,
        stage: str,
        latency_ms: int,
    ) -> ProviderResponse:
        parsed = super()._parse(
            result,
            stage=stage,
            latency_ms=latency_ms,
        )
        usage_raw = result.data.get("usage")
        usage_raw = usage_raw if isinstance(usage_raw, dict) else {}
        metadata: dict[str, Any] = dict(parsed.metadata)

        cost_ticks = _integer(usage_raw.get("cost_in_usd_ticks"))
        if cost_ticks is not None:
            metadata["cost_in_usd_ticks"] = cost_ticks

        zero_retention = header_value(
            result.headers,
            "x-zero-data-retention",
        )
        if zero_retention is not None:
            metadata["zero_data_retention"] = (
                zero_retention.strip().casefold() == "true"
            )

        return replace(parsed, metadata=metadata)
