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


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _finish_reason(value: str | None) -> str | None:
    if value in {"stop", "eos"}:
        return "stop"
    if value in {"length", "max_tokens", "max_completion_tokens"}:
        return "length"
    if value in {"tool_call", "tool_calls"}:
        return "tool_call"
    if value in {"content_filter", "refusal", "sensitive"}:
        return "content_filter"
    return value


def _invalid_option(message: str) -> ProviderError:
    return ProviderError(
        f"Qwen configuration is invalid: {message}",
        category=ErrorCategory.INVALID_REQUEST,
        retryable=False,
    )


def _optional_number(
    extra: dict[str, Any],
    name: str,
    *,
    minimum_inclusive: float | None = None,
    minimum_exclusive: float | None = None,
    maximum_inclusive: float | None = None,
    maximum_exclusive: float | None = None,
) -> int | float | None:
    if name not in extra:
        return None
    value = extra[name]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise _invalid_option(f"extra.{name} must be numeric")
    if not math.isfinite(float(value)):
        raise _invalid_option(f"extra.{name} must be finite")
    if minimum_inclusive is not None and value < minimum_inclusive:
        raise _invalid_option(
            f"extra.{name} must be at least {minimum_inclusive:g}"
        )
    if minimum_exclusive is not None and value <= minimum_exclusive:
        raise _invalid_option(
            f"extra.{name} must be greater than {minimum_exclusive:g}"
        )
    if maximum_inclusive is not None and value > maximum_inclusive:
        raise _invalid_option(
            f"extra.{name} must not exceed {maximum_inclusive:g}"
        )
    if maximum_exclusive is not None and value >= maximum_exclusive:
        raise _invalid_option(
            f"extra.{name} must be less than {maximum_exclusive:g}"
        )
    return value


class QwenProvider(Provider):
    """Alibaba Model Studio's OpenAI-compatible Chat Completions API."""

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
        if output_schema is not None and "json" not in (
            f"{system_prompt}\n{user_prompt}".casefold()
        ):
            # Alibaba requires the prompt itself to mention JSON whenever
            # response_format=json_object is used. CouncilLogic still performs
            # the authoritative schema validation after this JSON-mode call.
            system_prompt = (
                f"{system_prompt.rstrip()}\n\n"
                "Return the required object as valid JSON."
            )

        extra = self.config.extra
        enable_thinking = extra.get("enable_thinking", False)
        if not isinstance(enable_thinking, bool):
            raise _invalid_option("extra.enable_thinking must be a boolean")

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            # max_tokens is deprecated by Alibaba. max_completion_tokens also
            # bounds reasoning tokens if thinking is explicitly enabled.
            "max_completion_tokens": (
                max_output_tokens
                if max_output_tokens is not None
                else self.config.output_tokens_for(stage)
            ),
            "stream": False,
            # Qwen 3.7 defaults to thinking. Council calls are deliberately
            # bounded and non-streaming, so require an explicit opt-in.
            "enable_thinking": enable_thinking,
        }
        if output_schema is not None:
            # Model Studio currently supports JSON-object mode, not strict JSON
            # Schema, for this interface. The protocol parser validates the
            # returned object against CouncilLogic's stricter local contract.
            payload["response_format"] = {"type": "json_object"}

        thinking_budget = _optional_number(
            extra,
            "thinking_budget",
            minimum_exclusive=0,
        )
        if thinking_budget is not None:
            if not isinstance(thinking_budget, int):
                raise _invalid_option("extra.thinking_budget must be an integer")
            if not enable_thinking:
                raise _invalid_option(
                    "extra.thinking_budget requires extra.enable_thinking=true"
                )
            payload["thinking_budget"] = thinking_budget

        temperature = _optional_number(
            extra,
            "temperature",
            minimum_inclusive=0,
            maximum_exclusive=2,
        )
        top_p = _optional_number(
            extra,
            "top_p",
            minimum_exclusive=0,
            maximum_inclusive=1,
        )
        if temperature is not None and top_p is not None:
            raise _invalid_option("set only one of extra.temperature or extra.top_p")
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p

        if "seed" in extra:
            seed = extra["seed"]
            if (
                not isinstance(seed, int)
                or isinstance(seed, bool)
                or not 0 <= seed <= 2**31 - 1
            ):
                raise _invalid_option(
                    "extra.seed must be an integer between 0 and 2147483647"
                )
            payload["seed"] = seed

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
                "Qwen response did not complete normally"
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
            refused = bool(message.get("refusal"))
            raise ProviderError(
                "Qwen response did not contain generated text",
                category=(
                    ErrorCategory.CONTENT_FILTER
                    if refused
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
            finish_reason=finish_reason,
            metadata={
                "stage": stage,
                "client_request_id": result.client_request_id,
                "provider_finish_reason": raw_finish_reason,
            },
        )
