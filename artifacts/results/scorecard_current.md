# Migration Readiness Scorecard — Demo — Patents RAG

**REPLAY — every number below comes from model calls recorded 2026-08-09T16:07:15+00:00 to 2026-08-12T04:28:21+00:00, not from a run just now.**

Gates are checked against 95% confidence-range bounds, so a passing gate licenses "quality parity within measurement under pre-agreed gates" — never "zero quality drop".

## Verdicts

| Subagent | Baseline | Candidate | Gates evaluated | Verdict | Why |
| --- | --- | --- | --- | --- | --- |
| Chunk Summarizer | `claude_baseline` | `gemini_tuned_v1` | 6 of 6 | **UNDETERMINED** | gate(s) cost_savings_pct, quality_delta_pp failed. That pattern matches no verdict rule in gates.yaml — they are neither quality gates nor blocking gates — so no verdict is issued and the gates file needs a rule for it. |
| Feature Extractor | `claude_baseline` | `gemini_optimizer_v1` | 6 of 6 | **UNDETERMINED** | gate(s) cost_savings_pct, latency_p95 failed. That pattern matches no verdict rule in gates.yaml — they are neither quality gates nor blocking gates — so no verdict is issued and the gates file needs a rule for it. shadow_agreement missed its confidence-range bound (ci_lower = 0.8857 vs 0.9) and cleared on the alt clause pre-registered in gates.yaml ("on disagreements, judge-adjudicated wins >= losses"), measured at 31W/16L overall; 28W/16L excluding structurally malformed `claude_baseline` emissions; passes on either figure. |
| Query Rewriter | `claude_baseline` | `gemini_targeted_v1` | 5 of 6 | **INCOMPLETE** (provisional: UNDETERMINED) | 5 of 6 pre-agreed gates were measured; groundedness_delta_pp were not. A verdict over a subset of the gates is not the verdict that was agreed, so none is issued. Were every unmeasured gate to pass, it would be UNDETERMINED. shadow_agreement missed its confidence-range bound (ci_lower = 0.4714 vs 0.9) and cleared on the alt clause pre-registered in gates.yaml ("on disagreements, judge-adjudicated wins >= losses"), measured at 16W/2L overall; 10W/2L excluding structurally malformed `claude_baseline` emissions; passes on either figure. |

## Chunk Summarizer

| Gate | Bound (gates.yaml) | Measured (95% confidence range) | Bound tested | Result |
| --- | --- | --- | --- | --- |
| `quality_delta_pp` | `>= -2.0` | -1.61 pp [-3.93 pp, +0.89 pp] (paired n=70) | ci_lower = -3.929 | **FAIL** — parity not demonstrated |
| `json_schema_validity` | `>= 0.99` | 1.000 [1.000, 1.000] | ci_lower = 1 | PASS |
| `groundedness_delta_pp` | `>= -1.0` | +0.00 pp [+0.00 pp, +0.00 pp] (paired n=68, 2 unpaired dropped) | ci_lower = 0 | PASS |
| `shadow_agreement` | `>= 0.9` | 0.971 [0.929, 1.000] | ci_lower = 0.9286 | PASS |
| `cost_savings_pct` | `>= 30.0` | -6.6% [-11.7%, -1.7%] | ci_lower = -11.68 | **FAIL** |
| `latency_p95` | `<= claude_baseline_p95` | 6,314 ms [4,898 ms, 7,016 ms] (same-region probe, global) | ci_upper = 7016 | PASS |

A `quality_delta_pp` failure marked *parity not demonstrated* means the confidence interval spans zero: the gate fails on **precision**, because the data cannot rule out a drop larger than the bound — not because a drop was demonstrated. A failure marked *measured regression* is the stronger finding: the entire interval is below zero, so a real drop was measured. Reporting the first as though it were the second overstates a negative result, which is the same error as overstating a positive one.

### Evidence

| Evidence | Value |
| --- | --- |
| Claude `json_schema_validity` (`claude_baseline`) | 0.971 [0.929, 1.000] — tool-use JSON; native structured outputs unavailable under this org's policy — not the model's ceiling. |
| Gemini `json_schema_validity` (`gemini_tuned_v1`) | 1.000 [1.000, 1.000] |
| Judge score — Claude | 0.918 [0.879, 0.954] (judged n=70, split=all) |
| Judge score — Gemini | 0.902 [0.866, 0.936] (judged n=70, split=all) |
| Latency p95 | 6,314 ms [4,898 ms, 7,016 ms] (same-region probe, global) |
| Cost per call | not measured |
| Monthly run rate | not measured |
| Annual run rate | not measured |
| Cost savings vs Claude | -6.6% [-11.7%, -1.7%] — measured per-call tokens over this corpus at list prices, not the registered profile-volume basis |

