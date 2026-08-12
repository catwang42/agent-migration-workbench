# Module 08 — The scorecard

*~5 min read · [07 Shadow & adjudication](07-shadow-and-adjudication.md) → **08** → [Results](../results/index.md)*

---

```bash
python cli.py scorecard        # gates → verdicts → markdown report
```

The scorecard is the gates evaluated. Nothing more is added to it, and nothing is
taken out of it.

Two cards ship. **The verdicts below are the recommendation** — the deployment
generation. The development-generation card follows further down, because the
failures on it are what produced the prompts running here.

## The verdicts: deployment generation

Incumbent **Claude Sonnet 5**. Candidate **Gemini 3.6 Flash with the reasoning
budget minimised** — the configuration a deployment would pin.

| Subagent | Incumbent | Candidate model | Candidate prompt | Gates | Verdict |
|---|---|---|---|---|---|
| Query Rewriter | Claude Sonnet 5 | **Gemini 3.6 Flash** (capped thinking) | `gemini_targeted_v1` | 5 of 6 | **INCOMPLETE** (provisional: MIGRATE) |
| Chunk Summarizer | Claude Sonnet 5 | **Gemini 3.6 Flash** (capped thinking) | `gemini_tuned_v1` | 6 of 6 | **UNDETERMINED** |
| Feature Extractor | Claude Sonnet 5 | **Gemini 3.6 Flash** (capped thinking) | `gemini_optimizer_v1` | 6 of 6 | **UNDETERMINED** |

Gates version 1, hash `92f9d018432f` — the same file signed off before any of
this was measured.

### What each verdict rests on

| Gate | Query Rewriter | Chunk Summarizer | Feature Extractor |
|---|---|---|---|
| `quality_delta_pp` | **PASS** +7.32 pp [+2.86, +12.14] | FAIL −2.50 pp [−5.36, +0.36] | FAIL +0.32 pp [−3.76, +4.46] |
| `json_schema_validity` | **PASS** 1.000 [1.000, 1.000] | **PASS** 1.000 [1.000, 1.000] | **PASS** 1.000 [1.000, 1.000] |
| `groundedness_delta_pp` | not applicable | **PASS** +0.00 pp | **PASS** +0.00 pp |
| `shadow_agreement` | **PASS** on `alt` (16W/2L) | **PASS** 0.971 [0.929, 1.000] | **PASS** on `alt` (31W/16L) |
| `cost_savings_pct` | **PASS** 42.7% [40.3, 45.3] | **PASS** 56.1% [54.5, 57.5] | **PASS** 64.8% [63.8, 65.7] |
| `latency_p95` | **PASS** 6,471 ms | FAIL 7,388 ms | FAIL 8,906 ms |

Bracketed pairs are the 95% confidence range, and every gate is tested against
the worst end of it. All three arms are the same model — Gemini 3.6 Flash with
the reasoning budget minimised — differing only in their prompt.
{ .amw-legend }

**Query Rewriter is the strong result.** It does not merely fail to fail
`quality_delta_pp`; it passes, with the lower bound at **+2.86 pp**. The
candidate is measurably better than the incumbent on this subagent, and its
schema validity is 1.000 against the incumbent's 0.814.

**Both `quality_delta_pp` failures are precision failures, not measured
regressions.** Chunk Summarizer's interval spans zero and so does Feature
Extractor's — whose point estimate is *positive*. Neither says a drop happened;
both say this sample size cannot rule one out at the agreed bound.

### Why two rows say UNDETERMINED

`quality_delta_pp` and `latency_p95` both failed. That combination matches no
rule in `gates.yaml`: they are neither the quality gates that produce
TUNE_FIRST nor the blocking gates that produce HOLD. So the engine issues no
verdict.

That is a **gap in the gates file**, and it ships named as a gap rather than
resolved by whoever happens to be rendering the report. Writing the missing rule
after seeing which subagents it would catch is precisely the move pre-registered
gates exist to prevent.

### Why Query Rewriter says INCOMPLETE when nothing failed

`groundedness_delta_pp` is inapplicable to it **by instrument design**. Its
output is a search plan, which makes no claim about the input that could be
supported or unsupported by a source — there is nothing for a groundedness
instrument to score. So five of six gates were measured, and:

> A verdict over a subset of the gates is not the verdict that was agreed, so none
> is issued.

