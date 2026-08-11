# Act 1 Build Plan — Workshop-Ready by Thursday, Aug 13

> **Calendar revised 2026-08-11 by the owner:** delivery moved Wed Aug 12 → **Thu Aug 13**,
> content freeze moved Tue Aug 11 → **Wed Aug 12**. Day labels below are updated;
> the day-by-day *content* is unchanged.

**Scope**: Act 1 (workshop) only. Act 2 (BYOT) deferred — *except the canonical trace schema*, which the replay layer needs anyway. Recording every trace in that schema from day one means Act 2 later is just converters, zero refactor.

**Clock**: Thu Aug 6 → **Thu Aug 13** delivery. ≈ 4.5 build days + 1 hardening day, weekend as overflow buffer only.

---

## 1. Honest Schedule Assessment

Feasible **only** under three conditions:

1. **Claude access path decided today.** Recommended default: **Claude via Vertex Model Garden** (traffic stays inside GCP — direct Anthropic API calls from a corp-managed machine may hit egress restrictions). Fallbacks in order: direct Anthropic API → record the baseline corpus from wherever access exists and run baseline in replay. Someone must confirm Model Garden enablement + quota in the target project **today**.
2. **All platform risk front-loaded into Day-0 timeboxed spikes** (Vertex GenAI Eval Service, VAIPO, Claude adapter). Each spike gets go/no-go by end of Day 0. A red spike ships via its pre-written fallback (§3), never via hope. Nothing unspiked appears in the demo path.
3. **Wednesday is content-freeze.** Full live run (auto-builds the replay corpus), pricing verification, timed rehearsal, presentation fixes only. No feature work after Tuesday night.

Claude Code accelerates the build, but a human must drive: GCP auth (`gcloud auth application-default login`), quota/enablement checks, dataset sanity review (~10 items/subagent, Day 1 checkpoint), tuned-prompt judgment calls, and the rehearsal.

---

## 2. Scope: P0 / P1 / P2

| Tier | Item | Notes |
|---|---|---|
| **P0** (ship or no demo) | Config system: `models.yaml`, `pricing.yaml` (VERIFY placeholders + refresh script), `gates.yaml`, one demo customer profile | Prices never hardcoded elsewhere |
| P0 | Canonical trace schema + replay store + **record-on-live** | Also the Act 2 foundation |
| P0 | Adapters: Gemini live; Claude (chosen path); modes `live \| replay \| hybrid` | Hybrid = default workshop mode |
| P0 | **3 subagents**: Query Rewriter, Chunk Summarizer, Feature Extractor — each with Claude-baseline, naive-Gemini, tuned-Gemini prompt packs + strict `response_schema` | Root orchestrator = code stub + diagram only, not evaluated |
| P0 | Synthetic dataset: patents domain, ~70 cases/subagent (deterministic pass), 25–30 judged core, gold refs + per-item rubrics, provenance tags, fixed seed | Human sanity-check checkpoint Day 1 |
| P0 | Eval engine: deterministic metrics (schema validity, filter P/R, citation coverage) + local rubric-anchored LLM judge (rationales captured) + stats (k=2 repeats on core, bootstrap 95% CIs, paired deltas) | Local judge is the guaranteed lane |
| P0 | Ablation ladder **A0–A4 with hand-tuned rungs** driven by failure clusters | VAIPO replaces/augments A4 only if spike is green |
| P0 | Shadow runner + agreement + disagreement triage (judge-adjudicated win/loss/tie) | |
| P0 | Gates evaluator → MIGRATE / TUNE_FIRST / HOLD; **Markdown scorecard** with CI notation + provenance footer | |
| P0 | Economics: cost model at customer volumes + caching **breakeven calculator** (math only) | |
| P0 | 2 thin notebooks (baseline+tuning; shadow+scorecard) calling `amw/` functions | Logic lives in the library, notebooks display |
| P0 | `cli.py` phase runners with `--mode`; replay e2e script; golden-fixture tests for every metric | |
| P0 | Docs: `WORKSHOP_RUNBOOK.md` (run-of-show + fallback tree + talk track), gates one-pager, `objection_handling.md`, `data_request_onepager.md` | One-pager stays: it's the closing ask and costs ~30 min |
| **P1** (add iff spike green / time allows) | Vertex GenAI Eval Service rubric metrics + loss clustering | Spike S2 |
| P1 | VAIPO as ablation rung A4′ | Spike S3 |
| P1 | Dual-judge cross-check (Claude judge, 20% sample, agreement + κ on footer) | Cheap once `judge.py` exists |
| P1 | **Answer Drafter** subagent → the deliberate TUNE_FIRST scorecard row | Fallback honesty device if cut: show one core subagent mid-ladder |
| P1 | Live context-caching call (real cached-token counts on screen) | Calculator is P0 regardless |
| P1 | HTML scorecard; root-orchestrator runnable stub | |
| **P2** (post-delivery / Act 2) | BYOT converters, PII scrub, notebook 04, LLM Comparator export, multi-domain generator, local hill-climb APO, risk register, CI pipeline | Schema already in place |

