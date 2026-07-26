from __future__ import annotations

import argparse
import json
import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)


_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_TERMINAL_STATES = {"completed", "partial", "failed", "cancelled"}


class _NoRedirectHandler(HTTPRedirectHandler):
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


@dataclass(frozen=True)
class RemoteCouncilError(RuntimeError):
    status: int
    code: str
    message: str
    request_id: str | None = None

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


def _validated_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        raise ValueError("server URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("server URL must not contain a query or fragment")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("server URL must be an absolute HTTP(S) URL")
    if parsed.scheme != "https" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("non-loopback council URLs must use HTTPS")
    return value.rstrip("/")


def read_private_token(path: str | os.PathLike[str]) -> str:
    token_path = Path(path).expanduser()
    if token_path.is_symlink():
        raise ValueError("token file must not be a symlink")
    metadata = token_path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("token path must be a regular file")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError("token file must not be accessible by group or others")
    token = token_path.read_text(encoding="utf-8").strip()
    if len(token) < 32:
        raise ValueError("token file does not contain a valid high-entropy token")
    if any(character.isspace() for character in token):
        raise ValueError("token must be a single non-whitespace value")
    return token


class RemoteCouncilClient:
    """Thin client for the authenticated council service.

    Provider credentials never enter this process. The only local credential is
    the revocable council principal token.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.base_url = _validated_base_url(base_url)
        self._loopback = urlsplit(self.base_url).hostname in _LOOPBACK_HOSTS
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("a high-entropy council token is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._token = token
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_token_file(
        cls,
        base_url: str,
        token_file: str | os.PathLike[str],
        *,
        timeout_seconds: float = 30.0,
    ) -> RemoteCouncilClient:
        return cls(
            base_url,
            read_private_token(token_file),
            timeout_seconds=timeout_seconds,
        )

    def create_run(
        self,
        question: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question cannot be empty")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key cannot be empty")
        return self._request(
            "POST",
            "/v1/runs",
            payload={"question": question.strip()},
            extra_headers={"Idempotency-Key": idempotency_key.strip()},
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        try:
            normalized = str(uuid.UUID(run_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError("run_id must be a UUID") from exc
        return self._request("GET", f"/v1/runs/{quote(normalized)}")

    def wait(
        self,
        run_id: str,
        *,
        timeout_seconds: float = 300.0,
        poll_seconds: float = 0.25,
    ) -> dict[str, Any]:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("wait and poll intervals must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            payload = self.get_run(run_id)
            if payload.get("status") in _TERMINAL_STATES:
                return payload
            if time.monotonic() >= deadline:
                raise TimeoutError(f"council run {run_id} did not finish in time")
            time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz", authenticated=False)

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "model-council-remote/0.2.0a1",
        }
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        if extra_headers:
            headers.update(extra_headers)
        body: bytes | None = None
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        opener = (
            build_opener(ProxyHandler({}), _NoRedirectHandler())
            if self._loopback
            else build_opener(_NoRedirectHandler())
        )
        try:
            with opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            try:
                try:
                    raw = exc.read()
                except OSError:
                    raw = b""
            finally:
                exc.close()
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = {}
            error = decoded.get("error") if isinstance(decoded, dict) else None
            if not isinstance(error, dict):
                error = {}
            raise RemoteCouncilError(
                status=exc.code,
                code=str(error.get("code") or "http_error"),
                message=str(error.get("message") or "Council request failed"),
                request_id=(
                    str(error.get("request_id") or decoded.get("request_id"))
                    if isinstance(decoded, dict)
                    and (error.get("request_id") or decoded.get("request_id"))
                    else None
                ),
            ) from None
        except URLError as exc:
            raise ConnectionError("could not reach the council service") from exc
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("council service returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("council service returned a non-object response")
        return decoded


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="council-remote",
        description="Submit work to a governed CouncilLogic service.",
    )
    parser.add_argument("--server", required=True, help="Council service base URL")
    parser.add_argument(
        "--token-file",
        required=True,
        help="0600 file containing this caller's council token",
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--json", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("health")

    run = subparsers.add_parser("run")
    input_group = run.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--question")
    input_group.add_argument("--file")
    run.add_argument(
        "--idempotency-key",
        required=True,
        help="Stable non-secret key required before the request is sent",
    )
    run.add_argument("--wait", action="store_true")
    run.add_argument("--wait-seconds", type=float, default=300.0)

    status = subparsers.add_parser("status")
    status.add_argument("run_id")

    wait = subparsers.add_parser("wait")
    wait.add_argument("run_id")
    wait.add_argument("--wait-seconds", type=float, default=300.0)
    return parser


def _print_payload(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if payload.get("run_id"):
        print(f"Run: {payload['run_id']}")
    if payload.get("status"):
        print(f"Status: {payload['status']}")
    result = payload.get("result")
    if isinstance(result, dict) and result.get("answer"):
        print("\n" + str(result["answer"]).strip())
    elif payload.get("mode") and payload.get("ready") is not None:
        print(f"Mode: {payload['mode']}")
        print("Ready" if payload["ready"] else "Not ready")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        client = RemoteCouncilClient.from_token_file(
            args.server,
            args.token_file,
            timeout_seconds=args.timeout_seconds,
        )
        if args.command == "health":
            payload = client.health()
        elif args.command == "run":
            question = (
                Path(args.file).read_text(encoding="utf-8")
                if args.file
                else args.question
            )
            idempotency_key = args.idempotency_key
            payload = client.create_run(
                question,
                idempotency_key=idempotency_key,
            )
            if args.wait:
                accepted_run_id = str(payload["run_id"])
                try:
                    payload = client.wait(
                        accepted_run_id,
                        timeout_seconds=args.wait_seconds,
                    )
                except (
                    OSError,
                    ValueError,
                    RuntimeError,
                    TimeoutError,
                ):
                    print(
                        json.dumps(
                            {
                                "recovery": True,
                                "run_id": accepted_run_id,
                                "idempotency_key": idempotency_key,
                            },
                            indent=2,
                            sort_keys=True,
                        )
                    )
                    raise
            payload.setdefault("idempotency_key", idempotency_key)
        elif args.command == "status":
            payload = client.get_run(args.run_id)
        else:
            payload = client.wait(
                args.run_id,
                timeout_seconds=args.wait_seconds,
            )
        _print_payload(payload, as_json=args.json)
        return 0
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f"error: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
