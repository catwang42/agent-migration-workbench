# Module 01 — Why subagents migrate first

*~5 min read · [Setup](../setup.md) → **01** → [02 The reference workload](02-reference-workload.md)*

---

--8<-- "_includes/development-generation.md"

## The one-sentence version

Migrate the subagents that are cheap to measure and expensive to run, measure them
with the instrument that matches what they actually decide, and refuse to issue a
verdict on the ones the instrument does not cover.

## The shape claim, and its limits

Level 1 subagents — single-call transforms — are the high-frequency tier of a RAG
stack. In this workshop's demo profile they are 1.85M calls/day across three
subagents (250k + 1.2M + 400k, from `config/customers/demo_patents.yaml`).

Two honesty notes attach to that number and travel with it everywhere:

- Those volumes are **illustrative, not customer-confirmed**
  (`volumes_confirmed: false`).
- "This tier dominates the bill" is a *shape* claim derived from the call-volume
  profile. It is **not a measured share**, and no dollar figure exists, because
  `config/pricing.yaml` is unverified.

What makes this tier migrate first is not only the volume. It is that one input,
one output, a gold reference and a deterministic metric make it the easiest tier
to *prove*. Biggest call volume, lowest architectural risk, strongest measurement
— that combination is the argument.

## The four-category taxonomy

Classify every subagent in your stack into one of these rows. The row picks the
instrument, and the instrument decides whether a verdict is available today.

| Subagent pattern | Autonomy level | What the model actually decides | Instrument | Status here |
|---|---|---|---|---|
| **Prompt-based** | **Level 1 — transform** | Output content only. One call in, one structured object out. | Bench harness: gold references, deterministic metrics, rubric-anchored judge, bootstrap CIs | **Measured in full. Verdict-capable.** |
| **Tool-calling** | **Level 2 — tool decider** | *Which* tool to call, and with what arguments | Tool-call quality scoring: selection accuracy, argument validity, call-sequence sanity | **Partial.** Argument and schema formatting are measured on recorded calls; *selection* accuracy is follow-on. |
| **Retrieval-augmented** | **Level 2/3** | What to retrieve, and whether the answer is supported by it | Retrieval + groundedness trajectory evaluation: recall@k, citation faithfulness across the chain | **Follow-on.** Citation coverage is measured for the summarizer; end-to-end retrieval quality is not. |
| **Orchestration** | **Level 3 — looping** | When to loop, when to delegate, when to stop | Trajectory evaluation in the runtime: step-level traces, task completion, cost-per-resolution | **Follow-on. No verdict on this scorecard.** |

All three subagents in this workshop are row 1. That is a deliberate scoping
choice, and it is printed in the scorecard footer so nobody has to remember it.

## The three subagents, and what each one is measured with

=== "Query Rewriter"

    Turns a natural-language question into a structured retrieval plan (intent +
    filters).

    | What | Instrument | Why this one |
    |---|---|---|
    | `exact_match_intent` | Deterministic, vs gold | Intent drives the whole retrieval path. Wrong intent means the wrong corpus slice, however good the filters are. |
    | `filter_precision` / `filter_recall` / `filter_f1` | Deterministic set comparison, normalised codes | Precision and recall fail *differently*: a missed assignee filter over-retrieves; an invented one silently drops relevant documents. Averaging them hides which. |
    | `json_schema_validity` | Deterministic parse against the frozen schema | A plan that does not parse is an outage, not a quality dip. **Blocking** gate. |
    | `judge_score` | Rubric-anchored LLM judge, k=2 repeats, full 70 | Catches "structurally valid but semantically wrong" — the class exact match scores into noise. |

=== "Chunk Summarizer"

    Compresses retrieved chunks into a cited summary. This is the one row with a
    groundedness instrument.

    | What | Instrument | Why this one |
    |---|---|---|
    | `citation_coverage` | Deterministic: every claim maps to a provided chunk id | The entire value of a RAG summarizer is that its claims are traceable. |
    | `fabricated_citation_rate` | Deterministic: the cited id must exist in the provided set | The worst failure in this tier — it looks *more* trustworthy while being less so. |
    | `uncited_claim_rate` | Deterministic | The other half of groundedness: claims with no citation at all. |
    | `json_schema_validity` | Deterministic | Blocking gate. |
    | `judge_score` | Rubric judge, k=2, full 70 | Fluency and faithfulness, which no deterministic metric here covers. |

    Scope limit, stated plainly: this measures *whether claims cite supplied
    chunks*. It does not measure whether the right chunks were retrieved. That is
    the row-3 instrument, and it is follow-on.

=== "Feature Extractor"

    Pulls structured patent features out of a document. This is the honest row.

    | What | Instrument | Why this one |
    |---|---|---|
    | `extraction_accuracy` | Deterministic, per-field vs gold | The primary task metric. |
    | `answered_precision` | Deterministic, over fields the model chose to answer | Separates "got it wrong" from "declined to answer" — different fixes. |
    | `omission_rate` | Deterministic | Declining too often is a real failure even when precision is perfect. |
    | `hallucination_rate` | Deterministic: a non-null answer where gold is null | The abstention discipline. |
    | `json_schema_validity` | Deterministic | Blocking gate. |
    | `judge_score` | Rubric judge, k=2, full 70 | Registered from the start — this is where the contested finding lives. |

## The row that justifies the whole method

On Feature Extractor the deterministic instruments are **saturated**. Gemini
scores 1.000 `extraction_accuracy`, 1.000 `json_schema_validity`, 0.000
`omission_rate`, 0.000 `hallucination_rate`; Claude scores 0.971 / 0.957 / 0.029 /
0.000 (n=70, `artifacts/results/phase2_n70_widened.json`).

On the deterministic instruments alone, Gemini wins outright. The judge disagrees:
Claude **0.900 [0.868, 0.929]** against Gemini naive **0.821 [0.787, 0.854]**,
non-overlapping at n=70.

That is the most important row in the workshop, because it is the row where the
cheap instrument and the expensive instrument point in opposite directions. Had we
run only the cheap one, we would have shipped a confident wrong answer.

## What is deliberately not measured

- **Tool *selection*** — which tool the model picks. Row 2 instrument, follow-on.
- **End-to-end retrieval quality** — recall@k over a real index. Row 3, follow-on.
- **Multi-step trajectories** — the orchestrator's loop, delegation and stopping
  behaviour. Row 4, follow-on, and it receives no verdict today.
- **Real customer traffic** — this corpus is synthetic and every item carries
  `provenance: synthetic`.
- **Dollar figures** — prices are unverified and volumes unconfirmed. Em dashes
  until a human clears both.

The orchestrator stays put not because it cannot move, but because migrating it on
the strength of a Level 1 measurement would mean extrapolating across two autonomy
levels. That is exactly the move this method exists to refuse.

---

**Next:** [Module 02 — The reference workload](02-reference-workload.md)

*Source: `docs/what_we_measure.md` (version 2026-08-11), `artifacts/results/phase2_n70_widened.json`, `config/customers/demo_patents.yaml`. Corpus: `demo_patents`, synthetic, seed `20260812`, 70 cases per subagent.*
