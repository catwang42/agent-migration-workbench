# Module 07 — Shadow & adjudication

*~6 min read · [06 The second judge](06-the-second-judge.md) → **07** → [08 The scorecard](08-the-scorecard.md)*

---

--8<-- "_includes/both-generations.md"

A shadow run puts both arms on the same inputs and asks a narrower question than
the eval does: **where do these two models actually produce different answers, and
when they differ, who is right?**

```bash
python cli.py shadow --mode replay      # shadow run + disagreement triage
```

## What the scorecard reads: the deployment generation

These are the `shadow_agreement` figures the
[scorecard](../results/scorecard.md) gates on: **Claude Sonnet 5**
(`claude_baseline`) against **Gemini 3.6 Flash**, n=70, adjudicated in full.

!!! warning "One thing on this page is carried, not re-measured"

    The shadow campaign ran on Gemini 3.6 Flash at its **default** reasoning
    budget. The recommended deployment configuration caps that budget. Same model
    ID, same prompt bytes, same shadow slice — different `thinking_budget`. The
    scorecard carries these numbers onto the capped arms and
    [says so in its run notes](../results/scorecard.md#footer); every *other* gate
    on that card is measured on the capped configuration itself. It is carried
    because the campaign predates the capped arms and there was no time to re-run
    it before freeze, and it ships labeled rather than quietly.

### Two agreement numbers, and why they differ so much

The same run yields two very different figures, and confusing them is the fastest
way to misread this whole section.

| Subagent | Incumbent | Candidate | Item-level agreement (whole object) | Structured-field agreement (gated) |
|---|---|---|---|---|
| query_rewriter | **Claude Sonnet 5** | **Gemini 3.6 Flash** | 0.186 [0.100, 0.271] | **0.586 [0.471, 0.700]** |
| chunk_summarizer | **Claude Sonnet 5** | **Gemini 3.6 Flash** | 0.114 [0.043, 0.200] | **0.971 [0.929, 1.000]** |
| feature_extractor | **Claude Sonnet 5** | **Gemini 3.6 Flash** | 0.057 [0.014, 0.114] | **0.943 [0.886, 0.986]** |

*n=70, `artifacts/results/shadow_current_{query_rewriter,chunk_summarizer,feature_extractor}.json`.*

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
    | `summary` | prose | 0.129 | 0.473 |
    | `key_points.text` | prose | 0.800 | 0.671 |
    | `key_points.chunk_ids` | scalar set | **0.971** | — |

    The chunk ids — the part that has a right answer — agree 68 times out of 70.
    The prose summary agrees 9 times out of 70. Both facts are true; only one of
    them is a quality signal.

=== "feature_extractor"

    | Field | Kind | Agreement | Mean similarity |
    |---|---|---|---|
    | `title` | scalar | 0.943 | — |
    | `assignee` | scalar | 0.971 | — |
    | `filing_date` | scalar | 0.971 | — |
    | `jurisdiction` | code | 0.971 | — |
    | `cpc_codes` | code set | 0.971 | — |
    | `independent_claim_count` | scalar | 0.971 | — |
    | `technical_field` | prose | 0.214 | 0.384 |
    | `novelty_statement` | prose | 0.386 | 0.529 |

    Six structured fields at 0.943–0.971; the two open-text fields far below.
    Those two are exactly where the judge disagreement in
    [module 04](04-the-naive-swap.md#feature-extractor) lives — on both Gemini
    generations.

=== "query_rewriter"

    | Field | Kind | Agreement | Mean similarity |
    |---|---|---|---|
    | `query` | prose | 0.286 | 0.467 |
    | `intent` | scalar | **0.657** | — |
    | `filters.date_from` | scalar | 0.843 | — |
    | `filters.date_to` | scalar | 0.929 | — |
    | `filters.assignees` | scalar set | 0.857 | — |
    | `filters.jurisdictions` | code set | 0.914 | — |
    | `filters.cpc_codes` | code set | 0.900 | — |

    `intent` at 0.657 is the outlier that drags the structured figure to 0.586,
    and it is a field with exactly one right answer. This is a real disagreement,
    not a prose artefact.

### The adjudication, deployment generation

Where the two models gave different answers, a judge ruled on every
disagreement: win, loss, or tie. Verdicts are from the **candidate's** point of
view — a win is an item where the judge scored the Gemini answer above the Claude
one.

| Subagent | Incumbent | Candidate | Disagreements | W / L / T | Excluding malformed baseline | Gate outcome |
|---|---|---|---|---|---|---|
| query_rewriter | **Claude Sonnet 5** | **Gemini 3.6 Flash** `gemini_targeted_v1` | 57 | 16W / 2L / 39T | 10W / 2L | 0.586 misses the 0.9 bound → **passes on the `alt` clause** |
| chunk_summarizer | **Claude Sonnet 5** | **Gemini 3.6 Flash** `gemini_tuned_v1` | 62 | 2W / 8L / 52T | 1W / 8L | 0.971 clears the primary bound |
| feature_extractor | **Claude Sonnet 5** | **Gemini 3.6 Flash** `gemini_optimizer_v1` | 66 | 31W / 16L / 19T | 28W / 16L | 0.943 misses on its lower bound (0.886) → **passes on the `alt` clause** |

W / L / T = wins / losses / ties. Bracketed pairs elsewhere on this page are the
95% confidence range.
{ .amw-legend }

Two rows here miss the primary `shadow_agreement` bound and clear the
[`alt` clause](03-gates-as-contract.md#the-alt-clause-on-shadow_agreement): *on
the disagreements, adjudicated wins must be at least losses.* That clause was
written into `gates.yaml` before any of this was measured. Both pass on either
figure — with the malformed-baseline items and without them.

Chunk Summarizer is the row worth pausing on: it **clears the gate outright at
0.971** and still loses the adjudication 2W/8L. A passing agreement gate is not a
statement that the candidate answered better; it is a statement that the two arms
rarely differ. When they did differ here, the incumbent was usually judged right.
That is published because a passing gate is not a reason to stop showing the
disagreements underneath it.

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
    baseline merely answered worse. No verdict changes between the two figures,
    which is the only reason a single verdict can be stated at all.

---

## How the rules were found: the development generation

Everything below ran on **Gemini 2.5 Flash**. It is where the three instruction
rules in the shipping Query Rewriter prompt came from, and it is the cleanest
demonstration on this site that a prompt change — not a model change — moved a
result.

Query Rewriter failed the primary `shadow_agreement` bound on both
development-generation candidate arms, so the `alt` clause decided it. The scores
are the recorded phase-2 judge scores for those exact outputs, replayed — no judge
call was made to build these cards.

<div class="amw-compare" markdown="1">
<div class="amw-compare__card amw-compare__card--ships" markdown="1">
<p class="amw-compare__title">gemini_targeted_v1 <span class="amw-compare__tag amw-compare__tag--ships">Prompt that ships</span></p>
<p class="amw-compare__verdict amw-compare__verdict--pass">Passes the alt clause</p>
<div class="amw-compare__row"><span class="amw-compare__key">Model</span><span class="amw-compare__val">Gemini 2.5 Flash <em>vs</em> Claude Sonnet 5</span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Disagreements</span><span class="amw-compare__val">51</span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Wins / losses / ties, overall</span><span class="amw-compare__val"><span class="amw-wl amw-wl--win">15W</span><span class="amw-wl amw-wl--loss">3L</span><span class="amw-wl amw-wl--tie">33T</span></span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Excluding malformed baseline emissions</span><span class="amw-compare__val"><span class="amw-wl amw-wl--win">9W</span><span class="amw-wl amw-wl--loss">3L</span></span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Structured agreement</span><span class="amw-compare__val">0.643 [0.529, 0.757]</span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Verdict</span><span class="amw-compare__val">Passes on either figure</span></div>
</div>
<div class="amw-compare__card amw-compare__card--replaced" markdown="1">
<p class="amw-compare__title">gemini_tuned_v1 <span class="amw-compare__tag amw-compare__tag--replaced">Replaced — kept as the control</span></p>
<p class="amw-compare__verdict amw-compare__verdict--fail">Fails the alt clause</p>
<div class="amw-compare__row"><span class="amw-compare__key">Model</span><span class="amw-compare__val">Gemini 2.5 Flash <em>vs</em> Claude Sonnet 5</span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Disagreements</span><span class="amw-compare__val">60</span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Wins / losses / ties, overall</span><span class="amw-compare__val"><span class="amw-wl amw-wl--win">14W</span><span class="amw-wl amw-wl--loss">20L</span><span class="amw-wl amw-wl--tie">26T</span></span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Excluding malformed baseline emissions</span><span class="amw-compare__val"><span class="amw-wl amw-wl--win">8W</span><span class="amw-wl amw-wl--loss">15L</span></span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Structured agreement</span><span class="amw-compare__val">0.557 [0.443, 0.671]</span></div>
<div class="amw-compare__row"><span class="amw-compare__key">Verdict</span><span class="amw-compare__val">Fails on either figure</span></div>
</div>
</div>

**Same model, same data — three instruction rules written from the measured
failures moved the adjudication from 14W/20L to 15W/3L.** Both arms have
`json_schema_validity` 1.000 [1.000, 1.000] and both are the same Gemini 2.5 Flash
model on the same corpus. Nothing changed but the prompt, and the rules were not
guessed: each one was written from a loss cluster counted in the n=70
adjudication of the arm on the right.

Then read the deployment row above: the same prompt, on Gemini 3.6 Flash, holds at
16W/2L. **The rules transferred across a model generation** — which is the whole
reason this development-generation card is still on the page.

For completeness, the other two subagents on the development generation, both
against **Claude Sonnet 5** with the candidate on **Gemini 2.5 Flash**:

| Subagent | Candidate | Disagreements | W / L / T | Excluding malformed | Structured agreement |
|---|---|---|---|---|---|
| chunk_summarizer | `gemini_tuned_v1` | 61 | 3W / 11L / 47T | 2W / 11L | 0.957 [0.900, 1.000] |
| feature_extractor | `gemini_tuned_v1` | 56 | 5W / 39L / 12T | 5W / 37L | 0.971 [0.929, 1.000] |

Feature Extractor's 5W/39L on the development generation against 31W/16L on the
deployment generation is the largest single movement in this study, and it comes
from a different shipping prompt (`gemini_optimizer_v1`) as well as a different
model. Those two causes are **not** separated by this table, and no claim here
attributes the movement to either one.

## Ties are not "not adjudicated"

A **tie** means the recorded judge scored both arms equally on that item. **Not
adjudicated** means no recorded verdict exists for it, and adjudicating it would
require new judge calls. On every run on this page, `not_adjudicated` is 0 for all
three subagents — every disagreement has a recorded verdict behind it.

## Latency, and why the shadow run has no number

The shadow run does capture per-arm latency. It also captures why you cannot use
it:

> **CROSS-REGION:** `claude-sonnet` ran in `global`, `gemini-flash` in
> `us-central1`. Latency here compares two regions and is not a like-for-like
> p95; the `gates.yaml` basis for `latency_p95` asks for same region, same load
> profile.

So on the shadow runs, `latency_p95` renders as **not comparable — region split
disclosed**. Not evaluated, and specifically not passed.

That is a property of how these particular calls were routed, not a permanent
hole in the method. The deployment candidates were separately probed with both
arms pinned to `global`, which is the only input that can open the gate; the probe
refuses to record itself at all if the two arms end up in different regions. The
per-subagent results are on the [scorecard](../results/scorecard.md) — and they
are directional, at n=10 per arm, with the caveat printed beside them.

---

**Next:** [Module 08 — The scorecard](08-the-scorecard.md)

*Source: deployment generation — `artifacts/results/shadow_current_{query_rewriter,chunk_summarizer,feature_extractor}.json` and `artifacts/results/shadow_triage_current_*.md`, candidate model `gemini-flash-current` (Gemini 3.6 Flash). Development generation — `artifacts/results/shadow_widened.json`, `artifacts/results/shadow_qr_targeted.json`, `artifacts/results/shadow_triage_widened.md`, `artifacts/results/shadow_triage_qr_targeted.md`, candidate model `gemini-flash` (Gemini 2.5 Flash). Incumbent `claude-sonnet` (Claude Sonnet 5) throughout. Mode `replay`, similarity metric `token_jaccard_lexical_proxy`, prose threshold 0.6, bootstrap seed `20260812`.*
