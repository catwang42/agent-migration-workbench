# WORKSHOP_RUNBOOK.md — Agent Migration Workbench, Act 1

**Delivery:** Thu Aug 13, 2026 · **Content freeze:** Wed Aug 12
**Default mode:** `hybrid` (Claude replayed, Gemini live) · **Fallback:** `replay`
**Version:** 2026-08-11b — T10/T11/T12/T13 merged; every flag below is reconciled against
the shipped CLI. Reconcile again against the frozen corpus on Wed AM before delivery.

> Every command in this runbook has been run. If one of them does not exist or does not
> behave as narrated when you rehearse, that is the wiring having regressed, not the
> runbook being wrong — fix the wiring, don't edit the narration.

---

## 0. T-2h pre-flight (delivery machine, delivery network)

```bash
source .venv/bin/activate
gcloud auth list                                  # identity only — never print a token
python cli.py smoke --mode live -n 2              # both backends, real request shapes
python cli.py e2e --mode replay                   # zero-credential path still green
pytest tests/ -q
```

- `smoke` green → run the workshop in **hybrid**.
- `smoke` red on Gemini → run in **replay**. Say so on screen; the corpus is real and dated.
- `smoke` red on Claude → still fine in hybrid: Claude is replayed in hybrid anyway.

**`CLAUDE_PATH=vertex` only.** The direct Anthropic path has never had a live round trip
and is locked out of every demo flow. Do not first-run it on delivery day.

Execute both notebooks headless once, in the mode you intend to use, then export HTML —
those files are the last-resort visuals if the kernel dies on stage.

```bash
mkdir -p artifacts/notebooks artifacts/backup
papermill notebooks/01_baseline_and_tuning.ipynb \
    artifacts/notebooks/01.out.ipynb -p MODE hybrid          # ~10 min live; use replay to rehearse
papermill notebooks/02_shadow_scorecard.ipynb \
    artifacts/notebooks/02.out.ipynb -p SHADOW_METRIC structured
jupyter nbconvert --to html --output-dir artifacts/backup artifacts/notebooks/*.out.ipynb
```

Both notebooks default to `MODE = "replay"` in their parameters cell, so opening one costs
nothing. Notebook 02 also takes `SHADOW_METRIC` — see §2:05 below before you change it.

---

## 1. Run-of-show (~3h)

| Time | Segment | Command / asset | Live or pre-run |
|---|---|---|---|
| 0:00–0:20 | Decision framework + **gates sign-off** | `docs/migration_decision_framework.md`, `config/gates.yaml` | discussion |
| 0:20–0:35 | **Methodology beat**: matching instrument to autonomy level | `docs/what_we_measure.md` taxonomy table | discussion |
| 0:35–0:55 | Architecture mapping + cost calculator at *their* volumes | `cli.py scorecard --volume` | live, cheap |
| 0:55–1:15 | Reference system walkthrough — the ADK app, same prompt files the bench measured | `cli.py adk-demo --mode live` | live Gemini, 2 queries, **skippable** |
| 1:15–2:05 | Baseline eval: full n=70 pre-run + **live 10-case subset**; loss clusters; the naive-swap failure; the ablation ladder | notebook 01 | full set pre-run, subset live |
| 2:05–2:35 | Shadow scorecard + disagreement triage + ROI | notebook 02, `cli.py shadow --live-slice 5` | pre-run + one live slice |
| 2:35–2:55 | Verdicts vs gates → **the ask** | `docs/data_request_onepager.md` | discussion |
| 2:55–3:00 | Wrap | | |

Target ≥15 min slack. If you are behind at 2:05, cut the live slice, not the triage.

---

## 2. Per-segment commands and talk track

### 0:00 — Gates sign-off (do this *before* they see a single number)

```bash
cat config/gates.yaml
```

- "Before I show you any result, I want you to agree what 'pass' means. If we set the bar
  after seeing the numbers, the numbers are worthless."
- "Every gate is checked on the **95% confidence-interval lower bound**, not the point
  estimate. That's a deliberately harder test than most migration decks run."
- "The scorecard footer prints a hash of this file. If we moved a threshold after the run,
  you'd be able to tell."
- Get an explicit yes on the six gates. Write down any threshold they want changed — that
  is a real deliverable, not a derailment.

### 0:20 — The methodology beat: *we match the instrument to the subagent's autonomy level*

This is the intellectual core of the workshop. Spend the full 15 minutes.

Put up the taxonomy table from `docs/what_we_measure.md`. Then:

- "People ask 'can Gemini replace Claude in my agent system' as one question. It's four
  questions, because you have four kinds of subagent, and they fail differently."
- Walk the rows: transform → tool decider → retrieval → orchestration.
- "The instrument has to match what the model actually *decides*. A bench harness with
  gold references is the right tool for a single-call transform and the wrong tool for a
  looping orchestrator. Using it anyway doesn't give you a weak answer, it gives you a
  confident wrong one."
