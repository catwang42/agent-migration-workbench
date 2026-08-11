# A4-targeted — the Query Rewriter rung, and what it can and cannot tell you

**Rung:** `A4-targeted` · **Variant:** `gemini_targeted_v1` · **Branches from:** `A1-A3`
**Authored:** 2026-08-11, after the n=70 adjudication, before the rung was measured
**Prompt:** `amw/agents/prompts/query_rewriter/gemini_targeted_v1.txt`

---

## Read this first: the rules are bundled and per-rule credit was not isolated

The owner ruled on 2026-08-11: *"Option 1 — three rules in one rung. Bundling
accepted: this rung answers 'does targeted tuning fix the identified failures,'
not 'which rule earned what.'"*

So there is exactly one number here, and it is a **joint** number. If
`A4-targeted` beats `A1-A3`, that tells you the three rules together recovered
some of the gap. It does **not** license any of:

- "the publication-number rule is worth N points"
- "the date rule was the one that mattered"
- "two of the three rules worked"

Isolating per-rule credit needs three more rungs (one per rule held out) and
was not built. Anyone who wants that answer should be told the price — three
more rungs at 70 items each — not given a guess. The unbundling precedent is
Feature Extractor's 2×2, which cost four rungs to separate two variables.

## What the three rules are, and which measured failure each answers

Every rule traces to a named cluster in the full-70 adjudication
(`artifacts/results/shadow_triage_widened.md`). Nothing was added because it
seemed like a good idea.

| # | Rule | Cluster | n | The failure, as the judge recorded it |
|---|---|---|---|---|
| 1 | Publication numbers survive verbatim in `query` | `publication_number_kept` | 6 | qr-0009: *"the query field does not contain the publication number 'US10869083B2' from the user's request"* |
| 2 | An explicit `date_to` is copied; only a bare period is expanded | `date_range_exact` | 5 | qr-0005: *"`filters.date_to` is '2023-11-01' instead of the required '2023-10-31'"* |
| 3 | `landscape` vs `ownership` decided by which side of the question is unknown | `intent_landscape` | 5 | qr-0036: *"sets the intent to 'ownership' when the user is surveying a company's activity"* |

Two clusters were deliberately left out of scope: `inventor_preserved` (2) and
`assignee_replaced` (2). Rule 1's closing sentence — put any identifier the
schema has no field for into the query — covers `inventor_preserved` as a side
effect. That is a *prediction*, not a claim; if those two items improve, it is
an unearned bonus from a rule aimed elsewhere, and the rung still gets no
credit for having targeted them.

## One of these is a prompt defect, not a model deficiency

Rule 2 deserves plain speech in front of the customer, because reporting it as
"Gemini got dates wrong" would be false.

The gold answers treat an explicitly named end date as literal: *"filing date
between 2020-04-01 and 2023-10-31"* has gold `date_to: "2023-10-31"`. But both
the incumbent XML prompt (`claude_baseline.txt`, line 22) and the tuned prompt
declare `date_to` to be the **exclusive** latest filing date. Under that
instruction, `2023-11-01` is the *correct* answer to the question as asked.
Gemini followed the written spec and was marked wrong against a gold that
encodes a different one.

Claude does not lose these items, but that is not evidence it understood the
spec better — the baseline states the same exclusive rule and only ever
illustrates it with a bare year, so the conflict never surfaces there. The
honest framing is: **the prompt was ambiguous about explicit end dates, one
model resolved the ambiguity one way and one the other, and the fix is to say
what you mean.** This is exactly the class of finding a migration is supposed
to surface — a latent under-specification that only shows up when a second
model reads the same words.

Rules 1 and 3 are ordinary prompt-transfer gaps and carry no such caveat.

## Contamination

`few_shot_item_ids = ()` — the rung quotes no scored corpus item.

The two inherited worked examples (lithium metal anodes / CRISPR base editing)
are carried over from `gemini_tuned_v1` **unchanged and unexamined**, because
changing them would confound the three-rule delta with an example swap. The
three new illustrations are freshly authored and verified absent from
`datasets/query_rewriter.jsonl`: publication number `US8874512B1`, assignee
`Halbrecht Fluidics GmbH`, subject matter `peristaltic pump occlusion sensing`
and `rotary vane vacuum pump seals`, date range `2014-08-09`–`2017-03-14`.

Same discipline as the fe-0003 swap (`notes/fe_worked_example_swap.md`): an
illustration must never be one of the answer keys.

## Sizing

Measured at **n=70**, the full corpus, matching the widened FE ladder and the
gated QR row. The rules were authored against evidence from all 70 items, so a
core-28 measurement would have been scored on a subset of the same items that
motivated the rules while presenting itself as the whole picture.

Note what that does and does not buy. The rung is **not** held out: the loss
clusters were read off these 70 items and the prompt was written to fix them.
This measures *"can the identified failures be fixed by prompting"*, which is a
real and useful question, and it is **not** an estimate of how the rung would
perform on unseen traffic. That number needs the customer's own traces — the
Act 2 ask.
