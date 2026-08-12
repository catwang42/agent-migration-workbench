# Migration Readiness Scorecard — Demo — Patents RAG

**REPLAY — every number below comes from model calls recorded 2026-08-09T16:07:15+00:00 to 2026-08-11T06:20:37+00:00, not from a run just now.**

Gates are checked against 95% CI bounds, so a passing gate licenses "quality parity within measurement under pre-agreed gates" — never "zero quality drop".

## Verdicts

| Subagent | Baseline | Candidate | Gates evaluated | Verdict | Why |
| --- | --- | --- | --- | --- | --- |
| Chunk Summarizer | `claude_baseline` | `gemini_tuned_v1` | 3 of 6 | **INCOMPLETE** (provisional: TUNE_FIRST) | 3 of 6 pre-agreed gates were measured; cost_savings_pct, latency_p95, shadow_agreement were not. A verdict over a subset of the gates is not the verdict that was agreed, so none is issued. Were every unmeasured gate to pass, it would be TUNE_FIRST. |
| Feature Extractor | `claude_baseline` | `gemini_tuned_v1` | 3 of 6 | **INCOMPLETE** (provisional: TUNE_FIRST) | 3 of 6 pre-agreed gates were measured; cost_savings_pct, latency_p95, shadow_agreement were not. A verdict over a subset of the gates is not the verdict that was agreed, so none is issued. Were every unmeasured gate to pass, it would be TUNE_FIRST. |
| Query Rewriter | `claude_baseline` | `gemini_tuned_v1` | 2 of 6 | **INCOMPLETE** (provisional: TUNE_FIRST) | 2 of 6 pre-agreed gates were measured; cost_savings_pct, groundedness_delta_pp, latency_p95, shadow_agreement were not. A verdict over a subset of the gates is not the verdict that was agreed, so none is issued. Were every unmeasured gate to pass, it would be TUNE_FIRST. |

## Chunk Summarizer

| Gate | Bound (gates.yaml) | Measured (95% CI) | Bound tested | Result |
| --- | --- | --- | --- | --- |
| `quality_delta_pp` | `>= -2.0` | -2.32 pp [-5.00 pp, +0.36 pp] (paired n=70) | ci_lower = -5 | **FAIL** — parity not demonstrated |
| `json_schema_validity` | `>= 0.99` | 1.000 [1.000, 1.000] | ci_lower = 1 | PASS |
| `groundedness_delta_pp` | `>= -1.0` | +0.00 pp [+0.00 pp, +0.00 pp] (paired n=68, 2 unpaired dropped) | ci_lower = 0 | PASS |
| `shadow_agreement` | `>= 0.9` | not evaluated — no shadow run in this artifact set — shadow_agreement is produced by `cli.py shadow`, which has not been run for this corpus | not evaluated | not evaluated |
| `cost_savings_pct` | `>= 30.0` | not measured | not evaluated | not evaluated |
| `latency_p95` | `<= claude_baseline_p95` | not comparable — region split disclosed | not evaluated | not evaluated |

A `quality_delta_pp` failure marked *parity not demonstrated* means the confidence interval spans zero: the gate fails on **precision**, because the data cannot rule out a drop larger than the bound — not because a drop was demonstrated. A failure marked *measured regression* is the stronger finding: the entire interval is below zero, so a real drop was measured. Reporting the first as though it were the second overstates a negative result, which is the same error as overstating a positive one.

### Evidence

| Evidence | Value |
| --- | --- |
| Claude `json_schema_validity` (`claude_baseline`) | 0.971 [0.929, 1.000] — tool-use JSON; native structured outputs unavailable under this org's policy — not the model's ceiling. |
| Gemini `json_schema_validity` (`gemini_tuned_v1`) | 1.000 [1.000, 1.000] |
| Judge score — Claude | 0.918 [0.879, 0.954] (judged n=70, split=all) |
| Judge score — Gemini | 0.895 [0.857, 0.929] (judged n=70, split=all) |
| Latency p95 | not comparable — region split disclosed |
| Cost per call | not measured |
| Monthly run rate | not measured |
| Annual run rate | not measured |
| Cost savings vs Claude | not measured |

