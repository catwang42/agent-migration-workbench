# What We Need From You — one page

**Version:** 2026-08-11 · Follow-on to the Act 1 workshop.

Today you saw the methodology run end-to-end on a synthetic patents corpus. Everything you
saw was real model calls against fake data. To turn it into an answer about **your** system,
we need three things: a classification of your subagents, a sample of your traffic, and two
numbers only you have.

Rough effort on your side: **half a day of one engineer, plus a volumes lookup.**

---

## 1. Classify your subagents (30 minutes, in a spreadsheet)

Put every subagent in your stack into exactly one row. The row determines which instrument
applies and whether we can give you a verdict.

| Subagent pattern | Autonomy level | What the model actually decides | Instrument | Status in this workshop |
|---|---|---|---|---|
| **Prompt-based** | **Level 1 — transform** | Output content only. One call in, one structured object out. | **Bench harness**: gold references, deterministic metrics, rubric-anchored judge, bootstrap CIs | **Measured in full here. Verdict-capable.** |
| **Tool-calling** | **Level 2 — tool decider** | *Which* tool to call and with what arguments | **Tool-call quality scoring**: selection accuracy, argument validity, call-sequence sanity | **Partial.** Argument/schema formatting is measured now on recorded calls; *selection* accuracy is follow-on. |
| **Retrieval-augmented** | **Level 2/3** | What to retrieve, and whether the answer is supported by it | **Retrieval + groundedness trajectory evaluation**: recall@k, citation faithfulness across the whole chain | **Follow-on.** Citation coverage is measured here for the summarizer, but end-to-end retrieval quality is not. |
| **Orchestration** | **Level 3 — looping** | When to loop, when to delegate, when to stop | **Trajectory evaluation in the runtime**: step-level traces, task completion, cost-per-resolution | **Follow-on. HOLD on this scorecard — no verdict today.** |

This table is reproduced verbatim from `docs/what_we_measure.md`. If the two ever differ,
that one is canonical.

**What we need back:** one row per subagent — name, taxonomy row, calls/day, current model,
and whether it's on the critical path.

Sort by calls/day. The Level 1 rows at the top of that list are your migration candidates:
biggest share of the inference bill, lowest architectural risk, strongest measurement. The
orchestrator stays where it is until it gets its own instrument.

---

## 2. A sample of real traces

**Ask: 200–500 traces per candidate subagent.** More is better; 200 is enough for usable CIs
at the gate thresholds we agreed.

Per trace, the minimum:

| Field | Why |
|---|---|
| Input (the user/system content that reached the subagent) | Reproduces the call |
| Retrieved context chunks, if any | Groundedness metrics need the supplied set |
| Tools offered, if any | Part of the replay key; changes model behavior |
| The model's output | Becomes the incumbent baseline |
| Model ID and prompt version | So we know what we're comparing against |
| Timestamp | Sampling window and drift |

Nice to have, materially better if present:

- **Outcome labels** — thumbs, escalations, retries, downstream success. Turns "different" into
  "worse," which is the difference between an opinion and a gate.
- **Known-bad cases.** Your worst 20 traces are worth 200 average ones.
- **A rough distribution note** — which query types dominate, any seasonality.

**Format:** JSONL, one trace per line, any field names — we write the converter. If you already
have LangSmith / Langfuse / Vertex logging exports, send those raw; they are all supported
conversion sources.

### Privacy — assume nothing leaves your control until you say so

- **Redact before sending.** PII scrub runs on our side too, but yours is authoritative.
- **Pseudonymised entities are fine.** Consistent placeholders (`COMPANY_A`) preserve the
  structure the metrics need.
- **Or don't send anything.** The whole harness runs in your project against your data; we can
  ship the converters and run it there. Air-gapped is a supported mode, not a special case.
- Every ingested item is tagged `provenance: customer` and stays visibly distinct from
  synthetic items in every artifact.

---

## 3. Two numbers only you have

**Call volumes per subagent per day.** Today's scorecard used illustrative volumes and said so
in the footer. With your real volumes the footer flips to `volumes: customer-provided` and the
cost gate becomes meaningful. Peak and average both, if you have them.

**Your latency and quality SLOs.** The p95 you actually have to hold, and what a quality
regression costs you. These set the gate thresholds — the defaults in `config/gates.yaml` are
our recommendation, not your requirements.

---

## What you get back

1. **A scorecard on your data** — same gates, same CI-lower-bound discipline, one verdict per
   Level 1 subagent: MIGRATE / TUNE_FIRST / HOLD.
2. **Tuned prompt packs** for each candidate, with the ablation ladder showing what each change
   bought.
3. **A replayable corpus** of your traces plus every call we make — so the evaluation is
   re-runnable by you, offline, with zero credentials, after we leave.
4. **Named instruments and scope** for your Level 2/3 subagents, with a real plan rather than
   an extrapolated verdict.
5. **A cost model at your volumes**, with prices sourced and dated, and caching breakeven shown
   as arithmetic separate from the measurement.

---

## The sequence

| Step | Who | Effort |
|---|---|---|
| Return the subagent classification sheet | You | 30 min |
| Agree pilot subagents (2–3) and gate thresholds | Together | 1 hour |
| Export traces for the pilot subagents | You | 2–4 hours |
| Convert, spot-check, run baseline | Us | 2 days |
| Tuning ladder + shadow comparison | Us | 3 days |
| Scorecard review and cutover decision | Together | 2 hours |

**The single blocking item is the trace export.** Everything downstream waits on it, so start
there — even a partial sample from one subagent unblocks the converter work.

---

### Contact / next step

Send the classification sheet first. It's the cheapest artifact and it determines everything
else: which subagents are in the pilot, which instruments we build, and what a verdict on your
system will actually mean.
