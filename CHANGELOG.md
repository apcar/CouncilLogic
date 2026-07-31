# Changelog

All notable public changes to CouncilLogic are recorded here.

## Unreleased

### Added

- xAI's Grok 4.5 as a fifth live CLI provider, using the Responses API,
  structured jury output, official-host restriction, external-only
  `XAI_API_KEY` resolution, and provider-reported cost metadata.
- A deterministic workload preflight that bounds question size and projected
  proposal, jury, and synthesis prompt growth before any provider call.
- Stage-specific provider output-token and request-timeout budgets, one
  auditable larger-output recovery for known length truncations, explicit
  clean/degraded completion quality, and workload/membership telemetry.

### Changed

- Anthropic structured-output requests now remove provider-unsupported length
  and item-count schema constraints at the adapter boundary while retaining the
  canonical bounded schema and local artifact validation.
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
- Proposal instructions now set concise target lengths below the schema's hard
  bounds and state field-specific item limits so constrained models do not pad,
  overfill, or corrupt artifacts at provider-relaxed schema limits. Local
  validation also bounds JSON-escaped string size and rejects unpaired
  surrogates before artifacts can exceed planned downstream prompts.
- Proposal responses are locally validated, size-bounded JSON artifacts.
  Juries consume those artifacts, and synthesis consumes bounded candidates
  plus compact vote records instead of every jury rationale.
- The default call budget is 16, leaving room for the normal eleven-call live
  topology plus at most one truncation recovery per participant slot; the
  default cooperative deadline is 480 seconds.
- Gemini uses low thinking by default and a 4,096-token stage budget so hidden
  thinking is less likely to consume the visible artifact budget.
- Replaced the fixed-member square logo with a provider-count-neutral
  CouncilLogic mark and PageParcel-sized README header, and made the public
  product framing independent of the current provider roster.
- The default live council now uses five proposals, five juries, and one
  synthesis. Existing file-backed four-provider configurations remain
  four-provider until they add an explicit `[providers.xai]` section.
- The mock-only `0.2.0a1` service remains frozen at four deterministic
  lineages and nine logical calls.

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

[0.2.0a1]: https://github.com/apcar/CouncilLogic/releases/tag/v0.2.0a1
