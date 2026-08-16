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
from model_council.providers.http import (  # noqa: E402
    HttpResponse,
    JsonHttpClient,
)
from model_council.providers.qwen import QwenProvider  # noqa: E402


ENDPOINT = (
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
)


def config(
    *,
    extra: dict[str, object] | None = None,
    stage_max_output_tokens: dict[str, int] | None = None,
) -> ProviderConfig:
    return ProviderConfig(
        name="qwen",
        model="qwen3.7-max",
        lineage="alibaba-qwen",
        secret_name="DASHSCOPE_API_KEY",
        endpoint=ENDPOINT,
        timeout_seconds=3,
        max_attempts=3,
        extra=extra or {},
        stage_max_output_tokens=stage_max_output_tokens or {},
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


def response(
    status: int,
    value: object,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    return HttpResponse(status, headers or {}, body)


def client(
    transport: Callable[[Request, float], HttpResponse],
) -> JsonHttpClient:
    return JsonHttpClient(
        transport=transport,
        sleep=lambda _: None,
        random_value=lambda: 0.5,
    )


class QwenProviderTests(unittest.TestCase):
    def test_json_mode_payload_and_chat_response_parsing(self) -> None:
        transport = SequenceTransport(
            response(
                200,
                {
                    "id": "chatcmpl_body",
                    "model": "qwen3.7-max-2026-06-08",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": '{"winner":"CANDIDATE_01"}',
                                "reasoning_content": "not persisted as content",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 19,
                        "completion_tokens": 12,
                        "total_tokens": 31,
                        "prompt_tokens_details": {"cached_tokens": 4},
                        "completion_tokens_details": {"reasoning_tokens": 3},
                    },
                },
                {"x-request-id": "req_qwen"},
            )
        )
        provider = QwenProvider(
            config(stage_max_output_tokens={"jury": 2200}),
            "qwen-secret",
            client=client(transport),
        )

        parsed = provider.generate(
            system_prompt="Return the required object.",
            user_prompt="Evaluate the candidates.",
            stage="jury",
        )

        self.assertEqual(parsed.content, '{"winner":"CANDIDATE_01"}')
        self.assertEqual(parsed.resolved_model, "qwen3.7-max-2026-06-08")
        self.assertEqual(parsed.request_id, "req_qwen")
        self.assertEqual(parsed.finish_reason, "stop")
        self.assertEqual(parsed.usage.input_tokens, 19)
        self.assertEqual(parsed.usage.output_tokens, 12)
        self.assertEqual(parsed.usage.total_tokens, 31)
        self.assertEqual(parsed.usage.cached_input_tokens, 4)
        self.assertEqual(parsed.usage.reasoning_tokens, 3)

        request = transport.requests[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_header("Authorization"), "Bearer qwen-secret")
        body = json.loads(request.data or b"{}")
        self.assertEqual(body["model"], "qwen3.7-max")
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("JSON", body["messages"][0]["content"])
        self.assertEqual(body["messages"][1]["role"], "user")
        self.assertEqual(body["max_completion_tokens"], 2200)
        self.assertNotIn("max_tokens", body)
        self.assertFalse(body["stream"])
        self.assertFalse(body["enable_thinking"])
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_synthesis_omits_json_mode_and_accepts_call_overrides(self) -> None:
        transport = SequenceTransport(
            response(
                200,
                {
                    "id": "chatcmpl_synthesis",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "## Outcome\nResult",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        )
        provider = QwenProvider(
            config(
                extra={
                    "enable_thinking": True,
                    "thinking_budget": 900,
                    "temperature": 0.0,
                    "seed": 17,
                }
            ),
            "qwen-secret",
            client=client(transport),
        )

        parsed = provider.generate(
            system_prompt="system",
            user_prompt="question",
            stage="synthesis",
            max_output_tokens=4096,
            timeout_seconds=7.5,
        )

        self.assertEqual(parsed.request_id, "chatcmpl_synthesis")
        body = json.loads(transport.requests[0].data or b"{}")
        self.assertNotIn("response_format", body)
        self.assertEqual(body["max_completion_tokens"], 4096)
        self.assertTrue(body["enable_thinking"])
        self.assertEqual(body["thinking_budget"], 900)
        self.assertEqual(body["temperature"], 0.0)
        self.assertEqual(body["seed"], 17)
        self.assertEqual(transport.timeouts, [7.5])

    def test_length_response_remains_recoverable(self) -> None:
        transport = SequenceTransport(
            response(
                200,
                {
                    "id": "chatcmpl_length",
                    "model": "qwen3.7-max",
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
            )
        )
        provider = QwenProvider(
            config(),
            "qwen-secret",
            client=client(transport),
        )

        parsed = provider.generate(
            system_prompt="system JSON",
            user_prompt="question",
            stage="proposal",
        )

        self.assertEqual(parsed.content, "")
        self.assertEqual(parsed.finish_reason, "length")

    def test_rejects_non_completion_finish_reasons(self) -> None:
        for finish_reason, category in (
            ("tool_calls", ErrorCategory.INVALID_RESPONSE),
            ("content_filter", ErrorCategory.CONTENT_FILTER),
            (None, ErrorCategory.INVALID_RESPONSE),
        ):
            with self.subTest(finish_reason=finish_reason):
                transport = SequenceTransport(
                    response(
                        200,
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
                        },
                    )
                )
                provider = QwenProvider(
                    config(),
                    "qwen-secret",
                    client=client(transport),
                )

                with self.assertRaises(ProviderError) as caught:
                    provider.generate(
                        system_prompt="system",
                        user_prompt="question",
                        stage="synthesis",
                    )

                self.assertEqual(caught.exception.category, category)
                self.assertFalse(caught.exception.ambiguous)

    def test_invalid_provider_options_fail_before_transmission(self) -> None:
        cases = (
            {"enable_thinking": "false"},
            {"thinking_budget": 100},
            {"enable_thinking": True, "thinking_budget": 1.5},
            {"temperature": -0.1},
            {"temperature": 2},
            {"top_p": float("nan")},
            {"temperature": 0.4, "top_p": 0.8},
            {"seed": True},
        )
        for extra in cases:
            with self.subTest(extra=extra):
                transport = SequenceTransport()
                provider = QwenProvider(
                    config(extra=extra),
                    "qwen-secret",
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


if __name__ == "__main__":
    unittest.main()
