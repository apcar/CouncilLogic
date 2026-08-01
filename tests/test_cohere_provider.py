from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import sys
import unittest
from urllib.request import Request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.models import (  # noqa: E402
    ErrorCategory,
    ProviderConfig,
    ProviderError,
)
from model_council.providers.cohere import CohereProvider  # noqa: E402
from model_council.providers.http import (  # noqa: E402
    HttpResponse,
    JsonHttpClient,
)
from model_council.protocol import structured_output_schema  # noqa: E402


def config(
    *,
    extra: dict[str, object] | None = None,
    stage_max_output_tokens: dict[str, int] | None = None,
    stage_timeout_seconds: dict[str, float] | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name="cohere",
        model="command-a-plus-05-2026",
        lineage="cohere-command",
        secret_name="COHERE_API_KEY",
        endpoint="https://api.cohere.ai/v2/chat",
        timeout_seconds=3,
        max_attempts=3,
        extra=extra or {},
        stage_max_output_tokens=stage_max_output_tokens or {},
        stage_timeout_seconds=stage_timeout_seconds or {},
    )


class SequenceTransport:
    def __init__(self, *items: HttpResponse) -> None:
        self.items = list(items)
        self.requests: list[Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: Request, timeout: float) -> HttpResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        return self.items.pop(0)


def response(
    value: object,
    headers: dict[str, str] | None = None,
    *,
    status: int = 200,
) -> HttpResponse:
    return HttpResponse(
        status,
        headers or {},
        json.dumps(value).encode("utf-8"),
    )


def client(
    transport: Callable[[Request, float], HttpResponse],
) -> JsonHttpClient:
    return JsonHttpClient(
        transport=transport,
        sleep=lambda _: None,
        random_value=lambda: 0.5,
    )


