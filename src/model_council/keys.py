"""Key-management helpers for ``council doctor``: rotation and optional checks.

Nothing here prints, logs, or persists a key. ``check_key`` makes one free,
lightweight "list models" request and is only invoked by
``council doctor --check-keys``.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request

from .providers.http import Transport, urllib_transport
from .secrets import redact

KEY_MANAGEMENT_URLS = {
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "gemini": "https://aistudio.google.com/app/apikey",
    "mistral": "https://console.mistral.ai/api-keys",
    "xai": "https://console.x.ai",
    "qwen": "https://modelstudio.console.alibabacloud.com/",
    "cohere": "https://dashboard.cohere.com/api-keys",
    "upstage": "https://console.upstage.ai/api-keys",
    "openrouter": "https://openrouter.ai/settings/keys",
}

_MODELS_URLS = {
    "openai": "https://api.openai.com/v1/models",
    "anthropic": "https://api.anthropic.com/v1/models",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/models",
    "mistral": "https://api.mistral.ai/v1/models",
    "xai": "https://api.x.ai/v1/models",
    "cohere": "https://api.cohere.ai/v1/models",
    "upstage": "https://api.upstage.ai/v1/models",
    # Returns key metadata (limits/usage) without spending credits.
    "openrouter": "https://openrouter.ai/api/v1/key",
}

ROTATION_DAYS = 90


def provider_kind(name: str) -> str:
    return "mock" if name.startswith("mock") else name


def models_url(kind: str, endpoint: str | None = None) -> str | None:
    if kind == "qwen":
        host = urlparse(endpoint or "").hostname or "dashscope-intl.aliyuncs.com"
        return f"https://{host}/compatible-mode/v1/models"
    return _MODELS_URLS.get(kind)


def _auth_headers(kind: str, key: str) -> dict[str, str]:
    if kind == "anthropic":
        return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    if kind == "gemini":
        return {"x-goog-api-key": key}
    return {"Authorization": f"Bearer {key}"}


def check_key(
    kind: str,
    key: str,
    *,
    endpoint: str | None = None,
    transport: Transport = urllib_transport,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Validate one key with a free models-list GET. Never echoes the key."""

    url = models_url(kind, endpoint)
    if url is None:
        return {"status": "unsupported", "detail": "no key check for provider"}
    request = Request(url, method="GET", headers=_auth_headers(kind, key))
    try:
        response = transport(request, timeout)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        return {
            "status": "error",
            "detail": redact(f"{type(exc).__name__}: {exc}", key),
        }
    if 200 <= response.status_code < 300:
        return {"status": "valid", "detail": f"HTTP {response.status_code}"}
    if response.status_code in (401, 403):
        return {"status": "invalid", "detail": f"HTTP {response.status_code}"}
    detail = f"HTTP {response.status_code}"
    try:
        body = json.loads(response.body[:2000] or b"{}")
        message = body.get("error", {}).get("message") if isinstance(body, dict) and isinstance(body.get("error"), dict) else None
    except (ValueError, AttributeError):
        message = None
    if isinstance(message, str):
        detail += ": " + redact(message, key)[:200]
    return {"status": "error", "detail": detail}


def rotation_status(
    rotated_at: datetime | None, *, now: datetime | None = None
) -> dict[str, Any]:
    if rotated_at is None:
        return {
            "key_age_days": None,
            "rotation_due": None,
            "reminder": (
                "Key age unknown; set <NAME>_ROTATED_AT=YYYY-MM-DD or store "
                f"the key in Keychain. Rotate at least every {ROTATION_DAYS} days."
            ),
        }
    now = now or datetime.now(timezone.utc)
    age = max(0, (now - rotated_at).days)
    due = age >= ROTATION_DAYS
    return {
        "key_age_days": age,
        "rotation_due": due,
        "reminder": (
            f"Key is {age} days old; rotation is due (policy {ROTATION_DAYS} days)."
            if due
            else f"Key is {age} days old; next rotation in {ROTATION_DAYS - age} days."
        ),
    }
