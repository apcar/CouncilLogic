from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council import agent as agent_module  # noqa: E402
from model_council.agent import ALLOWED_FIELDS, AGENT_ACTIONS, SCHEMA_VERSION, handle_request  # noqa: E402
from model_council.config import mock_config  # noqa: E402
from model_council.mcp_server import TOOLS, McpServer  # noqa: E402
from model_council.providers.mock import MockProvider  # noqa: E402
from model_council.store import CouncilStore  # noqa: E402

SCHEMA_DIR = PROJECT_ROOT / "docs" / "agent-schema" / "v1"
SERVICE_DIR = PROJECT_ROOT / "scripts" / "service-user"
QUESTION = "Should we migrate the billing system to the new vendor?"
# Changing a v1 schema file is a breaking-change review point: bump
# schema_version (new docs/agent-schema/vN) or update these hashes on purpose.
LOCKED_SCHEMA_SHA256 = {
    "request.schema.json": "dec5f95746c686eb2522680132cf5fd93d2dbf79f5d82b181596d09ee016d432",
    "reply.schema.json": "14e24bcafc82d4d943e84c7d28b360d021ee2ba93d810acb8b9850ec34ed0934",
}


def _types(value: object) -> set[str]:
    if value is None:
        return {"null"}
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, int):
        return {"integer", "number"}
    if isinstance(value, float):
        return {"number"}
    if isinstance(value, str):
        return {"string"}
    if isinstance(value, list):
        return {"array"}
    return {"object"}


def validate(value: object, schema: dict, path: str = "$") -> list[str]:
    """Small JSON Schema subset validator (stdlib only) for these schemas."""

    errors: list[str] = []
    if "type" in schema:
        allowed = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not _types(value) & set(allowed):
            return [f"{path}: expected {allowed}, got {sorted(_types(value))}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: expected const {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} not in enum")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path}: too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: too long")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{path}: pattern mismatch")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: below minimum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: not above exclusiveMinimum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path}: too few items")
        for index, item in enumerate(value):
            if "items" in schema:
                errors += validate(item, schema["items"], f"{path}[{index}]")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing {key}")
        props = schema.get("properties", {})
        for key, item in value.items():
            if key in props:
                errors += validate(item, props[key], f"{path}.{key}")
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected {key}")
    if "oneOf" in schema:
        matches = [s for s in schema["oneOf"] if not validate(value, s, path)]
        if len(matches) != 1:
            errors.append(f"{path}: matched {len(matches)} oneOf variants")
    for sub in schema.get("allOf", []):
        if "if" in sub:
            if not validate(value, sub["if"], path):
                errors += validate(value, sub.get("then", {}), path)
        else:
            errors += validate(value, sub, path)
    return errors