- groundedness_delta_pp is measured here as **citation coverage** — the share of key points citing at least one chunk that was actually supplied. A point citing a chunk that was never supplied is a fabricated citation and does not count as grounded.
- citation_coverage scored an identical 1.000 on every item of both arms (68 and 70 scored items), so the paired delta and its interval are a single point rather than a tight measurement. The gate passes because neither arm did what the metric counts, not because the instrument resolved a difference between them.
- shadow_agreement counts **structured fields only** — the fields with a defined right answer. Prose fields are excluded here and adjudicated separately in the disagreement triage; this figure is not a claim that the prose matched.

## Feature Extractor

| Gate | Bound (gates.yaml) | Measured (95% confidence range) | Bound tested | Result |
| --- | --- | --- | --- | --- |
| `quality_delta_pp` | `>= -2.0` | +5.04 pp [+0.69 pp, +9.44 pp] (paired n=70) | ci_lower = 0.687 | PASS |
| `json_schema_validity` | `>= 0.99` | 1.000 [1.000, 1.000] | ci_lower = 1 | PASS |
| `groundedness_delta_pp` | `>= -1.0` | +0.00 pp [+0.00 pp, +0.00 pp] (paired n=66, 2 unpaired dropped) | ci_lower = 0 | PASS |
| `shadow_agreement` | `>= 0.9` | 0.943 [0.886, 0.986] | ci_lower = 0.8857 | PASS (by pre-registered alt clause: adjudication 31W/16L) |
| `cost_savings_pct` | `>= 30.0` | -23.1% [-29.5%, -16.9%] | ci_lower = -29.45 | **FAIL** |
| `latency_p95` | `<= claude_baseline_p95` | 7,473 ms [6,075 ms, 7,796 ms] (same-region probe, global) | ci_upper = 7796 | **FAIL** |

`shadow_agreement` did not clear its confidence-range bound (ci_lower = 0.8857, bound ≥ 0.9). It passes on the alternative route pre-registered in gates.yaml — "on disagreements, judge-adjudicated wins >= losses" — measured at 31W/16L overall; 28W/16L excluding structurally malformed `claude_baseline` emissions; passes on either figure. Under this demo organization's Vertex AI policy configuration (`constraints/vertexai.allowedPartnerModelFeatures`), partner-model structured outputs were unavailable, so the Claude baseline was measured using tool-call structured emission; the excluded items are ones where that emission was structurally broken (see `amw/shadow/emission.py`), not ones where the baseline merely answered worse. The clause was written before any of this was measured; it is the pre-agreed second route, not a threshold chosen after seeing the result.

### Evidence

| Evidence | Value |
| --- | --- |
| Claude `json_schema_validity` (`claude_baseline`) | 0.957 [0.900, 1.000] — tool-use JSON; native structured outputs unavailable under this org's policy — not the model's ceiling. |
| Gemini `json_schema_validity` (`gemini_optimizer_v1`) | 1.000 [1.000, 1.000] |
| Judge score — Claude | 0.900 [0.868, 0.929] (judged n=70, split=all) |
| Judge score — Gemini | 0.950 [0.926, 0.970] (judged n=70, split=all) |
| Latency p95 | 7,473 ms [6,075 ms, 7,796 ms] (same-region probe, global) |
| Cost per call | not measured |
| Monthly run rate | not measured |
| Annual run rate | not measured |
| Cost savings vs Claude | -23.1% [-29.5%, -16.9%] — measured per-call tokens over this corpus at list prices, not the registered profile-volume basis |

- groundedness_delta_pp is measured here as **source-supported claim rate**, not citation coverage: Feature Extractor emits field values rather than citations, so groundedness is the share of the fields the model chose to assert for which the source states something. Asserting a value the source never mentions is the extraction equivalent of a fabricated citation. Reading a wrong-but-in-source value counts as grounded and is scored by quality_delta_pp instead.
- supported_claim_rate scored an identical 1.000 on every item of both arms (66 and 68 scored items), so the paired delta and its interval are a single point rather than a tight measurement. The gate passes because neither arm did what the metric counts, not because the instrument resolved a difference between them.
- shadow_agreement counts **structured fields only** — the fields with a defined right answer. Prose fields are excluded here and adjudicated separately in the disagreement triage; this figure is not a claim that the prose matched.

## Query Rewriter

