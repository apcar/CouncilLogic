from __future__ import annotations

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
import unittest
from urllib.request import Request


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.models import (  # noqa: E402
    ErrorCategory,
    ProviderConfig,
    ProviderError,
)
from model_council.providers.anthropic import AnthropicProvider  # noqa: E402
from model_council.providers.factory import create_provider  # noqa: E402
from model_council.providers.gemini import GeminiProvider  # noqa: E402
from model_council.providers.http import (  # noqa: E402
    HttpResponse,
    JsonHttpClient,
    urllib_transport,
)
from model_council.providers.mistral import MistralProvider  # noqa: E402
from model_council.providers.mock import MockProvider  # noqa: E402
from model_council.providers.openai import OpenAIProvider  # noqa: E402
from model_council.providers.xai import XAIProvider  # noqa: E402
from model_council.protocol import structured_output_schema  # noqa: E402


def config(
    name: str,
    endpoint: str,
    model: str = "test-model",
    *,
    extra: dict[str, object] | None = None,
    stage_max_output_tokens: dict[str, int] | None = None,
    stage_timeout_seconds: dict[str, float] | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        model=model,
        lineage=f"{name}-lineage",
        secret_name=f"{name.upper()}_KEY",
        endpoint=endpoint,
        timeout_seconds=3,
        max_attempts=3,
        extra=extra or {},
        stage_max_output_tokens=stage_max_output_tokens or {},
        stage_timeout_seconds=stage_timeout_seconds or {},
    )


