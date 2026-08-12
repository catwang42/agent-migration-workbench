# Module 07 — Shadow & adjudication

*~6 min read · [06 The second judge](06-the-second-judge.md) → **07** → [08 The scorecard](08-the-scorecard.md)*

---

--8<-- "_includes/development-generation.md"

A shadow run puts both arms on the same inputs and asks a narrower question than
the eval does: **where do these two models actually produce different answers, and
when they differ, who is right?**

```bash
python cli.py shadow --mode replay      # shadow run + disagreement triage
```

## Two agreement numbers, and why they differ so much

The same run yields two very different figures, and confusing them is the fastest
way to misread this whole section.

| Subagent | Item-level agreement (whole object) | Structured-field agreement (gated) |
|---|---|---|
| query_rewriter | 0.143 [0.071, 0.229] | **0.557 [0.443, 0.671]** |
| chunk_summarizer | 0.129 [0.057, 0.214] | **0.957 [0.900, 1.000]** |
| feature_extractor | 0.200 [0.114, 0.300] | **0.971 [0.929, 1.000]** |

*Candidate arm `gemini_tuned_v1`, n=70, `artifacts/results/shadow_widened.json`.*

Item-level agreement asks whether the *entire* emitted object matched, prose
included. Two summaries that say the same thing in different words disagree at
item level, and that is not a defect — it is what free text does.

The gate therefore counts **structured fields only** — the fields with a defined
right answer. Prose is excluded here and adjudicated separately. `shadow_agreement`
is not, and never claims to be, a statement that the prose matched.

The field breakdown makes the split visible:

=== "chunk_summarizer"

    | Field | Kind | Agreement | Mean similarity |
    |---|---|---|---|
    | `summary` | prose | 0.129 | 0.464 |
    | `key_points.text` | prose | 0.829 | 0.709 |
    | `key_points.chunk_ids` | scalar set | **0.957** | — |

    The chunk ids — the part that has a right answer — agree 67 times out of 70.
    The prose summary agrees 9 times out of 70. Both facts are true; only one of
    them is a quality signal.

=== "feature_extractor"

    | Field | Kind | Agreement | Mean similarity |
    |---|---|---|---|
    | `title` | scalar | 0.971 | — |
    | `assignee` | scalar | 0.971 | — |
    | `filing_date` | scalar | 0.971 | — |
    | `jurisdiction` | code | 0.971 | — |
    | `cpc_codes` | code set | 0.971 | — |
    | `independent_claim_count` | scalar | 0.971 | — |
    | `technical_field` | prose | 0.486 | 0.535 |
    | `novelty_statement` | prose | 0.500 | 0.552 |

    Six structured fields at 0.971; the two open-text fields at roughly a coin
    flip. Those two are exactly where the judge disagreement in module 04 lives.

=== "query_rewriter"

    | Field | Kind | Agreement | Mean similarity |
    |---|---|---|---|
    | `query` | prose | 0.243 | 0.432 |
    | `intent` | scalar | **0.686** | — |
    | `filters.date_from` | scalar | 0.843 | — |
    | `filters.date_to` | scalar | 0.886 | — |
    | `filters.assignees` | scalar set | 0.814 | — |
    | `filters.jurisdictions` | code set | 0.914 | — |
    | `filters.cpc_codes` | code set | 0.900 | — |

    `intent` at 0.686 is the outlier that drags the structured figure to 0.557,
    and it is a field with exactly one right answer. This is a real disagreement,
    not a prose artefact.

## Adjudication: two arms, same subagent, opposite verdicts

Where the two models gave different answers, a judge ruled on every
disagreement: win, loss, or tie.

