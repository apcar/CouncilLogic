# Security

## Public-alpha posture

CouncilLogic `0.2.0a1` adds a hardened, governed service alpha to the
single-user local CLI. The service is mock-only, loopback-only, and must not be
exposed as a public network daemon. It has application-level multi-principal
controls, but it is not a production identity, billing, or Internet-facing
system. Live service, Cloudflare or another remote front door, work use, and
commercial use remain gated.

The five-provider CLI is a separate surface. Local tests and prior live checks
do not remove the alpha boundaries below, establish current provider health,
or authorize service deployment.

Before using sensitive material, decide whether sending that material to all
five providers is permitted under the applicable contracts, account settings,
data residency requirements, and law.

## Assets and trust boundaries

The principal assets are:

- OpenAI, Anthropic, Gemini, Mistral, and xAI API credentials.
- Questions and contextual information supplied by the operator.
- Provider prompts and raw responses.
- Jury records, aggregate rankings, and synthesized answers.
- Provider request IDs, usage, latency, errors, and local configuration.
- The SQLite audit database, exports, and backups.
- Council bearer tokens and their persisted one-way verifiers.
- Principal, mandate, ownership, idempotency, accepted-job, and logical
  invocation-reservation records.

The trust boundaries are:

1. The local operator account and Python process.
2. An optional external secret-command executable.
3. The local filesystem and backup system.
4. The network connection to each official provider API.
5. Each provider's processing, logging, retention, and abuse-monitoring systems.
6. Model-generated text, which is untrusted content even when several models
   agree.
7. The loopback HTTP boundary between a thin client and the mock-only service.
8. The single active SQLite writer and a separately fenced standby.

## Implemented controls

### Mock-only loopback service

The `0.2.0a1` service accepts only loopback bind addresses and constructs four
deterministic mock lineages. This frozen reference fixture does not mirror the
five-provider live CLI.
Startup rejects a store containing any live-mode run. This is a fail-closed
separation from the live CLI, not permission to add a tunnel.

The server bounds workers, queue depth, connection read time, body size,
question size, headers, and JSON shape. POST error responses close the
connection so an unread or malformed body cannot desynchronize HTTP
keepalive. A symlinked database path is rejected before resolution.

### Principal authentication and mandates

The service catalog has six fixed principals. The four personal principals
may be enabled for mock testing; `work-laptop-human` and
`work-laptop-agent` are forced disabled. Each enabled principal uses a
separate high-entropy bearer token whose verifier, not plaintext token, is
stored.

Token rotation replaces that principal's current verifier atomically. Every
rotated or revoked verifier hash is retained as a durable tombstone with its
principal, token identifier, retirement time, and reason; plaintext tokens are
never stored. Bootstrap and rotation reject a verifier found in any
principal's tombstone history. Therefore a configuration rollback presenting
old token A after replacement token B is active cannot reactivate A or mutate
B. The operator can narrowly revoke one principal's token with
`council-service --data-dir DIR --revoke-token PRINCIPAL`.

Retirement is intentionally one-way in this alpha. There is no bootstrap flag,
database edit, or ordinary rotation path that reissues the same token bytes.
If a future operational requirement genuinely needs reissue, it must cross a
separate authenticated, explicitly approved, audited operator boundary with
its own tests. Until that mechanism exists, generate a new high-entropy token.

Durable mandates authorize the actions `run:create`, `run:read`, and
`provider:invoke`, along with provider/model allowlists and per-principal
inflight, daily, and monthly logical-invocation ceilings. A revoked mandate is
preserved across restart and fails closed rather than being overwritten.

### Ownership, idempotency, and recovery

Run reads and idempotency keys are scoped to the authenticated owner.
Cross-owner reads have the same external result as an unknown run. Concurrent
exact submissions collapse onto one durable run, while reuse by the same owner
with a different request conflicts.

Accepted nonterminal jobs are recovered after restart. If a crash leaves an
idempotency binding before its Council run row, an exact replay repairs that
run under its assigned identifier. In-process scheduling is deduplicated.
An invocation found `running` after a crash remains ambiguous/uncertain and is
not blindly retried; its reservation remains held.

### Single active writer and client separation

`CouncilApplication` takes an exclusive nonblocking process lock for its
lifetime. A second service process cannot share the data directory. A standby
must remain fenced because the local lock is not distributed consensus.

