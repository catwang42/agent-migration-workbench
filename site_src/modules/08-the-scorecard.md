# Module 08 — The scorecard

*~5 min read · [07 Shadow & adjudication](07-shadow-and-adjudication.md) → **08** → [Results](../results/index.md)*

---

```bash
python cli.py scorecard        # gates → verdicts → markdown report
```

The scorecard is the gates evaluated. Nothing more is added to it, and nothing is
taken out of it.

## The verdicts this run produced

| Subagent | Candidate | Gates evaluated | Verdict |
|---|---|---|---|
| Chunk Summarizer | `gemini_tuned_v1` | 4 of 6 | **INCOMPLETE** (provisional: TUNE_FIRST) |
| Feature Extractor | `gemini_tuned_v1` | 3 of 6 | **INCOMPLETE** (provisional: TUNE_FIRST) |
| Query Rewriter | `gemini_tuned_v1` | 3 of 6 | **HOLD** |

No MIGRATE. That is the honest output of this corpus under these gates, and it is
the strongest evidence available that the instrument is not decorative.

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

`shadow_agreement` failed on the CI bound: 0.557 [0.443, 0.671] against a 0.90
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
| Query Rewriter | −0.64 pp [−5.50, +4.32], paired n=70 | fails on the CI lower bound (−5.501 against a −2.0 bound) |

All three **FAIL** `quality_delta_pp`, because the gate tests the CI lower bound
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

- **`cost_savings_pct`** — not computable. `config/pricing.yaml` has 13 rates still
  reading `VERIFY` and `verified_on` is null; the customer profile's volume block
  is illustrative (`volumes_confirmed: false`). Two independent gates, each of
  which a human has to clear. Every cost cell is an em dash — not a zero.
- **`latency_p95`** — not comparable, region split disclosed. Claude ran in
  `global`, Gemini in `us-central1`. The `claude_baseline_p95` sentinel resolves
  only from a same-region probe.
- **`groundedness_delta_pp`** (QR and FE) — those subagents have no
  `citation_coverage` instrument, so it was not measured for them.

An unmeasured gate is printed as unmeasured, with its reason, in the same table as
the measured ones. It never quietly becomes a pass.

## The footer is part of the evidence

| Field | Value |
|---|---|
| Customer | Demo — Patents RAG (`demo_patents`) |
| Provenance | synthetic, generator `t06.1`, dataset seed `20260812` |
| Bootstrap | 95% CI, seed `20260812` |
| Judge | Gemini 2.5 Pro, prompt `v1`, k=2 repeats |
| Mode | `replay` |
| Run date | no live run — assembled from recordings |
| Recording window | 2026-08-09T16:07:15+00:00 → 2026-08-11T06:20:37+00:00 |
| Region(s) | Claude `global`, Gemini + judge `us-central1` |
| Prices verified on | UNVERIFIED — 13 rates still read `VERIFY` |
| Volumes | volumes: illustrative |
| Gates | version 1, hash `92f9d018432f` |

And the scope line that outranks every verdict above it:

> Verdicts apply to each subagent's measured behavior class (Level 1 single-call
> transforms, measured in full here); tool-selection and multi-step trajectory
> behaviors are evaluated with their own instruments in the follow-on and receive
> no verdict today.

## Read the report itself

Every caveat above is welded into the report, not appended to it — each one sits in
the same cell as the figure it qualifies, so a screenshot of a row carries its own
context.

[Open the Migration Readiness Scorecard](../results/scorecard.md){ .md-button .md-button--primary }

---

**Next:** [Results](../results/index.md) · [Exercises](../exercises.md)

*Source: `artifacts/results/scorecard_widened.md`, `config/gates.yaml` (version 1, hash `92f9d018432f`).*
