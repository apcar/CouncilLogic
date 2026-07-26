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
from model_council.protocol import jury_json_schema

from .base import Provider
from .http import JsonHttpClient, JsonResponse


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _finish_reason(value: str | None) -> str | None:
    if value in {"completed", "stop"}:
        return "stop"
    if value in {"max_output_tokens", "max_tokens", "length"}:
        return "length"
    if value in {"content_filter", "refusal"}:
        return "content_filter"
    if value in {"tool_call", "tool_calls"}:
        return "tool_call"
    return value


class OpenAIProvider(Provider):
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
    ) -> ProviderResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "instructions": system_prompt,
            "input": user_prompt,
            "max_output_tokens": self.config.max_output_tokens,
            "store": False,
            "reasoning": {
                "effort": str(
                    self.config.extra.get("reasoning_effort", "low")
                )
            },
        }
        if stage == "jury":
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "model_council_jury",
                    "strict": True,
                    "schema": jury_json_schema(),
                }
            }
        if "temperature" in self.config.extra:
            payload["temperature"] = self.config.extra["temperature"]

        started = time.monotonic()
        result = self._client.post_json(
            url=self.config.endpoint,
            headers={"Authorization": f"Bearer {self._api_key}"},
            payload=payload,
            timeout_seconds=self.config.timeout_seconds,
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
        text_parts: list[str] = []
        refused = False
        output = data.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "output_text" and isinstance(
                        part.get("text"), str
                    ):
                        text_parts.append(part["text"])
                    elif part.get("type") == "refusal":
                        refused = True

        content = "".join(text_parts).strip()
        if not content and isinstance(data.get("output_text"), str):
            content = data["output_text"].strip()
        if not content:
            category = (
                ErrorCategory.CONTENT_FILTER
                if refused
                else ErrorCategory.INVALID_RESPONSE
            )
            raise ProviderError(
                "OpenAI response did not contain generated text",
                category=category,
                retryable=False,
                status_code=result.status_code,
                request_id=result.request_id,
                attempts=result.attempts,
                ambiguous=True,
            )

        usage_raw = data.get("usage")
        usage_raw = usage_raw if isinstance(usage_raw, dict) else {}
        input_details = usage_raw.get("input_tokens_details")
        input_details = input_details if isinstance(input_details, dict) else {}
        output_details = usage_raw.get("output_tokens_details")
        output_details = output_details if isinstance(output_details, dict) else {}
        usage = Usage(
            input_tokens=_integer(usage_raw.get("input_tokens")),
            output_tokens=_integer(usage_raw.get("output_tokens")),
            total_tokens=_integer(usage_raw.get("total_tokens")),
            cached_input_tokens=_integer(input_details.get("cached_tokens")),
            reasoning_tokens=_integer(output_details.get("reasoning_tokens")),
        )

        incomplete = data.get("incomplete_details")
        incomplete = incomplete if isinstance(incomplete, dict) else {}
        raw_finish_reason = incomplete.get("reason")
        if not isinstance(raw_finish_reason, str):
            status = data.get("status")
            raw_finish_reason = status if isinstance(status, str) else None
        finish_reason = _finish_reason(raw_finish_reason)

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