---

## 3. Pre-Decided Fallbacks (written down now, not improvised on delivery day)

| Risk | Trigger | Fallback (honest framing) |
|---|---|---|
| Vertex Eval Service spike red | S2 fails Day 0 | Local judge pipeline only; failure clustering done by grouping on failed rubric criteria. Mention the managed service with docs — **do not fake its output**. |
| VAIPO spike red | S3 fails Day 0/1 | Ladder tops out at A4 hand-tuned (real, measured). Reference VAIPO as the automation path without showing numbers that weren't produced. |
| Model Garden not enabled in time | Day 0 check fails | Direct Anthropic API adapter; if egress blocks that, record baseline corpus from wherever access exists → baseline runs in replay, Gemini stays live (hybrid). |
| Live demo instability on delivery day | Anything | One flag: `--mode hybrid` → `--mode replay`. Wednesday's freeze run is the corpus. Notebook HTML exports as last-resort visuals. |
| Judged CIs too wide at n=25–30, k=2 | Wed review | Widen core set to 40 on the affected subagent overnight (runs are cheap); or report the metric as directional and lean on deterministic gates. |

---

## 4. Day-by-Day

### Day 0 — Thu Aug 6 (today, ~half day)
**Human (blocking, do first):** pick Claude path; confirm GCP project, region, Vertex quota, Model Garden enablement; `gcloud auth application-default login`; export env vars; obtain Anthropic key only if the direct-API path is chosen.

**Claude Code:** T01–T04 (scaffold, config system, trace schema + replay store, Gemini adapter with record-on-live, Claude adapter for the chosen path).

**Spikes (timeboxed 60–90 min each, human at keyboard for auth prompts):**
- **S1** Claude adapter round-trip: one real call, trace recorded, replayed back byte-identical.
- **S2** Vertex GenAI Eval Service hello-world: one rubric metric over 3 items, results parsed.
- **S3** VAIPO hello-world: one optimization iteration on a toy instruction completes and returns a candidate.

**EOD Day 0 gate:** go/no-go recorded per spike in `SPIKES.md` → locks P1 scope. No red item enters the demo path.

### Day 1 — Fri Aug 7
T06–T09: dataset generator + rubrics (human sanity-checks 10 items/subagent before full generation), three subagent prompt packs, metrics + judge + stats + golden tests, `cli.py phase2` end-to-end on 10 live items per subagent (traces auto-recorded).
**EOD:** first real baseline numbers exist (small n); failure clusters eyeballed to seed Monday's tuned prompts.

### Weekend Aug 8–9 (buffer only)
No planned scope. Good use if available: fire the full-set deterministic runs (cheap babysitting), or overflow from Day 1.

### Day 2 — Mon Aug 10 / Tue Aug 11
T10–T14: translator + ablation runner A0–A4 (tuned rungs iterated against Friday's failure clusters), shadow runner + triage, gates evaluator + scorecard + economics, thin notebooks. Then green-lit P1 items in order: Vertex metrics → dual-judge → Answer Drafter → VAIPO rung → caching live demo → HTML scorecard.
**Hard stop:** feature work ends Tuesday night.

### Day 3 — Wed Aug 12 (freeze + harden)
- **AM:** `scripts/refresh_pricing.py` (set `verified_on`, sources). Full live run of every phase → complete replay corpus + final scorecard artifacts. Judged-CI width review (fallback §3 if needed).
- **PM:** full timed rehearsal **in replay mode** against the runbook; presentation fixes only; export notebook HTML backups; drill the fallback flag once (`hybrid` → `replay` mid-notebook).

### Day 4 — Thu Aug 13 (delivery)
T-2h: `cli.py smoke --mode live -n 2` per backend; confirm replay flag; open in **hybrid** mode. Ship.

---

## 5. Sizing (descoped but still defensible)
- Deterministic metrics: full set (~70/subagent), single pass.
- Judged metrics: 25–30 core items × k=2 × {baseline, A0…A4} — a few hundred judged calls per subagent; cost and wall-clock are trivial. CIs are wider than the full design; gates in `gates.yaml` are checked against **CI lower bounds**, so wide-but-passing is still an honest pass, and wide-and-ambiguous gets reported as directional (never rounded up to "parity").

## 6. What Was Cut and Where It Went
Multi-domain generator, BYOT converters/PII scrub, LLM Comparator export, local hill-climb APO, CI pipeline, risk register → **P2 backlog** (Act 2 sprint). Nothing cut changes the delivery-day story; everything cut has a landing spot.
