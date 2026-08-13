# RUNBOOK UPDATES — final-results pass (2026-08-13)

Three surgical edits to `WORKSHOP_RUNBOOK.md` + one optional README addition.
Apply via Claude Code (paste this file, ask it to apply, commit, push) or edit
directly — if you edit on GitHub, the VM must `git pull` before any further work.

---

## EDIT 1 — REPLACE the whole "### 1:15 — Baseline eval, the heart of it" section with:

### 1:15 — Act one: the development generation (how we got here)

```bash
python cli.py phase2 --mode hybrid -n 10        # LIVE, ~10 min, proves realness
# statistics come from the pre-runs; the screen names the artifact and its dates
```

Never run the full eval live. The 10-case live subset proves the pipeline is
real; the pre-runs provide the statistics. Say exactly that.

Four beats, unchanged — this is the *method* story, on Gemini 2.5 Flash
(development generation), and say that scoping out loud:

1. **The naive swap fails.** Query Rewriter intent match drops to 0.571
   [0.457, 0.686] with the Claude prompt pasted in verbatim. "This is what
   'just point it at Gemini' looks like."
2. **The ladder closes it.** Tuned Query Rewriter reaches 0.814 [0.714,
   0.900] vs Claude's 0.729 — overlapping intervals. "Parity within
   measurement. I'm not going to oversell an overlap."
3. **The baseline has a defect too.** Claude's schema validity 0.814 against
   a 0.99 gate — mechanism caveat welded to the cell. "An assessment that
   only finds problems on the target side isn't an assessment."
4. **Tuning made one subagent worse — and the judge caught it.** Feature
   Extractor tuned 0.795 < naive 0.821; the optimizer later recovered it.
   Strongest credibility moment before act two.

### 1:40 — Act two: the deployment answer (Gemini 3.6 Flash, thinking capped)

Open `artifacts/results/scorecard_current-capped.md` — the deliverable file,
not a webpage. Five beats:

1. **The generation moved, so we moved.** "Everything you just saw was
   measured on the development generation. The verdicts you're about to see
   are on the model you'd deploy today — same prompts, same corpus, same
   judges, same gates."
2. **Quality holds — and on the headline worker, wins.** Query Rewriter
   judge 0.959 [0.934, 0.982] vs Claude 0.886 [0.838, 0.932]: +7.3 points
   with the lower bound above zero. A measured quality win, confirmed by a
   judge from each vendor. The other two workers: statistical ties —
   intervals span zero; say "parity not demonstrated either way at this
   sample size."
3. **The thinking tax — the finding nobody else has.** "At default
   settings this generation *thinks* before answering and bills the
   thinking as output. We proved it arithmetically: answers the same
   length as Claude's, billed 2–2.6× the tokens — 56–61% of the output
   bill was reasoning the model discarded. Every team running high-volume
   micro-tasks on this generation is paying that tax right now,
   unmeasured."
4. **The fix is a configuration line.** "Cap the thinking budget and the
   same workload, same quality instruments, lands at savings of 42.7%,
   56.1%, and 64.8% against Claude Sonnet 5 — every worker clearing the
   30% bar we signed before any number existed. No threshold moved after
   the numbers landed; the hash on the footer proves it."
5. **The verdicts, honestly.** Query Rewriter: INCOMPLETE, provisional
   MIGRATE — one gate is inapplicable by instrument design and the engine
   refuses to invent a rule. The other two: UNDETERMINED — quality ties
   plus demo-window latency; "exactly the two things you resolve by running
   this on your own traffic — which is the engagement we're proposing."

---

## EDIT 2 — REPLACE Hard rule 4 with:

4. **Present latency only with its caveats attached.** It was measured
   same-region (n=10 per arm): Query Rewriter passes (6,471 ms vs Claude's
   6,893); Chunk Summarizer and Feature Extractor fail. The baseline itself
   drifted 3,471 → 6,893 ms across the afternoon, so say: "directional —
   latency gets measured on your infrastructure, not a demo window." The
   old "not comparable — region split" line applies ONLY to the
   development-generation shadow runs, scoped as history.

## EDIT 3 — REPLACE Hard rule 5 with:

5. **Quote only stamped, measured dollars — and name their basis.** Prices
   were human-verified 2026-08-12 (sources + verifier on every footer).
   Corpus-measured cost is not a volume projection: run-rate cells stay
   em-dashes until the customer's volumes are entered live, and the tool
   refuses to record unattributed volume claims. Say the savings
   percentages freely — they are measured; say monthly dollars only after
   their volumes go in on screen.

---

## PRESENTER CHEAT CARD (add at the top of the runbook, after the mode line)

- Headline: **QR judge 0.959 vs 0.886** (+7.3, lower bound +2.9) — quality win
- Capped savings vs Claude Sonnet 5: **QR +42.7 · CS +56.1 · FE +64.8** (all
  clear the pre-registered 30% bar on CI lower bounds)
- Thinking tax: **56–61% of billed output discarded** at defaults; fix = one
  config line (thinking budget)
- Verdicts: **QR INCOMPLETE (provisional MIGRATE) · CS, FE UNDETERMINED**
  (quality ties + demo-window latency → "prove it on your traffic")
- Judges: Gemini 2.5 Pro gated · Claude Sonnet 5 cross-check (93–99%
  agreement) — quote gated, cross-check beside, never merged
- Gates: version 1, hash **92f9d018432f** — "no threshold moved after the
  numbers landed"
- Files of record: `artifacts/results/scorecard_current-capped.md` ·
  `selection_table.md` · `crosscheck.md` · `models-in-this-study`

---

## OPTIONAL README ADDITION (after the corpus paragraph, ~line 88)

> **Headline result (freeze-v1):** on the deployment candidate with a
> minimised reasoning budget, Query Rewriter shows a measured quality win
> (judge 0.959 vs 0.886) and all three subagents clear the pre-registered
> 30% cost gate (+42.7% / +56.1% / +64.8%). Full card:
> [`artifacts/results/scorecard_current-capped.md`](artifacts/results/scorecard_current-capped.md)
> · judge cross-check: [`artifacts/results/crosscheck.md`](artifacts/results/crosscheck.md)