class SequenceTransport:
    def __init__(self, *items: HttpResponse | BaseException) -> None:
        self.items = list(items)
        self.requests: list[Request] = []
        self.timeouts: list[float] = []

    def __call__(self, request: Request, timeout: float) -> HttpResponse:
        self.requests.append(request)
        self.timeouts.append(timeout)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _RedirectFixture(BaseHTTPRequestHandler):
    sink_headers: list[str | None] = []

    def do_POST(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{self.server.server_port}/sink",
            )
            self.end_headers()
            return
        type(self).sink_headers.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def response(
    status: int,
    value: object,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return HttpResponse(status, headers or {}, body)


def client(
    transport: Callable[[Request, float], HttpResponse],
    sleeps: list[float] | None = None,
) -> JsonHttpClient:
    return JsonHttpClient(
        transport=transport,
        sleep=(sleeps.append if sleeps is not None else lambda _: None),
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


class ParsingTests(unittest.TestCase):
    def test_explicit_empty_length_responses_remain_recoverable(self) -> None:
        cases = (
            (
                OpenAIProvider,
                config(
                    "openai",
                    "https://api.openai.com/v1/responses",
                ),
                {
                    "id": "openai-length",
                    "model": "test-model",
                    "status": "incomplete",
                    "incomplete_details": {
                        "reason": "max_output_tokens",
                    },
                    "output": [],
                },
            ),
            (
                AnthropicProvider,
                config(
                    "anthropic",
                    "https://api.anthropic.com/v1/messages",
                ),
                {
                    "id": "anthropic-length",
                    "model": "test-model",
                    "stop_reason": "max_tokens",
                    "content": [],
                },
            ),
            (
                GeminiProvider,
                config(
                    "gemini",
                    (
                        "https://generativelanguage.googleapis.com/"
                        "v1beta/models/{model}:generateContent"
                    ),
                ),
                {
                    "responseId": "gemini-length",
                    "modelVersion": "test-model",
                    "candidates": [
                        {
                            "content": {"parts": []},
                            "finishReason": "MAX_TOKENS",
                        }
                    ],
                },
            ),
            (
                MistralProvider,
                config(
                    "mistral",
                    "https://api.mistral.ai/v1/chat/completions",
                ),
                {
                    "id": "mistral-length",
                    "model": "test-model",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                            },
                            "finish_reason": "length",
                        }
                    ],
                },
            ),
        )

        for provider_type, provider_config, payload in cases:
            with self.subTest(provider=provider_config.name):
                transport = SequenceTransport(response(200, payload))
                provider = provider_type(
                    provider_config,
                    "provider-secret",
                    client=client(transport),
                )

                parsed = provider.generate(
                    system_prompt="system",
                    user_prompt="question",
                    stage="proposal",
                )

                self.assertEqual(parsed.content, "")
                self.assertEqual(parsed.finish_reason, "length")

    def test_openai_responses_api_payload_and_parsing(self) -> None:
        transport = SequenceTransport(
            response(
                200,
                {
                    "id": "resp_body",
                    "model": "gpt-5.6-sol-2026-07-01",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "Answer"}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 4,
                        "total_tokens": 14,
                        "input_tokens_details": {"cached_tokens": 3},
                        "output_tokens_details": {"reasoning_tokens": 2},
                    },
                },
                {"x-request-id": "req_openai"},
            )
        )
        provider = OpenAIProvider(
            config(
                "openai",
                "https://api.openai.com/v1/responses",
                "gpt-5.6-sol",
            ),
            "openai-secret",
            client=client(transport),
        )

        parsed = provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="jury",
        )

        self.assertEqual(parsed.content, "Answer")
        self.assertEqual(parsed.request_id, "req_openai")
        self.assertEqual(parsed.usage.cached_input_tokens, 3)
        self.assertEqual(parsed.usage.reasoning_tokens, 2)
        body = json.loads(transport.requests[0].data or b"{}")
        self.assertFalse(body["store"])
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(body["instructions"], "system")
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertFalse(
            body["text"]["format"]["schema"]["additionalProperties"]
        )

    def test_xai_grok_responses_payload_parsing_and_metadata(self) -> None:
        transport = SequenceTransport(
            response(
                200,
                {
                    "id": "resp_grok",
                    "model": "grok-4.5",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "Grok answer"}
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 5,
                        "total_tokens": 17,
                        "cost_in_usd_ticks": 123456,
                    },
                },
                {
                    "x-request-id": "req_xai",
                    "x-zero-data-retention": "true",
                },
            )
        )
        provider = XAIProvider(
            config(
                "xai",
                "https://api.x.ai/v1/responses",
                "grok-4.5",
            ),
            "xai-secret",
            client=client(transport),
        )

        parsed = provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="jury",
        )

        self.assertEqual(parsed.content, "Grok answer")
        self.assertEqual(parsed.request_id, "req_xai")
        self.assertEqual(parsed.resolved_model, "grok-4.5")
        self.assertEqual(parsed.usage.total_tokens, 17)
        self.assertEqual(parsed.metadata["cost_in_usd_ticks"], 123456)
        self.assertTrue(parsed.metadata["zero_data_retention"])
        body = json.loads(transport.requests[0].data or b"{}")
        self.assertEqual(body["model"], "grok-4.5")
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertNotIn("xai-secret", json.dumps(body))
        self.assertEqual(
            transport.requests[0].get_header("Authorization"),
            "Bearer xai-secret",
        )
        self.assertNotIn("xai-secret", json.dumps(parsed.to_dict()))

    def test_anthropic_messages_parsing(self) -> None:
        transport = SequenceTransport(
            response(
                200,
                {
                    "id": "msg_1",
                    "model": "claude-test",
                    "content": [
                        {"type": "thinking", "thinking": "private"},
                        {"type": "text", "text": "First"},
                        {"type": "text", "text": " second"},
                    ],
                    "stop_reason": "end_turn",
                    "usage": {
                        "input_tokens": 8,
                        "output_tokens": 3,
                        "cache_read_input_tokens": 2,
                    },
                },
                {"request-id": "req_anthropic"},
            )
        )
        provider = AnthropicProvider(
            config("anthropic", "https://api.anthropic.com/v1/messages"),
            "anthropic-secret",
            client=client(transport),
        )

        parsed = provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="jury",
        )

        self.assertEqual(parsed.content, "First second")
        self.assertEqual(parsed.finish_reason, "stop")
        self.assertEqual(
            parsed.metadata["provider_finish_reason"],
            "end_turn",
        )
        self.assertEqual(parsed.usage.total_tokens, 11)
        self.assertEqual(parsed.usage.cached_input_tokens, 2)
        body = json.loads(transport.requests[0].data or b"{}")
        self.assertEqual(
            body["output_config"]["format"]["type"], "json_schema"
        )
        self.assertIn(
            "abstain",
            body["output_config"]["format"]["schema"]["required"],
        )

    def test_anthropic_strips_only_unsupported_schema_constraints(
        self,
    ) -> None:
        transport = SequenceTransport(
            *[
                response(
                    200,
                    {
                        "id": f"msg_{stage}",
                        "model": "claude-test",
                        "content": [{"type": "text", "text": "{}"}],
                        "stop_reason": "end_turn",
                    },
                )
                for stage in ("proposal", "jury")
            ]
        )
        provider = AnthropicProvider(
            config("anthropic", "https://api.anthropic.com/v1/messages"),
            "anthropic-secret",
            client=client(transport),
        )

        for stage in ("proposal", "jury"):
            provider.generate(
                system_prompt="system",
                user_prompt="question",
                stage=stage,
            )

        for stage, request in zip(
            ("proposal", "jury"), transport.requests, strict=True
        ):
            body = json.loads(request.data or b"{}")
            schema = body["output_config"]["format"]["schema"]
            self.assertTrue(
                {"type", "properties", "required", "additionalProperties"}
                <= set(schema)
            )
            self.assertTrue(
                {"minLength", "maxLength", "maxItems"}
                .isdisjoint(schema_keys(schema))
            )
            if stage == "proposal":
                properties = schema["properties"]
                self.assertIn(
                    "600 characters",
                    properties["outcome"]["description"],
                )
                self.assertIn(
                    "Must contain at most 4 items",
                    properties["evidence_and_reasoning"][
                        "description"
                    ],
                )
                self.assertIn(
                    "350 characters",
                    properties["evidence_and_reasoning"]["items"][
                        "description"
                    ],
                )
                self.assertIn(
                    "Must contain at most 3 items",
                    properties["uncertainty"]["description"],
                )
                self.assertIn(
                    "280 characters",
                    properties["uncertainty"]["items"]["description"],
                )
                self.assertIn(
                    "Must contain at most 4 items",
                    properties["verification_needed"]["description"],
                )
                self.assertIn(
                    "280 characters",
                    properties["verification_needed"]["items"][
                        "description"
                    ],
                )
            else:
                properties = schema["properties"]
                self.assertIn(
                    "700 characters",
                    properties["rationale"]["description"],
                )
                for field_name in (
                    "material_disagreements",
                    "verification_needed",
                ):
                    self.assertIn(
                        "Must contain at most 4 items",
                        properties[field_name]["description"],
                    )
                    self.assertIn(
                        "280 characters",
                        properties[field_name]["items"]["description"],
                    )
            canonical = structured_output_schema(stage)
            self.assertIsNotNone(canonical)
            self.assertTrue(
                {"minLength", "maxLength", "maxItems"}
                & schema_keys(canonical)
            )

    def test_gemini_generate_content_parsing(self) -> None:
        transport = SequenceTransport(
            response(
                200,
                {
                    "responseId": "gem_body",
                    "modelVersion": "gemini-test-001",
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": "Gemini answer"}]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 7,
                        "candidatesTokenCount": 3,
                        "totalTokenCount": 12,
                        "cachedContentTokenCount": 1,
                        "thoughtsTokenCount": 2,
                    },
                },
                {"x-goog-request-id": "req_gemini"},
            )
        )
        provider = GeminiProvider(
            config(
                "gemini",
                (
                    "https://generativelanguage.googleapis.com/"
                    "v1beta/models/{model}:generateContent"
                ),
                extra={"thinking_level": "low"},
            ),
            "gemini-secret",
            client=client(transport),
        )

        parsed = provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="jury",
        )

        self.assertEqual(parsed.content, "Gemini answer")
        self.assertEqual(parsed.finish_reason, "stop")
        self.assertEqual(parsed.metadata["provider_finish_reason"], "STOP")
        self.assertEqual(parsed.usage.reasoning_tokens, 2)
        self.assertIn(
            "/models/test-model:generateContent",
            transport.requests[0].full_url,
        )
        body = json.loads(transport.requests[0].data or b"{}")
        response_format = body["generationConfig"]["responseFormat"]
        self.assertEqual(
            response_format["text"]["mimeType"], "APPLICATION_JSON"
        )
        self.assertIn(
            "abstain", response_format["text"]["schema"]["required"]
        )
        self.assertEqual(
            body["generationConfig"]["thinkingConfig"],
            {"thinkingLevel": "low"},
        )

    def test_mistral_chat_payload_and_parsing(self) -> None:
        transport = SequenceTransport(
            response(
                200,
                {
                    "id": "chatcmpl_body",
                    "model": "mistral-medium-3-5",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": '{"winner":"A"}',
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 9,
                        "completion_tokens": 4,
                        "total_tokens": 13,
                        "prompt_tokens_details": {"cached_tokens": 2},
                    },
                },
                {"mistral-correlation-id": "req_mistral"},
            )
        )
        provider = MistralProvider(
            config(
                "mistral",
                "https://api.mistral.ai/v1/chat/completions",
                "mistral-medium-3-5",
            ),
            "mistral-secret",
            client=client(transport),
        )

        parsed = provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="jury",
        )

        self.assertEqual(parsed.content, '{"winner":"A"}')
        self.assertEqual(parsed.request_id, "req_mistral")
        self.assertEqual(parsed.finish_reason, "stop")
        self.assertEqual(parsed.usage.total_tokens, 13)
        self.assertEqual(parsed.usage.cached_input_tokens, 2)
        body = json.loads(transport.requests[0].data or b"{}")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertFalse(body["stream"])
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["response_format"]["type"], "json_schema")
        schema = body["response_format"]["json_schema"]
        self.assertEqual(schema["name"], "model_council_jury")
        self.assertTrue(schema["strict"])
        self.assertFalse(schema["schema"]["additionalProperties"])

    def test_mistral_preserves_known_length_completions_for_recovery(
        self,
    ) -> None:
        for finish_reason in ("length", "model_length"):
            with self.subTest(finish_reason=finish_reason):
                transport = SequenceTransport(
                    response(
                        200,
                        {
                            "id": "chatcmpl_incomplete",
                            "model": "mistral-medium-3-5",
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "partial output",
                                    },
                                    "finish_reason": finish_reason,
                                }
                            ],
                        },
                        {"mistral-correlation-id": "req_incomplete"},
                    )
                )
                provider = MistralProvider(
                    config(
                        "mistral",
                        "https://api.mistral.ai/v1/chat/completions",
                        "mistral-medium-3-5",
                    ),
                    "mistral-secret",
                    client=client(transport),
                )

                parsed = provider.generate(
                    system_prompt="system",
                    user_prompt="question",
                    stage="synthesis",
                )

                self.assertEqual(parsed.content, "partial output")
                self.assertEqual(parsed.finish_reason, "length")
                self.assertEqual(parsed.request_id, "req_incomplete")

    def test_mistral_rejects_other_non_success_finish_reasons(self) -> None:
        for finish_reason in ("error", "tool_calls"):
            with self.subTest(finish_reason=finish_reason):
                transport = SequenceTransport(
                    response(
                        200,
                        {
                            "id": "chatcmpl_incomplete",
                            "model": "mistral-medium-3-5",
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "partial output",
                                    },
                                    "finish_reason": finish_reason,
                                }
                            ],
                        },
                        {"mistral-correlation-id": "req_incomplete"},
                    )
                )
                provider = MistralProvider(
                    config(
                        "mistral",
                        "https://api.mistral.ai/v1/chat/completions",
                    ),
                    "mistral-secret",
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
                    ErrorCategory.INVALID_RESPONSE,
                )
                self.assertFalse(caught.exception.ambiguous)

    def test_stage_limits_and_call_overrides_reach_provider_payload(
        self,
    ) -> None:
        transport = SequenceTransport(
            response(
                200,
                {
                    "id": "resp_limits",
                    "model": "gpt-test",
                    "status": "completed",
                    "output_text": "Answer",
                },
            ),
            response(
                200,
                {
                    "id": "resp_override",
                    "model": "gpt-test",
                    "status": "completed",
                    "output_text": "Answer",
                },
            ),
        )
        provider = OpenAIProvider(
            config(
                "openai",
                "https://api.openai.com/v1/responses",
                stage_max_output_tokens={"proposal": 3210},
                stage_timeout_seconds={"proposal": 45.0},
            ),
            "openai-secret",
            client=client(transport),
        )

        provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="proposal",
        )
        provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="proposal",
            max_output_tokens=6543,
            timeout_seconds=67.0,
        )

        first = json.loads(transport.requests[0].data or b"{}")
        second = json.loads(transport.requests[1].data or b"{}")
        self.assertEqual(first["max_output_tokens"], 3210)
        self.assertEqual(second["max_output_tokens"], 6543)
        self.assertEqual(transport.timeouts, [45.0, 67.0])
        self.assertEqual(
            first["text"]["format"]["name"],
            "model_council_proposal",
        )
        self.assertIn(
            "evidence_and_reasoning",
            first["text"]["format"]["schema"]["required"],
        )