- Land it: "So today you get verdicts on Level 1 — measured in full, 70 cases per
  subagent, real calls. Levels 2 and 3 get named instruments and a follow-on, and they get
  **no verdict on this scorecard**. That line is in the footer."

**Skeptic answer — "but agents execute tools, so isn't your harness measuring the wrong
thing?"**

> "Good — this is the distinction the whole design rests on. The model never executes
> anything. It emits a structured request; *your runtime* executes it. So there are two
> separable things to measure, and they need different instruments.
>
> One: does the model emit a well-formed, correct request? That's a single-call transform.
> It has a gold answer, it's cheap to measure, and it's what I've measured here — 630
> real calls, schema validity, argument correctness, the lot.
>
> Two: given the whole conversation and the tools available, does it pick the *right* tool
> at the *right* moment, and does the loop terminate? That's trajectory behavior. It has no
> single gold answer, and you can only see it in the runtime with step-level traces.
>
> Today I'm giving you a hard verdict on the first and an honest 'not yet' on the second.
> If I gave you one number for both, you should distrust it."

### 0:35 — Economics at their volumes

```bash
# illustrative volumes (rehearsal default) — footer reads `volumes: illustrative`
python cli.py scorecard --mode replay \
    --shadow artifacts/results/shadow.json

# customer volumes entered live, in the room. --volumes-confirmed-by is not
# optional: the tool exits 2 rather than record an unattributed customer claim.
python cli.py scorecard --mode replay \
    --shadow artifacts/results/shadow.json \
    --volume query_rewriter:250000 \
    --volume chunk_summarizer:1200000 \
    --volume feature_extractor:400000 \
    --volumes-confirmed-by "<their name, said out loud>"
```

- Type their real call volumes in live. The footer flips from
  **`volumes: illustrative`** to **`volumes: customer-provided`** on screen.
- The three numbers above are the profile's **illustrative** figures. Overwrite them with
  what the customer says; do not present them as anyone's real traffic.
- **If pricing is still unverified, every dollar cell renders `—`.** Say this out loud
  rather than letting them notice: "I won't show you a savings percentage I can't source.
  The prices come from one file with a `verified_on` date and source URLs in the footer."
- The cache-breakeven calculator is math, not a measurement: show the formula and the
  breakeven calls/day, and be clear which is which.

### 0:55 — Reference system walkthrough: the ADK app (demo only, ~8 min)

```bash
python cli.py adk-demo --mode live      # 2 sample queries, ~40s each, ~6 Gemini calls
```

Five lines, in this order:

1. "This is the same three subagents, wired up as an ADK app on Vertex. Root orchestrator
   delegates; each leaf is one of the agents you've been looking at."
2. Point at the trace as it prints: "`[orchestration]` root, then `[prompt-based]`
   rewriter, retrieval, `[prompt-based]` summarizer, `[prompt-based]` extractor — those are
   the taxonomy rows from the table we did at 0:20, printed per agent as it runs."
3. Point at the `prompt:` line under each leaf: "That's the path and SHA of the prompt file
   on disk. The ADK app doesn't have its own copy — a test fails the build if the
   instruction text and the prompt pack ever differ."
4. "So the thing I measured and the thing you'd ship are the same prompts. That's the point
   of this segment; it isn't a benchmark."
5. "Nothing here goes near a scorecard. Every number you'll see from 1:15 runs through the
   adapter harness, which records every call so it replays offline. The ADK runtime has no
   such hook, so measuring through it would give you numbers I can't reproduce for you."

**Fallback:** if `adk-demo` does not run cleanly — no ADC, quota 429, ADK version drift —
**skip it entirely and say so**: "The runnable version of this is a side quest; here's the
architecture." Then show the diagram, open two prompt files under `amw/agents/prompts/`,
and go straight to the harness at 1:15. Nothing downstream depends on this segment, and
there is no replay mode for it — an ADK run is live or it does not happen, because no ADK
call is recorded and a replayed one would be fabricated.

### 1:15 — Baseline eval, the heart of it

```bash
python cli.py phase2 --mode hybrid -n 10        # LIVE, ~10 min, proves realness
# the statistics come from the pre-run:
#   artifacts/results/phase2_n70.json   (630 calls, 2026-08-09 → 08-10)
```

Never run the full eval live. The 10-case live subset proves the pipeline is real; the
pre-run provides the statistics. Say exactly that.

Four beats, in this order:

1. **The naive swap fails.** Gemini QR `exact_match_intent` drops to 0.571 [0.457, 0.686]
   when you paste the Claude XML prompt in verbatim. "This is what 'just point it at
   Gemini' looks like."
2. **The ladder closes it.** Tuned QR reaches 0.814 [0.714, 0.900] — above Claude's 0.729,
   with overlapping CIs. "Parity within measurement. Not 'better'. I'm not going to
   oversell an overlap."
