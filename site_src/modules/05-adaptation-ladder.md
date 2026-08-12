# Module 05 — The adaptation ladder

*~7 min read · [04 The naive swap](04-the-naive-swap.md) → **05** → [06 The second judge](06-the-second-judge.md)*

---

--8<-- "_includes/both-generations.md"

"Adapt the prompt" is not one action, so it cannot be one number. The ladder runs
each adaptation as its own rung against the same corpus, so you can see which
change bought what — and, on one subagent here, which change **cost** something.

```bash
python cli.py ablate --subagent query_rewriter
```

## The rungs

| Rung | Model | What changes | Output mode |
|---|---|---|---|
| `baseline` | **Claude Sonnet 5** | Incumbent: the customer's XML prompt on Claude, `emit_*` tool | `tool` |
| `A0` | **Gemini 2.5 Flash** | Naive endpoint swap: the same prompt bytes on Gemini, same tool | `tool` |
| `A1-A3` | **Gemini 2.5 Flash** | Bundled: Markdown restructure + system/user split + enforced `response_schema` + two recalibrated few-shots | `response_schema` |
| `A0-schema` *(Feature Extractor)* | **Gemini 2.5 Flash** | **Mode only**: A0's prompt, enforced `response_schema` instead of the tool | `response_schema` |
| `A4-novelty-tool` *(Feature Extractor)* | **Gemini 2.5 Flash** | **Prompt only**: A0 plus the `novelty_statement` rule (claim 1 is the point of novelty; numeric limits survive) and one worked example | `tool` |
| `A4-novelty-schema` *(Feature Extractor)* | **Gemini 2.5 Flash** | **Both**: the novelty prompt under the enforced `response_schema` | `response_schema` |
| `A4-optimizer` *(Feature Extractor)* | **Gemini 2.5 Flash** | Optimizer: a VAIPO-suggested instruction over `gemini_naive`, read against `judge_score` | `tool` |
| `A4-targeted` *(Query Rewriter)* | **Gemini 2.5 Flash** | A1-A3 plus three bundled rules for the three measured loss clusters — publication numbers verbatim in `query`, explicit `date_to` copied not expanded, landscape vs ownership by which side of the question is unknown (per-rule credit is **not** isolated) | `response_schema` |
| `A0-current` | **Gemini 3.6 Flash** | Generation only: A0's prompt bytes, unchanged, on the deployment-generation model | `tool` |
| `A0-35` | **Gemini 3.5 Flash** | Generation only: the same, on the second candidate | `tool` |
| `ship-current` | **Gemini 3.6 Flash** | The winning prompt from the ladder above, unchanged, on the deployment-generation model | as selected |
| `ship-35` | **Gemini 3.5 Flash** | The same winning prompt, on the second candidate | as selected |
| `ship-current-capped` | **Gemini 3.6 Flash (capped thinking)** | **Recommended.** `ship-current` with `thinking_config.thinking_budget` minimised — same model ID, same prompt bytes, one setting apart | as selected |

Every rung name above is a **prompt** identity, not a model identity. The last
five rungs re-run *already-selected* prompts against different models, which is
exactly why every result table names its model in its own column.

`A0-current` and `A0-35` exist so that "the newer model fixed it" and "the prompt
work fixed it" can never be collapsed into one claim.

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

Bracketed pairs in every table below are the **95% confidence range** — the
span the true value is likely to sit in at this sample size. Wide brackets mean a
small sample, not a shaky measurement.

The comparison column below is the rung's 95% confidence-range lower bound against the
incumbent's point estimate *on the same split*. It is **unpaired**, and it is not
the `quality_delta_pp` gate — that gate is a paired bootstrap over per-item
differences, and it is what the verdict is decided on. This table informs the
selection; it does not make it.

## What ships: the deployment generation

These are the rungs the scorecard reads. All are **n=70 on the full corpus**, so
they compare against the full-70 Claude incumbent, not the core-28 one. The
recommended row is bold.

**Query Rewriter** — incumbent `claude_baseline` on **Claude Sonnet 5**, 0.886 [0.838, 0.932]

| Rung | Model | Mode | Judged score (95% confidence range) | `exact_match_intent` | vs incumbent |
|---|---|---|---|---|---|
| **`ship-current-capped`** | **Gemini 3.6 Flash (capped thinking)** | `response_schema` | **0.959 [0.934, 0.982]** | 0.814 [0.714, 0.900] | **clears** (lo 0.934 > 0.886) |
| `ship-current` | Gemini 3.6 Flash | `response_schema` | 0.964 [0.939, 0.986] | 0.857 [0.771, 0.929] | **clears** (lo 0.939 > 0.886) |
| `ship-35` | Gemini 3.5 Flash | `response_schema` | 0.968 [0.943, 0.989] | 0.943 [0.886, 0.986] | **clears** (lo 0.943 > 0.886) |
| `A0-current` | Gemini 3.6 Flash | `tool` | 0.904 [0.871, 0.936] | 0.614 [0.500, 0.729] | parity — unmodified prompt (lo 0.871 ≤ 0.886 ≤ hi 0.936) |
| `A0-35` | Gemini 3.5 Flash | `tool` | 0.855 [0.820, 0.888] | 0.514 [0.400, 0.629] | parity, at the margin (hi 0.888 clears 0.886 by 0.002) |

