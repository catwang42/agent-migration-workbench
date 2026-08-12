# Module 04 — The naive swap

*~5 min read · [03 Gates as contract](03-gates-as-contract.md) → **04** → [05 The adaptation ladder](05-adaptation-ladder.md)*

---

--8<-- "_includes/both-generations.md"

The naive swap is the migration everybody actually tries first: point the same
prompt bytes at a different endpoint and see what happens. In the ablation ladder
it is rung **A0** — the customer's production prompt, unmodified, on Gemini,
through the **same tool-call mechanism** the Claude baseline uses.

Keeping the mechanism identical is not a detail. It is what makes the
Claude-vs-A0 delta a statement about *prompt format* rather than about output
plumbing.

**The naive swap was run on both Gemini generations,** so the third column below
is not history — it is the control that tells you whether a finding is about the
prompt or about the model. Every figure is n=70 on the full corpus, judged by
Gemini 2.5 Pro prompt `v1` at k=2, bootstrap seed `20260812`. Bracketed pairs are
the **95% confidence range**. Every call was executed live and recorded.

Deployment-generation calls recorded `2026-08-12T03:51:24+00:00` → `2026-08-12T03:56:08+00:00` (`artifacts/results/ablation_*.json`, rung `A0-current`) · development-generation calls `2026-08-09T16:07:15+00:00` → `2026-08-11T06:20:37+00:00` (`artifacts/results/phase2_n70_widened.json`)
{ .amw-provenance }

--8<-- "charts/naive-swap.md"

## Query Rewriter

| Metric | **Claude Sonnet 5**<br><small>incumbent · `claude_baseline`, tool</small> | **Gemini 3.6 Flash**<br><small>deployment gen · `A0-current`, tool</small> | **Gemini 2.5 Flash**<br><small>development gen · `gemini_naive`, tool</small> |
|---|---|---|---|
| Judge score | 0.886 [0.838, 0.932] | **0.904 [0.871, 0.936]** | 0.831 [0.797, 0.865] |
| `exact_match_intent` | 0.729 [0.629, 0.829] | 0.614 [0.500, 0.729] | 0.571 [0.457, 0.686] |
| `filter_f1` | 0.973 [0.927, 1.000] | 0.990 [0.974, 1.000] | 0.950 [0.909, 0.981] |
| `filter_precision` | 0.915 [0.840, 0.981] | 0.941 [0.882, 0.985] | 0.914 [0.854, 0.965] |
| `filter_recall` | 0.754 [0.646, 0.862] | 1.000 [1.000, 1.000] | 0.965 [0.927, 0.992] |
| `json_schema_validity` | 0.814 [0.714, 0.900] | 1.000 [1.000, 1.000] | 0.971 [0.929, 1.000] |

A generation of model progress moved the naive swap's judge score from 0.831 to
0.904 — past the incumbent's 0.886, on overlapping intervals. **It did not fix the
thing that was actually broken.** `exact_match_intent` is still 0.614 against the
incumbent's 0.729: the unmodified prompt still misreads what the user is asking
for, on roughly one query in three. Filter recall goes to a perfect 1.000 in the
same column.

That is the whole argument for the ladder in one table. The metric that a
newer model fixed by itself is not the metric the adaptation work was for, and a
single averaged score would have hidden both movements.

## Chunk Summarizer

| Metric | **Claude Sonnet 5**<br><small>incumbent · `claude_baseline`, tool</small> | **Gemini 3.6 Flash**<br><small>deployment gen · `A0-current`, tool</small> | **Gemini 2.5 Flash**<br><small>development gen · `gemini_naive`, tool</small> |
|---|---|---|---|
| Judge score | 0.918 [0.879, 0.954] | 0.902 [0.864, 0.936] | 0.868 [0.820, 0.911] |
| `citation_coverage` | 1.000 [1.000, 1.000] | **1.000 [1.000, 1.000]** | 0.940 [0.881, 0.985] |
| `fabricated_citation_rate` | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] |
| `uncited_claim_rate` | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] |
| `json_schema_validity` | 0.971 [0.929, 1.000] | 1.000 [1.000, 1.000] | 0.943 [0.886, 0.986] |