**Not evaluated, and why**

- `cost_savings_pct` — not computable — volumes unconfirmed
- `latency_p95` — not comparable — region split disclosed: Claude ran in global, Gemini in us-central1. The claude_baseline_p95 sentinel resolves only from a same-region probe, so this gate is not evaluated — it is not passed.
- `shadow_agreement` — no shadow run in this artifact set — shadow_agreement is produced by `cli.py shadow`, which has not been run for this corpus

- groundedness_delta_pp is measured here as **citation coverage** — the share of key points citing at least one chunk that was actually supplied. A point citing a chunk that was never supplied is a fabricated citation and does not count as grounded.
- citation_coverage scored an identical 1.000 on every item of both arms (68 and 70 scored items), so the paired delta and its interval are a single point rather than a tight measurement. The gate passes because neither arm did what the metric counts, not because the instrument resolved a difference between them.

### Ablation ladder

| Rung | Variant | Output mode | Judged score (95% CI) | `citation_coverage` | `fabricated_citation_rate` | `json_schema_validity` | `uncited_claim_rate` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `baseline` | `claude_baseline` | `tool` | 0.902 [0.830, 0.956] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 0.964 [0.893, 1.000] | 0.000 [0.000, 0.000] |
| `A0` | `gemini_naive` | `tool` | 0.897 [0.830, 0.955] (judged n=28, split=core) | 0.963 [0.889, 1.000] | 0.000 [0.000, 0.000] | 0.964 [0.893, 1.000] | 0.000 [0.000, 0.000] |
| `A1-A3` | `gemini_tuned_v1` | `response_schema` | 0.915 [0.871, 0.955] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `A0-current` | `gemini_naive` | `tool` | 0.902 [0.864, 0.936] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `ship-current` | `gemini_tuned_v1` | `response_schema` | 0.902 [0.866, 0.936] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |

The ladder reports every rung that was run, including rungs that did not help and rungs that were never measured. It ranks nothing and selects nothing — the shipping arm is chosen against the pre-agreed gates on the gated rows above, not against this table.

Rungs in this ladder were scored on **different splits** (`core`, `all`). A score at one n and a score at another are two instruments, not two points on one curve: read down a split, not across the table. The split travels with every number above for exactly this reason.

## Feature Extractor

| Gate | Bound (gates.yaml) | Measured (95% CI) | Bound tested | Result |
| --- | --- | --- | --- | --- |
| `quality_delta_pp` | `>= -2.0` | -10.44 pp [-13.78 pp, -7.12 pp] (paired n=70) | ci_lower = -13.78 | **FAIL** — measured regression |
| `json_schema_validity` | `>= 0.99` | 1.000 [1.000, 1.000] | ci_lower = 1 | PASS |
| `groundedness_delta_pp` | `>= -1.0` | +0.00 pp [+0.00 pp, +0.00 pp] (paired n=66, 2 unpaired dropped) | ci_lower = 0 | PASS |
| `shadow_agreement` | `>= 0.9` | not evaluated — no shadow run in this artifact set — shadow_agreement is produced by `cli.py shadow`, which has not been run for this corpus | not evaluated | not evaluated |
| `cost_savings_pct` | `>= 30.0` | not measured | not evaluated | not evaluated |
| `latency_p95` | `<= claude_baseline_p95` | not comparable — region split disclosed | not evaluated | not evaluated |

### Evidence

| Evidence | Value |
| --- | --- |
| Claude `json_schema_validity` (`claude_baseline`) | 0.957 [0.900, 1.000] — tool-use JSON; native structured outputs unavailable under this org's policy — not the model's ceiling. |
| Gemini `json_schema_validity` (`gemini_tuned_v1`) | 1.000 [1.000, 1.000] |
| Judge score — Claude | 0.900 [0.868, 0.929] (judged n=70, split=all) |
| Judge score — Gemini | 0.795 [0.760, 0.828] (judged n=70, split=all) |
| Latency p95 | not comparable — region split disclosed |
| Cost per call | not measured |
| Monthly run rate | not measured |
| Annual run rate | not measured |
| Cost savings vs Claude | not measured |

