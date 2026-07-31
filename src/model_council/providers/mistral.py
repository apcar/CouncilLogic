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


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _finish_reason(value: str | None) -> str | None:
    if value in {"stop", "eos"}:
        return "stop"
    if value in {"length", "model_length", "max_tokens"}:
        return "length"
    if value in {"tool_call", "tool_calls"}:
        return "tool_call"
    if value in {"content_filter", "refusal"}:
        return "content_filter"
    return value


class MistralProvider(Provider):
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
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": (
                max_output_tokens
                if max_output_tokens is not None
                else self.config.output_tokens_for(stage)
            ),
            "stream": False,
            "reasoning_effort": str(
                self.config.extra.get("reasoning_effort", "none")
            ),
        }
        if output_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": f"model_council_{stage}",
                    "strict": True,
                    "schema": output_schema,
                },
            }
        for option in ("temperature", "random_seed"):
            if option in self.config.extra:
                payload[option] = self.config.extra[option]

        started = time.monotonic()
        result = self._client.post_json(
            url=self.config.endpoint,
            headers={"Authorization": f"Bearer {self._api_key}"},
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
        choices = data.get("choices")
        choice = (
            choices[0]
            if isinstance(choices, list)
            and choices
            and isinstance(choices[0], dict)
            else None
        )
        message = choice.get("message") if choice else None
        message = message if isinstance(message, dict) else {}
        content_raw = message.get("content")
        text_parts: list[str] = []
        if isinstance(content_raw, str):
            text_parts.append(content_raw)
        elif isinstance(content_raw, list):
            for part in content_raw:
                if isinstance(part, str):
                    text_parts.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
        content = "".join(text_parts).strip()

        raw_finish_reason = choice.get("finish_reason") if choice else None
        if not isinstance(raw_finish_reason, str):
            raw_finish_reason = None
        normalized_finish_reason = _finish_reason(raw_finish_reason)
        if normalized_finish_reason not in {"stop", "length"}:
            raise ProviderError(
                "Mistral response did not complete normally"
                f" (finish_reason={raw_finish_reason or 'missing'})",
                category=(
                    ErrorCategory.CONTENT_FILTER
                    if normalized_finish_reason == "content_filter"
                    else ErrorCategory.INVALID_RESPONSE
                ),
                retryable=False,
                status_code=result.status_code,
                request_id=result.request_id,
                attempts=result.attempts,
                ambiguous=False,
            )
        if not content and normalized_finish_reason != "length":
            raise ProviderError(
                "Mistral response did not contain generated text",
                category=(
                    ErrorCategory.CONTENT_FILTER
                    if raw_finish_reason in {"content_filter", "refusal"}
                    else ErrorCategory.INVALID_RESPONSE
                ),
                retryable=False,
                status_code=result.status_code,
                request_id=result.request_id,
                attempts=result.attempts,
                ambiguous=True,
            )

        usage_raw = data.get("usage")
        usage_raw = usage_raw if isinstance(usage_raw, dict) else {}
        prompt_details = usage_raw.get("prompt_tokens_details")
        prompt_details = (
            prompt_details if isinstance(prompt_details, dict) else {}
        )
        completion_details = usage_raw.get("completion_tokens_details")
        completion_details = (
            completion_details if isinstance(completion_details, dict) else {}
        )
        usage = Usage(
            input_tokens=_integer(usage_raw.get("prompt_tokens")),
            output_tokens=_integer(usage_raw.get("completion_tokens")),
            total_tokens=_integer(usage_raw.get("total_tokens")),
            cached_input_tokens=_integer(prompt_details.get("cached_tokens")),
            reasoning_tokens=_integer(completion_details.get("reasoning_tokens")),
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
            finish_reason=normalized_finish_reason,
            metadata={
                "stage": stage,
                "client_request_id": result.client_request_id,
                "provider_finish_reason": raw_finish_reason,
            },
        )