| Gate | Bound (gates.yaml) | Measured (95% confidence range) | Bound tested | Result |
| --- | --- | --- | --- | --- |
| `quality_delta_pp` | `>= -2.0` | +7.86 pp [+3.75 pp, +12.32 pp] (paired n=70) | ci_lower = 3.75 | PASS |
| `json_schema_validity` | `>= 0.99` | 1.000 [1.000, 1.000] | ci_lower = 1 | PASS |
| `groundedness_delta_pp` | `>= -1.0` | not evaluated — query_rewriter has no groundedness instrument. Its output is a search plan, which makes no claim about the input that could be supported or unsupported by it, so groundedness_delta_pp was not measured — not passed | not evaluated | not evaluated |
| `shadow_agreement` | `>= 0.9` | 0.586 [0.471, 0.700] | ci_lower = 0.4714 | PASS (by pre-registered alt clause: adjudication 16W/2L) |
| `cost_savings_pct` | `>= 30.0` | -16.5% [-23.6%, -9.8%] | ci_lower = -23.65 | **FAIL** |
| `latency_p95` | `<= claude_baseline_p95` | 6,222 ms [3,671 ms, 6,924 ms] (same-region probe, global) | ci_upper = 6924 | **FAIL** |

`shadow_agreement` did not clear its confidence-range bound (ci_lower = 0.4714, bound ≥ 0.9). It passes on the alternative route pre-registered in gates.yaml — "on disagreements, judge-adjudicated wins >= losses" — measured at 16W/2L overall; 10W/2L excluding structurally malformed `claude_baseline` emissions; passes on either figure. Under this demo organization's Vertex AI policy configuration (`constraints/vertexai.allowedPartnerModelFeatures`), partner-model structured outputs were unavailable, so the Claude baseline was measured using tool-call structured emission; the excluded items are ones where that emission was structurally broken (see `amw/shadow/emission.py`), not ones where the baseline merely answered worse. The clause was written before any of this was measured; it is the pre-agreed second route, not a threshold chosen after seeing the result.

### Evidence

| Evidence | Value |
| --- | --- |
| Claude `json_schema_validity` (`claude_baseline`) | 0.814 [0.714, 0.900] — tool-use JSON; native structured outputs unavailable under this org's policy — not the model's ceiling. |
| Gemini `json_schema_validity` (`gemini_targeted_v1`) | 1.000 [1.000, 1.000] |
| Judge score — Claude | 0.886 [0.838, 0.932] (judged n=70, split=all) |
| Judge score — Gemini | 0.964 [0.939, 0.986] (judged n=70, split=all) |
| Latency p95 | 6,222 ms [3,671 ms, 6,924 ms] (same-region probe, global) |
| Cost per call | not measured |
| Monthly run rate | not measured |
| Annual run rate | not measured |
| Cost savings vs Claude | -16.5% [-23.6%, -9.8%] — measured per-call tokens over this corpus at list prices, not the registered profile-volume basis |

**Not evaluated, and why**

- `groundedness_delta_pp` — query_rewriter has no groundedness instrument. Its output is a search plan, which makes no claim about the input that could be supported or unsupported by it, so groundedness_delta_pp was not measured — not passed

- shadow_agreement counts **structured fields only** — the fields with a defined right answer. Prose fields are excluded here and adjudicated separately in the disagreement triage; this figure is not a claim that the prose matched.

## Cost — configurations compared

Same model, same prompt bytes, same corpus. One setting apart.

| Configuration | Subagent | Savings vs Claude | Corpus cost | Output tokens (vs Claude) |
| --- | --- | --- | --- | --- |
| **reasoning budget minimised** — recommended | Chunk Summarizer | +56.1% [+54.5, +57.5] | $0.7168 → $0.3149 | 21,668 (0.55x) |
| **reasoning budget minimised** — recommended | Feature Extractor | +64.8% [+63.8, +65.7] | $0.6777 → $0.2386 | 11,050 (0.31x) |
| **reasoning budget minimised** — recommended | Query Rewriter | +42.7% [+40.3, +45.3] | $0.4723 → $0.2704 | 5,584 (0.34x) |
| default reasoning budget | Chunk Summarizer | -6.6% [-11.7, -1.7] | $0.7168 → $0.7640 | 81,549 (2.08x) |
| default reasoning budget | Feature Extractor | -23.1% [-29.5, -16.9] | $0.6777 → $0.8340 | 90,441 (2.54x) |
| default reasoning budget | Query Rewriter | -16.5% [-23.6, -9.8] | $0.4723 → $0.5502 | 42,891 (2.63x) |

