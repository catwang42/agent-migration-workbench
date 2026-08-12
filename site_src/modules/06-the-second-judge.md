# Module 06 — The second judge

*~5 min read · [05 The adaptation ladder](05-adaptation-ladder.md) → **06** → [07 Shadow & adjudication](07-shadow-and-adjudication.md)*

---

--8<-- "_includes/development-generation.md"

The obvious objection to this whole exercise: a Gemini model is grading Gemini's
output. That is a real methodological weakness, it is the biggest one on this
scorecard, and the right response is not to deny it. It is to bound it, publish
the bound, and let a second vendor's judge check the work.

## What bounds the gated judge

- **Rubric-anchored per item, not free-form.** The judge is not asked "which is
  better". It is handed a rubric written from the gold reference *before* either
  model runs, and asked criterion by criterion. Judge prompts are versioned text
  files in `amw/eval/judge_prompts/` — they are shown to customers, so read them.
- **Blind to the arm.** The judge does not know which model produced the text.
- **k=2 repeats, bootstrap CIs.** Self-preference that is not stable across
  repeats shows up as a wide interval rather than as a point estimate.
- **Registered before results.** Gemini 2.5 Pro, prompt `v1`, is the gated
  instrument, fixed before the run.
- **No verdict rests on the judge alone.** The judge is also the noisiest
  instrument here. The blocking gates — `json_schema_validity` and
  `shadow_agreement` — are deterministic.

There is also an empirical check, and it is the uncomfortable kind that is worth
more than an argument: on Feature Extractor the Gemini 2.5 Pro judge scored
**Claude Sonnet 5 higher** — 0.900 [0.868, 0.929] against **Gemini 2.5 Flash**
(`gemini_naive`) 0.821 [0.787, 0.854], non-overlapping at n=70. A judge with a
thumb on the scale does not hand the contested row to the competitor.

## The cross-check

```bash
python cli.py crosscheck
```

Claude Sonnet 5 (prompt `v1_crosscheck`, `tool` mode) re-scored the **same recorded
outputs** against the **same rubrics**. Run 11 Aug 2026, 11:52 AM (SGT); the
outputs re-scored were recorded 10 Aug 2026, 12:07 AM → 10 Aug 2026, 10:45 AM (SGT).

Run UTC: `2026-08-11T03:52:19+00:00` · outputs recorded `2026-08-09T16:07:18+00:00` → `2026-08-10T02:45:59+00:00`
{ .amw-provenance }

The **Model** column is the model whose *outputs* were re-scored. Both judges are
the same in every row: Gemini 2.5 Pro gated, Claude Sonnet 5 cross-check.

!!! warning "This validation ran on the development-generation arms only"

    The cross-check re-scored **Gemini 2.5 Flash** outputs, not the Gemini 3.6
    Flash outputs the scorecard gates on. It was not re-run against the deployment
    generation before freeze, and no result on this page is a claim about those
    arms.

    What it establishes is a property of the **instrument**, not of the arms: the
    gated judge and a second vendor's judge, on the same rubrics, agree at
    92.9–99.0%. The gated judge is the same model, same prompt version and same
    k=2 configuration on every arm in this study, so that bound is the reason to
    trust the instrument that produced the deployment-generation scores — but it
    is not itself a measurement of them.