**Not evaluated, and why**

- `cost_savings_pct` — not computable — volumes unconfirmed
- `latency_p95` — not comparable — region split disclosed: Claude ran in global, Gemini in us-central1. The claude_baseline_p95 sentinel resolves only from a same-region probe, so this gate is not evaluated — it is not passed.
- `shadow_agreement` — no shadow run in this artifact set — shadow_agreement is produced by `cli.py shadow`, which has not been run for this corpus

- groundedness_delta_pp is measured here as **source-supported claim rate**, not citation coverage: Feature Extractor emits field values rather than citations, so groundedness is the share of the fields the model chose to assert for which the source states something. Asserting a value the source never mentions is the extraction equivalent of a fabricated citation. Reading a wrong-but-in-source value counts as grounded and is scored by quality_delta_pp instead.
- supported_claim_rate scored an identical 1.000 on every item of both arms (66 and 68 scored items), so the paired delta and its interval are a single point rather than a tight measurement. The gate passes because neither arm did what the metric counts, not because the instrument resolved a difference between them.

### Ablation ladder

| Rung | Variant | Output mode | Judged score (95% CI) | `answered_precision` | `extraction_accuracy` | `hallucination_rate` | `json_schema_validity` | `omission_rate` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `baseline` | `claude_baseline` | `tool` | 0.903 [0.857, 0.946] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 0.929 [0.821, 1.000] | 0.000 [0.000, 0.000] | 0.929 [0.821, 1.000] | 0.074 [0.000, 0.185] |
| `A0` | `gemini_naive` | `tool` | 0.837 [0.791, 0.882] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `A1-A3` | `gemini_tuned_v1` | `response_schema` | 0.807 [0.756, 0.853] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `A0-schema` | `gemini_naive_schema` | `response_schema` | 0.826 [0.773, 0.875] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `A0-schema` | `gemini_naive_schema` | `response_schema` | 0.831 [0.798, 0.862] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `A4-novelty-tool` | `gemini_novelty_v1_tool` | `tool` | 0.901 [0.859, 0.940] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `A4-novelty-tool` | `gemini_novelty_v1_tool` | `tool` | 0.892 [0.864, 0.918] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `A4-novelty-schema` | `gemini_novelty_v1_schema` | `response_schema` | 0.920 [0.878, 0.957] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `A4-novelty-schema` | `gemini_novelty_v1_schema` | `response_schema` | 0.903 [0.876, 0.929] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `A4-optimizer` | `gemini_optimizer_v1` | `tool` | 0.949 [0.911, 0.979] (judged n=28, split=core) | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `A4-optimizer` | `gemini_optimizer_v1` | `tool` | 0.951 [0.929, 0.970] (judged n=70, split=all) † | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `A0-current` | `gemini_naive` | `tool` | 0.827 [0.790, 0.861] (judged n=70, split=all) | 0.997 [0.991, 1.000] | 0.998 [0.993, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] |
| `ship-current` | `gemini_optimizer_v1` | `tool` | 0.950 [0.926, 0.970] (judged n=70, split=all) | 1.000 [1.000, 1.000] | 0.995 [0.988, 1.000] | 0.000 [0.000, 0.000] | 1.000 [1.000, 1.000] | 0.005 [0.000, 0.012] |

The ladder reports every rung that was run, including rungs that did not help and rungs that were never measured. It ranks nothing and selects nothing — the shipping arm is chosen against the pre-agreed gates on the gated rows above, not against this table.

Rungs in this ladder were scored on **different splits** (`core`, `all`). A score at one n and a score at another are two instruments, not two points on one curve: read down a split, not across the table. The split travels with every number above for exactly this reason.

