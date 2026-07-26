# CouncilLogic

<p align="center">
  <img src="assets/model-council-logo.png" alt="CouncilLogic logo" width="240">
</p>

<p align="center">
  <a href="https://github.com/apcar/councillogic/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/apcar/councillogic/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="License: AGPL-3.0-only" src="https://img.shields.io/badge/license-AGPL--3.0--only-blue">
  <img alt="Status: public alpha" src="https://img.shields.io/badge/status-public%20alpha-orange">
</p>

CouncilLogic is a local-first, auditable council of heterogeneous language
models. OpenAI, Anthropic, Gemini, and Mistral independently propose answers,
judge blinded candidates, contribute to a deterministic Borda aggregate, and
produce a final synthesis. The complete run record is stored locally.

This repository is a **public alpha (`0.2.0a1`)**, not a production service or
a truth oracle. Multiple models can share the same error. Verify consequential
claims against primary sources.

## What is—and is not—new

The council algorithm is not claimed as novel. Multi-model ensembles, model
juries, debate, voting, blinded labels, and synthesis predate this project; the
workflow is a governed descendant of the council pattern popularized by
[Karpathy's `llm-council`](https://github.com/karpathy/llm-council).

The contribution is the control plane around that pattern: attributable
principal identity, action-scoped mandates, owner-scoped access, bounded
logical-invocation budgets, durable audit records, restart recovery,
idempotency, token rotation and revocation, single-writer fencing, and explicit
operator authority.

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
establishes software behavior; it does not show that nine calls outperform a
strong single model. Establishing that would require matched-cost baselines,
protocol ablations, and evaluation across additional users and task domains.
See [Research](docs/RESEARCH.md).

## Four-stage council

1. **Propose:** OpenAI, Anthropic, Gemini, and Mistral answer independently.
2. **Judge:** each lineage ranks relabeled candidates without provider
   attribution; structured responses are validated locally.
3. **Aggregate:** deterministic Borda scoring combines valid juries.
4. **Synthesize:** the selected lineage receives the candidates, aggregate,
   and jury records and writes the final answer.

A normal run uses nine application-level calls: four proposals, four juries,
and one synthesis. Successful work is persisted and reused on resume. The
application does not give models tools, web access, code execution, or
model-initiated actions.

## Five-minute credential-free quickstart

Requires Python 3.11 or newer. Mock mode is deterministic, makes no provider
requests, and needs no credentials.

```bash
git clone https://github.com/apcar/councillogic.git
cd councillogic
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .

council --mock --data-dir ./work/demo doctor
council --mock --data-dir ./work/demo run \
  --question "What should I verify before relying on this council?" \
  --json
council --mock --data-dir ./work/demo list
```

Use the returned `run_id` to inspect or export the durable record:

```bash
council --mock --data-dir ./work/demo inspect RUN_ID --json
council --mock --data-dir ./work/demo export RUN_ID \
  --format markdown --output ./work/demo-run.md
```

## Live CLI setup

The live CLI sends the question and candidate text to all four configured
providers. Review provider contracts, data handling, model availability, and
cost controls before using sensitive material.

```bash
export OPENAI_API_KEY="..."
export ANTHROPIC_API_KEY="..."
export GEMINI_API_KEY="..."
export MISTRAL_API_KEY="..."

council doctor
council providers
council run --question "Your decision question"
```

Avoid putting credentials in shell history. For durable use, configure
`MODEL_COUNCIL_SECRET_COMMAND` with an absolute executable path. The executable
receives one logical secret name, must print only its value, and must fail
closed. `.env` loading is deliberately unsupported. See
[Operations](docs/OPERATIONS.md) and [Security](docs/SECURITY.md).

Default models and endpoints are recorded in
[`council.example.toml`](council.example.toml). Provider access and pricing
change over time; verify them with the official provider documentation before
a live run.

## Service warning

> [!WARNING]
> The `0.2.0a1` HTTP service is **mock-only and loopback-only**. It cannot
> construct live-provider adapters. Do not bind it to a non-loopback address,
> place it behind a tunnel or remote front door, enable either work principal,
> or use it for production or commercial traffic.

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
until a counsel-reviewed contributor license agreement is available. Please
read [CONTRIBUTING.md](CONTRIBUTING.md) and the
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