- **The thinking tax.** Both rows are the same provider model ID (`gemini-3.6-flash`) on the same prompt bytes over the same 70 items. The only difference is `thinking_config.thinking_budget`. The default-budget row bills reasoning tokens that are never returned to the caller — they are folded into billable output tokens and cannot be separated after the fact — which is what moves the savings column across the gate line.
- **Market context.** Reasoning-by-default is the current direction of travel for frontier models on both sides of this comparison. A cost estimate built on a default-settings call is therefore an estimate of a moving target; the recommended row is the configuration a deployment would actually pin.
- Neither row is a quality claim. Quality, schema validity and latency for the recommended configuration are measured in the gate table above, on the same recorded calls these dollar figures are summed from.

## Economics

**not computable — volumes unconfirmed.** No dollar figure is produced — not a zero, not a placeholder. The gate below has to be cleared by a human.

| Gate | Why it is closed | Clears when |
| --- | --- | --- |
| volumes | the customer profile's volume block is illustrative (volumes_confirmed: false) | the customer states their call volumes — economics.confirm_volumes() or `cli.py scorecard --volume` |

Volume basis: volumes: illustrative — 3 evaluated subagent(s), x0.5/x1/x2 sensitivity band and cached/uncached rows ready to run the moment both gates clear.

## Cost projection — Claude Opus 5

**Projection, not a measurement.** No arm in this study ran on Claude Opus 5. These are the measured Claude Sonnet 5 token counts re-priced — they assume an Opus run would emit the same tokens, which nothing here measured. Read them as an order-of-magnitude sizing for a customer whose incumbent subagents are on Opus, not as a result. No quality, schema, agreement or latency claim is made about Opus anywhere in this report.

Basis: the *incumbent* arm's recorded input and output token counts over this 70-item corpus, re-priced at the Claude Opus 5 uncached list rates in `config/pricing.yaml` ($5/$25 per 1M in/out, verified 2026-08-12). Claude Sonnet 5's measured column is shown beside it for scale. The difference column is the same figure on every row because Opus 5 lists at a flat multiple of Sonnet 5 on both the input and the output rate — that is a property of the price list, not a coincidence in the data.

| Subagent | Measured (as run) | Projected | Difference |
| --- | --- | --- | --- |
| Query Rewriter | $0.4723 | $1.1807 | +150.0% |
| Chunk Summarizer | $0.7168 | $1.7919 | +150.0% |
| Feature Extractor | $0.6777 | $1.6942 | +150.0% |

## Footer

Verdicts apply to each subagent's measured behavior class (Level 1 single-call transforms, measured in full here); tool-selection and multi-step trajectory behaviors are evaluated with their own instruments in the follow-on and receive no verdict today.

| Field | Value |
| --- | --- |
| Customer | Demo — Patents RAG (`demo_patents`) |
| Provenance | synthetic, generator `t06.1`, dataset seed `20260812` |
| Bootstrap | 95% confidence range, seed `20260812` |
| Judge | Gemini 2.5 Pro, prompt `v1`, k=2 repeats |
| Mode | `replay` |
| Run date | no live run — assembled from recordings |
| Recording window | 2026-08-09T16:07:15+00:00 to 2026-08-12T04:28:21+00:00 |
| Region(s) | Claude `global`, Gemini + judge `global` (source: config/models.yaml region pin where set, else $CLAUDE_REGION / $REGION, falling back to the customer profile; claude-sonnet -> global, gemini-flash-current -> global) |
| Prices verified on | 2026-08-12 |
| Pricing sources | https://cloud.google.com/vertex-ai/generative-ai/pricing, https://cloud.google.com/vertex-ai/generative-ai/pricing#claude-models |
| Volumes | volumes: illustrative |
| Gates | version 1, hash `92f9d018432f` |
| Models | [Models in this study](../models-in-this-study.md) — every arm's exact model ID, access path, part in the study, and recording window |

**Run notes**

- judged on the FULL corpus, not the registered core split: chunk_summarizer, feature_extractor, query_rewriter. Every other subagent is judged on core only, so judged n differs across subagents — see each arm's judge.split and judge.items_scored before comparing judge scores.
- Arms assembled for the gemini-flash-current scorecard: claude_baseline from phase2_n70_widened.json (full-70 widening) and each subagent's ship-current rung from artifacts/results/ablation_*.json. Both sets were scored by the phase-2 scorer; this assembly copies them and recomputes nothing.
- cost_savings_pct basis: measured per-call tokens over this corpus, uncached list prices from config/pricing.yaml — NOT the registered profile-volume basis, which stays uncomputed while customer volumes are unconfirmed
