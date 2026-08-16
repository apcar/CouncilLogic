from __future__ import annotations

from collections.abc import Mapping
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


# Cohere Structured Outputs implements a documented subset of JSON Schema.
# Removing provider-unsupported bounds affects only remote constrained decoding;
# CouncilLogic still validates the returned artifact against its canonical limits.
_UNSUPPORTED_STRUCTURED_OUTPUT_CONSTRAINTS = frozenset(
    {
        "allOf",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maximum",
        "maxItems",
        "maxLength",
        "minimum",
        "minItems",
        "minLength",
        "multipleOf",
        "not",
        "oneOf",
        "uniqueItems",
    }
)


def _cohere_schema(value: Any) -> Any:
    """Translate CouncilLogic's schema to Cohere's supported subset."""

    if isinstance(value, dict):
        transformed = {
            key: _cohere_schema(item)
            for key, item in value.items()
            if key not in _UNSUPPORTED_STRUCTURED_OUTPUT_CONSTRAINTS
            and key != "type"
        }
        schema_type = value.get("type")
        if isinstance(schema_type, list):
            # Cohere documents ``anyOf`` support but not JSON Schema's compact
            # array-of-types notation. The jury winner is string-or-null.
            transformed["anyOf"] = [
                {"type": item}
                for item in schema_type
                if isinstance(item, str) and item
            ]
        elif "type" in value:
            transformed["type"] = _cohere_schema(schema_type)
        return transformed
    if isinstance(value, list):
        return [_cohere_schema(item) for item in value]
    return value


def _integer(value: Any) -> int | None:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool)
        else None
    )


def _finish_reason(value: str | None) -> str | None:
    normalized = value.upper() if isinstance(value, str) else None
    if normalized in {"COMPLETE", "STOP_SEQUENCE"}:
        return "stop"
    if normalized == "MAX_TOKENS":
        return "length"
    if normalized == "TOOL_CALL":
        return "tool_call"
    if normalized == "TIMEOUT":
        return "timeout"
    if normalized == "ERROR":
        return "error"
    return value.lower() if isinstance(value, str) else None


def _invalid_option(message: str) -> ProviderError:
    return ProviderError(
        f"Cohere configuration is invalid: {message}",
        category=ErrorCategory.INVALID_REQUEST,
        retryable=False,
    )


def _optional_temperature(extra: dict[str, Any]) -> int | float | None:
    if "temperature" not in extra:
        return None
    value = extra["temperature"]
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value < 0
    ):
        raise _invalid_option(
            "extra.temperature must be finite and non-negative"
        )
    return value


def _optional_thinking(extra: dict[str, Any]) -> dict[str, Any] | None:
    if "thinking" not in extra:
        return None
    raw = extra["thinking"]
    if not isinstance(raw, Mapping):
        raise _invalid_option("extra.thinking must be a table/object")
    thinking = dict(raw)
    unknown = set(thinking) - {"type", "token_budget"}
    if unknown:
        raise _invalid_option(
            "extra.thinking has unsupported fields: "
            f"{', '.join(sorted(str(key) for key in unknown))}"
        )
    thinking_type = thinking.get("type")
    if thinking_type is not None and (
        not isinstance(thinking_type, str)
        or thinking_type not in {"enabled", "disabled"}
    ):
        raise _invalid_option(
            "extra.thinking.type must be 'enabled' or 'disabled'"
        )
    if "token_budget" in thinking:
        token_budget = thinking["token_budget"]
        if (
            not isinstance(token_budget, int)
            or isinstance(token_budget, bool)
            or token_budget < 1
        ):
            raise _invalid_option(
                "extra.thinking.token_budget must be a positive integer"
            )
        if thinking_type == "disabled":
            raise _invalid_option(
                "extra.thinking.token_budget cannot be used when thinking "
                "is disabled"
            )
    return thinking