class RetryAndErrorTests(unittest.TestCase):
    def test_default_transport_never_follows_redirect_with_credentials(self) -> None:
        _RedirectFixture.sink_headers = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectFixture)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/redirect",
                data=b"{}",
                headers={"Authorization": "Bearer redirect-canary"},
                method="POST",
            )
            result = urllib_transport(request, 2.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(result.status_code, 302)
        self.assertEqual(_RedirectFixture.sink_headers, [])

    def test_retries_explicit_429_and_honors_retry_after(self) -> None:
        sleeps: list[float] = []
        transport = SequenceTransport(
            response(429, {"error": "secret body"}, {"Retry-After": "2"}),
            response(200, {"ok": True}, {"x-request-id": "second"}),
        )

        parsed = client(transport, sleeps).post_json(
            url="https://api.example.test/generate",
            headers={"Authorization": "Bearer hidden"},
            payload={"input": "hello"},
            timeout_seconds=2,
            max_attempts=3,
        )

        self.assertEqual(parsed.attempts, 2)
        self.assertEqual(sleeps, [2.0])
        self.assertEqual(len(transport.requests), 2)

    def test_full_jitter_backoff_for_server_error(self) -> None:
        sleeps: list[float] = []
        transport = SequenceTransport(
            response(503, {}),
            response(200, {"ok": True}),
        )

        client(transport, sleeps).post_json(
            url="https://api.example.test/generate",
            headers={},
            payload={},
            timeout_seconds=2,
            max_attempts=2,
        )

        self.assertEqual(sleeps, [0.25])

    def test_timeout_is_ambiguous_and_not_automatically_retried(self) -> None:
        transport = SequenceTransport(
            socket.timeout(),
            response(200, {"would": "duplicate"}),
        )
        with self.assertRaises(ProviderError) as caught:
            client(transport).post_json(
                url="https://api.example.test/generate",
                headers={},
                payload={},
                timeout_seconds=2,
                max_attempts=3,
            )

        self.assertEqual(caught.exception.category, ErrorCategory.TIMEOUT)
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(len(transport.requests), 1)

    def test_401_is_classified_and_never_retried(self) -> None:
        secret = "never-disclose-this-key"
        transport = SequenceTransport(
            response(
                401,
                {"error": {"message": f"invalid {secret}"}},
                {"x-request-id": "auth_req"},
            )
        )
        with self.assertRaises(ProviderError) as caught:
            client(transport).post_json(
                url="https://api.example.test/generate",
                headers={"Authorization": f"Bearer {secret}"},
                payload={"secret_like": secret},
                timeout_seconds=2,
                max_attempts=3,
            )

        error = caught.exception
        self.assertEqual(error.category, ErrorCategory.AUTHENTICATION)
        self.assertFalse(error.retryable)
        self.assertEqual(error.request_id, "auth_req")
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, json.dumps(error.to_dict()))
        self.assertEqual(len(transport.requests), 1)

    def test_malformed_success_response_is_safe_and_not_retried(self) -> None:
        transport = SequenceTransport(
            response(200, b"{not-json"),
            response(200, {"would": "duplicate"}),
        )
        with self.assertRaises(ProviderError) as caught:
            client(transport).post_json(
                url="https://api.example.test/generate",
                headers={},
                payload={},
                timeout_seconds=2,
                max_attempts=3,
            )

        self.assertEqual(
            caught.exception.category,
            ErrorCategory.INVALID_RESPONSE,
        )
        self.assertTrue(caught.exception.ambiguous)
        self.assertEqual(len(transport.requests), 1)


