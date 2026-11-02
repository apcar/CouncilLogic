from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from model_council import agent as agent_module  # noqa: E402
from model_council.agent import handle_request  # noqa: E402
from model_council.approval import issue_token, plan_record  # noqa: E402
from model_council.cli import main  # noqa: E402
from model_council.config import default_config, mock_config  # noqa: E402
from model_council.mcp_server import TOOLS, McpServer  # noqa: E402
from model_council.store import CouncilStore  # noqa: E402

QUESTION = "Should we sunset the legacy platform this quarter?"


def _call(arguments: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        if stdin is None:
            code = main(arguments)
        else:
            with patch("sys.stdin", StringIO(stdin)):
                code = main(arguments)
    return code, out.getvalue(), err.getvalue()


def _live(tmp: str):
    from dataclasses import replace

    return replace(default_config(), data_dir=Path(tmp))


def _approve(config, question: str, name: str = "Andrew") -> str:
    plan = plan_record(config, question)
    token = issue_token(config.data_dir, plan, name)
    CouncilStore(config.data_dir).record_run_approval(
        hashlib.sha256(token.encode()).hexdigest(),
        plan["plan_hash"],
        name,
        plan["estimate"]["max_calls"],
    )
    return token


class StubEngine:
    """Stands in for paid providers: creates a real run, makes no calls."""

    def __init__(self, config) -> None:
        self.store = CouncilStore(config.data_dir)
        self.calls = 0

    def create_run(self, question: str, *, idempotency_key: str | None = None) -> str:
        self.calls += 1
        return self.store.create_run(
            question=question, protocol_id="p", protocol_version="v",
            protocol_hash="h", provider_configs=[], policy={},
            idempotency_key=idempotency_key,
        )

    def resume(self, run_id: str):
        return {"run_id": run_id, "status": "completed", "warnings": []}


class PlanTests(unittest.TestCase):
    def test_plan_json_has_hash_and_estimate(self) -> None:
        code, out, err = _call(["plan", "--question", QUESTION, "--json"])
        self.assertEqual(code, 0, err)
        plan = json.loads(out)
        self.assertRegex(plan["plan_hash"], r"^[0-9a-f]{64}$")
        self.assertTrue(plan["approval_required"])
        self.assertEqual(plan["estimate"]["healthy_run_calls"], 15)
        self.assertGreaterEqual(plan["estimate"]["max_calls"], 15)
        self.assertIsNone(plan["estimate"]["currency_estimate"])

    def test_plan_hash_binds_question_selection_and_policy(self) -> None:
        base = default_config()
        one = plan_record(base, QUESTION)["plan_hash"]
        self.assertEqual(one, plan_record(base, QUESTION)["plan_hash"])
        self.assertNotEqual(one, plan_record(base, QUESTION + "!")["plan_hash"])
        from dataclasses import replace

        bigger = replace(base, policy=replace(base.policy, max_calls=base.policy.max_calls + 5))
        self.assertNotEqual(one, plan_record(bigger, QUESTION)["plan_hash"])

    def test_mock_plan_needs_no_approval(self) -> None:
        plan = plan_record(mock_config(), QUESTION)
        self.assertFalse(plan["approval_required"])


class PaidRunRefusalTests(unittest.TestCase):
    def test_cli_paid_run_refused_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "model_council.cli._engine", side_effect=AssertionError("must not run")
        ):
            code, out, err = _call(["--data-dir", tmp, "run", "--question", QUESTION])
        self.assertEqual(code, 4)
        self.assertIn("human approval is required", err)

    def test_agent_paid_run_refused_without_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "model_council.cli._engine", side_effect=AssertionError("must not run")
        ):
            reply = handle_request({"action": "run", "question": QUESTION}, _live(tmp))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "approval_required")

    def test_token_mismatches_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "model_council.cli._engine", side_effect=AssertionError("must not run")
        ):
            config = _live(tmp)
            token = _approve(config, QUESTION)
            cases = [
                {"question": QUESTION + " And also the other one?", "approved_by": "Andrew", "approval_token": token},
                {"question": QUESTION, "approved_by": "Agent", "approval_token": token},
                {"question": QUESTION, "approved_by": "Andrew", "approval_token": token[:-1] + ("1" if token.endswith("0") else "0")},
                {"question": QUESTION, "approved_by": "Andrew", "approval_token": "cla1.deadbeef"},
                {"question": QUESTION, "providers": ["openai", "anthropic", "gemini"], "approved_by": "Andrew", "approval_token": token},
            ]
            for case in cases:
                reply = handle_request({"action": "run", **case}, config)
                self.assertFalse(reply["ok"], case)
                self.assertEqual(reply["error"], "approval_required", case)

    def test_bigger_run_with_same_question_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "model_council.cli._engine", side_effect=AssertionError("must not run")
        ):
            token = _approve(_live(tmp), QUESTION)
            code, _, err = _call([
                "--data-dir", tmp, "run", "--question", QUESTION, "--max-calls", "20",
                "--max-recovery-output-tokens", "9999",
                "--approved-by", "Andrew", "--approval-token", token,
            ])
        self.assertEqual(code, 4)
        self.assertIn("does not match", err)

    def test_forged_token_without_recorded_approval_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _live(tmp)
            token = issue_token(config.data_dir, plan_record(config, QUESTION), "Andrew")
            reply = handle_request(
                {"action": "run", "question": QUESTION, "approved_by": "Andrew", "approval_token": token},
                config,
            )
        self.assertEqual(reply["error"], "approval_required")
        self.assertIn("no recorded human approval", reply["message"])

    def test_valid_token_runs_once_and_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = _live(tmp)
            token = _approve(config, QUESTION)
            engines: list[StubEngine] = []

            def make(cfg):
                engines.append(StubEngine(cfg))
                return engines[-1]

            request = {"action": "run", "question": QUESTION, "approved_by": "Andrew", "approval_token": token}
            with patch("model_council.cli._engine", side_effect=make):
                first = handle_request(request, config)
                second = handle_request(request, config)
            self.assertTrue(first["ok"], first)
            self.assertEqual(first["approval"]["approved_by"], "Andrew")
            self.assertEqual(first["human_decision_status"], "awaiting")
            self.assertFalse(second["ok"])
            self.assertIn("already used", second["message"])
            self.assertEqual(sum(e.calls for e in engines), 1)
            events = CouncilStore(tmp).list_events(first["run_id"])
            approval = [e for e in events if e["event_type"] == "human_run_approval"]
            self.assertEqual(approval[0]["payload"]["approved_by"], "Andrew")
            self.assertNotIn(token, json.dumps(events))