def load(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


class SchemaLockTests(unittest.TestCase):
    def test_schema_files_are_locked(self) -> None:
        for name, digest in LOCKED_SCHEMA_SHA256.items():
            actual = hashlib.sha256((SCHEMA_DIR / name).read_bytes()).hexdigest()
            self.assertEqual(actual, digest, f"{name} changed: bump schema_version or update lock")

    def test_schema_matches_code(self) -> None:
        request = load("request.schema.json")
        variants = {v["properties"]["action"]["const"]: v for v in request["oneOf"]}
        self.assertEqual(set(variants), set(AGENT_ACTIONS))
        self.assertEqual(SCHEMA_VERSION, "1")
        for action, variant in variants.items():
            fields = set(variant["properties"]) - {"schema_version", "action"}
            self.assertEqual(fields, set(ALLOWED_FIELDS[action]), action)
        self.assertNotIn("decide", variants)
        self.assertNotIn("approve", variants)
        mcp_actions = {tool["name"].removeprefix("council_") for tool in TOOLS}
        self.assertEqual(mcp_actions, set(AGENT_ACTIONS))

    def test_example_requests_validate(self) -> None:
        schema = load("request.schema.json")
        good = [
            {"action": "capabilities"},
            {"schema_version": "1", "action": "plan", "question": "Q?", "deadline_seconds": 60},
            {"action": "run", "question": "Q?", "request_id": "abc-1", "approved_by": "Andrew",
             "approval_token": "cla1." + "0" * 64, "call_timeout_seconds": 30},
            {"action": "status", "run_id": "r"},
        ]
        for request in good:
            self.assertEqual(validate(request, schema), [], request)
        bad = [
            {"action": "decide", "run_id": "r"},
            {"action": "plan"},
            {"action": "plan", "question": "Q?", "extra": 1},
            {"action": "run", "question": "Q?", "request_id": "has space"},
            {"schema_version": "2", "action": "list"},
        ]
        for request in bad:
            self.assertNotEqual(validate(request, schema), [], request)

    def test_replies_validate_against_reply_schema(self) -> None:
        schema = load("reply.schema.json")
        with tempfile.TemporaryDirectory() as tmp:
            config = mock_config(tmp)
            requests = [
                {"action": "capabilities"},
                {"action": "plan", "question": QUESTION},
                {"action": "run", "question": QUESTION, "request_id": "r1"},
                {"action": "run", "question": QUESTION, "request_id": "r1"},
                {"action": "run", "question": QUESTION + "?", "request_id": "r1"},
                {"action": "list"},
                {"action": "status", "run_id": "missing"},
                {"action": "decide", "run_id": "x"},
                {"action": "nope"},
                {"schema_version": "9", "action": "list"},
                {"action": "list", "bogus": True},
                "not an object",
            ]
            replies = [handle_request(r, config) for r in requests]
            run_id = replies[2]["run_id"]
            replies.append(handle_request({"action": "inspect", "run_id": run_id}, config))
            replies.append(handle_request({"action": "status", "run_id": run_id}, config))
        for request, reply in zip(requests + ["inspect", "status"], replies, strict=True):
            self.assertEqual(validate(reply, schema), [], (request, reply))
            self.assertEqual(reply["schema_version"], "1")


class RequestIdTests(unittest.TestCase):
    def test_retry_returns_same_run_and_conflict_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = mock_config(tmp)
            first = handle_request({"action": "run", "question": QUESTION, "request_id": "req-1"}, config)
            again = handle_request({"action": "run", "question": "  " + QUESTION + " ", "request_id": "req-1"}, config)
            self.assertTrue(first["ok"])
            self.assertFalse(first["replayed"])
            self.assertTrue(again["replayed"])
            self.assertEqual(first["run_id"], again["run_id"])
            self.assertEqual(len(CouncilStore(tmp).list_runs()), 1)
            conflict = handle_request(
                {"action": "run", "question": QUESTION, "request_id": "req-1", "deadline_seconds": 5},
                config,
            )
            self.assertEqual(conflict["error"], "request_id_conflict")
            other = handle_request({"action": "run", "question": QUESTION, "request_id": "req-2"}, config)
            self.assertNotEqual(other["run_id"], first["run_id"])

    def test_paid_retry_does_not_need_or_consume_a_new_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = mock_config(tmp)
            first = handle_request({"action": "run", "question": QUESTION, "request_id": "p"}, config)
            with patch.object(agent_module, "guarded_create", side_effect=AssertionError("no new run")):
                again = handle_request({"action": "start", "question": QUESTION, "request_id": "p"}, config)
            self.assertTrue(again["replayed"])
            self.assertEqual(again["run_id"], first["run_id"])

    def test_start_and_run_share_request_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = mock_config(tmp)
            first = handle_request({"action": "run", "question": QUESTION, "request_id": "x"}, config)
            other = handle_request({"action": "run", "question": "Different?", "request_id": "x"}, config)
            self.assertTrue(first["ok"])
            self.assertEqual(other["error"], "request_id_conflict")


class SlowMock(MockProvider):
    delay = 0.3

    def generate(self, **kwargs):  # type: ignore[override]
        time.sleep(self.delay)
        return super().generate(**kwargs)


class TimeoutTests(unittest.TestCase):
    def test_deadline_gives_clean_partial_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "model_council.providers.factory.MockProvider", SlowMock
        ):
            config = mock_config(tmp)
            reply = handle_request(
                {"action": "run", "question": QUESTION, "deadline_seconds": 0.5}, config
            )
        self.assertTrue(reply["ok"], reply)
        self.assertIn(reply["status"], {"partial", "failed"})
        self.assertNotEqual(reply["completion_quality"], "clean")
        self.assertEqual(reply["human_decision_status"], "awaiting")
        self.assertTrue(reply["next_step"].startswith("Awaiting human decision"))

    def test_call_timeout_caps_every_stage(self) -> None:
        from model_council.cli import apply_call_timeout

        with tempfile.TemporaryDirectory() as tmp:
            config = mock_config(tmp)
            selected = agent_module.select_config(
                config, {"question": QUESTION, "call_timeout_seconds": 2.5}, None
            )
            for provider in selected.providers:
                self.assertLessEqual(provider.timeout_seconds, 2.5)
                for stage in ("proposal", "jury", "synthesis"):
                    self.assertLessEqual(provider.timeout_for(stage), 2.5)
            with self.assertRaises(ValueError):
                apply_call_timeout(config.providers[0], 0)
            bad = handle_request({"action": "plan", "question": QUESTION, "call_timeout_seconds": -1}, config)
            self.assertEqual(bad["error"], "bad_request")

    def test_timeouts_change_the_plan_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = mock_config(tmp)
            base = handle_request({"action": "plan", "question": QUESTION}, config)["plan_hash"]
            short = handle_request({"action": "plan", "question": QUESTION, "call_timeout_seconds": 5}, config)["plan_hash"]
            fast = handle_request({"action": "plan", "question": QUESTION, "deadline_seconds": 60}, config)["plan_hash"]
        self.assertEqual(len({base, short, fast}), 3)

    def test_cli_accepts_call_timeout(self) -> None:
        from model_council.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            out = StringIO()
            with redirect_stdout(out), redirect_stderr(StringIO()):
                code = main(["--mock", "--data-dir", tmp, "plan", "--question", QUESTION,
                             "--call-timeout-seconds", "3", "--deadline-seconds", "30", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("plan_hash", json.loads(out.getvalue()))


class AsyncTests(unittest.TestCase):
    def test_start_then_poll_status_until_finished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = mock_config(tmp)
            args = agent_module.worker_args_for(True, tmp, None)
            started = handle_request(
                {"action": "start", "question": QUESTION, "request_id": "async-1"},
                config,
                worker_args=args,
            )
            self.assertTrue(started["ok"], started)
            self.assertIn("poll", started["next_step"])
            deadline = time.monotonic() + 60
            status = started
            while time.monotonic() < deadline:
                status = handle_request({"action": "status", "run_id": started["run_id"]}, config)
                if status.get("finished"):
                    break
                time.sleep(0.2)
            self.assertTrue(status.get("finished"), status)
            self.assertEqual(status["status"], "completed")
            self.assertGreater(status["progress"]["finished_calls"], 0)
            self.assertTrue(status["next_step"].startswith("Awaiting human decision"))
            log = Path(tmp) / "worker-logs" / f"{started['run_id']}.log"
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_spawn_worker_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch("subprocess.Popen") as popen:
            agent_module.spawn_worker(["--mock", "--data-dir", tmp], "RID", Path(tmp))
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:3], ["-m", "model_council.cli"])
        self.assertEqual(argv[-3:], ["resume", "RID", "--json"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])


