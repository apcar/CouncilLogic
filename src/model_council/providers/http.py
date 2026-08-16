from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import math
import random
import socket
import time
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from model_council.models import ErrorCategory, ProviderError
from model_council.version import PACKAGE_VERSION


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class JsonResponse:
    data: dict[str, Any]
    status_code: int
    headers: Mapping[str, str]
    request_id: str | None
    client_request_id: str
    attempts: int


class Transport(Protocol):
    def __call__(self, request: Request, timeout: float) -> HttpResponse:
        """Perform one HTTP request without applying retry policy."""


class _NoRedirectHandler(HTTPRedirectHandler):
    """Return redirects to the caller instead of forwarding auth headers."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def urllib_transport(request: Request, timeout: float) -> HttpResponse:
    """Perform one bounded request without following provider redirects."""

    opener = build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            return HttpResponse(
                status_code=int(response.status),
                headers=dict(response.headers.items()),
                body=response.read(16 * 1024 * 1024 + 1),
            )
    except HTTPError as exc:
        try:
            try:
                body = exc.read(16 * 1024 * 1024 + 1)
            except OSError:
                body = b""
        finally:
            exc.close()
        return HttpResponse(
            status_code=int(exc.code),
            headers=dict(exc.headers.items()) if exc.headers else {},
            body=body,
        )


def header_value(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == wanted:
            return str(value)
    return None


def response_request_id(headers: Mapping[str, str]) -> str | None:
    for name in (
        "mistral-correlation-id",
        "x-kong-request-id",
        "request-id",
        "x-request-id",
        "x-goog-request-id",
        "x-cloud-trace-context",
    ):
        value = header_value(headers, name)
        if value:
            return value[:256]
    return None


def _classified_http_error(
    status_code: int,
    *,
    request_id: str | None,
) -> ProviderError:
    if status_code == 401:
        category = ErrorCategory.AUTHENTICATION
        retryable = False
    elif status_code == 403:
        category = ErrorCategory.PERMISSION
        retryable = False
    elif status_code == 408:
        category = ErrorCategory.TIMEOUT
        retryable = True
    elif status_code in (409, 425, 429):
        category = ErrorCategory.RATE_LIMIT
        retryable = True
    elif 500 <= status_code <= 599:
        category = ErrorCategory.PROVIDER_SERVER
        retryable = True
    elif 400 <= status_code <= 499:
        category = ErrorCategory.INVALID_REQUEST
        retryable = False
    else:
        category = ErrorCategory.INVALID_RESPONSE
        retryable = False

    # An explicit HTTP failure is safe to retry when classified retryable.
    # Network errors and successful-but-unparseable responses are handled as
    # ambiguous below and are never retried automatically.
    return ProviderError(
        f"Provider request failed with HTTP {status_code}",
        category=category,
        retryable=retryable,
        status_code=status_code,
        request_id=request_id,
        ambiguous=False,
    )


def _with_attempts(error: ProviderError, attempts: int) -> ProviderError:
    return ProviderError(
        str(error),
        category=error.category,
        retryable=error.retryable,
        status_code=error.status_code,
        request_id=error.request_id,
        attempts=attempts,
        ambiguous=error.ambiguous,
        client_request_id=error.client_request_id,
        elapsed_ms=error.elapsed_ms,
        transport_phase=error.transport_phase,
        timeout_subtype=error.timeout_subtype,
    )


class JsonHttpClient:
    """Minimal JSON/HTTPS client with conservative, observable retry rules."""

    def __init__(
        self,
        *,
        transport: Transport = urllib_transport,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        wall_time: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        backoff_base_seconds: float = 0.5,
        backoff_cap_seconds: float = 8.0,
        max_retry_after_seconds: float = 60.0,
        max_response_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self._transport = transport
        self._sleep = sleep
        self._random_value = random_value
        self._wall_time = wall_time
        self._monotonic = monotonic
        self._backoff_base_seconds = max(0.0, backoff_base_seconds)
        self._backoff_cap_seconds = max(0.0, backoff_cap_seconds)
        self._max_retry_after_seconds = max(0.0, max_retry_after_seconds)
        self._max_response_bytes = max(1, max_response_bytes)

    def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
        max_attempts: int,
    ) -> JsonResponse:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderError(
                "Provider endpoint must be credential-free HTTPS",
                category=ErrorCategory.INVALID_REQUEST,
                retryable=False,
            )
        if timeout_seconds <= 0:
            raise ProviderError(
                "Provider timeout must be positive",
                category=ErrorCategory.INVALID_REQUEST,
                retryable=False,
            )

        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                f"Provider request could not be encoded: {type(exc).__name__}",
                category=ErrorCategory.INVALID_REQUEST,
                retryable=False,
            ) from None

        attempts_allowed = max(1, int(max_attempts))
        client_request_id = str(uuid.uuid4())
        safe_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": f"model-council/{PACKAGE_VERSION}",
            "X-Client-Request-Id": client_request_id,
            **dict(headers),
        }

        for attempt in range(1, attempts_allowed + 1):
            request = Request(
                url,
                data=encoded,
                headers=safe_headers,
                method="POST",
            )
            attempt_started = self._monotonic()
            try:
                response = self._transport(request, timeout_seconds)
            except (socket.timeout, TimeoutError):
                error = ProviderError(
                    "Provider request timed out after transmission may have begun",
                    category=ErrorCategory.TIMEOUT,
                    retryable=True,
                    attempts=attempt,
                    ambiguous=True,
                    client_request_id=client_request_id,
                    elapsed_ms=self._elapsed_ms(attempt_started),
                    transport_phase="request_in_flight",
                    timeout_subtype="socket_or_os_timeout",
                )
                raise error from None
            except URLError as exc:
                category = (
                    ErrorCategory.TIMEOUT
                    if isinstance(exc.reason, (socket.timeout, TimeoutError))
                    else ErrorCategory.CONNECTION
                )
                error = ProviderError(
                    "Provider connection failed after transmission may have begun",
                    category=category,
                    retryable=True,
                    attempts=attempt,
                    ambiguous=True,
                    client_request_id=client_request_id,
                    elapsed_ms=self._elapsed_ms(attempt_started),
                    transport_phase="request_in_flight",
                    timeout_subtype=(
                        "url_error_timeout"
                        if category == ErrorCategory.TIMEOUT
                        else None
                    ),
                )
                raise error from None
            except (ConnectionError, OSError):
                error = ProviderError(
                    "Provider connection failed after transmission may have begun",
                    category=ErrorCategory.CONNECTION,
                    retryable=True,
                    attempts=attempt,
                    ambiguous=True,
                    client_request_id=client_request_id,
                    elapsed_ms=self._elapsed_ms(attempt_started),
                    transport_phase="request_in_flight",
                )
                raise error from None
            except ProviderError as exc:
                enriched = ProviderError(
                    str(exc),
                    category=exc.category,
                    retryable=exc.retryable,
                    status_code=exc.status_code,
                    request_id=exc.request_id,
                    attempts=exc.attempts,
                    ambiguous=exc.ambiguous,
                    client_request_id=(
                        exc.client_request_id or client_request_id
                    ),
                    elapsed_ms=(
                        exc.elapsed_ms
                        if exc.elapsed_ms is not None
                        else self._elapsed_ms(attempt_started)
                    ),
                    transport_phase=(
                        exc.transport_phase or "request_in_flight"
                    ),
                    timeout_subtype=exc.timeout_subtype,
                )
                raise _with_attempts(enriched, attempt) from None
            except Exception as exc:
                raise ProviderError(
                    f"Provider transport failed safely: {type(exc).__name__}",
                    category=ErrorCategory.UNKNOWN,
                    retryable=False,
                    attempts=attempt,
                    ambiguous=True,
                    client_request_id=client_request_id,
                    elapsed_ms=self._elapsed_ms(attempt_started),
                    transport_phase="request_in_flight",
                ) from None

            if not isinstance(response, HttpResponse):
                raise ProviderError(
                    "Provider transport returned an invalid response",
                    category=ErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                    attempts=attempt,
                    ambiguous=True,
                )

            request_id = response_request_id(response.headers)
            if not 200 <= response.status_code <= 299:
                error = _classified_http_error(
                    response.status_code,
                    request_id=request_id,
                )
                if (
                    error.retryable
                    and not error.ambiguous
                    and attempt < attempts_allowed
                ):
                    self._sleep(self._retry_delay(response.headers, attempt))
                    continue
                raise _with_attempts(error, attempt) from None

            if len(response.body) > self._max_response_bytes:
                raise ProviderError(
                    "Provider response exceeded the safe size limit",
                    category=ErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                    status_code=response.status_code,
                    request_id=request_id,
                    attempts=attempt,
                    ambiguous=True,
                )
            try:
                decoded = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ProviderError(
                    "Provider returned malformed JSON",
                    category=ErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                    status_code=response.status_code,
                    request_id=request_id,
                    attempts=attempt,
                    ambiguous=True,
                ) from None
            if not isinstance(decoded, dict):
                raise ProviderError(
                    "Provider returned a non-object JSON response",
                    category=ErrorCategory.INVALID_RESPONSE,
                    retryable=False,
                    status_code=response.status_code,
                    request_id=request_id,
                    attempts=attempt,
                    ambiguous=True,
                )
            return JsonResponse(
                data=decoded,
                status_code=response.status_code,
                headers=response.headers,
                request_id=request_id,
                client_request_id=client_request_id,
                attempts=attempt,
            )

        raise AssertionError("retry loop exhausted without returning or raising")

    def _elapsed_ms(self, started_at: float) -> int:
        """Return bounded-shape timing metadata without exception details."""

        elapsed_seconds = self._monotonic() - started_at
        if not math.isfinite(elapsed_seconds):
            return 0
        return max(0, int(round(elapsed_seconds * 1000)))

    def _retry_delay(
        self,
        headers: Mapping[str, str],
        attempt: int,
    ) -> float:
        retry_after = self._parse_retry_after(header_value(headers, "retry-after"))
        if retry_after is not None:
            return min(retry_after, self._max_retry_after_seconds)
        ceiling = min(
            self._backoff_cap_seconds,
            self._backoff_base_seconds * (2 ** max(0, attempt - 1)),
        )
        return min(max(0.0, self._random_value()), 1.0) * ceiling

    def _parse_retry_after(self, value: str | None) -> float | None:
        if not value:
            return None
        try:
            seconds = float(value.strip())
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = parsed.timestamp() - self._wall_time()
        if not math.isfinite(seconds):
            return None
        if seconds < 0:
            return 0.0
        return seconds
