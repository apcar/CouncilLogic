# Operations

This runbook covers the single-user live CLI and the public `0.2.0a1`
mock-only loopback service alpha. The service has a small governed
multi-principal control plane, but it is a fixed public reference deployment
and must not use live
providers, Cloudflare or another remote front door, work identities, or
commercial traffic. Do not infer current provider health from tests or prior
canaries; repeat the low-risk live gate under the credentials and configuration
intended for separate CLI use.

The service is a hardened/governed descendant of the Karpathy council pattern,
not a new council algorithm. Its operational distinction is the governance
layer: principal identity, action mandates, ownership, durable idempotency and
logical-invocation reservations, fail-closed recovery, and single-writer
fencing.

## Service-alpha boundary

The service retains the four deterministic mock lineages from its frozen
`0.2.0a1` reference topology. It does not mirror the separate five-provider
live CLI. It has six fixed principals; both work identities remain disabled.
Enabled principals have durable mandates for `run:create`, `run:read`, and
`provider:invoke`. Run reads and idempotency are owner-scoped.

Prepare a separate service data directory and one private `0600` regular token
file per enabled principal, then supply mappings explicitly:

```bash
council-service \
  --data-dir ./work/service-alpha \
  --token-file mini-a-agent=./private/mini-a-agent.token \
  --token-file personal-laptop-human=./private/personal-human.token
```

The service accepts loopback host values only and prints its local listening
address. A second process pointed at the same directory refuses to start
because `CouncilApplication` holds an exclusive nonblocking lock for its
lifetime. Do not try to defeat that lock; a standby stays stopped and fenced
until deliberate promotion.

Use the thin client rather than the local CLI:

```bash
council-remote \
  --server http://127.0.0.1:8765 \
  --token-file ./private/personal-human.token \
  run --question "Synthetic service-alpha check" \
  --idempotency-key "service-alpha-check-001" \
  --wait
```

The client holds only a Council bearer token. It refuses redirects, requires
HTTPS for non-loopback URLs, and ignores `HTTP_PROXY`/`HTTPS_PROXY` for
loopback requests. `council-remote run` requires the caller to choose a stable,
non-secret idempotency key before transmission; `--wait` preserves that key in
the final output, so an accepted run can be recovered after a lost response.
Local CLI commands that touch storage acquire the same directory lock as the
service before checking its persistent marker and refuse service-managed
storage. Service startup rejects live-mode runs and every preexisting run that
lacks a service ownership binding.

Rotate one principal by stopping the service and restarting it with a new
private token file for that principal. The verifier replacement is atomic and
invalidates the old token. The retired verifier hash remains as a durable
tombstone; a later restart with the old file fails closed and leaves the
current replacement unchanged. To revoke without installing a replacement:

```bash
council-service \
  --data-dir ./work/service-alpha \
  --revoke-token personal-laptop-human
```

Do not combine `--revoke-token` with `--token-file`. Revocation survives
restart. A revoked or rotated token can never be reused through bootstrap or
ordinary rotation, even for another principal. Use a newly generated token
for every replacement and verify the intended principal before restart.

There is no tombstone-deletion or same-token reissue command in this alpha.
Do not edit SQLite to bypass retirement. Any future reissue facility must be a
separate authenticated and audited operator action, not a configuration
rollback. Preserve the failed startup record, restore the last known-good
current token file if appropriate, and investigate why stale configuration was
presented.

Accepted nonterminal jobs recover on service restart. Exact replay repairs the
narrow crash case where an idempotency binding exists before its Council run
row. A provider invocation found running is ambiguous and is not blindly
retried; preserve its logical call-unit reservation and inspect the durable
record before any later operational reconciliation.

Service call units are application-level logical provider invocations. They
are not upstream HTTP-attempt counts, money, provider charges, or customer
billing. Live service remains disabled until trustworthy provider metering,
usage reconciliation, pricing, and provider-side limits exist.

## Gate-1 mock soak verifier

Run the reusable verifier with its safe default ephemeral data directory:

```bash
PYTHONPATH=src python3 scripts/gate1_mock_soak.py
```