class McpProgressTests(unittest.TestCase):
    def test_progress_notifications_for_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "model_council.providers.factory.MockProvider", SlowMock
        ):
            SlowMock.delay = 0.05
            server = McpServer(mock=True, data_dir=tmp, config_path=None)
            message = {
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "council_run", "arguments": {"question": QUESTION},
                           "_meta": {"progressToken": "tok"}},
            }
            stdout = StringIO()
            server.serve(StringIO(json.dumps(message) + "\n"), stdout, progress_interval=0.01)
            SlowMock.delay = 0.3
        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        notes = [m for m in lines if m.get("method") == "notifications/progress"]
        self.assertTrue(notes, lines)
        self.assertTrue(all(n["params"]["progressToken"] == "tok" for n in notes))
        progress = [n["params"]["progress"] for n in notes]
        self.assertEqual(progress, sorted(progress))
        final = lines[-1]
        self.assertEqual(final["id"], 7)
        self.assertEqual(final["result"]["structuredContent"]["human_decision_status"], "awaiting")


class ServiceUserScriptTests(unittest.TestCase):
    SCRIPTS = ("agent-entry", "human-entry", "read-key", "council-agent",
               "council-mcp-agent", "install.sh", "add-key.sh")

    def test_scripts_parse(self) -> None:
        for name in self.SCRIPTS:
            result = subprocess.run(["sh", "-n", str(SERVICE_DIR / name)], capture_output=True)
            self.assertEqual(result.returncode, 0, (name, result.stderr))

    def test_install_defaults_to_dry_run(self) -> None:
        result = subprocess.run(
            ["sh", str(SERVICE_DIR / "install.sh"), "--wheel", "/tmp/x.whl", "--agent-user", "bot"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        commands = [line for line in lines if not line.startswith("Next:")]
        self.assertTrue(commands)
        self.assertTrue(all(line.startswith("would run: ") for line in commands), commands)
        text = result.stdout
        self.assertIn("-m 0700", text)
        self.assertIn("/var/lib/councillogic/keys", text)
        self.assertIn("visudo -cf", text)
        self.assertIn("0440", text)

    def test_agent_entry_accepts_only_agent_or_mcp(self) -> None:
        for args in ([], ["decide"], ["approve"], ["agent", "extra"][1:], ["sh"]):
            result = subprocess.run(["sh", str(SERVICE_DIR / "agent-entry"), *args], capture_output=True)
            self.assertEqual(result.returncode, 64, args)
        text = (SERVICE_DIR / "agent-entry").read_text()
        self.assertIn("env -i", text)
        self.assertNotIn("decide", text.split("case")[1].split("esac")[0].replace("usage", ""))

    def test_human_entry_rejects_agent_mode(self) -> None:
        result = subprocess.run(["sh", str(SERVICE_DIR / "human-entry"), "agent"], capture_output=True)
        self.assertEqual(result.returncode, 64)

    def test_read_key_rejects_bad_names(self) -> None:
        for name in ("", "../etc/passwd", "lower", "A B", "A/B"):
            result = subprocess.run(["sh", str(SERVICE_DIR / "read-key"), name], capture_output=True)
            self.assertNotEqual(result.returncode, 0, name)
            self.assertEqual(result.stdout, b"")

    def test_sudoers_rule_is_narrow(self) -> None:
        rules = [
            line for line in (SERVICE_DIR / "sudoers.councillogic").read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        self.assertEqual(len(rules), 2)
        self.assertIn("env_reset", rules[0])
        rule = rules[1]
        self.assertTrue(rule.startswith("%councillogic-agents ALL=(councillogic) NOPASSWD: "))
        commands = [c.strip() for c in rule.split("NOPASSWD:", 1)[1].split(",")]
        self.assertEqual(commands, [
            "/usr/local/libexec/councillogic/agent-entry agent",
            "/usr/local/libexec/councillogic/agent-entry mcp",
        ])
        self.assertNotIn("ALL=(ALL)", rule)
        self.assertNotIn("human-entry", rule)

    def test_wrappers_call_only_agent_entry(self) -> None:
        self.assertIn("agent-entry agent", (SERVICE_DIR / "council-agent").read_text())
        self.assertIn("agent-entry mcp", (SERVICE_DIR / "council-mcp-agent").read_text())


if __name__ == "__main__":
    unittest.main()
