# Changelog

All notable public changes to CouncilLogic are recorded here.

## Unreleased

No changes beyond the prepared `0.3.1a1` candidate.

## [0.3.1a1] - 2026-09-11, hardened 2026-10-19

Prepared locally for release. Publication and current provider availability
are not established by this candidate.

### Hardening (2026-10-19)

- `council decide RUN_ID --accept|--reject|--defer --note TEXT` records the
  human final decision in a new append-only, hash-chained `human_decisions`
  table (schema 2; SQLite triggers reject UPDATE and DELETE). `run`, `resume`
  and `inspect` lead with the deterministic winner, vote split, dissent and
  self-vote check, then show *Awaiting human decision* or the latest decision.
  `inspect --json` and `export` include every decision.
- Protocol `1.3.0-beta`: untrusted data is wrapped in random per-run
  delimiters (`secrets.token_hex`), persisted so resume rebuilds identical
  prompts. The synthesizer must name the aggregate outcome first in Outcome,
  and the engine checks it: a missing section, wrong winner, invented tie or
  invented winner marks the run degraded and is recorded as `synthesis_check`.
- Results include `self_votes`: jurors that ranked their own candidate first,
  plus the Borda result with every juror's own candidate removed, and whether
  that changes the outcome.
- Optional key sources: macOS Keychain (`MODEL_COUNCIL_KEYCHAIN_SERVICE`) and
  1Password (`<KEY>_OP_REF=op://...`), before the environment, with no Python
  dependencies. `council doctor` shows each key's source, age, a rotation
  reminder and the provider's key page; `--check-keys` validates with one free
  models-list call. Provider `repr` redacts keys. See `docs/KEYS.md`.
- Agent interface: `council agent` (one JSON request in, one JSON reply out)
  and a stdlib-only MCP server `council-mcp` (stdio JSON-RPC) with plan, run,
  inspect, list and capabilities. Every run reply ends with "Awaiting human
  decision". `decide` and `approve` are human-only and not exposed to agents.
  See `docs/AGENT-INTERFACE.md` and `docs/skills/councillogic/SKILL.md`.
- Paid runs require human approval: `council plan --json` reports a plan hash
  and call/token estimate; `council approve` (interactive terminal only)
  issues a single-use HMAC token bound to that plan hash, estimate and
  approver; `council run` refuses paid runs without a matching, unused token
  (exit 4). Schema 3 adds the `run_approvals` table. Mock runs are exempt.
- Agent schema v1 (`docs/agent-schema/v1/`, locked by tests): every reply has
  `schema_version`, `action`, `ok`; unknown request fields are rejected.
  `request_id` makes `run`/`start` retries return the same run (schema 4
  `agent_requests`); a reused id with a different payload is an error.
  `deadline_seconds` and `call_timeout_seconds` (also `--call-timeout-seconds`)
  bound runs and are part of the plan hash. `start` runs in the background and
  `status` reports progress; `council-mcp` sends `notifications/progress`
  when given a progress token.
- Optional separate OS user install (`docs/SEPARATE-USER.md`,
  `scripts/service-user/`, dry-run by default) for Linux and macOS.
- CI adds a ruff and mypy gate; type errors it found are fixed (including
  `os.sys.stderr` in two error paths).

### Added

- Self-contained source archives include release metadata, example configuration,
  public operations documentation, and the mock validation script. CI runs the
  test suite against the extracted archive as well as the checkout.
- Offline `council plan` admission checks report projected stage sizes, the
  limiting stage, effective plain-ASCII question capacity and remaining
  headroom, synthesis order, mandatory call reservations, and recovery capacity
  without resolving credentials, opening run storage, or contacting providers.
- An ordered, run-locked synthesis chain with per-slot outcomes, fallback
  transitions, and recovery records. The no-config live default tries OpenAI,
  Anthropic, then Gemini and stops at the first completed synthesis response;
  a fallback completion is degraded.

### Changed

- Recovery preserves mandatory jury quorum and the full remaining synthesis
  chain. The default seven-provider graph reserves seventeen mandatory calls
  and three recovery slots under the 20-call ceiling; a healthy run still uses
  fifteen calls. Existing file-backed configurations that omit synthesis
  fallbacks keep their single-provider chain.
- Cooperative deadlines are finite and positive, checked before invocation
  creation and dispatch, and cap the dispatched adapter timeout at the time
  remaining. Work that expires while queued remains recorded as undispatched.