class ApproveCommandTests(unittest.TestCase):
    def _plan_hash(self, tmp: str) -> str:
        return plan_record(_live(tmp), QUESTION)["plan_hash"]

    def test_approve_requires_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = _call([
                "--data-dir", tmp, "approve", "--question", QUESTION,
                "--plan-hash", self._plan_hash(tmp), "--approved-by", "Andrew",
            ], stdin="")
        self.assertEqual(code, 4)
        self.assertIn("interactive terminal", err)
        self.assertNotIn("cla1.", out)

    def test_approve_rejects_other_plan_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = _call([
                "--data-dir", tmp, "approve", "--question", QUESTION,
                "--plan-hash", "0" * 64, "--approved-by", "Andrew",
            ])
        self.assertEqual(code, 4)
        self.assertIn("does not match", err)

    def test_interactive_approve_issues_working_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_hash = self._plan_hash(tmp)
            with patch("sys.stdin.isatty", return_value=True), patch(
                "builtins.input", return_value=plan_hash[:12]
            ):
                code, out, err = _call([
                    "--data-dir", tmp, "approve", "--question", QUESTION,
                    "--plan-hash", plan_hash, "--approved-by", "Andrew",
                ])
            self.assertEqual(code, 0, err)
            token = out.strip().splitlines()[-1]
            self.assertTrue(token.startswith("cla1."))
            with patch("model_council.cli._engine", side_effect=lambda cfg: StubEngine(cfg)):
                code, out, err = _call([
                    "--data-dir", tmp, "run", "--question", QUESTION, "--json",
                    "--approved-by", "Andrew", "--approval-token", token,
                ])
            self.assertEqual(code, 0, err)

    def test_wrong_confirmation_is_not_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_hash = self._plan_hash(tmp)
            with patch("sys.stdin.isatty", return_value=True), patch(
                "builtins.input", return_value="yes"
            ):
                code, out, err = _call([
                    "--data-dir", tmp, "approve", "--question", QUESTION,
                    "--plan-hash", plan_hash, "--approved-by", "Andrew",
                ])
        self.assertEqual(code, 4)
        self.assertNotIn("cla1.", out)

    def test_approve_refuses_mock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = _call([
                "--mock", "--data-dir", tmp, "approve", "--question", QUESTION,
                "--plan-hash", "0" * 64, "--approved-by", "Andrew",
            ])
        self.assertEqual(code, 2)