The service marks its directory as service-managed. Local CLI `run`, `resume`,
`inspect`, `list`, and `export` refuse that directory; access goes through the
authenticated `council-remote` surface. The remote client rejects redirects,
requires HTTPS away from loopback, requires a private regular token file, and
disables environment proxies for loopback requests.

### Logical-invocation reservations

Each authorized mock provider invocation obtains a durable logical call-unit
reservation. A settled reservation cannot be replayed as new authorization,
and an ambiguous call keeps its reservation pending.

A logical call unit is an application-level provider invocation. It is not an
upstream HTTP-attempt count, money, a provider charge, or customer billing.
The alpha has no live-provider metering, reconciliation, or pricing; live and
commercial service use remain disabled until those controls exist.

### Credential minimization

Credentials are resolved at process startup from either an external command or
the process environment. `.env` files are not loaded. Credential values are not
included in provider configuration records, printed by `doctor`, or
intentionally persisted.

The storage layer rejects recognized raw-credential field names and endpoint
URLs that carry credential-like query parameters. This does not find arbitrary
secrets embedded in free-form questions, prompts, errors, or model output.

### External secret command

When `MODEL_COUNCIL_SECRET_COMMAND` is set, the command receives one additional
argument containing the logical secret name. It has a ten-second timeout and a
minimal child environment containing only `PATH`. Its standard output becomes
the credential only when it exits successfully.

Treat this executable as security-critical:

- Use an absolute path.
- Keep it owned by the operator and non-writable by other users.
- Prefer mode `0700` or stricter.
- Avoid a shell wrapper when a direct executable is practical.
- Validate the logical name against a fixed allowlist.
- Never interpolate the name into a shell command.
- Print only the credential to standard output.
- Do not log the credential, arguments plus resolved value, or provider output.
- Emit only generic errors to standard error.
- Fail closed when the requested name is unknown.

**Key Keeper 0.8 is not directly integrated.** A Key Keeper-backed command
adapter would cross an additional trust boundary and must be separately
implemented, reviewed, and tested before relying on it.

### Provider destination restriction

Live endpoints must use HTTPS and are restricted by provider name to:

```text
api.openai.com
api.anthropic.com
generativelanguage.googleapis.com
api.mistral.ai
api.x.ai
```

Embedded URL credentials and non-allowlisted hosts are rejected. This reduces
accidental key exfiltration through configuration. It is not certificate
pinning and does not defend a compromised operating system, trust store, DNS
stack, Python runtime, or provider.

### No model tools or actions

Provider adapters request text generation only. The application does not give
models web search, code execution, filesystem access, function calls, or other
tools. Model text can recommend an action, but cannot execute it through this
application.

The application itself writes the audit database and explicit exports. An
external secret resolver can have its own effects and must be reviewed
separately.

### Constrained jury output

Jury calls include a fixed, non-sensitive JSON Schema so providers can
constrain the output before it reaches the local parser. No question, candidate
text, personal data, or secret is placed in the schema itself. The application
still validates the returned object and its candidate labels locally.
Provider refusals and output-limit truncation can bypass schema guarantees and
are treated as invalid judgments rather than trusted records.

### Diversity and blinded evaluation

The default live configuration uses five separately named lineages. Candidate
provider names are replaced by shuffled labels for jury prompts, and aggregate
input is anonymized for synthesis.

This is metadata blinding, not anonymity. Models may infer authorship from
style, content, capabilities, or shared training data. Providers are not
independent evidence sources, and correlated failure remains possible.

### Local storage permissions

The data directory is created or reset to mode `0700`; the main SQLite database
and exports are set to `0600`. A symbolic-link data directory is rejected.
SQLite WAL sidecars remain inside the protected directory.

These permissions do not protect against:

- The same operating-system user or a process running as that user.
- Root or administrator access.
- Malware, debugger attachment, memory inspection, or terminal capture.
- Unencrypted disks, snapshots, backups, swap, crash reports, or cloud sync.
- Files copied or exported to a less protected location.

Use full-disk encryption, a locked screen, current operating-system security
updates, and encrypted backups.

### Audit integrity metadata

The store records SHA-256 hashes for questions, prompts, responses, results,
events, and locked protocol material. It uses SQLite transactions, foreign
keys, WAL mode, and full synchronous writes.

