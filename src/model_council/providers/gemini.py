from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

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
    if value in {"STOP", "stop"}:
        return "stop"
    if value in {"MAX_TOKENS", "length"}:
        return "length"
    if value in {
        "SAFETY",
        "BLOCKLIST",
        "PROHIBITED_CONTENT",
        "SPII",
        "RECITATION",
        "IMAGE_SAFETY",
        "content_filter",
    }:
        return "content_filter"
    if value in {"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"}:
        return "tool_error"
    return value.lower() if isinstance(value, str) else None


class GeminiProvider(Provider):
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
        generation_config: dict[str, Any] = {
            "maxOutputTokens": self.config.max_output_tokens
        }
        if stage == "jury":
            generation_config["responseFormat"] = {
                "text": {
                    "mimeType": "APPLICATION_JSON",
                    "schema": jury_json_schema(),
                }
            }
        if "temperature" in self.config.extra:
            generation_config["temperature"] = self.config.extra["temperature"]

        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": user_prompt}],
                }
            ],
            "generationConfig": generation_config,
        }
        endpoint = self.config.endpoint.replace(
            "{model}", quote(self.config.model, safe="")
        )
        started = time.monotonic()
        result = self._client.post_json(
            url=endpoint,
            headers={"x-goog-api-key": self._api_key},
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
        candidates = data.get("candidates")
        candidate = (
            candidates[0]
            if isinstance(candidates, list)
            and candidates
            and isinstance(candidates[0], dict)
            else None
        )
        text_parts: list[str] = []
        if candidate:
            content_raw = candidate.get("content")
            if isinstance(content_raw, dict):
                parts = content_raw.get("parts")
                if isinstance(parts, list):
                    for part in parts:
                        if isinstance(part, dict) and isinstance(
                            part.get("text"), str
                        ):
                            text_parts.append(part["text"])
        content = "".join(text_parts).strip()
        if not content:
            feedback = data.get("promptFeedback")
            feedback = feedback if isinstance(feedback, dict) else {}
            blocked = bool(feedback.get("blockReason"))
            if candidate and candidate.get("finishReason") in (
                "SAFETY",
                "BLOCKLIST",
                "PROHIBITED_CONTENT",
            ):
                blocked = True
            raise ProviderError(
                "Gemini response did not contain generated text",
                category=(
                    ErrorCategory.CONTENT_FILTER
                    if blocked
                    else ErrorCategory.INVALID_RESPONSE
                ),
                retryable=False,
                status_code=result.status_code,
                request_id=result.request_id,
                attempts=result.attempts,
                ambiguous=True,
            )

        usage_raw = data.get("usageMetadata")
        usage_raw = usage_raw if isinstance(usage_raw, dict) else {}
        usage = Usage(
            input_tokens=_integer(usage_raw.get("promptTokenCount")),
            output_tokens=_integer(usage_raw.get("candidatesTokenCount")),
            total_tokens=_integer(usage_raw.get("totalTokenCount")),
            cached_input_tokens=_integer(
                usage_raw.get("cachedContentTokenCount")
            ),
            reasoning_tokens=_integer(usage_raw.get("thoughtsTokenCount")),
        )
        resolved_model = data.get("modelVersion")
        if not isinstance(resolved_model, str) or not resolved_model:
            resolved_model = self.config.model
        request_id = result.request_id
        if request_id is None and isinstance(data.get("responseId"), str):
            request_id = data["responseId"]
        raw_finish_reason = candidate.get("finishReason") if candidate else None
        if not isinstance(raw_finish_reason, str):
            raw_finish_reason = None
        finish_reason = _finish_reason(raw_finish_reason)

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
