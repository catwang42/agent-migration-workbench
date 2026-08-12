# Module 04 — The naive swap

*~5 min read · [03 Gates as contract](03-gates-as-contract.md) → **04** → [05 The adaptation ladder](05-adaptation-ladder.md)*

---

The naive swap is the migration everybody actually tries first: point the same
prompt bytes at a different endpoint and see what happens. In the ablation ladder
it is rung **A0** — the customer's production prompt, unmodified, on Gemini,
through the **same tool-call mechanism** the Claude baseline uses.

Keeping the mechanism identical is not a detail. It is what makes the
Claude-vs-A0 delta a statement about *prompt format* rather than about output
plumbing.

All figures below are n=70 on the full corpus, 95% bootstrap CIs, seed
`20260812`, from `artifacts/results/phase2_n70_widened.json`. Every call was
executed; the recording window is 2026-08-09T16:07:15+00:00 →
2026-08-11T06:20:37+00:00.

## Query Rewriter

| Metric | `claude_baseline` (tool) | `gemini_naive` (tool) |
|---|---|---|
| Judge score | 0.886 [0.838, 0.932] | 0.831 [0.797, 0.865] |
| `exact_match_intent` | 0.729 [0.629, 0.829] | 0.571 [0.457, 0.686] |
| `filter_f1` | 0.973 [0.927, 1.000] | 0.950 [0.909, 0.981] |
| `filter_precision` | 0.915 [0.840, 0.981] | 0.914 [0.854, 0.965] |
| `filter_recall` | 0.754 [0.646, 0.862] | 0.965 [0.927, 0.992] |
| `json_schema_validity` | 0.814 [0.714, 0.900] | 0.971 [0.929, 1.000] |

The naive swap costs intent accuracy badly — `exact_match_intent` drops from 0.729
to 0.571 — and it buys filter recall, from 0.754 to 0.965. Those are opposite
directions on two halves of the same task, which is precisely why the harness does
not average precision and recall into one score.

## Chunk Summarizer

| Metric | `claude_baseline` (tool) | `gemini_naive` (tool) |
|---|---|---|
| Judge score | 0.918 [0.879, 0.954] | 0.868 [0.820, 0.911] |
| `citation_coverage` | 1.000 [1.000, 1.000] | 0.940 [0.881, 0.985] |
| `fabricated_citation_rate` | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] |
| `uncited_claim_rate` | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] |
| `json_schema_validity` | 0.971 [0.929, 1.000] | 0.943 [0.886, 0.986] |

Nothing fabricates a citation on either arm, at any point in this workshop. What
the naive swap loses is coverage: 1.000 → 0.940. In a RAG summarizer, coverage is
the product.

## Feature Extractor

| Metric | `claude_baseline` (tool) | `gemini_naive` (tool) |
|---|---|---|
| Judge score | 0.900 [0.868, 0.929] | 0.821 [0.787, 0.854] |
| `extraction_accuracy` | 0.971 [0.929, 1.000] | 1.000 [1.000, 1.000] |
| `answered_precision` | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| `omission_rate` | 0.029 [0.000, 0.074] | 0.000 [0.000, 0.000] |
| `hallucination_rate` | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] |
| `json_schema_validity` | 0.957 [0.900, 1.000] | 1.000 [1.000, 1.000] |

Read those two columns again. On every deterministic instrument the naive Gemini
swap is **at least as good as, and mostly better than**, the incumbent — perfect
extraction accuracy, no omissions, perfect schema validity. And the judge scores
it 0.821 [0.787, 0.854] against Claude's 0.900 [0.868, 0.929], **non-overlapping**.

The disagreement is real, it is localised to the `novelty_statement` field, and it
is the reason this subagent never gets a clean recommendation. A cheap-metrics-only
evaluation would have shipped this migration with confidence.

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

*Source: `artifacts/results/phase2_n70_widened.json` (mode `replay`, judge Gemini 2.5 Pro prompt `v1`, k=2, bootstrap seed `20260812`, 10,000 resamples). Corpus: `demo_patents`, synthetic, seed `20260812`, n=70.*
