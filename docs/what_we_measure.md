# What We Measure — and Why Only Some of It Gets a Verdict

**Workshop:** Claude → Gemini subagent migration, patents-domain RAG
**Corpus:** `demo_patents`, synthetic, seed `20260812`, 70 cases/subagent
**Numbers cited here:** `artifacts/results/phase2_n70.json`, calls executed
2026-08-09T16:07Z → 2026-08-10T02:45Z
**Version:** 2026-08-11

---

## The one-sentence version

We migrate the subagents that are cheap to measure and expensive to run, we measure them
with the instrument that matches what they actually decide, and we refuse to issue a
verdict on the ones our instrument does not cover.

---

## Why these three subagents first

Your Level 1 subagents — the single-call transforms — are the 50,000-calls-a-day work.
In the demo profile they are 1.85M calls/day across three subagents (250k + 1.2M + 400k,
from `config/customers/demo_patents.yaml` — **illustrative volumes, not customer-confirmed**),
and in a typical RAG stack that tier dominates the inference bill while carrying almost
none of the architectural risk. That "dominates" is a shape claim from the call-volume
profile, not a measured share — no dollar figure exists yet, because prices are unverified.
They are also the easiest tier to *prove*: one input, one output, a
gold reference, a deterministic metric. That combination — biggest spend, lowest risk,
strongest measurement — is why they migrate first.

The orchestrator stays put. Not because it can't move, but because a looping,
tool-dispatching agent needs trajectory evaluation in the runtime, and a single-call bench
harness cannot see what it does. Migrating it on the strength of this scorecard would mean
extrapolating a Level 1 measurement onto Level 3 behavior. We don't do that here.

---

## The canonical taxonomy

Classify every subagent in your stack into one of these four rows. The row determines the
instrument, and the instrument determines whether a verdict is available today.

| Subagent pattern | Autonomy level | What the model actually decides | Instrument | Status in this workshop |
|---|---|---|---|---|
| **Prompt-based** | **Level 1 — transform** | Output content only. One call in, one structured object out. | **Bench harness**: gold references, deterministic metrics, rubric-anchored judge, bootstrap CIs | **Measured in full here. Verdict-capable.** |
| **Tool-calling** | **Level 2 — tool decider** | *Which* tool to call and with what arguments | **Tool-call quality scoring**: selection accuracy, argument validity, call-sequence sanity | **Partial.** Argument/schema formatting is measured now on recorded calls; *selection* accuracy is follow-on. |
| **Retrieval-augmented** | **Level 2/3** | What to retrieve, and whether the answer is supported by it | **Retrieval + groundedness trajectory evaluation**: recall@k, citation faithfulness across the whole chain | **Follow-on.** Citation coverage is measured here for the summarizer, but end-to-end retrieval quality is not. |
| **Orchestration** | **Level 3 — looping** | When to loop, when to delegate, when to stop | **Trajectory evaluation in the runtime**: step-level traces, task completion, cost-per-resolution | **Follow-on. HOLD on this scorecard — no verdict today.** |

The three subagents in this workshop are all row 1. That is a deliberate scoping choice,
and it is printed in the scorecard footer so nobody has to remember it.

---

## Per subagent: what, with which instrument, why

### Query Rewriter — Level 1

Turns a natural-language question into a structured retrieval plan (intent + filters).

