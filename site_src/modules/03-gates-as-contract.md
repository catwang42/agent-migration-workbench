# Module 03 — Gates as contract

*~6 min read · [02 The reference workload](02-reference-workload.md) → **03** → [04 The naive swap](04-the-naive-swap.md)*

---

A gate is a promise made **before** you see the answer. `config/gates.yaml` is the
only place a threshold exists in this repository, and the scorecard footer prints
its version hash — currently **version 1, hash `92f9d018432f`** — so a reader can
tell that no threshold moved after the numbers landed.

The workshop signs this file off in the first segment, before a single result is
shown.

## The header, verbatim

<!-- Embedded from the real file, not retyped: a hand-copied threshold is a
     threshold that can quietly disagree with the one the scorecard hashes. -->

```yaml
--8<-- "gates.yaml:1:11"
```

Verbatim from `config/gates.yaml`, version hash `92f9d018432f`.
{ .amw-chart__source }

## The six gates

Every "basis" below is a **95% confidence range** — the span the true value is
likely to sit in, given how few items were measured. Gates are checked against
the *worst* end of that range, never the middle: the lower bound where the gate
is a floor, the upper bound where it is a ceiling.

The `basis` strings inside `config/gates.yaml` write "confidence interval" in its
abbreviated form. That file is quoted verbatim here and its version hash is
printed in every report footer, so its wording is left exactly as it was signed
off; everywhere this site writes in its own voice, it says *confidence range*.

| Gate | Bound | Basis |
|---|---|---|
| `quality_delta_pp` | `min: -2.0` | 95% confidence-range lower bound of (Gemini − Claude) judged score, in percentage points |
| `json_schema_validity` | `min: 0.99` | 95% confidence-range lower bound of the fraction of responses parsing against the subagent `response_schema` |
| `groundedness_delta_pp` | `min: -1.0` | 95% confidence-range lower bound of (Gemini − Claude) citation-supported-claim rate, in percentage points |
| `shadow_agreement` | `min: 0.90` | 95% confidence-range lower bound of task-level agreement between Claude and Gemini on shadow traffic |
| `cost_savings_pct` | `min: 30` | Customer volumes from the profile, uncached list prices; caching upside reported separately |
| `latency_p95` | `max: claude_baseline_p95` | Same region, same load profile; the sentinel resolves to the measured Claude p95 for this subagent |

## Why confidence-range bounds and not point estimates

Every gate is tested against the 95% bootstrap confidence-range bound — the
lower bound for `min` gates, the upper bound for `max` gates. 10,000 resamples,
seed `20260812`.

This is a strictly harder bar than the point estimate, and it is deliberately the
harder direction for the candidate. It is also what licenses the only parity
sentence this project is allowed to write:

> quality parity within measurement under pre-agreed gates

Not a stronger sentence than that. A passing confidence-range bound says the data cannot
distinguish the arms at this sample size under this threshold. It does not say the
arms are identical, and no report here claims that.

## The `alt` clause on `shadow_agreement`

This is the clause that gets the most questions, so it is worth reading closely:

```yaml
  shadow_agreement:
    min: 0.90
    alt: "on disagreements, judge-adjudicated wins >= losses"
    basis: "95% CI lower bound of task-level agreement between Claude and Gemini on shadow traffic"
```

Raw agreement between two models is a blunt instrument. Two arms can disagree on
a large fraction of items and the candidate can still be *right* more often than
the incumbent on exactly those items — that is a different situation from
disagreeing and being wrong, and one number cannot carry both.

So the gate has two ways to be satisfied:

1. **The primary bound.** Agreement's 95% confidence-range lower bound is at or above 0.90.
2. **The `alt` clause.** Where the arms disagree, the recorded judge scores are
   adjudicated item by item, and the candidate's wins must be at least its losses.

The `alt` clause is not a softer gate; it is a *different question*, agreed in
advance, for the case where the primary one is uninformative. Module 07 walks
through an arm that passes on it and an arm that fails on it — with the same
prompt family and the same schema validity.

!!! note "What agreement counts"

    `shadow_agreement` counts **structured fields only** — the fields with a
    defined right answer. Prose fields are excluded and adjudicated separately in
    the disagreement triage. The figure is not a claim that the prose matched.

