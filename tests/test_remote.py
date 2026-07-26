from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council.remote import (  # noqa: E402
    RemoteCouncilClient,
    RemoteCouncilError,
    main as remote_main,
    read_private_token,
)


TOKEN = "portable-council-token-with-at-least-32-characters"


class _RemoteFixture(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []
    sink_authorization: list[str | None] = []
    redirect = False

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"ready": True, "mode": "mock"})
            return
        if self.path == "/sink":
            type(self).sink_authorization.append(
                self.headers.get("Authorization")
            )
            self._json(200, {"unexpected": True})
            return
        run_id = self.path.rsplit("/", 1)[-1]
        self._json(200, {"run_id": run_id, "status": "completed"})

    def do_POST(self) -> None:
        if type(self).redirect:
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://127.0.0.1:{self.server.server_port}/sink",
            )
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length") or "0")
        body = json.loads(self.rfile.read(length))
        type(self).requests.append(
            {
                "authorization": self.headers.get("Authorization"),
                "idempotency": self.headers.get("Idempotency-Key"),
                "body": body,
            }
        )
        self._json(
            202,
            {
                "run_id": "d55a7de7-5a80-4f13-a1bd-a4fb24246421",
                "status": "queued",
            },
        )

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _ProxyCanary(BaseHTTPRequestHandler):
    requests: list[tuple[str, str | None]] = []

    def do_GET(self) -> None:
        type(self).requests.append(
            (self.path, self.headers.get("Authorization"))
        )
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class RemoteCouncilClientTests(unittest.TestCase):
    def setUp(self) -> None:
        _RemoteFixture.requests = []
        _RemoteFixture.sink_authorization = []
        _RemoteFixture.redirect = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RemoteFixture)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_create_and_get_use_only_the_council_token(self) -> None:
        client = RemoteCouncilClient(self.base_url, TOKEN)
        created = client.create_run(
            "What should the council verify?",
            idempotency_key="request-1",
        )
        fetched = client.get_run(created["run_id"])

        self.assertEqual(fetched["status"], "completed")
        self.assertEqual(
            _RemoteFixture.requests,
            [
                {
                    "authorization": f"Bearer {TOKEN}",
                    "idempotency": "request-1",
                    "body": {"question": "What should the council verify?"},
                }
            ],
        )

    def test_redirect_is_returned_as_error_and_never_receives_token(self) -> None:
        _RemoteFixture.redirect = True
        client = RemoteCouncilClient(self.base_url, TOKEN)
        with self.assertRaises(RemoteCouncilError) as caught:
            client.create_run("Question", idempotency_key="redirect-test")
        self.assertEqual(caught.exception.status, 302)
        self.assertEqual(_RemoteFixture.sink_authorization, [])

    def test_non_loopback_plaintext_url_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            RemoteCouncilClient("http://example.com", TOKEN)

    def test_loopback_requests_ignore_hostile_environment_proxies(self) -> None:
        _ProxyCanary.requests = []
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyCanary)
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        try:
            with mock.patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": proxy_url,
                    "HTTPS_PROXY": proxy_url,
                    "http_proxy": proxy_url,
                    "https_proxy": proxy_url,
                    "NO_PROXY": "",
                    "no_proxy": "",
                },
            ):
                payload = RemoteCouncilClient(
                    self.base_url,
                    TOKEN,
                ).health()
            self.assertTrue(payload["ready"])
            self.assertEqual(_ProxyCanary.requests, [])
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=2)

    def test_token_file_must_be_private_regular_and_not_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "token"
            token_path.write_text(TOKEN + "\n", encoding="utf-8")
            os.chmod(token_path, 0o600)
            self.assertEqual(read_private_token(token_path), TOKEN)

            os.chmod(token_path, 0o644)
            with self.assertRaises(PermissionError):
                read_private_token(token_path)

            os.chmod(token_path, 0o600)
            link_path = Path(temporary) / "link"
            link_path.symlink_to(token_path)
            with self.assertRaisesRegex(ValueError, "symlink"):
                read_private_token(link_path)

    def test_run_cli_requires_explicit_idempotency_key_before_client_use(
        self,
    ) -> None:
        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit) as caught, mock.patch(
                "model_council.remote.RemoteCouncilClient.from_token_file"
            ) as build_client:
                remote_main(
                    [
                        "--server",
                        self.base_url,
                        "--token-file",
                        "/not/read",
                        "run",
                        "--question",
                        "Must not transmit.",
                    ]
                )
        self.assertEqual(caught.exception.code, 2)
        build_client.assert_not_called()

    def test_wait_output_preserves_explicit_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "token"
            token_path.write_text(TOKEN, encoding="utf-8")
            os.chmod(token_path, 0o600)
            output = StringIO()
            with redirect_stdout(output):
                status = remote_main(
                    [
                        "--server",
                        self.base_url,
                        "--token-file",
                        str(token_path),
                        "--json",
                        "run",
                        "--question",
                        "Wait without losing recovery identity.",
                        "--idempotency-key",
                        "durable-wait-key",
                        "--wait",
                    ]
                )
            self.assertEqual(status, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["idempotency_key"], "durable-wait-key")
            self.assertEqual(
                _RemoteFixture.requests[-1]["idempotency"],
                "durable-wait-key",
            )

    def test_wait_failure_emits_recovery_json_after_acceptance(self) -> None:
        client = mock.Mock()
        client.create_run.return_value = {
            "run_id": "d55a7de7-5a80-4f13-a1bd-a4fb24246421",
            "status": "queued",
        }
        client.wait.side_effect = TimeoutError("synthetic polling timeout")
        stdout = StringIO()
        stderr = StringIO()
        with mock.patch(
            "model_council.remote.RemoteCouncilClient.from_token_file",
            return_value=client,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            status = remote_main(
                [
                    "--server",
                    self.base_url,
                    "--token-file",
                    "/not-read-because-client-is-injected",
                    "--json",
                    "run",
                    "--question",
                    "Recover after accepted polling failure.",
                    "--idempotency-key",
                    "accepted-recovery-key",
                    "--wait",
                ]
            )

        self.assertEqual(status, 2)
        self.assertIn("synthetic polling timeout", stderr.getvalue())
        recovery = json.loads(stdout.getvalue())
        self.assertEqual(
            recovery,
            {
                "recovery": True,
                "run_id": "d55a7de7-5a80-4f13-a1bd-a4fb24246421",
                "idempotency_key": "accepted-recovery-key",
            },
        )
        client.create_run.assert_called_once_with(
            "Recover after accepted polling failure.",
            idempotency_key="accepted-recovery-key",
        )
        client.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
