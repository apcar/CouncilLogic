# Portable Council service

## Status

This document describes the implemented `0.2.0a1` portable-service alpha for
Model Council. It is a public reference deployment with deliberately narrow
boundaries:

- one authenticated service on one active POSIX host;
- a fixed catalog of six separately governed callers;
- loopback-only, deterministic mock execution;
- a thin remote client that never receives an upstream provider credential;
- durable Council runs in the existing SQLite store; and
- policy and budget controls that can be tested without spending money.

It is application code, not a deployed service. No tunnel or public endpoint
is authorized, no live provider key is accepted by this surface, and no work
principal can be enabled. The single-user live CLI remains a separate surface.

The service must not be switched from mock providers to OpenAI, Anthropic,
Google, or Mistral until the later release gates in this document pass.

### Implementation snapshot

The service alpha now implements:

- the four-lineage topology—OpenAI, Anthropic, Gemini, and Mistral in the
  separate live CLI—represented by four deterministic mock slots behind a
  loopback-only asynchronous HTTP server with bounded workers and queue;
- the fixed six-principal catalog, with both work principals forced disabled;
- per-principal bearer authentication with atomic verifier rotation and narrow
  operator revocation;
- durable action-scoped mandates for `run:create`, `run:read`, and
  `provider:invoke`, including provider/model allowlists and logical call-unit
  ceilings;
- owner-scoped run reads and idempotency bindings, including concurrent
  same-key collapse to one run;
- durable accepted-job recovery and replay repair when an idempotency binding
  precedes its Council run row;
- preservation of ambiguous/uncertain semantics for invocations found running
  after a crash, without blind retry;
- a lifetime, exclusive, nonblocking process lock for the service data
  directory, with the standby fenced;
- strict JSON, header, question, and request-body limits, bounded connection
  timeouts, and connection closing after POST errors;
- a thin remote client with private-token-file checks, non-loopback HTTPS
  enforcement, redirect refusal, and loopback proxy isolation; and
- a service-storage marker that bars local CLI run, resume, inspect, list, and
  export, while service startup rejects a store containing live-mode runs.

These controls are exercised by local regression tests. That does not mean a
deployment or later release gate has passed: the service remains mock-only,
and the operational soak and deployment gates remain separate evidence.

## What the product is