## Verdicts are a function of the gates, not a judgement call

```yaml
verdicts:
  MIGRATE:
    rule: all_pass
  TUNE_FIRST:
    rule: only_quality_gates_fail
    quality: [quality_delta_pp, groundedness_delta_pp]
  HOLD:
    rule: any_blocking_gate_fails
    blocking: [json_schema_validity, shadow_agreement]
```

- **MIGRATE** — all gates pass on their confidence-range bound.
- **TUNE_FIRST** — schema and agreement hold; quality is short. Prompt or tuning
  work, then re-run.
- **HOLD** — a blocking gate failed. Structural failure. Do not migrate this
  subagent on the current prompt pack.

A gate that appears in neither `blocking` nor `quality` still has to pass for
MIGRATE; it just does not by itself force HOLD.

## Sentinels, and the gate that is *not passed*

```yaml
sentinels:
  - claude_baseline_p95
```

A `min` or `max` may be a sentinel instead of a number, resolved at scorecard time
against measured baseline statistics. **An unresolved sentinel is a hard error,
never a skipped gate.**

On the **deployment generation** the sentinel resolves, because **Claude Sonnet
5 (via Vertex partner models)** and **Gemini 3.6 Flash (capped thinking)** were
probed with both arms pinned to `global`. The cell on the shipping scorecard
reads, verbatim:

| Gate | Bound (gates.yaml) | Measured (95% confidence range) | Bound tested | Result |
| --- | --- | --- | --- | --- |
| `latency_p95` | `<= claude_baseline_p95` | 6,471 ms [4,024 ms, 6,602 ms] (same-region probe, global) | ci_upper = 6602 | PASS |

That is a resolved sentinel: `claude_baseline_p95` became the incumbent's
measured 6,893 ms, and the candidate's *upper* bound is tested against it. The
probe carries a validator that refuses to produce a record at all if the two arms
landed in different regions, which is what stops "same region" from being an
assertion somebody typed.

The same rule produces the most-misread cell on the other card. On the
**development-generation** runs, Claude ran in region `global` (the `us-central1`
partner quota was exhausted) while Gemini and the judge ran in `us-central1`. The
`claude_baseline_p95` sentinel resolves only from a same-region probe, so on
those runs `latency_p95` renders as:

> not comparable — region split disclosed

That is **not evaluated**. It is emphatically not *passed*. Quality and cost gates
are unaffected by the region split; the latency gate simply has no measurement
behind it and says so.

## The gate with two human-cleared preconditions

`cost_savings_pct` has two independent preconditions that no measurement can
open. One has cleared since this workshop was written; one has not.

| Gate | State | Clears when |
|---|---|---|
| pricing | **cleared 2026-08-12** — `config/pricing.yaml` has 0 rates still reading `VERIFY`, and `verified_on` plus the source URLs are stamped | a human runs `scripts/refresh_pricing.py` |
| volumes | **still closed** — the customer profile's volume block is illustrative (`volumes_confirmed: false`) | the customer states their call volumes |

With prices cleared, the scorecard can report what this 70-item corpus actually
cost on each arm: measured token counts at uncached list rates. It still cannot
report a monthly bill, an annual run rate, or a saving at customer scale, because
that requires a call volume nobody has confirmed. Those cells render an em dash —
not a zero, not a placeholder, not a range, because a placeholder in a cost cell
is the single easiest number on a scorecard to mistake for a measurement.

The two are kept in separate columns and separate sections of the report. A
corpus cost is a measurement; a run rate is a multiplication against an
assumption.

## Read it yourself

```bash
cat config/gates.yaml
python cli.py scorecard        # gates → verdicts → markdown report
```

---

**Next:** [Module 04 — The naive swap](04-the-naive-swap.md)

*Source: `config/gates.yaml` (version 1, hash `92f9d018432f`); the resolved latency cell from `artifacts/results/scorecard_current-capped.md` (deployment generation — Gemini 3.6 Flash, capped thinking); the unresolved one from `artifacts/results/scorecard_widened.md` (development generation — Gemini 2.5 Flash).*
