# Counterfactual: what exact match would have reported for Feature Extractor

**Written 2026-08-09.** Workshop material. This is the case for why the metric
definition is part of the migration decision, not plumbing underneath it.

Everything below is computed from the recorded calls of the 2026-08-09 live
`-n 10` run — the same candidate outputs the published artifact scored,
replayed and re-scored under the *old* metric definition. Derivation:
[`scripts/_counterfactual_fe.py`](../scripts/_counterfactual_fe.py). Nothing
here is estimated.

## The dates

| date | event |
|---|---|
| 2026-08-07 | T06 realism review. `technical_field` and `novelty_statement` moved off exact match to the rubric judge. **Pre-baseline** — no comparative number existed yet. [Rationale.](fe_open_text_metric_change.md) |
| 2026-08-09 | First live phase-2 run, n=10. Numbers and clusters in [day1_failures.md](day1_failures.md). |
| 2026-08-09 | This counterfactual, computed by re-scoring that run's recorded outputs under the pre-08-07 definition. |

The order matters and is worth stating to a customer plainly: **the metric was
changed before anyone had seen a Claude-vs-Gemini number.** It was not moved to
produce a preferred result. The counterfactual below is what the old definition
would have said, reconstructed after the fact.

## What exact match would have reported

Feature Extractor, 10 items, all three arms, deterministic metrics only —
because under the old definition there was no judge criterion for these two
fields. That absence *is* the counterfactual.

| FE metric | claude | gemini A0 | gemini A1-A3 |
|---|---|---|---|
| `extraction_accuracy` | 0.700 | **0.763** | **0.763** |
| `answered_precision` | 0.679 | **0.706** | **0.719** |
| `omission_rate` (lower better) | 0.100 | **0.038** | 0.054 |
| `hallucination_rate` | 0.000 | 0.000 | 0.000 |
| `json_schema_validity` | 0.900 | **1.000** | **1.000** |

**Gemini ranks above Claude on every one of them.** Bold marks the better arm.

Now the same run as actually measured, with the two fields judged:

| FE metric | claude | gemini A0 | gemini A1-A3 |
|---|---|---|---|
| `extraction_accuracy` (6 fields) | 0.900 | **1.000** | **1.000** |
| `judge_score` | **0.879** | 0.782 | 0.783 |

## The thing worth showing the room

Exact match did not *miss* the two decisive fields. It scored them — and
scored them into noise. Per-field, "not correct" out of 10 items:

| field | claude | gemini A0 | gemini A1-A3 | scored by |
|---|---|---|---|---|
| `title` | 1 | 0 | 0 | exact match |
| `assignee` | 1 | 0 | 0 | exact match |
| `filing_date` | 1 | 0 | 0 | exact match |
| `jurisdiction` | 1 | 0 | 0 | exact match |
| `cpc_codes` | 1 | 0 | 0 | exact match |
| `independent_claim_count` | 1 | 0 | 0 | exact match |
| **`technical_field`** | **9** | **9** | **9** | exact match |
| **`novelty_statement`** | **9** | **10** | **10** | exact match |

Read the last two rows. Under exact match, Claude fails `novelty_statement` on
**9 of 10** items — and Claude is the arm that, per the judge, gets it right on
**8 of 10**. Exact match is not measuring correctness on these fields at all;
it is measuring whether the model happened to choose the gold's wording. Both
arms fail it at essentially the same rate, so the two fields contribute almost
no *differential* signal.

What that leaves is arithmetic. With the two decisive fields reduced to shared
noise, the ranking is decided entirely by the six mechanical fields — title,
assignee, date, jurisdiction, CPC, claim count — where Gemini is flawless and
Claude has one bad item (fe-0006, an empty tool call). So exact match reports
Gemini ahead, and the reason it reports Gemini ahead has nothing to do with the
two fields the analyst actually reads.

The judge, given the same outputs, separates them 2-vs-9:

| `novelty_statement` | claude | gemini A0 |
|---|---|---|
| failed under exact match | 9/10 | 10/10 |
| failed under the judge | **2/10** | **9/10** |

Same model outputs. Same run. One instrument says "both models are mediocre
here, and Gemini is slightly ahead overall"; the other says "Claude does this
job and Gemini has largely stopped doing it."

## What each path points toward

No verdict has been issued. The scorecard is T15, `shadow_agreement` has not
been measured yet, and at n=10 every interval overlaps. So this is about
*direction*, not a decision:

- **Deterministic-only, exact match** (the counterfactual): Gemini ahead on
  every FE metric, no quality gate with anything to fail on. Points at
  **MIGRATE**.
- **As measured** (`quality_delta_pp` gate, min −2.0pp on the CI lower bound):
  the FE judged gap is −9.7pp on the point estimate before the interval is
  even considered. Blocking gates hold — `json_schema_validity` is 1.000
  against a 0.99 floor. A quality gate failing while blocking gates hold is
  the **TUNE_FIRST** shape.

The counterfactual and the measurement point at different verdicts for the
same subagent on the same evidence. That is the finding.

## Two honest qualifications

1. **The anticipated harm and the actual harm differ.** The 08-07 note argued
   the worst effect of exact match was filing correct paraphrases next to
   genuine fabrications in `hallucination_rate`. In this run that did not
   happen: `hallucination_rate` is 0.000 under both definitions, because no
   item in this 10-item subset has a null gold for either rerouted field, so
   the paraphrase-as-fabrication case never arose. The harm that *did*
   materialise is paraphrase-as-`wrong`, which is less alarming per item and
   did more damage in aggregate — it is what flattened both arms to ~9/10
   failures and handed the ranking to the six mechanical fields. The reroute
   was right; the strongest argument for it is the one in this document, not
   the one anticipated.
2. **The judge is the noisiest instrument here.** FE `full_agreement_rate` is
   0.8 for both Gemini arms against 1.0 on every QR and CS arm. This
   counterfactual shows exact match was the wrong instrument; it does not by
   itself establish that the judge is a sufficient one. The dual-judge
   cross-check ((c) in [day1_failures.md](day1_failures.md)) runs before the
   judged gap is treated as settled.

## Reproducing it

```bash
python scripts/_counterfactual_fe.py     # replay only, no credentials, no live calls
```

One trap, recorded because the first attempt hit it: `FE_FIELDS` reaches the
scoring functions as a **default argument**, bound at definition time.
Reassigning the module attribute changes nothing and silently reproduces the
published post-reroute numbers. The script patches `__defaults__` /
`__kwdefaults__` and then asserts on a probe case that the patch took, before
computing anything.