**Chunk Summarizer** — incumbent `claude_baseline` on **Claude Sonnet 5**, 0.918 [0.879, 0.954]

| Rung | Model | Mode | Judged score (95% confidence range) | `citation_coverage` | vs incumbent |
|---|---|---|---|---|---|
| **`ship-current-capped`** | **Gemini 3.6 Flash (capped thinking)** | `response_schema` | **0.893 [0.857, 0.925]** | 1.000 [1.000, 1.000] | parity (lo 0.857 ≤ 0.918 ≤ hi 0.925) |
| `ship-current` | Gemini 3.6 Flash | `response_schema` | 0.902 [0.866, 0.936] | 1.000 [1.000, 1.000] | parity |
| `ship-35` | Gemini 3.5 Flash | `response_schema` | 0.896 [0.855, 0.934] | 1.000 [1.000, 1.000] | parity |
| `A0-current` | Gemini 3.6 Flash | `tool` | 0.902 [0.864, 0.936] | 1.000 [1.000, 1.000] | parity — unmodified prompt |
| `A0-35` | Gemini 3.5 Flash | `tool` | 0.896 [0.868, 0.925] | 1.000 [1.000, 1.000] | parity — unmodified prompt |

**Feature Extractor** — incumbent `claude_baseline` on **Claude Sonnet 5**, 0.900 [0.868, 0.929]

| Rung | Model | Mode | Judged score (95% confidence range) | `extraction_accuracy` | `omission_rate` | vs incumbent |
|---|---|---|---|---|---|---|
| **`ship-current-capped`** | **Gemini 3.6 Flash (capped thinking)** | `tool` | **0.903 [0.880, 0.924]** | 0.940 [0.921, 0.960] | 0.066 [0.046, 0.087] | parity (lo 0.880 ≤ 0.900 ≤ hi 0.924) |
| `ship-current` | Gemini 3.6 Flash | `tool` | 0.950 [0.926, 0.970] | 0.995 [0.988, 1.000] | 0.005 [0.000, 0.012] | **clears** (lo 0.926 > 0.900) |
| `ship-35` | Gemini 3.5 Flash | `tool` | 0.951 [0.927, 0.973] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | **clears** (lo 0.927 > 0.900) |
| `A0-current` | Gemini 3.6 Flash | `tool` | 0.827 [0.790, 0.861] | 0.998 [0.993, 1.000] | 0.000 [0.000, 0.000] | below incumbent (hi 0.861 < 0.900) |
| `A0-35` | Gemini 3.5 Flash | `tool` | 0.781 [0.744, 0.815] | 1.000 [1.000, 1.000] | 0.000 [0.000, 0.000] | below incumbent (hi 0.815 < 0.900) |

### What capping the reasoning budget costs

`ship-current` and `ship-current-capped` are the **same provider model ID on the
same prompt bytes over the same 70 items**. The only difference is
`thinking_config.thinking_budget`. So the gap between those two rows is the price
of the setting, and it is measured, not argued:

| Subagent | Judged score, default budget | Judged score, capped | Cost savings vs Claude, capped |
|---|---|---|---|
| Query Rewriter | 0.964 [0.939, 0.986] | 0.959 [0.934, 0.982] | +42.7% [+40.3, +45.3] |
| Chunk Summarizer | 0.902 [0.866, 0.936] | 0.893 [0.857, 0.925] | +56.1% [+54.5, +57.5] |
| Feature Extractor | 0.950 [0.926, 0.970] | **0.903 [0.880, 0.924]** | +64.8% [+63.8, +65.7] |

Query Rewriter and Chunk Summarizer barely move. **Feature Extractor pays for it:**
0.950 → 0.903, `extraction_accuracy` 0.995 → 0.940, and `omission_rate` from
0.005 to 0.066 — the capped arm stops answering roughly one field in fifteen it
would otherwise have filled. That is what the reasoning budget was buying on that
subagent, and it is why the same configuration produces a `clears` on one row and
a `parity` on another.

