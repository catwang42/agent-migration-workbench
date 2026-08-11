# Migration Decision Framework — the gates one-pager

**Version:** 2026-08-11 · Thresholds are authoritative in [`config/gates.yaml`](../config/gates.yaml)
(version `1`). This page explains them; that file *is* them. The scorecard footer prints a
hash of it.

---

## The rule

> **Agree what "pass" means before you see a number.**

Thresholds are signed off in the first twenty minutes of the workshop, before any result is
shown. Every scorecard prints the gates-file version hash, so any later reader can verify the
bar wasn't moved to fit the outcome.

Every gate is tested against the **95% bootstrap CI bound** — the *lower* bound for `min`
gates, the *upper* bound for `max` gates — never the point estimate. 10,000 resamples,
seed `20260812`. This is a deliberately harder test: a metric that looks good on average but
is unstable across the corpus fails.

That is also why nothing here ever says "zero quality drop." The claim these gates support is
**"quality parity within measurement under pre-agreed gates."**

---

## The six gates

| Gate | Bar | Tested on | What it protects |
|---|---|---|---|
| `quality_delta_pp` | ≥ **−2.0 pp** | CI lower bound of (Gemini − Claude) judged score | Task quality. Small regressions tolerated, drift is not. |
| `json_schema_validity` | ≥ **0.99** | CI lower bound of the fraction parsing against the subagent's `response_schema` | **Blocking.** A response that doesn't parse is an outage, not a quality dip. |
| `groundedness_delta_pp` | ≥ **−1.0 pp** | CI lower bound of (Gemini − Claude) citation-supported-claim rate | Faithfulness. Tighter than general quality because ungrounded claims are the RAG failure mode. |
| `shadow_agreement` | ≥ **0.90**, *or* on disagreements judge-adjudicated wins ≥ losses | CI lower bound of task-level agreement on shadow traffic | **Blocking.** Behavioral compatibility with what production does today. The `alt` clause lets a model that disagrees *and is right* still pass. |
| `cost_savings_pct` | ≥ **30%** | Customer volumes × uncached list prices | The reason to migrate at all. Caching upside is reported separately, never folded in. |
| `latency_p95` | ≤ **`claude_baseline_p95`** | Same region, same load profile | No latency regression against the incumbent. The sentinel resolves to the measured Claude p95 for that subagent. |

**Sentinels are hard errors, not skips.** If `claude_baseline_p95` cannot be resolved from a
measured same-region baseline, the scorecard raises rather than quietly dropping the gate. In
this run it does not resolve — Claude ran in `global`, Gemini in `us-central1` — so the
latency cell renders *"not comparable — region split disclosed."*

**Cost is double-gated.** A dollar figure requires (1) prices verified in
`config/pricing.yaml` with a `verified_on` date and source URLs, and (2) customer-confirmed
volumes. Fail either and every cost cell renders an em-dash. Illustrative volumes are
labelled `volumes: illustrative` in the footer; volumes typed in during the session flip it to
`volumes: customer-provided`.

---

## The three verdicts

Verdicts are per subagent, never for "the migration" as a whole.

```
any blocking gate fails            → HOLD
only quality gates fail            → TUNE_FIRST
all gates pass                     → MIGRATE
```

| Verdict | Rule | Means | Next action |
|---|---|---|---|
| **MIGRATE** | `all_pass` | Every gate clears on its CI bound. | Migrate this subagent. Shadow in production for one week, then cut over. |
| **TUNE_FIRST** | `only_quality_gates_fail` — quality gates are `quality_delta_pp`, `groundedness_delta_pp` | Structure and behavioral compatibility hold; quality is short. | Prompt/tuning work against the observed failure clusters, then re-run the same gates. Usually days, not weeks. |
| **HOLD** | `any_blocking_gate_fails` — blocking gates are `json_schema_validity`, `shadow_agreement` | Structural failure: output doesn't parse reliably, or behavior diverges from production. | Do not migrate on the current prompt pack. Fix the structural problem or keep the incumbent. |

A gate that is neither blocking nor a quality gate — `cost_savings_pct`, `latency_p95` — still
has to pass for MIGRATE. It just can't by itself force HOLD.

---

## Scope of a verdict

A verdict applies to **one subagent's measured behavior class**, on **this corpus**, under
**these thresholds**, at **this sample size**. It does not generalise:

- **Not to other behavior classes.** Level 1 single-call transforms are measured in full here.
  Tool-selection and multi-step trajectory behaviors are evaluated with their own instruments
  in the follow-on and receive **no verdict today**. See
  [`what_we_measure.md`](what_we_measure.md).
- **Not to other subagents.** Each row is independent. A MIGRATE on the summarizer says
  nothing about the orchestrator.
- **Not past a prompt change.** Verdicts are bound to a prompt pack version. Change the
  prompt, re-run the gates.
- **Not from synthetic to your traffic.** This corpus is `provenance: synthetic`. Converting
  the methodology to your traces is the ask — see
  [`data_request_onepager.md`](data_request_onepager.md).

---

## Reading a scorecard cell

Cells carry their own caveats so the number can't travel without them:

| You see | It means |
|---|---|
| `0.814 [0.714, 0.900]` | Point estimate with 95% bootstrap CI. The gate was tested on the bound, not the 0.814. |
| `judge 0.900 (n=70, full)` / `(n=28, core)` | Judged sample size and split. Scores at different n are not directly comparable. |
| `— ` (em-dash) | Not derivable. Prices unverified or volumes unconfirmed. Never a zero. |
| `not comparable — region split disclosed` | Measured, but across regions. Not a valid comparison. |
| a schema-validity number with the mechanism note in the same cell | Measured under tool-call structured emission because this org's Vertex policy blocked partner structured outputs. Environment finding, not a model ceiling. |
| `not measured` | The instrument for this doesn't exist in this workshop. The follow-on names it. |

Footer on every report: provenance, run date, recording window, region, gates version hash,
volumes basis, and the behavior-class taxonomy line.

---

## What to change if you disagree

The framework is meant to be argued with — that's the point of signing it before the run.
The levers, in order of how often they get pulled:

1. **Thresholds.** `−2.0 pp` too loose for your risk profile? Tighten it. Edit
   `config/gates.yaml`, re-run `python cli.py scorecard`. Verdicts recompute; the hash changes.
2. **Which gates block.** Moving `groundedness_delta_pp` into `blocking` turns faithfulness
   regressions from TUNE_FIRST into HOLD. One line.
3. **Sample size and split.** Wide CIs mean "we don't know yet," not "close enough." Widen the
   judged core set and re-run — judged calls are cheap.
4. **The corpus.** The strongest change: replace synthetic items with your traces.