It submits 100 deterministic mock jobs across the four enabled personal
principals with bounded concurrency and queue-full backoff. It requires every
job to complete, verifies ownership and cross-principal denial, checks exactly
nine reconciled logical invocation units per run, rejects orphan bindings or
unresolved reservations, runs SQLite integrity and foreign-key checks, verifies
representative runs through an online SQLite backup, and confirms that the
active service lock fences a second writer.

To retain the resulting store for inspection, supply a new or empty dedicated
directory:

```bash
PYTHONPATH=src python3 scripts/gate1_mock_soak.py \
  --data-dir ./work/gate1-soak
```

The verifier refuses a symlink, file, or nonempty explicit directory. It uses
`CouncilApplication` directly, constructs only deterministic mock providers,
and never resolves provider credentials or starts an HTTP service. A passing
result is Gate-1 mock/SQLite evidence only: logical call units are not provider
billing or money, and the verifier does not test live providers, a public
front door, or active-active failover.

## Release gate

Do not start with real sensitive material. From a clean checkout or release
artifact:

```bash
python3 --version
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Python must be 3.11 or newer. Then use a fresh, explicitly named data directory:

```bash
council --mock --data-dir ./work/release-gate doctor
council --mock --data-dir ./work/release-gate run \
  --question "Confirm the release-gate workflow." \
  --json
council --mock --data-dir ./work/release-gate list
```

Before live use:

1. Review `council.example.toml` and select the intended data directory.
2. Configure all five credentials through the environment or a tested external
   secret command.
3. Run `council --config ./council.toml doctor`.
4. Run `council --config ./council.toml providers` and verify every provider,
   model, and lineage.
5. Run one low-risk, non-sensitive live question.
6. Inspect and export that run; verify the result, failure record, permissions,
   latency, reported usage, and provider billing consoles.

`doctor` is a local preflight. It does not contact provider APIs or validate
model entitlement. If an external secret command is configured, `doctor`
invokes it.

## Command placement

Global options precede the subcommand:

```bash
council --config ./council.toml --data-dir ./private-data doctor
```

This form is invalid:

```text
council doctor --data-dir ./private-data
```

Use the same global options for `run`, `resume`, `inspect`, `list`, and
`export`. Otherwise a command can silently point at a different configured
database and report that a run is unknown.

## Starting a run

Question text can be passed directly:

```bash
council --config ./council.toml run \
  --question "What decision should be made, and what must be verified?"
```

For substantial input, prefer a UTF-8 file to avoid shell quoting and history:

```bash
council --config ./council.toml run \
  --file ./work/question.txt \
  --json
```

An idempotency key makes a repeated submission of the same locked request reuse
the existing run:

```bash
council --config ./council.toml run \
  --file ./work/question.txt \
  --idempotency-key "decision-2026-07-24-a"
```

Treat each idempotency key as permanent within a database. Reusing it with a
different locked request fails. Use a key that contains no secret or personal
data.

The default five-provider live path has eleven application-level provider
calls: five proposals, five juries, and one synthesis. Proposal and jury stages
run in parallel. Successful stage/provider slots are reused on resume. The
mock-only service remains a four-lineage, nine-call fixture.

## Policy controls

The defaults are:

```text
proposal_quorum = 3
jury_quorum = 3
min_lineages = 3
max_calls = 12
deadline_seconds = 420
allow_partial = true
```

Most fields can be overridden for one `run`:

```bash
council --config ./council.toml run \
  --question "Your question" \
  --proposal-quorum 4 \
  --jury-quorum 4 \
  --min-lineages 4 \
  --max-calls 12 \
  --deadline-seconds 600
