# CouncilLogic

<p align="center">
  <img src="docs/assets/councillogic-header.svg" alt="CouncilLogic — Independent answers. One governed record." width="760">
</p>

<p align="center">
  <a href="https://github.com/apcar/CouncilLogic/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/apcar/CouncilLogic/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="License: AGPL-3.0-only" src="https://img.shields.io/badge/license-AGPL--3.0--only-blue">
  <img alt="Status: public alpha" src="https://img.shields.io/badge/status-public%20alpha-orange">
</p>

**Send one question across models, blind their judgments, and keep the complete
record.**

CouncilLogic is a local-first, auditable orchestration and governance layer for
heterogeneous language-model deliberation. It sends a question to configured
providers independently, blinds and aggregates their judgments, synthesizes the
result, and stores the complete run record locally. It currently ships with
live adapters for OpenAI, Anthropic, Gemini, Mistral, xAI's Grok, Alibaba's
Qwen, and Cohere Command. An Upstage Solar adapter is available as an optional,
disabled bench provider.

## The operating thesis

CouncilLogic is a working expression of how I approach AI systems: separate
judgment from authority, make disagreement visible, bound work and failure
before execution, and preserve a record another operator can inspect.

| Principle | Implemented evidence |
|---|---|
| Separate judgment from authority | Distinct provider/model lineages answer independently; models receive no tools or permission to act. |
| Define limits before execution | Prompt growth, logical calls, concurrency, deadlines, output, and recovery are bounded by policy. |
| Preserve uncertainty | Metadata-blind juries, deterministic aggregation, abstentions, ties, disagreement, and degraded completion remain visible. |
| Make operation inspectable | Proposals, juries, failures, recoveries, policy, aggregate, and synthesis are durably recorded and exportable. |

