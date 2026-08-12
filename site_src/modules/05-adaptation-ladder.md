# Module 05 — The adaptation ladder

*~7 min read · [04 The naive swap](04-the-naive-swap.md) → **05** → [06 The second judge](06-the-second-judge.md)*

---

"Adapt the prompt" is not one action, so it cannot be one number. The ladder runs
each adaptation as its own rung against the same corpus, so you can see which
change bought what — and, on one subagent here, which change **cost** something.

```bash
python cli.py ablate --subagent query_rewriter
```

## The rungs

| Rung | What changes | Output mode |
|---|---|---|
| `baseline` | Incumbent: the customer's XML prompt on Claude, `emit_*` tool | `tool` |
| `A0` | Naive endpoint swap: the same prompt bytes on Gemini, same tool | `tool` |
| `A1-A3` | Bundled: Markdown restructure + system/user split + enforced `response_schema` + two recalibrated few-shots | `response_schema` |
| `A0-schema` *(FE)* | **Mode only**: A0's prompt, enforced `response_schema` instead of the tool | `response_schema` |
| `A4-novelty-tool` *(FE)* | **Prompt only**: A0 plus the `novelty_statement` rule (claim 1 is the point of novelty; numeric limits survive) and one worked example | `tool` |
| `A4-novelty-schema` *(FE)* | **Both**: the novelty prompt under the enforced `response_schema` | `response_schema` |
| `A4-optimizer` *(FE)* | Optimizer: a VAIPO-suggested instruction over `gemini_naive`, read against `judge_score` | `tool` |
| `A4-targeted` *(QR)* | A1-A3 plus three bundled rules for the three measured loss clusters — publication numbers verbatim in `query`, explicit `date_to` copied not expanded, landscape vs ownership by which side of the question is unknown (per-rule credit is **not** isolated) | `response_schema` |

`A0-schema`, `A4-novelty-tool` and `A4-novelty-schema` exist as a 2×2: prompt
change alone, mode change alone, and both. Bundling them would have made "the
enforced schema helped" unfalsifiable.

## Read the split before you read the number

!!! warning "Two splits, two incumbents"

    The ablation ladder is scored on the **core 28**; the gated scorecard run is
    scored on the **full 70**. Feature Extractor's Claude incumbent is
    **0.903** on the core 28 and **0.900** on the full 70 — two measurements on
    two corpora, not one number rounded twice. **Never quote one against the
    other.** A rung measured at n=70 and compared to 0.903 is being scored
    against a corpus it did not run on.

The comparison column below is the rung's 95% CI lower bound against the
incumbent's point estimate *on the same split*. It is **unpaired**, and it is not
the `quality_delta_pp` gate — that gate is a paired bootstrap over per-item
differences, and it is what the verdict is decided on. This table informs the
selection; it does not make it.

## Query Rewriter

**Core 28** — incumbent `baseline` 0.911 [0.839, 0.964]

| Rung | Mode | Judged score (95% CI) | vs incumbent |
|---|---|---|---|
| `baseline` | `tool` | 0.911 [0.839, 0.964] | incumbent |
| `A0` | `tool` | 0.836 [0.787, 0.886] | below incumbent (hi 0.886 < 0.911) |
| `A1-A3` | `response_schema` | 0.852 [0.796, 0.905] | below incumbent (hi 0.905 < 0.911) |

**Full 70** — incumbent `gated:claude_baseline` 0.886 [0.838, 0.932]

| Rung / arm | Mode | Judged score (95% CI) | `exact_match_intent` | `json_schema_validity` | vs incumbent |
|---|---|---|---|---|---|
| `A4-targeted` | `response_schema` | 0.963 [0.934, 0.986] | 0.957 [0.900, 1.000] | 1.000 [1.000, 1.000] | **clears** (lo 0.934 > 0.886) |
| `gated:claude_baseline` | `tool` | 0.886 [0.838, 0.932] | 0.729 [0.629, 0.829] | 0.814 [0.714, 0.900] | incumbent |
| `gated:gemini_naive` | `tool` | 0.831 [0.797, 0.865] | 0.571 [0.457, 0.686] | 0.971 [0.929, 1.000] | below incumbent (hi 0.865 < 0.886) |
| `gated:gemini_tuned_v1` | `response_schema` | 0.879 [0.843, 0.914] | 0.814 [0.714, 0.900] | 1.000 [1.000, 1.000] | **recovery to parity** (lo 0.843 ≤ 0.886 ≤ hi 0.914) |

The `A4-targeted` rung was built from the *measured* loss clusters, not from
intuition. Its three rules were bundled, so no per-rule credit is claimed.

## Chunk Summarizer

**Core 28** — incumbent `baseline` 0.902 [0.830, 0.956]