def schema_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(str(key) for key in value)
        for item in value.values():
            keys.update(schema_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(schema_keys(item))
    return keys


class CohereProviderTests(unittest.TestCase):
    def test_v2_chat_payload_parsing_schema_and_auth(self) -> None:
        transport = SequenceTransport(
            response(
                {
                    "id": "cohere-body-id",
                    "finish_reason": "COMPLETE",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "private"},
                            {"type": "text", "text": '{"winner":'},
                            {"type": "text", "text": '"CANDIDATE_01"}'},
                        ],
                    },
                    "usage": {
                        "billed_units": {
                            "input_tokens": 9,
                            "output_tokens": 4,
                        },
                        "tokens": {
                            "input_tokens": 41,
                            "output_tokens": 8,
                        },
                    },
                },
                {"x-request-id": "cohere-header-id"},
            )
        )
        provider = CohereProvider(
            config(
                extra={
                    "temperature": 0,
                    "seed": 42,
                    "safety_mode": "STRICT",
                    "thinking": {"token_budget": 512},
                },
                stage_max_output_tokens={"jury": 2200},
                stage_timeout_seconds={"jury": 17},
            ),
            "cohere-secret",
            client=client(transport),
        )

        parsed = provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="jury",
        )

        self.assertEqual(parsed.content, '{"winner":"CANDIDATE_01"}')
        self.assertEqual(parsed.resolved_model, "command-a-plus-05-2026")
        self.assertEqual(parsed.request_id, "cohere-header-id")
        self.assertEqual(parsed.finish_reason, "stop")
        self.assertEqual(parsed.usage.input_tokens, 41)
        self.assertEqual(parsed.usage.output_tokens, 8)
        self.assertEqual(parsed.usage.total_tokens, 49)
        self.assertEqual(parsed.metadata["billed_input_tokens"], 9)
        self.assertEqual(parsed.metadata["billed_output_tokens"], 4)
        self.assertEqual(
            parsed.metadata["provider_finish_reason"], "COMPLETE"
        )

        request = transport.requests[0]
        self.assertEqual(request.full_url, "https://api.cohere.ai/v2/chat")
        self.assertEqual(
            request.get_header("Authorization"), "Bearer cohere-secret"
        )
        self.assertEqual(request.get_header("X-client-name"), "model-council")
        self.assertEqual(transport.timeouts, [17.0])
        body = json.loads(request.data or b"{}")
        self.assertEqual(body["model"], "command-a-plus-05-2026")
        self.assertEqual(
            body["messages"][0],
            {"role": "system", "content": "system"},
        )
        self.assertEqual(
            body["messages"][1],
            {"role": "user", "content": "question"},
        )
        self.assertEqual(body["max_tokens"], 2200)
        self.assertFalse(body["stream"])
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(body["seed"], 42)
        self.assertEqual(body["safety_mode"], "STRICT")
        self.assertEqual(body["thinking"], {"token_budget": 512})
        self.assertEqual(body["response_format"]["type"], "json_object")
        schema = body["response_format"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertTrue(
            {"minLength", "maxLength", "minItems", "maxItems"}.isdisjoint(
                schema_keys(schema)
            )
        )
        winner = schema["properties"]["winner"]
        self.assertNotIn("type", winner)
        self.assertEqual(
            winner["anyOf"], [{"type": "string"}, {"type": "null"}]
        )
        self.assertIn(
            "1000 characters",
            schema["properties"]["rationale"]["description"],
        )
        self.assertNotIn("cohere-secret", json.dumps(body))
        self.assertNotIn("cohere-secret", json.dumps(parsed.to_dict()))

        canonical = structured_output_schema("jury")
        self.assertIsNotNone(canonical)
        self.assertIn("maxLength", schema_keys(canonical))
        self.assertEqual(
            canonical["properties"]["winner"]["type"],
            ["string", "null"],
        )

    def test_call_limits_override_configured_stage_limits(self) -> None:
        transport = SequenceTransport(
            response(
                {
                    "id": "cohere-override",
                    "finish_reason": "COMPLETE",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Answer"}],
                    },
                }
            )
        )
        provider = CohereProvider(config(), "secret", client=client(transport))

        provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="synthesis",
            max_output_tokens=4096,
            timeout_seconds=23.5,
        )

        body = json.loads(transport.requests[0].data or b"{}")
        self.assertEqual(body["max_tokens"], 4096)
        self.assertNotIn("response_format", body)
        self.assertEqual(transport.timeouts, [23.5])

    def test_length_completion_is_preserved_for_runner_recovery(self) -> None:
        for content_blocks in (
            [],
            [{"type": "text", "text": "partial output"}],
        ):
            with self.subTest(content_blocks=content_blocks):
                transport = SequenceTransport(
                    response(
                        {
                            "id": "cohere-length",
                            "finish_reason": "MAX_TOKENS",
                            "message": {
                                "role": "assistant",
                                "content": content_blocks,
                            },
                        }
                    )
                )
                provider = CohereProvider(
                    config(), "secret", client=client(transport)
                )

                parsed = provider.generate(
                    system_prompt="system",
                    user_prompt="question",
                    stage="proposal",
                )

                expected = "partial output" if content_blocks else ""
                self.assertEqual(parsed.content, expected)
                self.assertEqual(parsed.finish_reason, "length")
                self.assertEqual(parsed.request_id, "cohere-length")

    def test_rejects_non_success_finish_reasons(self) -> None:
        cases = {
            "TOOL_CALL": (ErrorCategory.INVALID_RESPONSE, False),
            "ERROR": (ErrorCategory.PROVIDER_SERVER, True),
            "TIMEOUT": (ErrorCategory.TIMEOUT, True),
            "UNKNOWN_REASON": (ErrorCategory.INVALID_RESPONSE, False),
            None: (ErrorCategory.INVALID_RESPONSE, False),
        }
        for finish_reason, (category, retryable) in cases.items():
            with self.subTest(finish_reason=finish_reason):
                payload: dict[str, object] = {
                    "id": "cohere-failed",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "partial"}],
                    },
                }
                if finish_reason is not None:
                    payload["finish_reason"] = finish_reason
                transport = SequenceTransport(response(payload))
                provider = CohereProvider(
                    config(), "secret", client=client(transport)
                )

                with self.assertRaises(ProviderError) as caught:
                    provider.generate(
                        system_prompt="system",
                        user_prompt="question",
                        stage="synthesis",
                    )

                self.assertEqual(caught.exception.category, category)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertFalse(caught.exception.ambiguous)

    def test_complete_response_requires_text_content(self) -> None:
        transport = SequenceTransport(
            response(
                {
                    "id": "cohere-no-text",
                    "finish_reason": "COMPLETE",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "private"}
                        ],
                    },
                }
            )
        )
        provider = CohereProvider(config(), "secret", client=client(transport))

        with self.assertRaises(ProviderError) as caught:
            provider.generate(
                system_prompt="system",
                user_prompt="question",
                stage="proposal",
            )

        self.assertEqual(
            caught.exception.category, ErrorCategory.INVALID_RESPONSE
        )
        self.assertTrue(caught.exception.ambiguous)

    def test_invalid_provider_options_fail_before_transmission(self) -> None:
        cases = (
            {"temperature": True},
            {"temperature": float("nan")},
            {"temperature": -0.1},
            {"seed": True},
            {"seed": -1},
            {"seed": 18_446_744_073_709_552_001},
            {"safety_mode": "strict"},
            {"safety_mode": "OFF"},
            {"safety_mode": {}},
            {"thinking": "enabled"},
            {"thinking": {"type": []}},
            {"thinking": {"type": "sometimes"}},
            {"thinking": {"token_budget": 0}},
            {"thinking": {"token_budget": 1.5}},
            {"thinking": {"type": "disabled", "token_budget": 512}},
            {"thinking": {"unknown": True}},
        )
        for extra in cases:
            with self.subTest(extra=extra):
                transport = SequenceTransport()
                provider = CohereProvider(
                    config(extra=extra),
                    "secret",
                    client=client(transport),
                )

                with self.assertRaises(ProviderError) as caught:
                    provider.generate(
                        system_prompt="system",
                        user_prompt="question",
                        stage="synthesis",
                    )

                self.assertEqual(
                    caught.exception.category,
                    ErrorCategory.INVALID_REQUEST,
                )
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(transport.requests, [])

    def test_cohere_invalid_token_status_is_authentication(self) -> None:
        transport = SequenceTransport(
            response(
                {"message": "invalid token"},
                {"x-request-id": "cohere-auth-failure"},
                status=498,
            )
        )
        provider = CohereProvider(
            config(),
            "invalid-secret",
            client=client(transport),
        )

        with self.assertRaises(ProviderError) as caught:
            provider.generate(
                system_prompt="system",
                user_prompt="question",
                stage="proposal",
            )

        self.assertEqual(
            caught.exception.category,
            ErrorCategory.AUTHENTICATION,
        )
        self.assertEqual(caught.exception.status_code, 498)
        self.assertEqual(caught.exception.request_id, "cohere-auth-failure")
        self.assertEqual(caught.exception.attempts, 1)
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("invalid-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