- Resume reconstructs the stored provider and ordered synthesis locks, honors
  durable fallback transitions, and reuses completed work. Ambiguous calls
  remain suppressed; eligible non-ambiguous failures retain bounded retries.
- Protocol `1.2.2-beta` uses narrower provider-generation proposal targets while
  retaining canonical local validation bounds.
- Jury-repair prompt sizing now covers control-character expansion. Planning
  accounts for JSON escaping when reporting headroom and rejects question text
  that cannot be encoded as UTF-8 before a run is created.
- Provider failure records retain normalized, allowlisted error codes and safe
  request metadata while excluding raw provider error bodies and messages.
  Markdown exports expose failed invocation details and preserve undispatched,
  recovery, and synthesis-transition audit records.

### Boundaries

- The mock-only, loopback-only service remains frozen at `0.2.0a1`, with four
  deterministic lineages and nine logical calls. This candidate adds no hosted
  service or public deployment.
- Local tests establish software behavior. They do not establish current model
  access, broad live-provider reliability, or improved decision quality.

## [0.3.0a1] - 2026-08-08

This alpha expands the visible live council from five to seven default
lineages. The substantive change is that the fifteen-call decision is now
preflighted and governed as one bounded, recoverable process with explicit
degradation and an inspectable audit record.

### Added

- A credential-free governance-path proof and a `council --version` surface,
  backed by release-metadata consistency checks.
- xAI's Grok 4.5 as a fifth live CLI provider, using the Responses API,
  structured jury output, official-host restriction, external-only
  `XAI_API_KEY` resolution, and provider-reported cost metadata.
- Alibaba Qwen 3.7 Max and Cohere Command A+ as the sixth and seventh default
  live CLI providers, with bounded reasoning, official-host restrictions,
  external-only credential resolution, provider-specific structured-output
  handling, and strict local artifact validation.
- An optional, disabled Upstage Solar Pro 3 bench adapter. It is registered for
  explicit file-backed use but remains outside the default council pending a
  credential and live structured-output canary.
- A deterministic workload preflight that bounds question size and projected
  proposal, jury, and synthesis prompt growth before any provider call.
- Stage-specific provider output-token and request-timeout budgets, one
  auditable larger-output recovery for known length truncations, explicit
  clean/degraded completion quality, and workload/membership telemetry.
- A locked `max_parallel_calls` run policy and CLI override that cap active
  provider requests within each stage; the default is five.
- Sanitized transport-failure telemetry with the generated client request ID,
  elapsed milliseconds, transport phase, and timeout subtype.
- Wider target-to-hard-limit margins in proposal and jury prompts so providers
  that cannot enforce every JSON Schema bound are less likely to lose an
  otherwise useful artifact to a small local length or item-count overrun.
- An optional one-attempt, same-provider jury-artifact repair for ballots whose
  vote fields are valid but whose prose violates local bounds. The original
  and repaired invocations are preserved separately, decision fields are
  immutable, synthesis keeps a reserved call, and repaired runs are degraded.

### Changed

- The repository landing page now front-loads the operating thesis, runnable
  proof, system flow, public/private boundary, and claim limits before the
  detailed implementation mechanics.
- The `1.2.1-beta` proposal prompt now decouples requested or numbered
  deliverable counts from proposal array lengths, requires a final
  count-and-merge pass, and states every absolute item limit while retaining
  the three/two/three target and four/three/four hard-limit margins.
- The default Cohere configuration now pins temperature to zero for more
  predictable bounded structured output. Operators can still set an explicit
  Cohere temperature in file-backed configuration.
- Anthropic structured-output requests now remove provider-unsupported length
  and item-count schema constraints at the adapter boundary, carry those
  bounds forward in field descriptions, and retain the canonical bounded
  schema and local artifact validation.
- Blinded candidates now use one durably locked, run-scoped randomized
  namespace of unambiguous opaque labels, with a separate deterministic
  presentation order per juror and the same namespace reused for synthesis.
  This freezes downstream membership across resume and prevents
  protocol-created cross-juror label collisions without weakening blinding.
- Candidate membership and ordered adjudication inputs are durably locked
  before their downstream stages, so proposal or jury recovery cannot change
  an already-persisted jury or synthesis prompt on resume. Partial Markdown
  exports now expose the locked candidate namespace even without an aggregate.
- Application-level retries now preserve the prior provider failure atomically
  before restarting an invocation and surface successful or failed retry
  outcomes in the result, preventing recovered runs from being mislabeled
  clean. Markdown exports include those derived recovery outcomes.