class FactoryAndMockTests(unittest.TestCase):
    def test_factory_maps_real_and_prefixed_mock_names(self) -> None:
        openai = create_provider(
            config("openai", "https://api.openai.com/v1/responses"),
            "secret",
        )
        mistral = create_provider(
            config(
                "mistral",
                "https://api.mistral.ai/v1/chat/completions",
            ),
            "secret",
        )
        xai = create_provider(
            config(
                "xai",
                "https://api.x.ai/v1/responses",
                "grok-4.5",
            ),
            "secret",
        )
        mock = create_provider(
            ProviderConfig(
                name="mock-2",
                model="mock-model-2",
                lineage="mock-lineage-2",
                secret_name="MOCK_KEY_2",
                endpoint="http://localhost/mock",
                extra={"seed": 2},
            )
        )

        self.assertIsInstance(openai, OpenAIProvider)
        self.assertIsInstance(mistral, MistralProvider)
        self.assertIsInstance(xai, XAIProvider)
        self.assertIsInstance(mock, MockProvider)

    def test_mock_is_deterministic_and_emits_valid_jury_json(self) -> None:
        provider = create_provider(
            ProviderConfig(
                name="mock-1",
                model="mock-model-1",
                lineage="mock-lineage-1",
                secret_name="MOCK_KEY_1",
                endpoint="http://localhost/mock",
                extra={"seed": 1},
            )
        )
        prompt = (
            "Allowed candidate labels: [\n"
            '  "A",\n'
            '  "B",\n'
            '  "C"\n'
            "]\n\n"
            "BEGIN_UNTRUSTED_EVALUATION_JSON\n"
            "{}\n"
            "END_UNTRUSTED_EVALUATION_JSON"
        )

        first = provider.generate(
            system_prompt="jury",
            user_prompt=prompt,
            stage="jury",
        )
        second = provider.generate(
            system_prompt="jury",
            user_prompt=prompt,
            stage="jury",
        )

        self.assertEqual(first, second)
        judgment = json.loads(first.content)
        self.assertEqual(judgment["winner"], "B")
        self.assertEqual(judgment["ranking"], ["B", "C", "A"])


if __name__ == "__main__":
    unittest.main()