class CohereProvider(Provider):
    """Cohere Chat API V2 adapter for the Command model family."""

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
        }
        if output_schema is not None:
            payload["response_format"] = {
                "type": "json_object",
                "schema": _cohere_schema(output_schema),
            }

        extra = self.config.extra
        temperature = _optional_temperature(extra)
        if temperature is not None:
            payload["temperature"] = temperature

        if "seed" in extra:
            seed = extra["seed"]
            if (
                not isinstance(seed, int)
                or isinstance(seed, bool)
                or not 0 <= seed <= 18_446_744_073_709_552_000
            ):
                raise _invalid_option(
                    "extra.seed must be an integer between 0 and "
                    "18446744073709552000"
                )
            payload["seed"] = seed

        if "safety_mode" in extra:
            safety_mode = extra["safety_mode"]
            if not isinstance(safety_mode, str) or safety_mode not in {
                "CONTEXTUAL",
                "STRICT",
            }:
                raise _invalid_option(
                    "extra.safety_mode must be CONTEXTUAL or STRICT"
                )
            payload["safety_mode"] = safety_mode

        thinking = _optional_thinking(extra)
        if thinking is not None:
            payload["thinking"] = thinking

        started = time.monotonic()
        try:
            result = self._client.post_json(
                url=self.config.endpoint,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "X-Client-Name": "model-council",
                },
                payload=payload,
                timeout_seconds=(
                    timeout_seconds
                    if timeout_seconds is not None
                    else self.config.timeout_for(stage)
                ),
                max_attempts=self.config.max_attempts,
            )
        except ProviderError as exc:
            # Cohere uses 498, in addition to 401, for an invalid API token.
            # The shared HTTP classifier intentionally handles standard codes.
            if exc.status_code == 498:
                raise ProviderError(
                    str(exc),
                    category=ErrorCategory.AUTHENTICATION,
                    retryable=False,
                    status_code=exc.status_code,
                    request_id=exc.request_id,
                    attempts=exc.attempts,
                    ambiguous=exc.ambiguous,
                ) from None
            raise
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
        raw_finish_reason = data.get("finish_reason")
        if not isinstance(raw_finish_reason, str):
            raw_finish_reason = None
        finish_reason = _finish_reason(raw_finish_reason)

        if finish_reason not in {"stop", "length"}:
            if finish_reason == "timeout":
                category = ErrorCategory.TIMEOUT
            elif finish_reason == "error":
                category = ErrorCategory.PROVIDER_SERVER
            else:
                category = ErrorCategory.INVALID_RESPONSE
            retryable = finish_reason in {"timeout", "error"}
            raise ProviderError(
                "Cohere response did not complete normally"
                f" (finish_reason={raw_finish_reason or 'missing'})",
                category=category,
                retryable=retryable,
                status_code=result.status_code,
                request_id=result.request_id,
                attempts=result.attempts,
                ambiguous=False,
            )

        message = data.get("message")
        message = message if isinstance(message, dict) else {}
        blocks = message.get("content")
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
        if not content and finish_reason != "length":
            raise ProviderError(
                "Cohere response did not contain generated text",
                category=ErrorCategory.INVALID_RESPONSE,
                retryable=False,
                status_code=result.status_code,
                request_id=result.request_id,
                attempts=result.attempts,
                ambiguous=True,
            )

        usage_raw = data.get("usage")
        usage_raw = usage_raw if isinstance(usage_raw, dict) else {}
        tokens_raw = usage_raw.get("tokens")
        tokens_raw = tokens_raw if isinstance(tokens_raw, dict) else {}
        input_tokens = _integer(tokens_raw.get("input_tokens"))
        output_tokens = _integer(tokens_raw.get("output_tokens"))
        total_tokens = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
        usage = Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

        metadata: dict[str, Any] = {
            "stage": stage,
            "client_request_id": result.client_request_id,
            "provider_finish_reason": raw_finish_reason,
        }
        billed_raw = usage_raw.get("billed_units")
        billed_raw = billed_raw if isinstance(billed_raw, dict) else {}
        billed_input_tokens = _integer(billed_raw.get("input_tokens"))
        billed_output_tokens = _integer(billed_raw.get("output_tokens"))
        if billed_input_tokens is not None:
            metadata["billed_input_tokens"] = billed_input_tokens
        if billed_output_tokens is not None:
            metadata["billed_output_tokens"] = billed_output_tokens

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
            metadata=metadata,
        )