Hashes stored beside the data are not signatures. An attacker able to rewrite
the database can rewrite both content and hashes. The database is not an
append-only, externally timestamped, or cryptographically attested ledger.

## Plaintext audit caveat

The database intentionally keeps enough raw material to explain a run:
questions, full generated prompts, provider responses, results, failures,
request metadata, and configured secret *names*. It is plaintext.

Do not submit:

- API keys, passwords, recovery codes, private keys, or session tokens.
- Material whose disclosure to all five providers is prohibited.
- Personal or regulated data that has not passed the relevant review.
- Unredacted production incidents when a minimum reproduction will do.

There is no automatic input/output redaction, field-level encryption, retention
timer, or secure erase. Models can echo sensitive input into multiple response
records and exports. Provider-side retention is outside this application's
control.

## Prompt injection and untrusted content

Questions and included source material are placed into model prompts. Treat all
external text as untrusted. The absence of tools limits direct side effects,
but prompt injection can still distort proposals, juries, or synthesis and can
attempt to induce disclosure of other prompt content.

Keep unrelated secrets out of the process and prompt. Separate instructions
from quoted evidence, minimize supplied context, and verify important claims
against primary sources outside the council. A confident consensus is not a
security control.

## Availability and cost

The application uses bounded retries for explicit retryable HTTP responses,
timeouts, quorum rules, an application-level call budget, process locks, and
resumable successful stages. Ambiguous transport outcomes are not
automatically retried. These controls improve recovery and reduce duplicate
billable calls but do not guarantee availability.

CLI `max_calls` and service call units count application-level logical provider
invocations, not HTTP attempts made inside provider retry loops.
`deadline_seconds` is cooperative and does not forcibly terminate an in-flight
request. There is no live pricing feed, currency-denominated budget, customer
billing, or service-side provider reconciliation. Configure provider-side
spend limits, alerts, and least-privilege projects for the separate live CLI;
do not enable live service operation.

## Recommended operator baseline

1. Run under a dedicated, non-administrator local user when practical.
2. Keep the repository, secret resolver, data directory, exports, and backups
   outside broadly synchronized folders.
3. Use separate, least-privilege provider projects and keys for the alpha.
4. Apply provider-side budget alerts and the minimum necessary permissions.
5. Use a reviewed external secret resolver rather than long-lived shell
   environment variables.
6. Run the test suite and deterministic mock workflow before each release.
7. Pin and review model identifiers; evaluate behavior before rotation.
8. Inspect failures and request IDs after every consequential run.
9. Export and back up only when needed, and protect those copies as strongly as
   the live database.
10. Never expose the mock service directly to untrusted users or the public
    internet.

## Credential exposure response

If a credential may have been exposed:

1. Stop new live runs.
2. Revoke or disable the credential in the provider account.
3. Create a replacement with least privilege and provider-side budget controls.
4. Remove the old value from process environments, shell startup files,
   terminal history, clipboard managers, scripts, CI logs, and operator notes.
5. Search the audit database, exports, backups, and repository history for the
   exposed value without printing it to shared output.
6. Review provider usage and request logs for unauthorized activity.
7. Update the secret resolver and complete the rotation procedure in
   `OPERATIONS.md`.
8. Preserve a restricted incident record containing identifiers and timing,
   not the credential itself.

Deleting a local database does not revoke a key or erase provider-side data.

## Audit-data exposure response

If the database or an export may have been disclosed:

1. Stop processes writing to the affected directory.
2. Preserve one encrypted, access-restricted forensic copy before cleanup.
3. Identify the affected runs, questions, providers, exports, backups, and
   synchronization targets.
4. Determine whether any prompt or response contained credentials or regulated
   data; rotate exposed credentials immediately.
5. Follow the notification and reporting rules applicable to the data.
6. Restore from a known-good backup into a new protected directory when
   integrity is uncertain.
7. Do not resume a run whose stored record cannot be trusted.

## Reporting a security issue

Use GitHub's **privately report a security vulnerability** feature for
`apcar/CouncilLogic`. Do not open a public issue containing secrets, sensitive
prompts, raw provider responses, exploit details, or identifying data.

Include a concise description, affected version, reproduction conditions,
impact, and any suggested mitigation. Do not include live credentials or data
that you are not authorized to disclose. The repository owner will acknowledge
the report through GitHub's private vulnerability-reporting conversation and
coordinate disclosure there.