| Rung | Mode | Judged score (95% CI) | `citation_coverage` | vs incumbent |
|---|---|---|---|---|
| `baseline` | `tool` | 0.902 [0.830, 0.956] | 1.000 [1.000, 1.000] | incumbent |
| `A0` | `tool` | 0.897 [0.830, 0.955] | 0.963 [0.889, 1.000] | **recovery to parity** |
| `A1-A3` | `response_schema` | 0.915 [0.871, 0.955] | 1.000 [1.000, 1.000] | **recovery to parity** |

## Feature Extractor — the rung that went backwards

**Core 28** — incumbent `baseline` 0.903 [0.857, 0.946]

| Rung | Mode | Judged score (95% CI) | vs incumbent |
|---|---|---|---|
| `baseline` | `tool` | 0.903 [0.857, 0.946] | incumbent |
| `A0` | `tool` | 0.837 [0.791, 0.882] | below incumbent (hi 0.882 < 0.903) |
| `A1-A3` | `response_schema` | 0.807 [0.756, 0.853] | below incumbent (hi 0.853 < 0.903) |
| `A0-schema` | `response_schema` | 0.826 [0.773, 0.875] | below incumbent (hi 0.875 < 0.903) |
| `A4-novelty-tool` | `tool` | 0.901 [0.859, 0.940] | **recovery to parity** |
| `A4-novelty-schema` | `response_schema` | 0.920 [0.878, 0.957] | **recovery to parity** |
| `A4-optimizer` | `tool` | **0.949 [0.911, 0.979]** | **clears** (lo 0.911 > 0.903) |

Two things in that column deserve to be said out loud.

**First: the tuning made it worse before it made it better.** `A1-A3` — the
generic adaptation bundle that helped Chunk Summarizer — took Feature Extractor
from 0.837 down to 0.807. That is a **measured regression, recovered by tuning**,
and it stays in the report. An ablation ladder that only ever goes up is not a
ladder, it is a sales chart.

**Second: the 2×2 says where the recovery came from.** `A0-schema` (mode only)
0.826 and `A4-novelty-tool` (prompt only) 0.901 against `A0` 0.837 — the
`novelty_statement` rule carried the recovery, the enforced schema alone did not.
`A4-novelty-schema` (both) reaches 0.920.

### The optimizer rung and its contamination

`A4-optimizer` is a Vertex AI Prompt Optimizer suggestion over `gemini_naive`,
read against `judge_score`. It is the strongest rung in the workshop, and the
figure that ships is the **clean core-28 measurement**:

<div class="amw-stats" markdown>
<div class="amw-stat">
  <span class="amw-stat__value">0.949</span>
  <span class="amw-stat__label">A4-optimizer, judged score (95% CI [0.911, 0.979])</span>
  <span class="amw-stat__source">core 28, no leaked example items · <code>artifacts/results/ablation_feature_extractor.json</code></span>
</div>
</div>

There is also a measurement of the same rung at n=70. It is **not** the shipping
figure, and this is why.[^contamination]

[^contamination]:
    **Excluded from the shipping quote: `A4-optimizer` at n=70, 0.951 [0.929, 0.970].**
    Reason: the rung's prompt quotes `fe-0004`, `fe-0021`, `fe-0029`, `fe-0032`,
    `fe-0037`, `fe-0041`, `fe-0046`, `fe-0051`, `fe-0056`, `fe-0060`, `fe-0064`
    and `fe-0067` as worked examples, and all 12 of those items are **inside** the
    n=70 split it was scored on (`leaked_example_items` in
    `artifacts/results/ablation_feature_extractor.json`). Its judged score there is
    optimistic by construction. The 12 items are not dropped from the split,
    because that would give this rung a different denominator from every other
    rung — so instead the whole n=70 measurement is quoted nowhere except in this
    footnote, with its reason attached. The core-28 measurement of the same rung
    has `leaked_example_items: []` and is the figure the selection table and this
    site use.

That is the general rule, and it is worth stating as a rule rather than as a
one-off: **a rung whose prompt contains items from the split it was scored on is
reported with its contamination, or it is not reported.** Deleting the number
hides the finding; quoting it plain would be the fabrication.

## The full table

Every rung, per subagent, at the n it was actually run on, with all deterministic
metrics beside the judged score:

[Open the selection table](../results/selection_table.md){ .md-button }

---

**Next:** [Module 06 — The second judge](06-the-second-judge.md)

*Source: `artifacts/results/ablation_{query_rewriter,chunk_summarizer,feature_extractor}.json`, `artifacts/reports/selection_table.md`, `artifacts/results/phase2_n70_widened.json`. Bootstrap seed `20260812`, 10,000 resamples, judge Gemini 2.5 Pro prompt `v1`, k=2.*
