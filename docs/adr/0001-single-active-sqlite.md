# ADR 0001: Single active SQLite service with a fenced standby

- Status: Accepted for the portable-service pilot
- Date: 2026-07-25
- Scope: CouncilLogic service state and reference-host failover

## Context

CouncilLogic already uses a local SQLite database for durable runs,
invocations, events, hashes, and results. SQLite WAL mode, transactions, and
the per-run process lock provide a strong single-host foundation. They do not
provide distributed consensus between two independent files on two Macs.

The intended deployment has Mac Mini A and Mac Mini B. Running both as writers
would create two authorities for run ownership, idempotency, budget
reservation, and provider-call state. A network partition or stale replica
could then:

- accept the same logical request twice;
- spend beyond a principal's ceiling;
- repeat a provider call with an ambiguous outcome;
- return different results for one idempotency key; or
- expose incomplete or incorrectly owned run data.

Multiple tunnel connectors improve network reachability but do not solve those
state-consistency problems. The two Minis are also likely to share home power
and Internet, so they are not geographic disaster recovery.

## Decision

Use active/passive service operation for the pilot:

1. Mac Mini A is the only active Council service and the only SQLite writer.
2. The service origin binds to loopback. A later authenticated front door
   routes ordinary client traffic only to the active Mini.
3. Mac Mini B remains fenced: its Council service is stopped and it has no
   active client route.
4. Mini B holds the exact verified release, reviewed configuration, and
   encrypted recoverable backups. When live secrets are eventually enabled,
   it receives its own revocable broker identity rather than Mini A's token.
5. Backups are SQLite-consistent snapshots, not copied live database and WAL
   files assembled independently.
6. Promotion is a deliberate operator action. Confirm or force Mini A
   inactive, remove its client route, restore and verify state on Mini B,
   inspect non-terminal calls, then activate Mini B.
7. A provider call with an ambiguous outcome is not automatically repeated
   during recovery. Its budget remains reserved and its state remains
   `uncertain` until reconciled.
8. Failback follows the same fencing rule. There is never more than one
   accepting writer.

The alpha persists mandates, run ownership, idempotency bindings, logical
call-unit reservations, and accepted job state in the active host's SQLite
authority. `CouncilApplication` also holds an exclusive nonblocking process
lock for its lifetime, so a second local service writer refuses to start.
The lock is host-local rather than distributed consensus: Mini B must still be
fenced before promotion.

These logical call units are application-level invocations, not upstream HTTP
attempts, money, provider usage reconciliation, or customer billing. Live
failover remains gated on trustworthy provider metering and reconciliation.

## Promotion checklist

Before Mini B accepts a request:

1. Stop or positively fence Mini A.
2. Disable Mini A's client route and confirm it cannot receive traffic.
3. Select the latest verified encrypted backup.
4. Restore into a protected directory and verify file permissions, schema,
   integrity, release version, protocol hash, and configuration lock.
5. Review every `created` or `running` run and every provider call whose
   transport outcome is ambiguous.
6. Reconcile or preserve budget reservations; do not convert uncertainty into
   an automatic retry.
7. Start Mini B on loopback and pass local health, authentication, isolation,
   idempotency, and mock-run checks.
8. Move the authenticated client route to Mini B.
9. Record the promotion time, backup point, outstanding uncertainties, and
   operator.

If Mini A cannot be positively fenced, Mini B must remain read-only or stopped.

## Consequences

Benefits:

- one authoritative SQLite transaction boundary;
- enforced refusal of a second local service process using the same data
  directory;
- understandable idempotency and budget behavior;
- no distributed lock or consensus dependency in the first product increment;
- simple encrypted backup and restore; and
- a recoverable path for host failure without pretending to be highly
  available.

Costs:

- failover is manual and has a nonzero recovery time;
- writes stop while the active host is unavailable or promotion is underway;
- recovery-point loss is bounded by the most recent verified backup;
- operator discipline is part of the safety mechanism; and
- Mini B protects against a host failure, not shared power, network, theft, or
  site loss.

## Alternatives considered

### Active/active SQLite on both Minis

Rejected. Independent SQLite files cannot atomically coordinate ownership,
idempotency, budgets, and provider-call claims. File synchronization or two
tunnel replicas would create split-brain risk rather than availability.

### Automatic active/passive promotion

Deferred. Safe automation needs a reliable fencing mechanism, replicated
transactional state, health evidence that distinguishes a failed host from a
partition, and tested handling of ambiguous billable calls.

### Hosted transactional database and queue

Deferred. A shared database and job queue could support later active/active or
regional operation, but they add subscriptions, operational dependencies,
privacy boundaries, and migration work before the loopback mock service has
proved its authorization model.

### No standby

Rejected as the long-term operating model because a second Mini is available
and can materially shorten recovery. It remains the actual Gate 1 state until
backup, restore, and promotion rehearsals pass.

## Revisit criteria

Revisit this decision only when all of the following exist:

- shared durable transactional state for runs, ownership, idempotency,
  mandates, reservations, reconciliations, and job claims;
- lease or consensus-based single-claim execution;
- explicit fencing and split-brain tests;
- provider-call receipts and reconciliation for ambiguous outcomes;
- encrypted artifact storage and tested backup/restore;
- a measured availability requirement that justifies the added complexity and
  subscription cost; and
- a migration and rollback plan that preserves the existing audit record.

Until then, one active writer is a product safety requirement.