The fastest review path is to run the
[credential-free proof](#five-minute-credential-free-proof), then inspect the
[security model](docs/SECURITY.md), [operations runbook](docs/OPERATIONS.md),
and [research boundaries](docs/RESEARCH.md).

This repository is a **public alpha (`0.3.0a1`)**, not a production service or
a truth oracle. Multiple models can share the same error. Verify consequential
claims against primary sources.

## What is—and is not—new

The council algorithm is not claimed as novel. Multi-model ensembles, model
juries, debate, voting, blinded labels, and synthesis predate this project; the
workflow is a governed descendant of the council pattern popularized by
[Karpathy's `llm-council`](https://github.com/karpathy/llm-council).

The contribution is the control discipline around that pattern, implemented on
two deliberately separate surfaces. The live CLI provides bounded
heterogeneous runs, durable audit records, restart recovery, and explicit
failure semantics. The frozen mock-only service separately exercises principal
identity, action-scoped mandates, owner-scoped access, idempotency, token
rotation and revocation, logical-invocation reservations, and single-writer
fencing.

### Why I built it

This began with a practical limitation in my own work. Giving one model several
roles could broaden a response, but it did not create genuinely independent
judgment: each role still inherited the same model lineage, training influences,
policies, and blind spots. I wanted distinct providers to answer independently,
evaluate blinded alternatives, preserve disagreement, and leave an inspectable
record.

That is a single-operator origin, not a validation result. Published research
makes heterogeneous model juries and aggregation worth testing, but also
documents correlated errors and inconsistent gains. The current test suite
establishes software behavior; it does not show that fifteen calls outperform a
strong single model. Establishing that would require matched-cost baselines,
protocol ablations, and evaluation across additional users and task domains.
See [Research](docs/RESEARCH.md).

## How CouncilLogic works

1. **Propose:** every configured lineage emits an independently reasoned,
   size-bounded JSON artifact.
2. **Judge:** each participating lineage ranks relabeled candidates without
   provider attribution; structured responses are validated locally.
3. **Aggregate:** deterministic Borda scoring combines valid juries.
4. **Synthesize:** the selected lineage receives bounded candidates, the
   aggregate, and compact vote records and writes the final answer.

```mermaid
flowchart LR
    Q["Question"] --> G{"Policy preflight"}
    G --> P["Independent proposals"]
    P --> J["Metadata-blind juries"]
    J --> A["Deterministic aggregate"]
    A --> S["Bounded synthesis"]
    P --> R[("Durable local record")]
    J --> R
    A --> R
    S --> R
    R --> O["Inspect · resume · export"]
```

A default live run uses fifteen application-level calls: seven proposals,
seven juries, and one synthesis. The frozen `0.2.0a1` mock-service profile
remains a four-lineage, nine-call fixture. Successful work is persisted and
reused on resume. The application does not give models tools, web access, code
execution, or model-initiated actions.

## Five-minute credential-free proof

Requires Python 3.11 or newer. Mock mode uses deterministic local provider
responses, makes no provider requests, and needs no credentials. It proves the
governed execution and audit path; it does not produce substantive model
advice.

```bash
git clone https://github.com/apcar/CouncilLogic.git
cd CouncilLogic
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .

council --version
council --mock --data-dir ./work/demo doctor
council --mock --data-dir ./work/demo run \
  --question "What controls must be in place before an AI agent receives write access to a consequential business workflow?" \
  --json
council --mock --data-dir ./work/demo list
```

The run produces four proposals, four separately ordered juries, one
deterministic aggregation step, and one synthesis. The durable result records a
clean nine-call execution, zero failed providers, and every verification
limitation. Run-scoped presentation order means scores, winner or tie, and
consensus classification can vary; each outcome is preserved exactly. See the
[annotated proof](examples/mock-governance-proof/README.md).

Use the returned `run_id` to inspect or export the durable record:

```bash
council --mock --data-dir ./work/demo inspect RUN_ID --json
council --mock --data-dir ./work/demo export RUN_ID \
  --format markdown --output ./work/demo-run.md
```

## Workload reliability

- Before any provider call, deterministic preflight checks the question and
  projected downstream prompt graph against locked policy bounds.
- Proposal, jury, and synthesis calls have separate output and timeout limits;
  shared call, concurrency, deadline, and recovery budgets cap the whole run.
- A known output-length truncation may receive one bounded retry. Ambiguous
  timeouts or connection loss are preserved and never retried automatically.
- Jury prose repair cannot change decision fields, consume the synthesis
  reserve, or trigger another repair. Any accepted repair makes the run
  `degraded`, not silently successful.
- Every terminal result exposes membership, failures, recoveries, projected and
  actual workload, call count, and `clean` or `degraded` completion quality.

The exact recovery precedence and operator gates are in
[Operations](docs/OPERATIONS.md); the availability, privacy, and cost
boundaries are in [Security](docs/SECURITY.md).

## Live CLI setup

The default live CLI sends the question and candidate text to all seven
configured providers. Review provider contracts, data handling, model
availability, and cost controls before using sensitive material.

```bash
export OPENAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export GEMINI_API_KEY="..."
export MISTRAL_API_KEY="..."
export XAI_API_KEY="..."
export DASHSCOPE_API_KEY="..."
export COHERE_API_KEY="..."

council doctor
council providers
council run --question "Your decision question"
```

The default Qwen destination is Alibaba Model Studio's
[Singapore/International endpoint](https://www.alibabacloud.com/help/en/model-studio/regions/).
`DASHSCOPE_API_KEY` must be issued for that region; Alibaba regional keys are
not interchangeable.

Upstage is registered but disabled in `council.example.toml`. Enabling it adds
an eighth participant, requires `UPSTAGE_API_KEY`, and should follow a direct
live proposal/jury canary because its structured-output compatibility has not
yet been verified in this project.

Avoid putting credentials in shell history. For durable use, configure
`MODEL_COUNCIL_SECRET_COMMAND` with an absolute executable path. The executable
receives one logical secret name, must print only its value, and must fail
closed. `.env` loading is deliberately unsupported. See
[Operations](docs/OPERATIONS.md) and [Security](docs/SECURITY.md).

Default models and endpoints are recorded in
[`council.example.toml`](council.example.toml). Provider access and pricing
change over time; verify them with the official provider documentation before
a live run.

## Public project boundary

This repository publishes the local council engine and protocol, mock execution
and audit format, provider adapters, workload and failure controls, tests,
packaging, and public documentation.

Production deployment automation, live operational configuration and secret
brokerage, private evaluation data and results, routing economics, and any
hosted or enterprise control plane are outside this public repository. That is
a publication boundary, not a roadmap or availability claim.

## Experimental service boundary

> [!WARNING]
> The `0.3.0a1` package retains the frozen `0.2.0a1` HTTP service profile.
> It is **mock-only and loopback-only** and cannot construct live-provider
> adapters. Do not bind it to a non-loopback address, place it behind a tunnel
> or remote front door, enable either work principal, or use it for production
> or commercial traffic.

The fixed six-principal catalog is a public reference deployment for testing
governance boundaries. It is not a claim that those principal names or that
topology fit another operator. The two work principals are forced disabled.
See [Portable service](docs/PORTABLE-SERVICE.md).

## Data and security

Runs are stored in a plaintext SQLite database. Questions, prompts, model
responses, jury records, errors, and provider metadata may all be retained.
Credentials are not intentionally persisted or printed, but secrets included
in input or model output become audit content. The default directory is
`~/.local/share/model-council/`; use `--data-dir` to choose another location.

Read the [security model](docs/SECURITY.md) before using non-public data.
Privately report vulnerabilities using GitHub's private vulnerability
reporting for this repository.

## Documentation

- [Operations runbook](docs/OPERATIONS.md)
- [Security model and vulnerability reporting](docs/SECURITY.md)
- [Portable mock service and fixed reference topology](docs/PORTABLE-SERVICE.md)
- [Research grounding and claim boundaries](docs/RESEARCH.md)
- [Licensing strategy](LICENSING.md)
- [Architecture decisions](docs/adr/)
- [Support](SUPPORT.md)
- [Changelog](CHANGELOG.md)

## Research grounding

The design draws on published work about multi-agent debate, voting,
heterogeneous model juries, position bias, and adversarial persuasion. This
grounding motivates diversity, blinded ordering, deterministic aggregation,
and conservative claim boundaries; it does not establish that a council is
correct or independent evidence. See [Research](docs/RESEARCH.md) for sources
and design implications.

## Contributing

Bug reports and design proposals are welcome through the repository's issue
forms. External pull requests and other copyrightable contributions are closed
until a suitable contributor agreement is adopted. Please read
[CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md) before participating.

## License

The code is licensed under the
[GNU Affero General Public License v3 only](LICENSE), SPDX
`AGPL-3.0-only`. In brief, the AGPL permits use, study, modification,
distribution, self-hosting, and commercial use subject to its terms. Modified
versions offered for remote network interaction must offer Corresponding
Source to those users as required by section 13. This summary is not legal
advice; the license text controls. Trademark treatment for the CouncilLogic
name and mark is separate from the code license.
