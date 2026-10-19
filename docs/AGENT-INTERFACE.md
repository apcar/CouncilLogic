# Agent interface

CouncilLogic can be called by AI agents for **big, contested, hard-to-reverse
decisions**: a question where reasonable models may disagree and a wrong call
is costly to undo. It is not for routine lookups.

Every run ends in **Awaiting human decision**. Agents can ask the council;
only a human can approve spending money on it and only a human can decide.

## Entry points (zero dependencies)

| Entry point | Transport | Use |
|---|---|---|
| `council agent [--request FILE]` | one JSON object on stdin (or file) → one JSON reply on stdout | shell-capable agents, scripts |
| `council-mcp [--mock] [--data-dir DIR] [--config FILE]` | MCP over stdio (newline-delimited JSON-RPC 2.0) | MCP clients |
| [`docs/skills/councillogic/SKILL.md`](skills/councillogic/SKILL.md) | skill doc | agents that load skill files |

Global flags (`--mock`, `--data-dir`, `--config`) go before `agent`, e.g.
`council --mock agent < request.json`. Exit code is 0 when `ok` is true, else 2.

## Actions

| Action | MCP tool | Request fields | Network / cost |
|---|---|---|---|
| `capabilities` | `council_capabilities` | none | none |
| `plan` | `council_plan` | `question`, optional `providers`, `synthesis_providers` | none (offline) |
| `run` | `council_run` | `question`, same selection, `approved_by` + `approval_token` for paid runs, optional `idempotency_key` | paid unless `--mock` |
| `inspect` | `council_inspect` | `run_id` | none |
| `list` | `council_list` | optional `limit` | none |

`decide` and `approve` are **not** actions. Asking for them returns
`{"ok": false, "error": "human_only", ...}` and the MCP server does not list or
accept them.

## Required flow for a paid run

1. **Agent: plan.** `{"action": "plan", "question": "..."}` returns
   `plan_hash`, `estimate` (`max_calls`, `healthy_run_calls`,
   `max_input_tokens_upper_bound`, `max_output_tokens_upper_bound`, providers
   and models) and `approval_required`. No API calls are made. There is no
   currency figure: multiply the token bounds by each provider's current price.
2. **Agent → human.** Show the human the question, the estimate and the plan
   hash, and ask for approval. Do not run anything yet.
3. **Human: approve, in a terminal.**
   `council approve --question "..." [same selection flags] --plan-hash HASH --approved-by NAME`
   recomputes the plan, refuses if the hash differs from what was reviewed,
   shows the estimate, and asks the human to retype the first 12 characters of
   the hash. It refuses when stdin is not a terminal. It prints a single-use
   token `cla1.<hmac>`.
4. **Agent: run** with the same question and selection, plus
   `"approved_by": "NAME", "approval_token": "cla1..."`.
5. **Agent → human.** Report the verdict (winner, vote split, dissent,
   self-votes, synthesis check) and the `next_step` text, which begins
   "Awaiting human decision". Never present the result as the decision.
6. **Human: decide.** `council decide RUN_ID --accept|--reject|--defer --note TEXT`.

Mock runs (`--mock`, or a config whose providers are all mock) need no approval.

### What the token binds

The token is `HMAC-SHA256(key, plan_hash ∥ approver ∥ max_calls)`. The key is
a random 32-byte secret in `<data-dir>/approval.key` (mode 0600), created by
the first `council approve`. The plan hash covers:
- the question's SHA-256;
- every provider's name, model, lineage and endpoint;
- the synthesis chain;
- the full run policy (including `max_calls`, deadlines and output and recovery limits);
- the workload projection and estimate.

Any change makes the run refuse: a different or longer question, extra
providers, a larger call budget or recovery tokens, or a different approver.
A token is recorded when issued, consumed when the run is created (it can't
be reused, even with the same idempotency key) and linked to the run as a
`human_run_approval` event. The token itself is never stored in the run
record, only its SHA-256. A token that was computed but never issued by
`council approve` is refused.

## Why `decide` and `approve` are human-only

- **Decide.** CouncilLogic's core claim is that *a human makes the final
  decision*. If an agent could write the permanent decision record, that
  claim would be false. The record would then show what an agent chose, not
  what a person chose. So the agent module never imports or calls the decision
  writer, and tests check this.
- **Approve.** The approval gate exists so a person agrees to spend before
  money is spent. An agent that could approve its own plan would make the gate
  decorative. That is why `approve` needs an interactive terminal and a
  retyped hash prefix.

### Limits of this boundary (honest version)

These are guardrails against mistakes and overreach, not a sandbox. An agent
with unrestricted shell access as the same OS user could read
`approval.key`, fake a TTY, or open the SQLite file directly. For agents
you don't fully trust:
- give them only the MCP server or `council agent`, not a general shell;
- or run the agent as a different OS user, so it can't read
  `<data-dir>/approval.key` or write `council.sqlite3`.

The append-only triggers and the hash chain make tampering with decisions
detectable and awkward, not impossible for the file's owner.

## Example (mock, no keys)

```bash
echo '{"action":"plan","question":"Sunset the legacy platform this quarter?"}' \
  | council --mock --data-dir /tmp/demo agent
echo '{"action":"run","question":"Sunset the legacy platform this quarter?"}' \
  | council --mock --data-dir /tmp/demo agent
```

The run reply includes `"human_decision_status": "awaiting"` and
`"next_step": "Awaiting human decision. ..."`.

## MCP client configuration

```json
{
  "mcpServers": {
    "councillogic": {
      "command": "council-mcp",
      "args": ["--data-dir", "/Users/you/.local/share/model-council"]
    }
  }
}
```

Add `"--mock"` to try it without keys or cost. Keys resolve the same way as
the CLI (see [KEYS.md](KEYS.md)); the MCP server never accepts keys as tool
arguments.