class MockAgentFlowTests(unittest.TestCase):
    def test_plan_then_run_with_mock_via_cli_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            common = ["--mock", "--data-dir", tmp, "agent"]
            code, out, err = _call(common, stdin=json.dumps({"action": "plan", "question": QUESTION}))
            self.assertEqual(code, 0, err)
            plan = json.loads(out)
            self.assertFalse(plan["approval_required"])
            request = Path(tmp) / "req.json"
            request.write_text(json.dumps({"action": "run", "question": QUESTION}))
            code, out, err = _call([*common, "--request", str(request)])
            self.assertEqual(code, 0, err)
            run = json.loads(out)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["human_decision_status"], "awaiting")
            self.assertTrue(run["next_step"].startswith("Awaiting human decision"))
            self.assertTrue(run["verdict"][0].startswith(("Council winner", "Council result")))
            code, out, _ = _call(common, stdin=json.dumps({"action": "inspect", "run_id": run["run_id"]}))
            self.assertEqual(json.loads(out)["human_decision_status"], "awaiting")
            code, out, _ = _call(common, stdin=json.dumps({"action": "list"}))
            self.assertEqual(len(json.loads(out)["runs"]), 1)

    def test_bad_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            common = ["--mock", "--data-dir", tmp, "agent"]
            for raw in ["not json", "[]", json.dumps({"action": "nope"}), json.dumps({"action": "plan"})]:
                code, out, _ = _call(common, stdin=raw)
                self.assertEqual(code, 2, raw)
                self.assertFalse(json.loads(out)["ok"])


class DecideIsHumanOnlyTests(unittest.TestCase):
    def test_agent_cannot_decide_or_approve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = mock_config(tmp)
            run = handle_request({"action": "run", "question": QUESTION}, config)
            for action in ("decide", "approve"):
                reply = handle_request(
                    {"action": action, "run_id": run["run_id"], "decision": "accept", "note": "x"},
                    config,
                )
                self.assertFalse(reply["ok"])
                self.assertEqual(reply["error"], "human_only")
            # Smuggled decision fields on allowed actions are rejected.
            smuggled = handle_request({"action": "inspect", "run_id": run["run_id"], "decision": "accept"}, config)
            self.assertEqual(smuggled["error"], "unknown_field")
            self.assertEqual(CouncilStore(tmp).list_human_decisions(run["run_id"]), [])

    def test_agent_module_never_writes_decisions(self) -> None:
        source = Path(agent_module.__file__).read_text()
        self.assertNotIn("record_human_decision", source)
        self.assertNotIn("issue_token", source)
        self.assertNotIn("record_run_approval", source)

    def test_mcp_exposes_no_decide_or_approve(self) -> None:
        names = {tool["name"] for tool in TOOLS}
        self.assertEqual(
            names,
            {"council_capabilities", "council_plan", "council_run", "council_start",
             "council_status", "council_inspect", "council_list"},
        )
        server = McpServer(mock=True, data_dir=None, config_path=None)
        for name in ("council_decide", "council_approve", "decide"):
            reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name}})
            assert reply is not None
            self.assertEqual(reply["error"]["code"], -32602)


class McpServerTests(unittest.TestCase):
    def test_stdio_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = McpServer(mock=True, data_dir=tmp, config_path=None)
            lines = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "council_plan", "arguments": {"question": QUESTION}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "council_run", "arguments": {"question": QUESTION}}},
                {"jsonrpc": "2.0", "id": 5, "method": "bogus"},
            ]
            stdin = StringIO("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
            stdout = StringIO()
            server.serve(stdin, stdout)
            replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in replies], [1, 2, 3, 4, 5, None])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "councillogic")
        self.assertEqual(len(replies[1]["result"]["tools"]), 7)
        self.assertFalse(replies[2]["result"]["structuredContent"]["approval_required"])
        run = replies[3]["result"]["structuredContent"]
        self.assertEqual(run["human_decision_status"], "awaiting")
        self.assertFalse(replies[3]["result"]["isError"])
        self.assertEqual(replies[4]["error"]["code"], -32601)
        self.assertEqual(replies[5]["error"]["code"], -32700)

    def test_mcp_paid_run_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch(
            "model_council.cli._engine", side_effect=AssertionError("must not run")
        ):
            server = McpServer(mock=False, data_dir=tmp, config_path=None)
            reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "council_run", "arguments": {"question": QUESTION}}})
        assert reply is not None
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(reply["result"]["structuredContent"]["error"], "approval_required")


if __name__ == "__main__":
    unittest.main()