The provisional reading — MIGRATE, were the unmeasured gate to pass — is printed
beside it and marked conditional. That is a different sentence from a verdict.

### The economics turn on one setting

| Configuration | Query Rewriter | Chunk Summarizer | Feature Extractor |
|---|---|---|---|
| Reasoning budget minimised — **recommended** | **+42.7%** | **+56.1%** | **+64.8%** |
| Default reasoning budget | −16.5% | −6.6% | −23.1% |

Same provider model ID, same prompt bytes, same 70 items. The only difference is
`thinking_config.thinking_budget`. Reasoning tokens are billed as output tokens
and cannot be separated after the fact, which is what moves the savings column
across the gate line — from *losing* money on every subagent to clearing a 30%
gate on all three. The default-budget arms emit 2.08x to 2.63x the incumbent's
output tokens; the capped arms emit 0.31x to 0.55x.

These are measured corpus costs at verified list prices, not projected bills.
Volumes remain unconfirmed, so every run-rate cell is still an em dash.

### Latency is the weakest measurement here

Both arms were pinned to `global`, which is the only thing that opens the gate.
But it is **n=10 per arm**, and the incumbent's own p95 moved from 3,471 ms to
6,893 ms between two probes of the same model in the same region three hours
apart. Latency requires measurement on production infrastructure; demo-window
figures are directional, and the two FAILs above should be read as "not
demonstrated here", not "known slower".

---

## How we got here: the development generation

Everything above runs prompts that were written and tuned on **Gemini 2.5
Flash**. That run is published too, because its failures are the evidence that
the method works — and because the prompts porting unchanged across a model
generation is itself the finding.

| Subagent | Incumbent | Candidate model | Candidate prompt | Gates | Verdict |
|---|---|---|---|---|---|
| Chunk Summarizer | Claude Sonnet 5 | **Gemini 2.5 Flash** | `gemini_tuned_v1` | 4 of 6 | **INCOMPLETE** (provisional: TUNE_FIRST) |
| Feature Extractor | Claude Sonnet 5 | **Gemini 2.5 Flash** | `gemini_tuned_v1` | 3 of 6 | **INCOMPLETE** (provisional: TUNE_FIRST) |
| Query Rewriter | Claude Sonnet 5 | **Gemini 2.5 Flash** | `gemini_tuned_v1` | 3 of 6 | **HOLD** |

No MIGRATE on Gemini 2.5 Flash. That is the honest output of this corpus under
these gates, and it is the strongest evidence available that the instrument is
not decorative.

### Why two rows say INCOMPLETE rather than TUNE_FIRST

Chunk Summarizer measured 4 of its 6 pre-agreed gates; `cost_savings_pct` and
`latency_p95` were not measured. Feature Extractor measured 3 of 6;
`cost_savings_pct`, `groundedness_delta_pp` and `latency_p95` were not.

> A verdict over a subset of the gates is not the verdict that was agreed, so none
> is issued.

The provisional reading — "were every unmeasured gate to pass, it would be
TUNE_FIRST" — is printed beside it, clearly marked as conditional. That is a
different sentence from a verdict, and the report keeps them different.

### Why Query Rewriter is HOLD

`shadow_agreement` failed on the **95% confidence range** bound: 0.557
[0.443, 0.671] against a 0.90
minimum, and the `alt` clause also fails for this arm (14W/20L, or 8W/15L
excluding structurally malformed baseline emissions). `shadow_agreement` is a
**blocking** gate, so the rule in `gates.yaml` produces HOLD without discretion:

> Structural failure. Do not migrate this subagent on the current prompt pack.

Note the last four words. Module 07's `gemini_targeted_v1` is a different prompt
pack, and it passes the `alt` clause — but it was not the arm gated in this run,
so it does not change this verdict.

## The three per-subagent deltas, in the wording that ships

Each quality-delta row has an agreed sentence, because the same number can be
described honestly in more than one way and consistency is what makes it checkable
across a deck, a report and this site.

| Subagent | `quality_delta_pp` (paired) | Wording that ships |
|---|---|---|
| Chunk Summarizer | −2.32 pp [−5.00, +0.36], paired n=70 | **not demonstrated at this sample size** |
| Feature Extractor | −10.44 pp [−13.78, −7.12], paired n=70 | **measured regression, recovered by tuning** |
| Query Rewriter | −0.64 pp [−5.50, +4.32], paired n=70 | fails on the confidence-range lower bound (−5.501 against a −2.0 bound) |