The Council is a hardened, governed descendant of the pattern popularized by
[Karpathy's `llm-council`](https://github.com/karpathy/llm-council):

1. different model lineages independently propose answers;
2. candidates are relabeled and judged without direct provider attribution;
3. the application computes a deterministic aggregate; and
4. a designated model synthesizes the final answer.

That is an ancestry statement, not an algorithm-novelty claim. Multi-model
ensembles, model juries, debate, voting, blinded labels, and synthesis all
predate this product. The ancestry must not be presented as a claim that the
underlying council algorithm is unique or that this repository copied Karpathy
source.

The governance wedge—the product differentiation—is the system around the
inference pattern:

- four genuinely different provider lineages, including Mistral;
- deterministic aggregation and a resumable, inspectable run record;
- separately authenticated human and agent callers;
- personal/work tenant separation;
- durable, action-scoped and revocable mandates with provider/model allowlists;
- durable logical-invocation reservation and bounded per-principal budgets;
- no distribution of upstream provider credentials to clients;
- read-own-run authorization and attributable receipts; and
- an explicit active/standby operating model that avoids two SQLite writers.

In short, the algorithm is the starting point. The governed, portable,
auditable control plane is the product work.

## Fixed six-principal reference topology

The alpha ships a fixed six-principal catalog to make authorization and tenant
boundaries reproducible. These identifiers describe a public reference
deployment; they are not an operator-specific inventory or a general topology
recommendation:

| Principal | Reference context | Tenant | Initial state |
| --- | --- | --- | --- |
| `mini-a-agent` | reference host A agent | personal | enabled for mock testing |
| `mini-b-agent` | reference host B agent | personal | enabled for mock testing |
| `personal-laptop-human` | interactive personal use | personal | enabled for mock testing |
| `personal-laptop-agent` | personal laptop agent | personal | enabled for mock testing |
| `work-laptop-human` | interactive work use | work | **disabled** |
| `work-laptop-agent` | work laptop agent | work | **disabled** |

The human and agent on one laptop are different principals. They must not
share a token. The personal and work contexts are different tenants.

Both work principals stay disabled until the employer approves the executable,
authentication method, network destination, provider accounts, permitted data
classes, retention policy, and billing owner. Successful connectivity from a
managed Mac is not authorization to send work data.

## Boundary and request flow

The reference-alpha topology is intentionally local:

```text
client process
  |
  | Bearer <one principal token>
  v
127.0.0.1 Council HTTP service
  |
  | authenticated principal + mandate
  v
policy check and budget reservation
  |
  v
durable Council run + deterministic mock providers
```

Only the service process can construct provider adapters. In this alpha those
adapters must be the four local deterministic mocks. A client holds one
revocable Council credential, not an OpenAI, Anthropic, Gemini, Mistral, or
Bitwarden credential.

A later personal-access gate may put an authenticated HTTPS front door in
front of the loopback origin. Loopback binding is not permission to expose the
origin directly, and a tunnel must never weaken application-level
authorization.

## API contract

The minimal portable contract is:

| Method and path | Authentication | Purpose |
| --- | --- | --- |
| `GET /healthz` | none | Liveness/readiness without secret or run data |
| `POST /v1/runs` | principal token | Create one owned run |
| `GET /v1/runs/{run_id}` | principal token | Read an owned run or poll its status |

`POST /v1/runs` accepts a JSON object containing a non-empty `question` and an
`Idempotency-Key` request header. It returns a run identifier and status. The
client may poll the returned identifier until the run reaches `completed`,
`partial`, `failed`, or `cancelled`.

`council-remote run` requires an explicit stable, non-secret idempotency key
before it transmits the request. It never invents an ephemeral key. When
`--wait` succeeds, the key remains in the final output. If creation has already
returned an accepted `run_id` and polling then fails or times out, the client
prints a JSON recovery record with `recovery: true`, that `run_id`, and the
explicit `idempotency_key`, then reports failure with a nonzero exit status.
The operator can safely retry creation with the same key or use the run
identifier with `status`/`wait`; the client does not submit another run
automatically.

The first increment does not promise list-all, event-stream, export,
cancellation, resume, mandate administration, or browser endpoints. Those
surfaces must not be inferred from the local CLI. Add them only with explicit
authorization rules and tests.

All responses are JSON objects. Errors use a stable machine-readable code, a
non-sensitive message, and a request identifier. Authentication failures,
unknown runs, and runs owned by another principal must not disclose another
tenant's data. The service must impose request-size and content-type limits
before parsing a body.

## Authentication

The reference alpha uses one high-entropy bearer token per enabled principal.
This is a
bootstrap mechanism for a loopback mock service, not the final Internet-facing
identity system.

The rules are:

- generate tokens from a cryptographically secure source;
- keep each client token in a regular, non-symlink file with mode `0600`;
- never place a token in a URL, question, idempotency key, command argument,
  repository, audit record, or log;
- store only a one-way verifier on the service side;
- retain rotated and revoked verifier hashes as durable per-principal
  tombstones, never plaintext tokens;
- compare verifiers without timing-sensitive string comparison;
- reject redirects in the remote client so a token cannot follow a redirect;
- require HTTPS for every non-loopback service URL;
- map exactly one token to one principal and tenant;
- make revocation independent for every principal; and
- never translate a Council token into a response containing an upstream
  credential.

Rotation is rollback-safe. Installing A, revoking A, rotating to B, and later
restarting with stale configuration that presents A causes startup to fail;
A remains retired and B remains current. Tombstone checks are global, so an
old verifier cannot be reassigned to another principal. Repeating the current
B configuration is idempotent, but every retired verifier is rejected by both
bootstrap and rotation.

Retirement has no ordinary bypass. The alpha provides no tombstone deletion or
same-token reissue command. Any future reissue capability must be a distinct,
authenticated, explicitly approved, audited operator boundary rather than a
bootstrap option or direct database edit. The safe current response is to
generate a new high-entropy token.

Interactive passkey or MFA authentication and unattended workload credentials
belong at the later authenticated front door. Human approval should be needed
for enrollment, mandate changes, unusually sensitive work, or materially
higher limits—not for every ordinary call under an already approved mandate.

A token file under the same macOS user is useful attribution but not strong
process isolation. Unattended agents should eventually run under dedicated
service identities where policy permits.

## Ownership and authorization

Authentication identifies the caller. A mandate then decides what that caller
may do. Each mandate binds at least:

- owner and subject principal;
- tenant;
- allowed actions;
- allowed provider and model identifiers;
- issue time, expiry, and revocation state; and
- per-run, daily, and monthly ceilings.

An agent cannot approve or enlarge its own mandate. A personal principal
cannot act in the work tenant, and a work principal cannot act in the personal
tenant.

Run ownership must be recorded before background execution begins. A caller
may read only its own run unless a future, separately tested human
administration rule says otherwise. A denied cross-principal read should have
the same external shape as an unknown run.

## Idempotency

Retries are expected: a client may lose a response after the server has already
accepted a run. The client therefore supplies a non-secret `Idempotency-Key`
for every create request.

The implemented rule is:

- the first accepted key is bound to the authenticated principal and canonical
  request hash;
- an exact repeat returns the same run identifier and does not start another
  Council;
- reuse with a different request fails with a conflict;
- a key cannot be used to claim or discover another principal's run; and
- the binding remains durable across a service restart.

The service records ownership and the idempotency binding before dispatch.
The binding is scoped to its principal, so the same textual key used by a
different principal neither aliases nor reveals the first run. If a crash
leaves a binding before its Council run row, an exact replay repairs the run
in place under the already assigned identifier.

Idempotency prevents duplicate Council creation; it does not by itself prove
that an ambiguous upstream provider request was unbilled. Ambiguous provider
outcomes remain explicitly uncertain and must not be retried blindly.

## Budgets

Two controls have different jobs:

- the existing `max_calls` policy bounds logical provider attempts within a
  Council run; and
- the service policy bounds a principal's logical invocation units across
  runs.

Before a mock provider invocation, the service atomically authorizes the
principal, mandate, `provider:invoke` action, provider, and model, then reserves
one logical call unit. A settled invocation settles its reservation. An
ambiguous running outcome keeps its durable reservation rather than being
silently retried or released.

Reservation identifiers are idempotent, and settled reservations cannot be
replayed as fresh authorization. Transactions prevent concurrent requests
from racing past daily or monthly ceilings.

A call unit is an application-level logical provider invocation. It is not a
count of upstream HTTP attempts, a currency amount, a provider charge, or a
customer-billing unit. The service has no real-provider metering,
reconciliation, or pricing. Live service and commercial use remain disabled
until those controls and provider-side hard limits exist and are reviewed.

## Active and standby

Mac Mini A is the sole active service and SQLite writer. Mac Mini B is a
fenced warm standby with the exact release, encrypted backups, reviewed
configuration, and—only after the secret-broker gate—its own separately
revocable broker identity.

The service holds an exclusive nonblocking process lock for its entire
lifetime. A second service process pointed at the same directory refuses to
start. This protects one host and one data directory; it is not distributed
consensus, so standby fencing remains an operator requirement.

Local CLI operations that touch a data directory acquire this same lock before
checking the persistent service marker and hold it through the operation.
Service startup refuses every preexisting run without a service ownership
binding, so a CLI that wins the lock first cannot have its store silently
converted when the service later starts.

Mini B must not accept traffic while Mini A is active. Promotion is deliberate:
fence Mini A, restore and verify durable state, review all non-terminal and
ambiguous calls, then move the client front door and start the service on Mini
B. The detailed rationale is in
[ADR 0001](adr/0001-single-active-sqlite.md).

Two tunnel connectors do not make two SQLite files one database. Active/active
is deferred until the system has shared transactional state, idempotent job
claims, and split-brain protection.

## Subscription posture

Gate 1 is subscription-neutral. It uses the existing Macs, Python standard
library, Git repository, SQLite, and deterministic mocks. It makes no provider
API calls and requires no new paid Cloudflare, Tailscale, Bitwarden, hosting,
database, queue, or observability plan.

That does not mean the eventual service is free. Later gates may incur:

- usage-based charges from the four model providers;
- a secret-manager tier or additional machine identity;
- an authenticated tunnel or identity plan;
- backup storage and monitoring; and
- a hosted database or regional service if active/active becomes necessary.

No subscription should be purchased merely to complete Gate 1. Choose and
cost later services only after their corresponding gate has evidence and a
clear owner.

## Release gates

Gates are cumulative. Passing a later gate never excuses a failed earlier one.

### Gate 0 — work authorization

- Record employer approval for executable, identity, destination, provider,
  data-egress, retention, and billing rules.
- Keep both work principals disabled until every required approval exists.
- Use synthetic data for all pre-approval work-device tests.

### Gate 1 — loopback mock service

- Full test suite passes from a clean checkout.
- The server refuses live provider configuration.
- The origin binds only to loopback and the client rejects non-loopback HTTP.
- Unauthenticated and invalid-token requests fail closed.
- All six principal records exist; both work records are disabled.
- Cross-principal and cross-tenant reads disclose no run.
- Exact idempotent retries return one run; conflicting reuse is rejected.
- Concurrency cannot race past an authorization or budget ceiling.
- Tokens and authorization headers are absent from logs, errors, SQLite, and
  exported artifacts.
- At least 100 mock runs complete without a policy bypass, unexplained
  duplicate, or inconsistent terminal record.

Passing Gate 1 authorizes local synthetic testing only.

### Gate 2 — personal authenticated front door

- Put an authenticated HTTPS layer in front of the loopback origin without
  opening a home-router port.
- Enroll `personal-laptop-human` first.
- Prove login, logout, expiry, revocation, origin isolation, and mock execution
  from a different personal device.
- Threat-model and test headers supplied by the front door; the origin must not
  trust caller-controlled identity headers.

Passing Gate 2 still does not authorize live model keys.

### Gate 3 — secret broker and bounded live canary

- Implement and independently review the Council-to-secret-broker adapter.
- Issue separate, least-privilege, expiring service identities.
- Rotate one provider credential at a time out of the legacy local resolver.
- Prove that no client, log, request, database, package, or Git object contains
  an upstream credential.
- Run a non-sensitive, tightly capped canary across OpenAI, Anthropic, Google,
  and Mistral and reconcile Council records to provider billing.
- Rehearse revocation and rollback before removing any legacy secret.

### Gate 4 — personal agents

- Enroll each personal agent separately with a narrow, expiring mandate.
- Prove read-own-run isolation and independent revocation.
- Start with low per-run and daily ceilings.
- Add device binding or a documented compensating control before unattended
  access leaves the local network.

### Gate 5 — standby and recovery

- Install the exact signed release on Mini B.
- Restore an encrypted backup and verify hashes, permissions, schema, and run
  ownership.
- Rehearse a fenced manual promotion during proposal, jury, and synthesis
  stages.
- Demonstrate no split-brain writes and no silent replay of an uncertain
  provider call.

### Gate 6 — work human, then work agent

- Require Gate 0 approval to still be current.
- Enroll `work-laptop-human` first and test with synthetic data.
- Prove tenant, provider, retention, and billing separation.
- Enroll `work-laptop-agent` under a narrower mandate.
- Permit real work data only after a separate data-egress review.

### Gate 7 — product operations

- Add real-provider metering and reconcile logical reservations to trustworthy
  provider usage before enabling live service.
- Add encrypted backup/restore, retention, deletion, rotation, alerting,
  monitoring, incident response, update rollback, and protected break glass.
- Complete load, restart, crash-consistency, dependency, and abuse testing.
- Soak at least 100 mock and 20 bounded live runs without policy bypass,
  unexplained spend, credential exposure, or audit inconsistency.
- Publish a support boundary, service-level objective, privacy terms, and
  billing semantics before charging another user per API call.

## Current non-goals

This increment does not:

- expose the service to the public Internet;
- import or distribute provider credentials;
- treat logical call-unit reservations as money or a billing ledger;
- enable work use;
- provide active/active availability;
- promise output truth or model independence;
- sell metered access; or
- replace provider-side account controls.

Those exclusions are release controls, not missing documentation.
