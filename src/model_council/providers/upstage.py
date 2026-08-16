from __future__ import annotations

import math
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


_REASONING_EFFORTS = frozenset({"minimal", "low", "medium", "high"})
_UNSUPPORTED_STRUCTURED_OUTPUT_CONSTRAINTS = frozenset(
    {
        "maxItems",
        "maxLength",
        "minItems",
        "minLength",
    }
)


def _upstage_schema(value: Any) -> Any:
    """Keep Upstage's constrained decoder on its documented schema subset."""

    if isinstance(value, dict):
        return {
            key: _upstage_schema(item)
            for key, item in value.items()
            if key not in _UNSUPPORTED_STRUCTURED_OUTPUT_CONSTRAINTS
        }
    if isinstance(value, list):
        return [_upstage_schema(item) for item in value]
    return value


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


def _reasoning_effort(config: ProviderConfig) -> str:
    value = config.extra.get("reasoning_effort", "low")
    if not isinstance(value, str) or value not in _REASONING_EFFORTS:
        raise ProviderError(
            "Upstage configuration is invalid: extra.reasoning_effort must be "
            "minimal, low, medium, or high",
            category=ErrorCategory.INVALID_REQUEST,
            retryable=False,
        )
    return value


def _number_option(
    config: ProviderConfig,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> int | float | None:
    if name not in config.extra:
        return None
    value = config.extra[name]
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value < minimum
        or value > maximum
    ):
        raise ProviderError(
            f"Upstage configuration is invalid: extra.{name} must be a "
            f"finite number between {minimum:g} and {maximum:g}",
            category=ErrorCategory.INVALID_REQUEST,
            retryable=False,
        )
    return value


class UpstageProvider(Provider):
    """Upstage's OpenAI-compatible Chat Completions API for Solar.

    Solar Pro 3's documented structured-output subset does not include the
    nullable union used by CouncilLogic's jury schema. Proposals therefore use
    strict JSON Schema while juries use JSON-object mode and remain subject to
    CouncilLogic's authoritative local parser and validation.
    """

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
        if stage == "jury" and "json" not in (
            f"{system_prompt}\n{user_prompt}".casefold()
        ):
            # Upstage requires an explicit JSON instruction in JSON-object mode.
            system_prompt = (
                f"{system_prompt.rstrip()}\n\n"
                "Return the required object as valid JSON."
            )

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
            # Medium and high reserve at least 4,096 and 8,192 reasoning tokens,
            # respectively. Low is the reliable bounded-workload default.
            "reasoning_effort": _reasoning_effort(self.config),
        }
        if stage == "proposal" and output_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "model_council_proposal",
                    "strict": True,
                    "schema": _upstage_schema(output_schema),
                },
            }
        elif stage == "jury":
            payload["response_format"] = {"type": "json_object"}

        for option, minimum, maximum in (
            ("temperature", 0.0, 2.0),
            ("top_p", 0.0, 1.0),
            ("frequency_penalty", -2.0, 2.0),
            ("presence_penalty", -2.0, 2.0),
        ):
            value = _number_option(
                self.config,
                option,
                minimum=minimum,
                maximum=maximum,
            )
            if value is not None:
                payload[option] = value

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
        finish_reason = _finish_reason(raw_finish_reason)
        if finish_reason not in {"stop", "length"}:
            raise ProviderError(
                "Upstage response did not complete normally"
                f" (finish_reason={raw_finish_reason or 'missing'})",
                category=(
                    ErrorCategory.CONTENT_FILTER
                    if finish_reason == "content_filter"
                    else ErrorCategory.INVALID_RESPONSE
                ),
                retryable=False,
                status_code=result.status_code,
                request_id=result.request_id,
                attempts=result.attempts,
                ambiguous=False,
            )
        if not content and finish_reason != "length":
            raise ProviderError(
                "Upstage response did not contain generated text",
                category=ErrorCategory.INVALID_RESPONSE,
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
            finish_reason=finish_reason,
            metadata={
                "stage": stage,
                "client_request_id": result.client_request_id,
                "provider_finish_reason": raw_finish_reason,
            },
        )
