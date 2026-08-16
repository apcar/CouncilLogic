# Credential-free governance-path proof

This example demonstrates CouncilLogic's execution, control, and audit path
without provider credentials or network requests. It is deliberately not a
claim about the quality of mock-model advice.

## Decision question

> What controls must be in place before an AI agent receives write access to a
> consequential business workflow?

Run it from the repository root:

```bash
python -m pip install -e .
council --mock --data-dir ./work/governance-proof run \
  --question "What controls must be in place before an AI agent receives write access to a consequential business workflow?" \
  --json
```

## What every run records

| Field | Recorded value |
|---|---|
| Protocol | `independent-jury@1.2.1-beta` |
| Completion | `completed`, quality `clean` |
| Membership | 4 requested, 4 proposals, 4 valid juries |
| Aggregate | Borda scores, ranking, winner or tie, consensus, and disagreement |
| Application calls | 9 |
| Provider-stage failures | 0 |
| Recoveries and warnings | 0 |
| Credential or network use | None |

The four mock providers create bounded proposal artifacts. Each juror receives
the same candidate namespace in a separately randomized presentation order.
The local engine validates the juries, calculates the Borda aggregate, reserves
synthesis, and stores the result with workload telemetry.

Presentation order is randomized by design, so scores, winner or tie, and
consensus classification can vary between runs. The inspectable invariants are
the protocol, policy, membership, bounded call graph, completion quality,
failure record, and durable audit path.

## Inspect the proof

Use the `run_id` returned by the command:

```bash
council --mock --data-dir ./work/governance-proof inspect RUN_ID --json
council --mock --data-dir ./work/governance-proof export RUN_ID \
  --format markdown --output ./work/governance-proof.md
```

The export includes the question hash, protocol hash, proposals, blinded jury
records, aggregate, synthesis, limitations, failures, recoveries, and projected
versus actual workload. Replace mock providers with configured external
providers only when substantive model judgment is intended.
