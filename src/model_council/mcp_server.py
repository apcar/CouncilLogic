"""Minimal stdlib-only MCP server (stdio, newline-delimited JSON-RPC 2.0).

Exposes the agent interface as tools: plan, run, inspect, list and
capabilities. ``decide`` and ``approve`` are intentionally absent; see
``model_council.agent`` and docs/AGENT-INTERFACE.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence, TextIO

from .agent import capabilities, config_for, handle_request
from .version import PACKAGE_VERSION

PROTOCOL_VERSION = "2025-06-18"

_SELECTION = {
    "providers": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Optional provider subset",
    },
    "synthesis_providers": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Optional ordered synthesis chain",
    },
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "council_capabilities",
        "description": "Describe the CouncilLogic agent interface and its human-only boundaries.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "council_plan",
        "description": (
            "Required first step. Offline cost estimate and plan hash for a "
            "council run on a big, contested, hard-to-reverse decision. No API calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"question": {"type": "string"}, **_SELECTION},
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "council_run",
        "description": (
            "Run the council. Paid runs need approved_by and approval_token "
            "issued by a human with `council approve` for this exact plan. "
            "The result is advice and ends 'Awaiting human decision'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                **_SELECTION,
                "approved_by": {"type": "string"},
                "approval_token": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "council_inspect",
        "description": "Read a run's verdict and human decision status.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}},
            "required": ["run_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "council_list",
        "description": "List recent runs.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1}},
            "additionalProperties": False,
        },
    },
]
_TOOL_ACTIONS = {
    "council_capabilities": "capabilities",
    "council_plan": "plan",
    "council_run": "run",
    "council_inspect": "inspect",
    "council_list": "list",
}


class McpServer:
    def __init__(self, *, mock: bool, data_dir: str | None, config_path: str | None) -> None:
        self.mock = mock
        self.data_dir = data_dir
        self.config_path = config_path

    def _result(self, request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def handle(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid Request")
        method = message.get("method")
        request_id = message.get("id")
        is_notification = "id" not in message
        if is_notification:
            return None
        params = message.get("params") or {}
        if method == "initialize":
            return self._result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "councillogic", "version": PACKAGE_VERSION},
                    "instructions": " ".join(capabilities()["rules"]),
                },
            )
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            action = _TOOL_ACTIONS.get(name) if isinstance(name, str) else None
            if action is None:
                return self._error(request_id, -32602, f"Unknown tool: {name}")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                return self._error(request_id, -32602, "arguments must be an object")
            try:
                base = config_for(self.mock, self.data_dir, self.config_path)
            except (OSError, ValueError) as exc:
                reply: dict[str, Any] = {"ok": False, "error": "bad_config", "message": str(exc)}
            else:
                reply = handle_request(
                    {**arguments, "action": action}, base, config_path=self.config_path
                )
            return self._result(
                request_id,
                {
                    "content": [{"type": "text", "text": json.dumps(reply, sort_keys=True)}],
                    "structuredContent": reply,
                    "isError": not reply.get("ok"),
                },
            )
        return self._error(request_id, -32601, f"Method not found: {method}")

    def serve(self, stdin: TextIO, stdout: TextIO) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response: dict[str, Any] | None = self._error(None, -32700, "Parse error")
            else:
                response = self.handle(message)
            if response is not None:
                stdout.write(json.dumps(response) + "\n")
                stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="council-mcp", description=__doc__)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--data-dir")
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    McpServer(mock=args.mock, data_dir=args.data_dir, config_path=args.config).serve(
        sys.stdin, sys.stdout
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