| Subagent | Arm | Model scored | Criterion agreement | Cohen's kappa | Gated mean | Cross-check mean | Criterion pairs |
|---|---|---|---|---|---|---|---|
| query_rewriter | `claude_baseline` | **Claude Sonnet 5** | 100.0% | 1.000 | 0.929 | 0.929 | 114 |
| query_rewriter | `gemini_naive` | **Gemini 2.5 Flash** | 99.1% | 0.966 | 0.846 | 0.837 | 110 |
| query_rewriter | `gemini_tuned_v1` | **Gemini 2.5 Flash** | 97.1% | 0.826 | 0.890 | 0.920 | 102 |
| **query_rewriter** | **all arms** | both | **98.8%** | **0.936** | 0.889 | 0.895 | 326 |
| chunk_summarizer | `claude_baseline` | **Claude Sonnet 5** | 99.0% | 0.954 | 0.880 | 0.870 | 100 |
| chunk_summarizer | `gemini_naive` | **Gemini 2.5 Flash** | 98.9% | 0.946 | 0.875 | 0.886 | 88 |
| chunk_summarizer | `gemini_tuned_v1` | **Gemini 2.5 Flash** | 99.0% | 0.942 | 0.900 | 0.910 | 100 |
| **chunk_summarizer** | **all arms** | both | **99.0%** | **0.948** | 0.885 | 0.889 | 288 |
| feature_extractor | `claude_baseline` | **Claude Sonnet 5** | 93.0% | 0.678 | 0.897 | 0.857 | 853 |
| feature_extractor | `gemini_naive` | **Gemini 2.5 Flash** | 92.6% | 0.763 | 0.823 | 0.791 | 856 |
| feature_extractor | `gemini_tuned_v1` | **Gemini 2.5 Flash** | 93.0% | 0.797 | 0.795 | 0.763 | 873 |
| **feature_extractor** | **all arms** | both | **92.9%** | **0.758** | 0.838 | 0.803 | 2582 |

All three are **VALIDATED** against an 85% agreement threshold. Query Rewriter and
Chunk Summarizer on a 20% stratified sample of the 70-item corpus (14 items each,
drawn from the 28 in the gated judge's core split); Feature Extractor on the full
gated split, all arms.

## How to read kappa here

Cohen's kappa corrects raw agreement for the agreement you would get by chance. On
the Query Rewriter sample, chance agreement is already **0.808** — both judges pass
most criteria (pass rates 0.890 and 0.896) — so kappa is heavily
prevalence-deflated. It can read low next to a very high raw agreement, and that is
an artefact of the base rate, not a sign of disagreement.

**Read the two together, never kappa alone.** The cross-check report says so
explicitly on the query_rewriter row.

## The design rule

> The Gemini 2.5 Pro judge is the gated instrument and was registered before
> results were seen. The Claude cross-check judge validates it. The two are never
> averaged and one is never substituted for the other.

Averaging them would produce an instrument that was never registered, from which
you could then read whichever number you preferred. Substituting one for the other
after seeing results is the same move with an extra step.

## Where they actually disagree

The cross-check report prints the largest per-item disagreements with both
rationales side by side — that is the part worth reading, because the *reasons*
are more informative than the agreement rate.

The pattern on Feature Extractor is consistent: the Claude judge is systematically
**stricter** on `technical_field_correct` and `novelty_statement_correct`, failing
candidates whose field label restates the title where the gated judge accepts it.
For example, on `fe-0033` the gated judge passed "tandem photovoltaic devices" as
supported by the title; the cross-check judge failed it for omitting the
perovskite material system named in the claims.

That is a rubric-interpretation difference, it is visible, and it is the same
direction on both vendors' outputs — which is exactly why it does not overturn the
ranking.

The cross-check also reproduces the finding that matters most: on Feature
Extractor the Claude judge *also* ranks `claude_baseline` (Claude Sonnet 5) above
`gemini_naive` above `gemini_tuned_v1` (both Gemini 2.5 Flash). The contested
result is not an artefact of one judge.

[Read the full cross-check report](../results/crosscheck.md){ .md-button }

---

**Next:** [Module 07 — Shadow & adjudication](07-shadow-and-adjudication.md)

*Source: `artifacts/results/crosscheck.md`, `artifacts/results/crosscheck.json`, `config/models.yaml`. Gated judge: Gemini 2.5 Pro, prompt `v1`, `response_schema`. Cross-check judge: Claude Sonnet 5, prompt `v1_crosscheck`, `tool`.*