The default-budget rows are **not** the recommendation: at default budget the
cost gate fails on all three subagents (−6.6%, −23.1%, −16.5% — a cost
*increase*). The trade is stated in full on the
[scorecard](../results/scorecard.md#cost-configurations-compared).

---

## How the rules were found: the development-generation ladder

Everything below ran on **Gemini 2.5 Flash**. It is not the recommendation — it is
where the instruction rules in the shipping prompts were written from, and the
prompts it produced are the ones re-run unchanged in the tables above.

### Query Rewriter

**Core 28** — incumbent `baseline` on **Claude Sonnet 5**, 0.911 [0.839, 0.964]

| Rung | Model | Mode | Judged score (95% confidence range) | vs incumbent |
|---|---|---|---|---|
| `baseline` | **Claude Sonnet 5** | `tool` | 0.911 [0.839, 0.964] | incumbent |
| `A0` | **Gemini 2.5 Flash** | `tool` | 0.836 [0.787, 0.886] | below incumbent (hi 0.886 < 0.911) |
| `A1-A3` | **Gemini 2.5 Flash** | `response_schema` | 0.852 [0.796, 0.905] | below incumbent (hi 0.905 < 0.911) |

**Full 70** — incumbent `gated:claude_baseline` on **Claude Sonnet 5**, 0.886 [0.838, 0.932]

| Rung / arm | Model | Mode | Judged score (95% confidence range) | `exact_match_intent` | `json_schema_validity` | vs incumbent |
|---|---|---|---|---|---|---|
| `A4-targeted` | **Gemini 2.5 Flash** | `response_schema` | 0.963 [0.934, 0.986] | 0.957 [0.900, 1.000] | 1.000 [1.000, 1.000] | **clears** (lo 0.934 > 0.886) |
| `gated:claude_baseline` | **Claude Sonnet 5** | `tool` | 0.886 [0.838, 0.932] | 0.729 [0.629, 0.829] | 0.814 [0.714, 0.900] | incumbent |
| `gated:gemini_naive` | **Gemini 2.5 Flash** | `tool` | 0.831 [0.797, 0.865] | 0.571 [0.457, 0.686] | 0.971 [0.929, 1.000] | below incumbent (hi 0.865 < 0.886) |
| `gated:gemini_tuned_v1` | **Gemini 2.5 Flash** | `response_schema` | 0.879 [0.843, 0.914] | 0.814 [0.714, 0.900] | 1.000 [1.000, 1.000] | **recovery to parity** (lo 0.843 ≤ 0.886 ≤ hi 0.914) |

The `A4-targeted` rung was built from the *measured* loss clusters, not from
intuition. Its three rules were bundled, so no per-rule credit is claimed.

### Chunk Summarizer

**Core 28** — incumbent `baseline` on **Claude Sonnet 5**, 0.902 [0.830, 0.956]

| Rung | Model | Mode | Judged score (95% confidence range) | `citation_coverage` | vs incumbent |
|---|---|---|---|---|---|
| `baseline` | **Claude Sonnet 5** | `tool` | 0.902 [0.830, 0.956] | 1.000 [1.000, 1.000] | incumbent |
| `A0` | **Gemini 2.5 Flash** | `tool` | 0.897 [0.830, 0.955] | 0.963 [0.889, 1.000] | **recovery to parity** |
| `A1-A3` | **Gemini 2.5 Flash** | `response_schema` | 0.915 [0.871, 0.955] | 1.000 [1.000, 1.000] | **recovery to parity** |

### Feature Extractor — the rung that went backwards

**Core 28** — incumbent `baseline` on **Claude Sonnet 5**, 0.903 [0.857, 0.946]

| Rung | Model | Mode | Judged score (95% confidence range) | vs incumbent |
|---|---|---|---|---|
| `baseline` | **Claude Sonnet 5** | `tool` | 0.903 [0.857, 0.946] | incumbent |
| `A0` | **Gemini 2.5 Flash** | `tool` | 0.837 [0.791, 0.882] | below incumbent (hi 0.882 < 0.903) |
| `A1-A3` | **Gemini 2.5 Flash** | `response_schema` | 0.807 [0.756, 0.853] | below incumbent (hi 0.853 < 0.903) |
| `A0-schema` | **Gemini 2.5 Flash** | `response_schema` | 0.826 [0.773, 0.875] | below incumbent (hi 0.875 < 0.903) |
| `A4-novelty-tool` | **Gemini 2.5 Flash** | `tool` | 0.901 [0.859, 0.940] | **recovery to parity** |
| `A4-novelty-schema` | **Gemini 2.5 Flash** | `response_schema` | 0.920 [0.878, 0.957] | **recovery to parity** |
| `A4-optimizer` | **Gemini 2.5 Flash** | `tool` | **0.949 [0.911, 0.979]** | **clears** (lo 0.911 > 0.903) |

--8<-- "charts/fe-ladder.md"

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

#### The optimizer rung and its contamination

`A4-optimizer` is a Vertex AI Prompt Optimizer suggestion over `gemini_naive`,
read against `judge_score`. It is the strongest rung in the workshop, and the
figure that ships is the **clean core-28 measurement**:

<div class="amw-callout" markdown="1">
<span class="amw-callout__value">0.949</span>
<span class="amw-callout__range">95% confidence range 0.911 – 0.979</span>
<span class="amw-callout__note">measured on the 28 held-out items the optimizer never trained on</span>
<span class="amw-callout__source"><code>artifacts/results/ablation_feature_extractor.json</code></span>
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