| What | Instrument | Why this one |
|---|---|---|
| `exact_match_intent` | Deterministic, vs gold | Intent drives the whole retrieval path. Wrong intent = wrong corpus slice, regardless of how good the filters are. |
| `filter_precision` / `filter_recall` / `filter_f1` | Deterministic set comparison, normalised codes | Precision and recall fail *differently* — a missed assignee filter over-retrieves, an invented one silently drops relevant docs. Averaging them would hide which. |
| `json_schema_validity` | Deterministic parse against the frozen schema | A plan that doesn't parse is a hard outage, not a quality dip. This is a **blocking** gate. |
| `judge_score` | Rubric-anchored LLM judge, k=2, **full 70** (widened 2026-08-11, sizing deviation #2) | Catches "structurally valid but semantically wrong" — the class exact-match scores into noise. |

**Measured (n=70):** Claude `exact_match_intent` 0.729 [0.629, 0.829] vs Gemini tuned
0.814 [0.714, 0.900]. Heavy CI overlap — parity within measurement, not "better".

**The finding that matters here:** Claude's `json_schema_validity` is **0.814
[0.714, 0.900]** against a 0.99 gate. That is a baseline defect, invisible at n=10 where
all three arms read 1.000. It is also *mechanism*, not model — see the asymmetry note below.

### Chunk Summarizer — Level 1, with a groundedness instrument

Compresses retrieved chunks into a cited summary.

| What | Instrument | Why this one |
|---|---|---|
| `citation_coverage` | Deterministic: every claim maps to a provided chunk id | The whole value of a RAG summarizer is that its claims are traceable. An uncited claim is the failure mode. |
| `fabricated_citation_rate` | Deterministic: cited id must exist in the provided set | Citing a chunk that was never supplied is the worst failure in this tier — it looks *more* trustworthy while being less so. |
| `uncited_claim_rate` | Deterministic | The other half of groundedness: claims with no citation at all. |
| `json_schema_validity` | Deterministic | Blocking gate, as above. |
| `judge_score` | Rubric judge, k=2, **full 70** (widened 2026-08-11, sizing deviation #2) | Fluency and faithfulness, which no deterministic metric here covers. |

**Measured (n=70):** `fabricated_citation_rate` and `uncited_claim_rate` are **0.000 on
all three arms**. `citation_coverage` 1.000 (Claude), 0.940 (Gemini naive), 1.000 (Gemini
tuned). Judge 0.902 / 0.897 / 0.915 with heavy overlap.

Note the scope limit: this measures *whether claims cite supplied chunks*. It does not
measure whether the right chunks were retrieved. That is the row-3 instrument, follow-on.

### Feature Extractor — Level 1, and the honest row

Pulls structured patent features out of a document.

| What | Instrument | Why this one |
|---|---|---|
| `extraction_accuracy` | Deterministic, per-field vs gold | The primary task metric. |
| `answered_precision` | Deterministic, over fields the model chose to answer | Separates "got it wrong" from "declined to answer" — different fixes. |
| `omission_rate` | Deterministic | Declining too often is a real failure even when precision is perfect. |
| `hallucination_rate` | Deterministic: non-null answer where gold is null | The abstention discipline. |
| `json_schema_validity` | Deterministic | Blocking gate. |
| `judge_score` | Rubric judge, k=2, **full 70** | Registered from the start — this is where the contested finding lives. QR and CS were widened to match on 2026-08-11. |

**Measured (n=70):** deterministic accuracy is **saturated** — Gemini scores 1.000 on
`extraction_accuracy`, 1.000 `json_schema_validity`, 0.000 `omission_rate`, 0.000
`hallucination_rate`. Claude scores 0.971 / 0.957 / 0.029 / 0.000.

On the deterministic instruments alone, Gemini wins outright. The judge disagrees:
**Claude 0.900 [0.868, 0.929] vs Gemini naive 0.821 [0.787, 0.854]**, non-overlapping at
n=70. The disagreement is real, it is localised to the `novelty_statement` field, and it
is the reason this subagent does not get a clean MIGRATE.

This is the most important row in the whole workshop, because it is the row where the
cheap instrument and the expensive instrument point in opposite directions — and where
picking the cheap one would have produced a confident wrong answer. The arithmetic is in
[`notes/counterfactual_scorecard.md`](../notes/counterfactual_scorecard.md).

---

## Three properties every number here has

**1. It came from an executed call.** No number in any artifact is a placeholder,
an estimate, or a projection. Replay mode replays *previously recorded real calls* and
prints the recording window on screen. If a value could not be measured, the cell says
"not measured" and names the reason.

**2. It is a CI bound, not a point estimate.** Every gate is checked against the 95%
bootstrap CI lower bound (upper bound for `max` gates), 10,000 resamples, seed `20260812`.
This is what licenses the phrase *"quality parity within measurement under pre-agreed
gates"* — and why nothing here says "zero quality drop."

**3. It carries its sample size and split.** Judge scores are labelled with judged n and
whether they came from the 28-item core split or the full 70. Two judge scores at
different n are not directly comparable, and the number alone doesn't say so.

As of 2026-08-11 every **gated** judge score is full-70: QR and CS were widened to the whole
corpus to complete the triage adjudication and narrow the delta CIs, registered as sizing
deviation #2 and decided before the widened results were seen. The **ablation ladder** is a
different instrument and stays at core-28, so its rungs stay comparable to each other. That
is why the FE ladder's incumbent reads 0.903 and the scorecard's FE baseline reads 0.900 —
two measurements on two splits, not one number rounded twice. Never quote one against the
other.

Widening is not free of risk and did not flatter us here: CS `quality_delta_pp` moved from a
core-28 `ci_lower` of −2.679 to a full-70 −2.32 pp [−5.00, +0.36]. The interval got *wider*
at the larger n, because the 42 added items carry more spread than the core did. The gate
fails on the widened data, and that is the number that ships.

---

## Two asymmetries you must know before reading the scorecard

**The schema-mechanism asymmetry.** Under this demo organization's Vertex AI policy
configuration (`constraints/vertexai.allowedPartnerModelFeatures`), partner-model
structured outputs were unavailable, so the Claude baseline was measured using tool-call
structured emission while Gemini's tuned arms use native enforced `response_schema`. Every
Claude schema-validity number in the scorecard carries that caveat in the same cell. It is
an *environment* finding about one org's policy, **not** a statement that Claude lacks
structured outputs on Vertex.

Fairness is preserved by construction: Gemini's A0 (naive) rung runs under the **same
tool-call mechanism** as Claude, so the Claude-vs-A0 delta is prompt format, not
mechanism. The A0→A1 delta is what the enforced schema is worth.

**The region asymmetry.** Claude ran in region `global` (us-central1 partner quota was
exhausted); Gemini and the judge ran in `us-central1`. Quality and cost gates are
unaffected. `latency_p95` is a cross-region comparison and therefore renders as
*"not comparable — region split disclosed"* rather than as a measurement.

---

## What we did not measure, stated plainly

- **Tool *selection*** — which tool the model picks. Row 2 instrument, follow-on.
- **End-to-end retrieval quality** — recall@k over the real index. Row 3 instrument, follow-on.
- **Multi-step trajectories** — the orchestrator's loop, delegation, and stopping behavior.
  Row 4 instrument, follow-on. **HOLD.**
- **Real customer traffic** — this corpus is synthetic and labelled `provenance: synthetic`
  on every item. Converting the methodology to your traces is the Act 2 ask; see
  [`data_request_onepager.md`](data_request_onepager.md).
- **Dollar figures** — `config/pricing.yaml` is still `VERIFY` placeholders, and customer
  volumes are unconfirmed. Both gates must be cleared by a human before any cost cell
  renders a number. Until then: em-dashes.