Nothing fabricates a citation on either vendor, at any point in this workshop.
The coverage loss that the development generation showed — 1.000 → 0.940, and in
a RAG summarizer coverage *is* the product — is **gone on the deployment
generation**, which recovers a perfect 1.000 from the unmodified prompt. This is
a case where the newer model closed the gap without any prompt work at all, and
saying so is as much a part of the report as the cases where it did not.

## Feature Extractor

| Metric | **Claude Sonnet 5**<br><small>incumbent · `claude_baseline`, tool</small> | **Gemini 3.6 Flash**<br><small>deployment gen · `A0-current`, tool</small> | **Gemini 2.5 Flash**<br><small>development gen · `gemini_naive`, tool</small> |
|---|---|---|---|
| Judge score | **0.900 [0.868, 0.929]** | 0.827 [0.790, 0.861] | 0.821 [0.787, 0.854] |
| `extraction_accuracy` | 0.971 [0.929, 1.000] | 0.998 [0.993, 1.000] | 1.000 [1.000, 1.000] |
| `answered_precision` | 1.000 [1.000, 1.000] | 0.997 [0.991, 1.000] | 1.000 [1.000, 1.000] |
| `omission_rate` | 0.029 [0.000, 0.074] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] |
| `hallucination_rate` | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] |
| `json_schema_validity` | 0.957 [0.900, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |

Read those columns again. On every deterministic instrument the naive Gemini swap
is **at least as good as, and mostly better than**, the incumbent — near-perfect
extraction accuracy, no omissions, perfect schema validity, on *both* Gemini
generations. And the judge scores the deployment generation 0.827 [0.790, 0.861]
against Claude's 0.900 [0.868, 0.929], **non-overlapping** — the same
non-overlapping gap the development generation showed at 0.821 [0.787, 0.854].

**A generation of model progress did not move this finding at all.** The
disagreement is real, it is localised to the `novelty_statement` field, and it is
the reason this subagent never gets a clean recommendation. A cheap-metrics-only
evaluation would have shipped this migration with confidence — and would have
shipped it twice.

## The defect on the incumbent's side

The swap surfaced a problem on Claude too, and it goes in the report because the
report is not there to flatter anybody.

Claude's Query Rewriter `json_schema_validity` is **0.814 [0.714, 0.900]** against
a 0.99 gate. That is a baseline defect, and it was invisible at n=10, where all
three arms read 1.000. Sample size is an instrument setting, not a budget line.

But the finding is about **mechanism**, not about the model:

> Under this demo organization's Vertex AI policy configuration
> (`constraints/vertexai.allowedPartnerModelFeatures`), partner-model structured
> outputs were unavailable, so the Claude baseline was measured using tool-call
> structured emission.

Note where that defect *isn't*: both Gemini generations read 1.000 [1.000, 1.000]
on the same instrument, from the same prompt bytes, through the same tool-call
mechanism.

Gemini's tuned arms use a natively enforced `response_schema`. That is not a
like-for-like comparison of the two models' schema conformance, and every Claude
schema-validity number in the scorecard carries that caveat **in the same cell**,
so it cannot be separated from the figure by a copy-paste.

It is an environment finding about one organization's policy configuration. It is
not a statement about what the model is capable of.

!!! note "How fairness is preserved by construction"

    Gemini's A0 rung runs under the **same tool-call mechanism** as Claude. So the
    Claude → A0 delta isolates prompt format, and the A0 → A1 delta is what the
    enforced schema is worth. The two effects are never bundled into one number
    and attributed to "the model".

## Run it

```bash
python cli.py phase2 --mode replay        # the full n=70 baseline, from recordings
python cli.py phase2 --mode hybrid -n 10  # a live 10-item subset, Claude replayed
```

---

**Next:** [Module 05 — The adaptation ladder](05-adaptation-ladder.md)

*Source: deployment generation — `artifacts/results/ablation_{query_rewriter,chunk_summarizer,feature_extractor}.json`, rung `A0-current`, model `gemini-flash-current` (Gemini 3.6 Flash), n=70 split `all`. Incumbent and development generation — `artifacts/results/phase2_n70_widened.json`, models `claude-sonnet` (Claude Sonnet 5) and `gemini-flash` (Gemini 2.5 Flash). Mode `replay`, judge Gemini 2.5 Pro prompt `v1`, k=2, bootstrap seed `20260812`, 10,000 resamples. Corpus: `demo_patents`, synthetic, seed `20260812`, n=70.*
