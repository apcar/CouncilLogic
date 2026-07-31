from __future__ import annotations

import time
from typing import Any

from model_council.models import (
    ErrorCategory,
    ProviderConfig,
    ProviderError,
    ProviderResponse,
    Usage,
)
from model_council.protocol import structured_output_schema

from .base import Provider
from .http import JsonHttpClient, JsonResponse


_UNSUPPORTED_STRUCTURED_OUTPUT_CONSTRAINTS = frozenset(
    {"minLength", "maxLength", "maxItems"}
)


def _anthropic_schema(value: Any) -> Any:
    """Remove bounds Anthropic rejects while preserving local validation."""

    if isinstance(value, dict):
        return {
            key: _anthropic_schema(item)
            for key, item in value.items()
            if key not in _UNSUPPORTED_STRUCTURED_OUTPUT_CONSTRAINTS
        }
    if isinstance(value, list):
        return [_anthropic_schema(item) for item in value]
    return value


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _finish_reason(value: str | None) -> str | None:
    if value in {"end_turn", "stop_sequence", "stop"}:
        return "stop"
    if value in {"max_tokens", "length"}:
        return "length"
    if value in {"tool_use", "tool_call"}:
        return "tool_call"
    if value in {"refusal", "content_filter"}:
        return "content_filter"
    return value


class AnthropicProvider(Provider):
    def __init__(
        self,
        config: ProviderConfig,
        api_key: str,
        *,
        client: JsonHttpClient | None = None,
    ) -> None:
        super().__init__(config, api_key)
        self._client = client or JsonHttpClient()

    def generate(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        stage: str,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ProviderResponse:
        output_schema = structured_output_schema(stage)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": (
                max_output_tokens
                if max_output_tokens is not None
                else self.config.output_tokens_for(stage)
            ),
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if output_schema is not None:
            payload["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": _anthropic_schema(output_schema),
                }
            }
        if "temperature" in self.config.extra:
            payload["temperature"] = self.config.extra["temperature"]

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": str(
                self.config.extra.get("anthropic_version", "2023-06-01")
            ),
        }
        beta = self.config.extra.get("anthropic_beta")
        if isinstance(beta, str) and beta:
            headers["anthropic-beta"] = beta

        started = time.monotonic()
        result = self._client.post_json(
            url=self.config.endpoint,
            headers=headers,
            payload=payload,
            timeout_seconds=(
                timeout_seconds
                if timeout_seconds is not None
                else self.config.timeout_for(stage)
            ),
            max_attempts=self.config.max_attempts,
        )
        return self._parse(
            result,
            stage=stage,
            latency_ms=max(0, round((time.monotonic() - started) * 1000)),
        )

    def _parse(
        self,
        result: JsonResponse,
        *,
        stage: str,
        latency_ms: int,
    ) -> ProviderResponse:
        data = result.data
        blocks = data.get("content")
        text_parts: list[str] = []
        if isinstance(blocks, list):
            for block in blocks:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    text_parts.append(block["text"])
        content = "".join(text_parts).strip()
        raw_finish_reason = data.get("stop_reason")
        if not isinstance(raw_finish_reason, str):
            raw_finish_reason = None
        finish_reason = _finish_reason(raw_finish_reason)
        if not content and finish_reason != "length":
            category = (
                ErrorCategory.CONTENT_FILTER
                if raw_finish_reason in {"refusal", "content_filter"}
                else ErrorCategory.INVALID_RESPONSE
            )
            raise ProviderError(
                "Anthropic response did not contain generated text",
                category=category,
                retryable=False,
                status_code=result.status_code,
                request_id=result.request_id,
                attempts=result.attempts,
                ambiguous=True,
            )

        usage_raw = data.get("usage")
        usage_raw = usage_raw if isinstance(usage_raw, dict) else {}
        input_tokens = _integer(usage_raw.get("input_tokens"))
        output_tokens = _integer(usage_raw.get("output_tokens"))
        total_tokens = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
        cached_tokens = _integer(usage_raw.get("cache_read_input_tokens"))
        usage = Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            cached_input_tokens=cached_tokens,
        )

        resolved_model = data.get("model")
        if not isinstance(resolved_model, str) or not resolved_model:
            resolved_model = self.config.model
        request_id = result.request_id
        if request_id is None and isinstance(data.get("id"), str):
            request_id = data["id"]
        return ProviderResponse(
            content=content,
            resolved_model=resolved_model,
            request_id=request_id,
            usage=usage,
            latency_ms=latency_ms,
            attempts=result.attempts,
            finish_reason=finish_reason,
            metadata={
                "stage": stage,
                "client_request_id": result.client_request_id,
                "provider_finish_reason": raw_finish_reason,
            },
        )