```

The application rejects an impossible quorum, insufficient lineage diversity,
a synthesis provider outside the selected providers, or a logical call budget
smaller than `2 × selected providers + 1`.

`max_calls` counts application-level provider-call attempts. Retrying a failed
slot on resume consumes another count, although its audit history stays in the
same logical stage/provider record. Each adapter may also make up to
`max_attempts` lower-level HTTP attempts inside one application-level call.
The setting is therefore neither an HTTP-request limit nor a monetary cost cap.
Use provider-side budgets and alerts.

`deadline_seconds` is cooperative. It prevents later work from starting once
the deadline is observed, but it is not a process watchdog. An in-flight call
can continue through its configured request timeout and retries. For a harder
bound, use an external process supervisor and understand that termination may
leave a resumable run.

## Status and exit codes

Run statuses are:

- `completed`: final synthesis exists.
- `partial`: useful audit material exists, but quorum or synthesis did not
  fully complete.
- `failed`: the run could not produce the minimum allowed material.
- `running` or `created`: the process was interrupted before terminal state.

CLI exit codes are:

- `0`: command succeeded; for `run` and `resume`, the status is `completed`.
- `2`: argument, configuration, credential, storage, lookup, or other local
  operational error. `doctor` also returns `2` when not ready.
- `3`: `run` or `resume` returned `partial` or `failed`.

Do not treat a printed answer alone as success. Check both the process exit
code and the persisted run status.

## Inspect, list, and export

```bash
council --config ./council.toml list --limit 20
council --config ./council.toml inspect RUN_ID
council --config ./council.toml inspect RUN_ID --json
council --config ./council.toml export RUN_ID \
  --format markdown \
  --output ./private-exports/RUN_ID.md
council --config ./council.toml export RUN_ID \
  --format json \
  --output ./private-exports/RUN_ID.json