3. **The baseline has a defect too.** Claude's QR schema validity is 0.814 against a 0.99
   gate — with the mechanism caveat welded to the cell. "This is not a Claude limitation;
   it's this org's Vertex policy forcing tool-use JSON. I'm showing it because a migration
   assessment that only finds problems on the target side isn't an assessment."
4. **Tuning made one subagent worse.** FE tuned 0.795 < naive 0.821. Do not hide this —
   see the objection doc; it is the strongest credibility moment in the deck.

### 2:05 — Shadow + triage

```bash
python cli.py shadow --mode replay                     # agreement + triage, from recordings
python cli.py shadow --mode hybrid --live-slice 5      # ~30 calls, the live head-to-head
```

- The full shadow comparison is computed from the recorded corpus — no re-run, no new spend.
- `--live-slice 5` is the stage moment: five items per subagent, both backends, live, in
  front of them. ~30 calls, not 630.
- Browse the triage table. Land on a disagreement where the judge sided with Claude — the
  point is that the pipeline is capable of saying no.

**Know which agreement figure is on screen before you say the word "agreement".** There
are two, they are not interchangeable, and on this corpus they differ by up to 8x:

```bash
python cli.py scorecard --mode replay --shadow artifacts/results/shadow.json \
    --shadow-metric structured     # fields with a defined right answer (default)
python cli.py scorecard --mode replay --shadow artifacts/results/shadow.json \
    --shadow-metric item           # every field must match; prose via a token-overlap proxy
```

The rendered card names the figure it used in each subagent's notes. Under `structured`,
Chunk Summarizer and Feature Extractor pass the agreement gate and Query Rewriter fails
it; under `item` all three fail. If a customer asks why you chose one: the item-level
figure moves with the proxy's threshold — 0.6 → 0.4 takes Query Rewriter from 0.143 to
0.357 — so a gate checked on it is substantially measuring the instrument. Say that
rather than defending the number.

### 2:35 — The ask

Hand over `docs/data_request_onepager.md`. Agree pilot subagents using the taxonomy table
in that doc. Schedule the real-trace eval.

---

## 3. Fallback tree — one flag, identical narration

```
--mode live  →  --mode hybrid  →  --mode replay
```

The narration does not change between modes. That is the design: replay serves
*previously recorded real calls* and prints the recording window on screen.

| Symptom | Action | What you say |
|---|---|---|
| A Gemini call hangs or 429s mid-notebook | Change `--mode hybrid` → `--mode replay` in the parameter cell, re-run | "Switching to the recorded corpus — same numbers, made on the 9th, and the screen says so." |
| Claude call fails | Nothing. Hybrid already replays Claude. | — |
| Notebook kernel dies | Open the HTML export in `artifacts/backup/` | "Falling back to the exported run." |
| Whole network is down | `python cli.py e2e --mode replay` + HTML exports. Zero credentials required. | "Everything you're about to see runs offline. That's a design property, not a workaround." |
| Live slice fails on stage | Skip it, go to the recorded triage table | "That's the demo gods. The recorded 630-call comparison is what the verdict rests on anyway." |
| Someone asks for a number that isn't there | Say it isn't measured, and name the instrument that would measure it | Never estimate on your feet. |

### The one-flag drill — rehearse this Wednesday, once, mid-notebook

1. Start notebook 01 with `MODE = "hybrid"`. Run to the live subset cell (§4 of the notebook).
2. Mid-run, change `MODE` to `"replay"` in the parameters cell at the top and re-run that cell.
3. Re-run the live cell. Confirm the on-screen banner switches to
   `REPLAY — every number above comes from calls recorded <from> to <to>`.
4. Time it. It must be under 30 seconds including narration.

If the banner does not appear, stop and fix it before delivery. The banner *is* the
honesty guarantee; without it, replay mode looks like a live run.

---

## 4. Hard rules on stage

1. **Never show a number that wasn't measured.** Cells that couldn't be measured say so.
2. **Never say "zero quality drop."** Say *"quality parity within measurement under
   pre-agreed gates."* Gates are CI lower bounds; overlapping intervals mean parity, not
   superiority.
3. **Never generalise the org-policy finding.** Say "under this demo organization's Vertex
   policy configuration…", never "Claude doesn't support structured outputs on Vertex."
4. **Never present latency as a comparison.** Claude ran in `global`, Gemini in
   `us-central1`. The cell says *"not comparable — region split disclosed"* and you say
   the same.
5. **Never quote a dollar figure while `pricing.yaml` is unverified.** Em-dashes are the
   correct output.
6. **Label the data as synthetic every time it comes up.** It is on every artifact anyway.
7. **Never say "agreement" without saying which one.** Structured-fields-only and
   whole-item are different measurements of different things. The card names the figure it
   used; name it out loud too.

---

## 5. Post-workshop, same day

- Commit any recordings made live during the session (`artifacts/replay/`). Record-on-live
  is always on, so the demo itself extends the corpus.
- Note which gates the customer wanted changed.
- Note which subagents they classified into which taxonomy row — that is the scope of the
  follow-on.