† `A4-optimizer` quotes `fe-0004`, `fe-0021`, `fe-0029`, `fe-0032`, `fe-0037`, `fe-0041`, `fe-0046`, `fe-0051`, `fe-0056`, `fe-0060`, `fe-0064`, `fe-0067` as a worked example, and those items are inside the split it was scored on (12 of 70). Its judged score is optimistic there by construction. The items are not excluded — that would give this rung a different denominator from every other rung, which is a worse problem than a disclosed one.

## Query Rewriter

| Gate | Bound (gates.yaml) | Measured (95% CI) | Bound tested | Result |
| --- | --- | --- | --- | --- |
| `quality_delta_pp` | `>= -2.0` | -0.64 pp [-5.50 pp, +4.32 pp] (paired n=70) | ci_lower = -5.501 | **FAIL** — parity not demonstrated |
| `json_schema_validity` | `>= 0.99` | 1.000 [1.000, 1.000] | ci_lower = 1 | PASS |
| `groundedness_delta_pp` | `>= -1.0` | not evaluated — query_rewriter has no groundedness instrument. Its output is a search plan, which makes no claim about the input that could be supported or unsupported by it, so groundedness_delta_pp was not measured — not passed | not evaluated | not evaluated |
| `shadow_agreement` | `>= 0.9` | not evaluated — no shadow run in this artifact set — shadow_agreement is produced by `cli.py shadow`, which has not been run for this corpus | not evaluated | not evaluated |
| `cost_savings_pct` | `>= 30.0` | not measured | not evaluated | not evaluated |
| `latency_p95` | `<= claude_baseline_p95` | not comparable — region split disclosed | not evaluated | not evaluated |

A `quality_delta_pp` failure marked *parity not demonstrated* means the confidence interval spans zero: the gate fails on **precision**, because the data cannot rule out a drop larger than the bound — not because a drop was demonstrated. A failure marked *measured regression* is the stronger finding: the entire interval is below zero, so a real drop was measured. Reporting the first as though it were the second overstates a negative result, which is the same error as overstating a positive one.

### Evidence

| Evidence | Value |
| --- | --- |
| Claude `json_schema_validity` (`claude_baseline`) | 0.814 [0.714, 0.900] — tool-use JSON; native structured outputs unavailable under this org's policy — not the model's ceiling. |
| Gemini `json_schema_validity` (`gemini_tuned_v1`) | 1.000 [1.000, 1.000] |
| Judge score — Claude | 0.886 [0.838, 0.932] (judged n=70, split=all) |
| Judge score — Gemini | 0.879 [0.843, 0.914] (judged n=70, split=all) |
| Latency p95 | not comparable — region split disclosed |
| Cost per call | not measured |
| Monthly run rate | not measured |
| Annual run rate | not measured |
| Cost savings vs Claude | not measured |

**Not evaluated, and why**

- `cost_savings_pct` — not computable — volumes unconfirmed
- `groundedness_delta_pp` — query_rewriter has no groundedness instrument. Its output is a search plan, which makes no claim about the input that could be supported or unsupported by it, so groundedness_delta_pp was not measured — not passed
- `latency_p95` — not comparable — region split disclosed: Claude ran in global, Gemini in us-central1. The claude_baseline_p95 sentinel resolves only from a same-region probe, so this gate is not evaluated — it is not passed.
- `shadow_agreement` — no shadow run in this artifact set — shadow_agreement is produced by `cli.py shadow`, which has not been run for this corpus

### Ablation ladder

