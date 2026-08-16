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
from model_council.protocol import structured_output_schema  # noqa: E402
from model_council.providers.http import (  # noqa: E402
    HttpResponse,
    JsonHttpClient,
)
from model_council.providers.upstage import UpstageProvider  # noqa: E402


ENDPOINT = "https://api.upstage.ai/v1/chat/completions"


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


def config(
    *,
    extra: dict[str, object] | None = None,
    stage_max_output_tokens: dict[str, int] | None = None,
    stage_timeout_seconds: dict[str, float] | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name="upstage",
        model="solar-pro3-260323",
        lineage="upstage-solar",
        secret_name="UPSTAGE_API_KEY",
        endpoint=ENDPOINT,
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
) -> HttpResponse:
    return HttpResponse(
        200,
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


class UpstageProviderTests(unittest.TestCase):
    def test_proposal_uses_strict_schema_and_parses_chat_response(self) -> None:
        transport = SequenceTransport(
            response(
                {
                    "id": "upstage-body-id",
                    "model": "solar-pro3-260323",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": '{"outcome":"Proceed"}',
                                "reasoning": "private and not persisted",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 31,
                        "completion_tokens": 12,
                        "total_tokens": 43,
                        "prompt_tokens_details": {"cached_tokens": 4},
                        "completion_tokens_details": {"reasoning_tokens": 0},
                    },
                },
                {"x-request-id": "upstage-header-id"},
            )
        )
        provider = UpstageProvider(
            config(
                stage_max_output_tokens={"proposal": 3200},
                stage_timeout_seconds={"proposal": 17},
            ),
            "upstage-secret",
            client=client(transport),
        )

        parsed = provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="proposal",
        )

        self.assertEqual(parsed.content, '{"outcome":"Proceed"}')
        self.assertEqual(parsed.resolved_model, "solar-pro3-260323")
        self.assertEqual(parsed.request_id, "upstage-header-id")
        self.assertEqual(parsed.finish_reason, "stop")
        self.assertEqual(parsed.usage.input_tokens, 31)
        self.assertEqual(parsed.usage.output_tokens, 12)
        self.assertEqual(parsed.usage.total_tokens, 43)
        self.assertEqual(parsed.usage.cached_input_tokens, 4)
        self.assertEqual(parsed.usage.reasoning_tokens, 0)
        self.assertNotIn("reasoning", parsed.metadata)
        self.assertNotIn("private", json.dumps(parsed.to_dict()))

        request = transport.requests[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(
            request.get_header("Authorization"), "Bearer upstage-secret"
        )
        self.assertEqual(transport.timeouts, [17.0])
        body = json.loads(request.data or b"{}")
        self.assertEqual(body["model"], "solar-pro3-260323")
        self.assertEqual(
            body["messages"],
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "question"},
            ],
        )
        self.assertEqual(body["max_tokens"], 3200)
        self.assertFalse(body["stream"])
        self.assertEqual(body["reasoning_effort"], "low")
        response_format = body["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(
            response_format["json_schema"]["name"],
            "model_council_proposal",
        )
        self.assertTrue(response_format["json_schema"]["strict"])
        provider_schema = response_format["json_schema"]["schema"]
        self.assertTrue(
            {"minLength", "maxLength", "minItems", "maxItems"}.isdisjoint(
                schema_keys(provider_schema)
            )
        )
        self.assertIn(
            "600 characters",
            provider_schema["properties"]["outcome"]["description"],
        )
        canonical_schema = structured_output_schema("proposal")
        self.assertIsNotNone(canonical_schema)
        self.assertIn("maxLength", schema_keys(canonical_schema))
        self.assertNotIn("upstage-secret", json.dumps(body))
        self.assertNotIn("upstage-secret", json.dumps(parsed.to_dict()))

    def test_jury_uses_json_object_mode_and_explicit_json_instruction(self) -> None:
        transport = SequenceTransport(
            response(
                {
                    "id": "upstage-jury",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    '{"winner":null,"ranking":[],"abstain":true}'
                                ),
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        )
        provider = UpstageProvider(
            config(), "upstage-secret", client=client(transport)
        )

        parsed = provider.generate(
            system_prompt="Judge the candidates.",
            user_prompt="Candidate record.",
            stage="jury",
        )

        self.assertIn('"winner":null', parsed.content)
        body = json.loads(transport.requests[0].data or b"{}")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertIn("JSON", body["messages"][0]["content"])
        self.assertEqual(body["reasoning_effort"], "low")

    def test_synthesis_omits_response_format_and_accepts_call_limits(self) -> None:
        transport = SequenceTransport(
            response(
                {
                    "id": "upstage-synthesis",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "## Outcome\nResult",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        )
        provider = UpstageProvider(
            config(extra={"temperature": 0}),
            "upstage-secret",
            client=client(transport),
        )

        parsed = provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="synthesis",
            max_output_tokens=4096,
            timeout_seconds=23.5,
        )

        self.assertEqual(parsed.request_id, "upstage-synthesis")
        body = json.loads(transport.requests[0].data or b"{}")
        self.assertNotIn("response_format", body)
        self.assertEqual(body["max_tokens"], 4096)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["temperature"], 0)
        self.assertEqual(transport.timeouts, [23.5])

    def test_length_completion_is_preserved_for_runner_recovery(self) -> None:
        transport = SequenceTransport(
            response(
                {
                    "id": "upstage-length",
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": ""},
                            "finish_reason": "length",
                        }
                    ],
                }
            )
        )
        provider = UpstageProvider(
            config(), "upstage-secret", client=client(transport)
        )

        parsed = provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="proposal",
        )

        self.assertEqual(parsed.content, "")
        self.assertEqual(parsed.finish_reason, "length")

    def test_rejects_non_success_finish_reasons_and_invalid_reasoning(self) -> None:
        for finish_reason, category in (
            ("tool_calls", ErrorCategory.INVALID_RESPONSE),
            ("content_filter", ErrorCategory.CONTENT_FILTER),
            (None, ErrorCategory.INVALID_RESPONSE),
        ):
            with self.subTest(finish_reason=finish_reason):
                transport = SequenceTransport(
                    response(
                        {
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "partial",
                                    },
                                    "finish_reason": finish_reason,
                                }
                            ]
                        }
                    )
                )
                provider = UpstageProvider(
                    config(), "upstage-secret", client=client(transport)
                )

                with self.assertRaises(ProviderError) as caught:
                    provider.generate(
                        system_prompt="system",
                        user_prompt="question",
                        stage="synthesis",
                    )

                self.assertEqual(caught.exception.category, category)
                self.assertFalse(caught.exception.ambiguous)

        transport = SequenceTransport()
        provider = UpstageProvider(
            config(extra={"reasoning_effort": "max"}),
            "upstage-secret",
            client=client(transport),
        )
        with self.assertRaises(ProviderError) as caught:
            provider.generate(
                system_prompt="system",
                user_prompt="question",
                stage="proposal",
            )
        self.assertEqual(caught.exception.category, ErrorCategory.INVALID_REQUEST)
        self.assertEqual(transport.requests, [])

        for extra in (
            {"temperature": True},
            {"temperature": float("nan")},
            {"temperature": 2.1},
            {"top_p": -0.1},
            {"frequency_penalty": -2.1},
            {"presence_penalty": 2.1},
        ):
            with self.subTest(extra=extra):
                transport = SequenceTransport()
                provider = UpstageProvider(
                    config(extra=extra),
                    "upstage-secret",
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
                    ErrorCategory.INVALID_REQUEST,
                )
                self.assertFalse(caught.exception.retryable)
                self.assertEqual(transport.requests, [])


if __name__ == "__main__":
    unittest.main()