Query Rewriter fails the primary `shadow_agreement` bound on both candidate arms.
So the [`alt` clause](03-gates-as-contract.md#the-alt-clause-on-shadow_agreement)
applies: on the disagreements, adjudicated wins must be at least losses. Verdicts
are from the **candidate's** point of view, and the scores are the recorded phase-2
judge scores for those exact outputs, replayed — no judge call was made to build
these tables.

<div class="amw-compare" markdown="1">
<div class="amw-compare__card amw-compare__card--ships" markdown="1">
<p class="amw-compare__title">gemini_targeted_v1 <span class="amw-compare__tag amw-compare__tag--ships">Ships</span></p>
<p class="amw-compare__verdict amw-compare__verdict--pass">Passes the alt clause</p>
<div class="amw-compare__row"><span class="amw-compare__key">Disagreements</span><span class="amw-compare__val">51</span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Wins / losses / ties, overall</span><span class="amw-compare__val"><span class="amw-wl amw-wl--win">15W</span><span class="amw-wl amw-wl--loss">3L</span><span class="amw-wl amw-wl--tie">33T</span></span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Excluding malformed baseline emissions</span><span class="amw-compare__val"><span class="amw-wl amw-wl--win">9W</span><span class="amw-wl amw-wl--loss">3L</span></span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Structured agreement</span><span class="amw-compare__val">0.643 [0.529, 0.757]</span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Verdict</span><span class="amw-compare__val">Passes on either figure</span></div>
</div>
<div class="amw-compare__card amw-compare__card--replaced" markdown="1">
<p class="amw-compare__title">gemini_tuned_v1 <span class="amw-compare__tag amw-compare__tag--replaced">Replaced — kept as the control</span></p>
<p class="amw-compare__verdict amw-compare__verdict--fail">Fails the alt clause</p>
<div class="amw-compare__row"><span class="amw-compare__key">Disagreements</span><span class="amw-compare__val">60</span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Wins / losses / ties, overall</span><span class="amw-compare__val"><span class="amw-wl amw-wl--win">14W</span><span class="amw-wl amw-wl--loss">20L</span><span class="amw-wl amw-wl--tie">26T</span></span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Excluding malformed baseline emissions</span><span class="amw-compare__val"><span class="amw-wl amw-wl--win">8W</span><span class="amw-wl amw-wl--loss">15L</span></span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Structured agreement</span><span class="amw-compare__val">0.557 [0.443, 0.671]</span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Verdict</span><span class="amw-compare__val">Fails on either figure</span></div>
</div>
</div>

Reading both cards: **15W / 3L / 33T** means 15 wins, 3 losses and 33 ties,
judged from the new model's point of view — a win is an item where the judge
scored the Gemini answer above the Claude one. Bracketed pairs are the 95%
confidence range. The left card is the arm the scorecard ships; the right card is
the arm it replaced, kept on the page as the control that makes the difference
readable.
{ .amw-legend }

**Same model, same data — three instruction rules written from the measured
failures moved the adjudication from 14W/20L to 15W/3L.** Both arms have
`json_schema_validity` 1.000 [1.000, 1.000] and both are the same Gemini 2.5 Flash
model on the same corpus. Nothing changed but the prompt, and the rules were not
guessed: each one was written from a loss cluster counted in the n=70
adjudication of the arm on the right.

!!! note "Why 'excluding structurally malformed baseline emissions' is quoted too"

    Under this demo organization's Vertex AI policy configuration
    (`constraints/vertexai.allowedPartnerModelFeatures`), partner-model structured
    outputs were unavailable, so the Claude baseline was measured using tool-call
    structured emission. On some items that emission was structurally broken — the
    whole payload re-encoded as a JSON string inside the `query` field, or a
    payload that fails `QueryPlan` validation outright.

    Counting a win against a baseline that emitted a broken envelope would flatter
    the candidate. So the adjudication is reported **both ways**: with those items
    and without them (`amw/shadow/emission.py` identifies them). The excluded items
    are ones where the emission was structurally broken, **not** ones where the
    baseline merely answered worse. Neither arm's verdict changes between the two
    figures, which is the only reason a single verdict can be stated at all.

### The other two subagents, for completeness

| Subagent | Candidate | Disagreements | W / L / T | Excluding malformed |
|---|---|---|---|---|
| chunk_summarizer | `gemini_tuned_v1` | 61 | 3W / 11L / 47T | 2W / 11L |
| feature_extractor | `gemini_tuned_v1` | 56 | 5W / 39L / 12T | 5W / 37L |

W / L / T = wins / losses / ties, judged from the new model's point of view.
{ .amw-legend }

Neither needs the `alt` clause — both clear the primary `shadow_agreement` bound
(0.957 and 0.971). Their adjudication tables are published anyway, because a
passing gate is not a reason to stop showing the disagreements underneath it.

## Ties are not "not adjudicated"

A **tie** means the recorded judge scored both arms equally on that item. **Not
adjudicated** means no recorded verdict exists for it, and adjudicating it would
require new judge calls. On the widened n=70 run, `not_adjudicated` is 0 for all
three subagents — every disagreement has a recorded verdict behind it.

## Latency, and why this run has no number

The shadow run does capture per-arm latency. It also captures why you cannot use
it:

> **CROSS-REGION:** `claude-sonnet` ran in `global`, `gemini-flash` in
> `us-central1`. Latency here compares two regions and is not a like-for-like
> p95; the `gates.yaml` basis for `latency_p95` asks for same region, same load
> profile.

So on **this** run — the development-generation arms above — `latency_p95`
renders as **not comparable — region split disclosed**. Not evaluated, and
specifically not passed.

That is a property of how these particular calls were routed, not a permanent
hole in the method. The deployment candidates were later probed with both arms
pinned to the same region, which is the only input that can open the gate; the
probe refuses to record itself at all if the two arms end up in different
regions. Whether the gate then passes or fails is a separate question, and the
answer for each subagent is in the scorecard — where a probe exists it is
evaluated on the confidence-range upper bound like any other ceiling, and where
none exists the gate stays unevaluated rather than borrowing one.

---

**Next:** [Module 08 — The scorecard](08-the-scorecard.md)

*Source: `artifacts/results/shadow_widened.json`, `artifacts/results/shadow_qr_targeted.json`, `artifacts/results/shadow_triage_widened.md`, `artifacts/results/shadow_triage_qr_targeted.md`. Mode `replay`, similarity metric `token_jaccard_lexical_proxy`, prose threshold 0.6, bootstrap seed `20260812`.*