```

JSON inspection and export include the question, stored run configuration,
result, prompts, raw response text, provider metadata, and errors. Treat exports
as sensitive. The application sets an export file to mode `0600` but does not
encrypt it.

## Resume and interrupted runs

Resume with the same data directory:

```bash
council --config ./council.toml resume RUN_ID --json
```

Resume reconstructs the original provider, model, endpoint, policy, and
synthesis-provider lock from the persisted run. The supplied config is used to
locate the database, not to replace those locked choices. It reuses completed
invocations and can retry absent or non-ambiguous failed logical slots. A call
left `running` by a crash, or a failed call whose outcome may have reached the
provider, is marked ambiguous and is **not** automatically retried; this avoids
silently duplicating a potentially billable request. Start a new run only
after reviewing the provider logs and deciding that another call is
appropriate.

Resume refuses a run when the protocol hash or the
provider/model/lineage/endpoint lock differs from the current application.
Keep the original release and configuration until important partial runs are
finished or exported.

The alpha does not include a run-lock migration command. Do not edit the
SQLite database to force compatibility. Resume still resolves the stored
logical secret names through the current environment or secret command, so
credential rotation does not alter the run lock.

If a completed run is resumed, its stored result is returned without additional
provider calls.

## Common incidents

### Credential missing

Run `doctor`. Confirm that the exact logical names are available:
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, and
`MISTRAL_API_KEY`, and `XAI_API_KEY`, unless the TOML uses different
`secret_name` values. For an external resolver, invoke it manually with a
logical name and check only the exit status; do not paste or record its output.

### Authentication or permission failure

Inspect the run's structured failure category and provider request ID. Confirm
the credential belongs to the intended project and that the configured model
is enabled. Rotate a suspected credential before retrying.

### Model not found or invalid request

Compare `council providers` with the provider's current official model catalog.
Change the model in a reviewed TOML file. A partial run locked to the old model
cannot be resumed under the new provider lock; retain the old config if it must
be completed.

### Rate limiting or provider server failure

The adapters perform bounded retries for explicit retryable HTTP responses such
as `429` and selected `5xx` responses. Under the default quorum, a run may still
complete with three healthy lineages when one provider is unavailable. Inspect
failures and provider request IDs, wait for provider recovery, then resume
using the same config.

### Timeout or deadline exhaustion

Inspect per-invocation latency and attempts. A timeout or connection loss after
transmission may have begun is ambiguous and is not automatically retried.
Check provider request and billing logs before starting a replacement run.
Distinguish an adapter request timeout from the cooperative run deadline.
Increase timeouts or deadlines only after checking cost and operational impact.

### Invalid jury output

The raw response remains in the audit record, but a jury that fails structured
validation is excluded from the aggregate. Resume does not replace a
successfully transported but structurally invalid response in this beta.
Export the record and start a new run if another jury attempt is required.

### Synthesis did not complete

The run should be `partial` with proposals, juries, and aggregate preserved.
After the cause is corrected, resume using the exact provider lock.

### Unknown run

Confirm that `--config`, `--data-dir`, `MODEL_COUNCIL_DATA_DIR`, and `--mock`
match the original invocation. Mock and live commands use whichever data
directory they are given; a run ID has meaning only inside its database.

## Storage

By default:

```text
~/.local/share/model-council/council.sqlite3
```

SQLite runs in WAL mode and may create `council.sqlite3-wal` and
`council.sqlite3-shm` beside the main database. The data directory is forced to
mode `0700`; the main database is forced to `0600`. The directory is rejected
if it is a symbolic link. Per-run process locks are stored under `locks/` and
prevent two processes from actively executing the same run at once.

A service directory also contains a service-management marker, durable
principal/mandate/ownership/idempotency/job/reservation state, and a lifetime
service lock. Do not point the local CLI at it or remove the marker to bypass
the guard. The service database path itself may not be a symlink. Service
standby promotion requires fencing the active writer, restoring and verifying
SQLite state, and reviewing every nonterminal or ambiguous invocation.

The database contains plaintext questions, prompts, responses, provider
metadata, results, and errors. It contains secret names, but the storage layer
rejects recognized raw credential fields and credential-bearing endpoint URLs.
This is a guardrail, not automatic secret detection for prompt or response
text.

## Backup

The safest beta procedure is a quiescent whole-directory backup:

1. Wait for every `council run` or `council resume` process using the data
   directory to exit.
2. Copy the entire data directory, including any `-wal` and `-shm` sidecars, to
   an encrypted local backup destination.
3. Preserve restrictive permissions.
4. Open the copied database with this same application version and run
   `council --data-dir BACKUP_DIR list`.
5. Record the application version and SHA-256 digest of the copied primary
   database separately from the backup.

Do not copy only the primary database while writes are active. Do not sync the
live data directory through an unreviewed cloud or collaboration service.

For higher-assurance backups, use SQLite's online backup API or a database-aware
backup tool and test restoration. That tooling is not bundled in this beta.

## Restore

1. Stop all processes using the target data directory.
2. Preserve the failed directory under a new, access-restricted name; do not
   overwrite the only forensic copy.
3. Restore the complete backup to a new directory.
4. Set the directory to `0700` and database to `0600`.
5. Run `list`, `inspect` on representative runs, and a mock run against a
   separate scratch directory.
6. Resume an important partial run only with its original application,
   provider lock, policy, and synthesis choice.

If the schema is newer than the installed application understands, install the
matching or newer reviewed release. Never hand-edit `PRAGMA user_version`.

## Credential rotation

Keys are resolved when the process builds its providers and are not
intentionally written to the database.

1. Create the replacement key in the provider account with the minimum needed
   permissions and appropriate provider-side budget controls.
2. Update the process environment or external secret resolver under the same
   logical secret name.
3. Start a new shell/process and run `doctor`.
4. Perform a low-risk live smoke run and confirm all five providers in the
   audit record and provider consoles. This incurs live usage.
5. Revoke the old key.
6. Clear old shell environment values, terminal scrollback, and any temporary
   operator notes containing the value.
7. If exposure is suspected, follow the incident procedure in `SECURITY.md`.

`doctor` verifies presence only. Do not revoke the old key solely because
`doctor` says `READY`; prove the replacement with a live smoke run first.

## Model and protocol rotation

Model changes create a new provider lock. Before changing a pinned model:

1. List and export important partial runs.
2. Finish them under the old configuration or retain an isolated copy of the
   old release and config for recovery.
3. Review the provider's migration notes and pricing.
4. Change the model in version-controlled configuration.
5. Run tests, mock mode, `doctor`, `providers`, and a live smoke run.
6. Compare output quality and structured-jury validity on a fixed evaluation
   set before routine use.

Protocol code changes alter the immutable protocol hash and intentionally block
old-run resume. Treat them as release changes, not live edits.

## Retention and deletion

There is no per-run deletion command. Do not delete rows manually because runs,
invocations, events, hashes, and foreign keys form one audit record.

For retention, export any records that must be kept, verify the export, stop all
processes, back up if required, and remove or archive the entire data directory
through an approved recoverable process. Record whether the material also
exists in backups or exported files. Plain deletion of the live database does
not erase provider-side logs or backups.
