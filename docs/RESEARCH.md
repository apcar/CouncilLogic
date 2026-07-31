# Research basis

This public alpha treats a model council as an inference-time method to
evaluate, not as a guarantee that consensus is true. Its default protocol is
independent heterogeneous proposals, metadata-blind and independently
randomized jury review, deterministic aggregation, and synthesis constrained
by the aggregate. Dissent, abstentions, failures, and verification needs remain
visible.

## Findings that shaped the default

- Du et al., [Improving Factuality and Reasoning in Language Models through
  Multiagent Debate](https://proceedings.mlr.press/v235/du24e.html) (ICML
  2024), showed that multi-agent debate can improve results on several
  controlled reasoning and factuality tasks. It does not establish that an
  arbitrary real-world council reliably produces truth.
- Smit et al., [Should we be going MAD? A Look at Multi-Agent Debate Strategies
  for LLMs](https://arxiv.org/abs/2311.17371), found that tested debate
  strategies did not consistently beat simpler self-consistency or ensemble
  baselines without tuning. Protocol identity and evaluation therefore matter.
- Choi, Zhu, and Li, [Debate or Vote: Which Yields Better Decisions in
  Multi-Agent Large Language
  Models?](https://proceedings.neurips.cc/paper_files/paper/2025/hash/934252acd87f254d5d4672fbde283bd2-Abstract-Conference.html)
  (NeurIPS 2025), separated sampling gains from interaction gains and found that
  voting explained most observed improvement across their benchmarks. This is
  why the alpha begins with independent answers and deterministic aggregation,
  not free-form debate.
- Verga et al., [Replacing Judges with Juries: Evaluating LLM Generations with
  a Panel of Diverse Models](https://arxiv.org/abs/2404.18796), reported that
  diverse model panels can reduce reliance on one judge and mitigate some
  same-family bias. This supports separate lineages and a jury stage; it does
  not make model judgments independent evidence.
- Li et al., [Judging the Judges: A Systematic Study of Position Bias in
  LLM-as-a-Judge](https://arxiv.org/abs/2406.07791), documented task-dependent
  position effects. Candidate labels are therefore arbitrary and presentation
  order is randomized separately for each juror.
- Huang et al., [Language Model Council: Democratically Benchmarking Foundation
  Models on Highly Subjective
  Tasks](https://aclanthology.org/2025.naacl-long.617/) (NAACL 2025), is the
  closest scholarly use of the council metaphor. It studies democratic model
  evaluation rather than a dependable personal decision-support product, but
  reinforces the need to inspect judge-family effects and preserve dissent.

Related protocol families remain useful comparison targets:

- Wang et al., [Mixture-of-Agents Enhances Large Language Model
  Capabilities](https://arxiv.org/abs/2406.04692)
- Chen et al., [ReConcile: Round-Table Conference Improves Reasoning via
  Consensus among Diverse LLMs](https://arxiv.org/abs/2309.13007)
- Zeng et al., [ChatEval: Towards Better LLM-based Evaluators through
  Multi-Agent Debate](https://arxiv.org/abs/2308.07201)

## Product implications

1. Independent generation and a cheaper voting or selection baseline come
   before debate.
2. Provider diversity is explicit; multiple personas on one model do not count
   as heterogeneous lineages.
3. Jurors receive randomized labels and no provider metadata.
4. Aggregation is deterministic and committed before prose synthesis.
5. A tie or abstention remains a tie or abstention.
6. Consequential claims still require primary-source or executable
   verification outside the council.
7. Any future debate, critique-revision, confidence weighting, or
   mixture-of-agents protocol needs a distinct version and matched-cost
   evaluation before becoming a default.

## Interpreting additional lineages

The public alpha adds Mistral to the OpenAI, Anthropic, and Gemini baseline. This
broadens vendor and model-family heterogeneity and permits a three-of-four
default quorum to tolerate one unavailable lineage. It does not establish
statistical independence, eliminate shared training-data effects, or prove that
four providers outperform a three-provider configuration.

The current live configuration adds xAI's Grok as a fifth lineage. That further
broadens provider and model-family coverage and gives a three-of-five quorum
tolerance for two unavailable lineages. It does not prove that five providers
outperform four.

The current default also adds Alibaba's Qwen and Cohere Command as the sixth
and seventh lineages. A clean run therefore samples a broader set of vendors
and model families, while a three-of-seven quorum can preserve a degraded
result through four unavailable providers. Neither property proves that seven
providers outperform five or that their errors are independent.

Upstage Solar is implemented only as an optional disabled bench provider. Its
presence in the adapter registry is not evaluation evidence and does not make
it part of the default council.

Treat each added lineage as an engineering hypothesis. Compare three-, four-,
five-, six-, and seven-lineage configurations on a fixed, blinded evaluation
set; report accuracy, valid-jury rate, abstentions, ties, latency, and total
provider cost; and keep the evidence separate. API compatibility and passing
live canaries are useful release checks, not research evidence that the council
is correct.

## Useful research and evaluation kits

- [DebateLLM](https://github.com/instadeepai/DebateLLM) for reproducing debate
  strategies.
- [Together MoA](https://github.com/togethercomputer/MoA) for the original
  layered proposer/aggregator protocol.
- [ReConcile](https://github.com/dinobby/ReConcile) for heterogeneous
  round-table experiments.
- [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) and
  [promptfoo](https://github.com/promptfoo/promptfoo) as possible external
  evaluation harnesses.

These are references and laboratory tools, not dependencies of this
standard-library-only alpha.