| Rung | Variant | Output mode | Judged score (95% CI) | `exact_match_intent` | `filter_f1` | `filter_precision` | `filter_recall` | `json_schema_validity` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `baseline` | `claude_baseline` | `tool` | 0.911 [0.839, 0.964] (judged n=28, split=core) | 0.786 [0.607, 0.929] | 0.985 [0.955, 1.000] | 0.977 [0.932, 1.000] | 0.815 [0.667, 0.963] | 0.893 [0.750, 1.000] |
| `A0` | `gemini_naive` | `tool` | 0.836 [0.787, 0.886] (judged n=28, split=core) | 0.536 [0.357, 0.714] | 0.961 [0.921, 0.991] | 0.949 [0.894, 0.991] | 0.986 [0.972, 1.000] | 0.964 [0.893, 1.000] |
| `A1-A3` | `gemini_tuned_v1` | `response_schema` | 0.852 [0.796, 0.905] (judged n=28, split=core) | 0.714 [0.536, 0.893] | 0.948 [0.895, 0.988] | 0.935 [0.866, 0.986] | 0.972 [0.931, 1.000] | 1.000 [1.000, 1.000] |
| `A4-targeted` | `gemini_targeted_v1` | `response_schema` | 0.963 [0.934, 0.986] (judged n=70, split=all) | 0.957 [0.900, 1.000] | 0.969 [0.941, 0.992] | 0.933 [0.881, 0.978] | 0.985 [0.962, 1.000] | 1.000 [1.000, 1.000] |
| `A0-current` | `gemini_naive` | `tool` | 0.904 [0.871, 0.936] (judged n=70, split=all) | 0.614 [0.500, 0.729] | 0.990 [0.974, 1.000] | 0.941 [0.882, 0.985] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| `ship-current` | `gemini_targeted_v1` | `response_schema` | 0.964 [0.939, 0.986] (judged n=70, split=all) | 0.857 [0.771, 0.929] | 0.985 [0.964, 1.000] | 0.934 [0.875, 0.978] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |

The ladder reports every rung that was run, including rungs that did not help and rungs that were never measured. It ranks nothing and selects nothing — the shipping arm is chosen against the pre-agreed gates on the gated rows above, not against this table.

Rungs in this ladder were scored on **different splits** (`core`, `all`). A score at one n and a score at another are two instruments, not two points on one curve: read down a split, not across the table. The split travels with every number above for exactly this reason.

## Economics

**not computable — volumes unconfirmed.** No dollar figure is produced — not a zero, not a placeholder. The gate below has to be cleared by a human.

| Gate | Why it is closed | Clears when |
| --- | --- | --- |
| volumes | the customer profile's volume block is illustrative (volumes_confirmed: false) | the customer states their call volumes — economics.confirm_volumes() or `cli.py scorecard --volume` |

Volume basis: volumes: illustrative — 3 evaluated subagent(s), x0.5/x1/x2 sensitivity band and cached/uncached rows ready to run the moment both gates clear.

## Footer

Verdicts apply to each subagent's measured behavior class (Level 1 single-call transforms, measured in full here); tool-selection and multi-step trajectory behaviors are evaluated with their own instruments in the follow-on and receive no verdict today.

| Field | Value |
| --- | --- |
| Customer | Demo — Patents RAG (`demo_patents`) |
| Provenance | synthetic, generator `t06.1`, dataset seed `20260812` |
| Bootstrap | 95% CI, seed `20260812` |
| Judge | Gemini 2.5 Pro, prompt `v1`, k=2 repeats |
| Mode | `replay` |
| Run date | no live run — assembled from recordings |
| Recording window | 2026-08-09T16:07:15+00:00 to 2026-08-11T06:20:37+00:00 |
| Region(s) | Claude `global`, Gemini + judge `us-central1` (source: $CLAUDE_REGION / $REGION, falling back to the customer profile) |
| Prices verified on | 2026-08-12 |
| Pricing sources | https://cloud.google.com/vertex-ai/generative-ai/pricing, https://cloud.google.com/vertex-ai/generative-ai/pricing#claude-models |
| Volumes | volumes: illustrative |
| Gates | version 1, hash `92f9d018432f` |
| Models | [Models in this study](../models-in-this-study.md) — every arm's exact model ID, access path, part in the study, and recording window |

**Run notes**

- judged on the FULL corpus, not the registered core split: chunk_summarizer, feature_extractor, query_rewriter. Every other subagent is judged on core only, so judged n differs across subagents — see each arm's judge.split and judge.items_scored before comparing judge scores.