- Workload synthesis bounds now model the larger all-candidate tie shape and
  maximum score widths, preventing a boundary preflight pass from becoming a
  runtime prompt-budget failure.
- Proposal and jury instructions now set concise target lengths below the
  schema's hard bounds and state field-specific item limits so constrained
  models do not pad, overfill, or corrupt artifacts at provider-relaxed schema
  limits. Local validation also bounds JSON-escaped string size and rejects
  unpaired surrogates before artifacts can exceed planned downstream prompts.
- The jury rationale target remains 400 characters while its hard validation
  limit increases from 700 to 1,000 characters. Protocol-enabled repair adds
  `jury_repair` invocation and workload telemetry,
  `jury_artifact_repair`/`incomplete_response_preserved` audit events, and the
  `membership.recovered_jury_repairs` count.
- The no-config live policy and example config enable one jury repair attempt.
  Programmatic `RunPolicy()`, mock/service policy, and file-backed configs that
  omit the new key retain zero attempts to avoid a silent billable-call change.
- Proposal responses are locally validated, size-bounded JSON artifacts.
  Juries consume those artifacts, and synthesis consumes bounded candidates
  plus compact vote records instead of every jury rationale.
- The no-config live and example-config call budget is 20, leaving five
  recovery slots above the normal fifteen-call seven-provider topology. Legacy
  file-backed configurations retain the prior 16-call default unless they opt
  into a higher ceiling. The default cooperative deadline is now 900 seconds.
- Gemini uses low thinking by default and a 4,096-token stage budget so hidden
  thinking is less likely to consume the visible artifact budget.
- Qwen defaults to Alibaba Model Studio's Singapore/International endpoint;
  other official regional hosts remain available only through explicit config.
  Its default proposal and jury timeouts are 300 seconds and its synthesis
  timeout is 360 seconds.
- Replaced the fixed-member square logo with a provider-count-neutral
  CouncilLogic mark and PageParcel-sized README header, and made the public
  product framing independent of the current provider roster.
- The default live council now uses seven proposals, seven juries, and one
  synthesis. Existing file-backed configurations do not silently acquire xAI,
  Qwen, Cohere, or Upstage; each post-`0.2.0a1` provider requires an explicit
  section, and Upstage must additionally be enabled.
- The mock-only `0.2.0a1` service remains frozen at four deterministic
  lineages and nine logical calls.

### Boundaries

- Seven live lineages broaden the engineering test surface; they are not
  evidence that fifteen calls outperform one strong model.
- The `0.3.0a1` package retains the mock-only, loopback-only `0.2.0a1`
  service profile. It is not a hosted or production service.
- Logical-call, prompt, concurrency, deadline, and recovery limits are not
  monetary budgets or provider-billing reconciliation.
- Current provider availability, pricing, and structured-output behavior must
  be verified against official documentation and direct canaries before use.

## [0.2.0a1] - 2026-07-26

Initial public alpha.

### Added

- Four-lineage OpenAI, Anthropic, Gemini, and Mistral council with independent
  proposals, blinded structured juries, deterministic Borda aggregation, and
  synthesis.
- Deterministic credential-free mock mode and a resumable SQLite audit record.
- Single-user live CLI with official-host restrictions, environment or
  external-command credential resolution, quorum controls, bounded logical
  call budgets, and explicit exports.
- Mock-only, loopback-only HTTP service with six fixed reference principals,
  action-scoped mandates, owner-scoped reads and idempotency, durable
  invocation reservations, token rotation and revocation, restart recovery,
  and single-writer fencing.
- Standard-library test suite, packaging checks, immutable-action CI, security
  and operations guidance, research grounding, and public project templates.

### Boundaries

- This is an alpha, not a production service or a correctness claim.
- The HTTP service cannot use live providers and must remain on loopback.
- Both reference work principals are forced disabled.
- Audit content is plaintext; there is no automatic redaction or retention.
- Logical call limits are not provider billing or monetary cost ceilings.
- External pull requests and copyrightable contributions remain closed until
  a counsel-reviewed contributor license agreement is available.

[Unreleased]: https://github.com/apcar/CouncilLogic/compare/v0.3.0a1...HEAD
[0.3.1a1]: #031a1---2026-09-11
[0.3.0a1]: https://github.com/apcar/CouncilLogic/releases/tag/v0.3.0a1
[0.2.0a1]: https://github.com/apcar/CouncilLogic/tree/v0.2.0a1