All three **FAIL** `quality_delta_pp`, because the gate tests the confidence-range lower bound
and all three lower bounds sit below −2.0. Chunk Summarizer's interval straddles
zero; that is why its row says the delta was not demonstrated at this sample size
rather than claiming a drop was measured. Feature Extractor's interval does not
straddle zero — a regression *was* measured there, and module 05 shows the tuning
rungs that recover it.

## Widening was not free, and it did not flatter us

Every gated judge score is now full-70. Query Rewriter and Chunk Summarizer were
widened from the core 28 on 2026-08-11, registered as a sizing deviation and
decided *before* the widened results were seen.

Chunk Summarizer's `quality_delta_pp` moved from a core-28 `ci_lower` of −2.679 to
a full-70 −2.32 pp [−5.00, +0.36]. The interval got **wider** at the larger n,
because the 42 added items carry more spread than the core did. The gate fails on
the widened data, and that is the number that ships.

## What is not evaluated, and why that is not a pass

Three cells recur across every subagent:

- **`cost_savings_pct`** — not computable *on this run*. Two independent gates,
  each of which a human has to clear, and at the time this card was rendered
  neither had: no price was verified and the customer profile's volume block is
  illustrative (`volumes_confirmed: false`). Prices cleared on 2026-08-12 —
  `config/pricing.yaml` now has 0 rates still read `VERIFY` — which is why the
  deployment card at the top of this page carries measured corpus costs and this
  one does not. Volumes are still open on both.
- **`latency_p95`** — not comparable, region split disclosed. Claude ran in
  `global`, Gemini in `us-central1`. The `claude_baseline_p95` sentinel resolves
  only from a same-region probe.
- **`groundedness_delta_pp`** (Query Rewriter and Feature Extractor) — those subagents have no
  `citation_coverage` instrument, so it was not measured for them.

An unmeasured gate is printed as unmeasured, with its reason, in the same table as
the measured ones. It never quietly becomes a pass.

## The footer is part of the evidence

| Field | Value |
|---|---|
| Customer | Demo — Patents RAG (`demo_patents`) |
| Provenance | synthetic, generator `t06.1`, dataset seed `20260812` |
| Bootstrap | 95% confidence range, seed `20260812` |
| Judge | Gemini 2.5 Pro, prompt `v1`, k=2 repeats |
| Mode | `replay` |
| Run date | no live run — assembled from recordings |
| Recording window | 10 Aug 2026, 12:07 AM → 11 Aug 2026, 2:20 PM (SGT) — UTC `2026-08-09T16:07:15+00:00` → `2026-08-11T06:20:37+00:00` |
| Region(s) | Claude `global`, Gemini + judge `us-central1` |
| Prices verified on | UNVERIFIED at render time — every rate still read `VERIFY` |
| Volumes | volumes: illustrative |
| Gates | version 1, hash `92f9d018432f` |

The deployment card's footer differs in three fields and is otherwise identical:
its recording window runs to `2026-08-12T12:58:33+00:00`, its regions read Claude
`global`, Gemini + judge `global`, and its prices are verified on 2026-08-12. The
gates hash does not differ, which is the point of printing it.

And the scope line that outranks every verdict above it:

> Verdicts apply to each subagent's measured behavior class (Level 1 single-call
> transforms, measured in full here); tool-selection and multi-step trajectory
> behaviors are evaluated with their own instruments in the follow-on and receive
> no verdict today.

## Read the reports themselves

Every caveat above is welded into the report, not appended to it — each one sits in
the same cell as the figure it qualifies, so a screenshot of a row carries its own
context.

[Open the Migration Readiness Scorecard](../results/scorecard.md){ .md-button .md-button--primary }
[The development-generation card](../results/scorecard_development_generation.md){ .md-button }

---

**Next:** [Results](../results/index.md) · [Exercises](../exercises.md)

*Sources: `artifacts/results/scorecard_current-capped.md` (deployment generation — Gemini 3.6 Flash, capped thinking) and `artifacts/results/scorecard_widened.md` (development generation — Gemini 2.5 Flash). Gates from `config/gates.yaml`, version 1, hash `92f9d018432f`, for both.*
