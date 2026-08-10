# Phase-2 n=70 baseline — validation summary

**Artifact:** `artifacts/results/phase2_n70.json`
**Live calls made:** 2026-08-09T16:07:01Z → 2026-08-10T02:45:59Z (UTC)
**Artifact assembled:** 2026-08-10, `mode=replay` over those recordings
**Written:** 2026-08-10

This is the full-corpus baseline the owner authorised on 2026-08-09 as the bridge
into Fan-out 3. It supersedes the live `-n 10` artifact (`artifacts/results/phase2.json`)
as the sizing of record; the `-n 10` artifact stays cited where the counterfactual
note refers to it (see [counterfactual_scorecard.md](counterfactual_scorecard.md)).

## What ran

| | |
|---|---|
| customer / dataset seed | `demo_patents` / `20260812`, generator `t06.1`, provenance `synthetic` |
| subagents × arms | 3 × 3 = 9 arms (`claude_baseline`, `gemini_naive` = A0, `gemini_tuned_v1` = A1–A3) |
| items per arm | 70 |
| generation calls | 630, **630 ok, 0 errors** |
| judge | Gemini 2.5 Pro, prompt `v1`, k=2 repeats, **0 failed repeats** |
| judged n | 28 (core split) for QR and CS; **70 (full corpus) for FE** |
| bootstrap | 95% CI, seed `20260812` |
| region | Gemini + judge `us-central1`; Claude `global` (see caveat 3) |

Total recorded in the window: **1,486 traces, 0 error traces** —
claude-sonnet 210, gemini-flash 491, gemini-pro (judge) 785.

## Sizing: one registered deviation, no silent ones

Per the owner's ruling, deterministic metrics run on the full 70 and the judge runs
on the 28-item core split × k=2 — **except FE, judged on its full 70 × k=2**, because
the contested `novelty_statement` finding and the judge noise both live there.

That exception is not a comment: it is machine-recorded. `JudgeReport.split` carries
`"all"` on the three FE arms and `"core"` on the other six, the CLI prints
`judge_score[all]` for the widened arms, and the artifact's `notes` field carries:

> judged on the FULL corpus, not the registered core split: feature_extractor. Every
> other subagent is judged on core only, so judged n differs across subagents — see
> each arm's judge.split and judge.items_scored before comparing judge scores.

A reader who compares an FE judge score against a QR one without noticing they are
n=70 vs n=28 has to ignore three separate labels to do it.

## Interruption and recovery — the part that needs disclosing

The first live run (pid 75831) was **signal-killed by session teardown at ~19:09 UTC**,
about 2.5 h in, before it wrote any artifact. Evidence it was a kill and not a crash:
the log ends mid-batch with no traceback, and the host had 13 GB free (not OOM).

Because record-on-live is unconditional (ground rule 5), every call it had already
made was already on disk. Recovery therefore did **not** re-run the whole baseline:
only the outstanding FE tuned-arm work was executed live (pid 3038, finishing
02:45:59Z), and the artifact was then assembled in `--mode replay` over the union.

Two consequences, both benign, both stated here rather than left to be discovered:

1. **Some keys were recorded twice.** The FE tuned arm's 70 generations exist from
   both the pre-kill pass (08-09 18–19h) and the resume (08-10 02h). The replay store
   supersedes on rewrite — later record wins — so the artifact reflects the 08-10 set.
   Both sets are genuine live calls; neither is fabricated.
2. **Generation/judge consistency is proven, not assumed.** The judge's replay key is
   `(subagent, model, input_sha)` where `input_sha` hashes the judge's own messages,
   which embed the candidate output being judged. If the judge scores on disk had been
   computed against the *superseded* generations, the assembly run would have raised
   `ReplayMissError` on every FE tuned item. It resolved 70/70 with 0 errors, so the
   judged text is the text replay now serves.

## Provenance stamp — a defect I shipped and fixed here

The first assembly printed its recording window as `2026-08-07T03:27:53 → 2026-08-10T02:45:59`.
That is the span of the **whole store**, including 08-07 spike traces and superseded
recordings from earlier runs — it dated fresh numbers as three days stale.

Fixed before this artifact was committed: `ReplayAdapter` now tracks the min/max `ts`
of the traces it has **actually served** (`served_window()`), `AdapterRouter` unions
that across models, and `run_phase2` stamps `recorded_from` / `recorded_to` after the
arms have run rather than from the corpus on disk. The artifact and the CLI footer now
read `2026-08-09T16:07:15 → 2026-08-10T02:45:59` — the calls this run replayed.
Covered by `tests/test_runner.py::test_replay_result_carries_the_recording_dates` and
`::test_cli_labels_a_replayed_number_with_its_recording_date`.

## Verification performed

- `pytest tests/` — 500 passed.
- `python cli.py e2e --mode replay` — 9 arms, 42 metrics measured, 0 call errors.
- Artifact re-assembled after the window fix; 9/9 arms 70/70 ok, 0 errors, 0 failed
  judge repeats.
- Trace-level audit of the 08-09/08-10 window: 1,486 traces, **0 with `status != "ok"`**.

## Caveats a reader must carry forward

1. **Judged n differs by subagent** (28 for QR/CS, 70 for FE) — deliberate, labelled.
2. **A1–A3 is one bundled rung**, not three measurable steps; `gemini_tuned_v1` is the
   whole ladder to date, and on FE it moves the judge score the *wrong* way.
3. **`latency_p95` is a cross-region comparison.** Claude ran in `global` (us-central1
   quota exhausted), Gemini and the judge in `us-central1`. Measured p95 over this
   window: claude-sonnet 7,105 ms; gemini-flash 8,843 ms; gemini-pro (judge) 19,056 ms.
   The scorecard must disclose the region split before using these against the
   `claude_baseline_p95` sentinel.
4. **The judge is the noisiest instrument in the artifact** and it is single-judge.
   The dual-judge cross-check on FE runs *first* on Monday, before the judged FE gap
   is treated as real — see the tuning-targets section of [day1_failures.md](day1_failures.md).
5. **Cost/savings numbers are not derivable yet.** `config/pricing.yaml` is still
   13 × `VERIFY`; token totals for this window are recorded
   (claude-sonnet 477,330 in / 91,208 out; gemini-flash 605,260 / 274,084;
   gemini-pro 1,232,200 / 1,313,348) but no price may be applied until
   `scripts/refresh_pricing.py` has stamped `verified_on` and source URLs.

## Headline findings (numbers, not conclusions)

Full per-arm table is in the artifact. Four things changed relative to n=10:

- **Claude's QR `json_schema_validity` is 0.814 [0.714, 0.900]** against a 0.99 gate.
  This is a *baseline* defect, invisible at n=10 (where all three QR arms read 1.000).
  Both Gemini QR arms beat it (0.971 naive, 1.000 tuned).
- **The FE judge CIs no longer overlap**: Claude 0.900 [0.868, 0.929] vs Gemini A0
  0.821 [0.787, 0.854] at n=70. The n=10 artifact carried point estimates only
  (0.879 vs 0.782, no CIs), so the gap was direction-only there. It is now a
  measurement — subject to caveat 4.
- **A1–A3 makes FE worse**: tuned 0.795 [0.760, 0.828] < naive 0.821. The ladder is
  actively harmful on this subagent, which is why the targeted `novelty_statement`
  rung exists.
- **QR tuned beats Claude on `exact_match_intent`**: 0.814 [0.714, 0.900] vs 0.729
  [0.629, 0.829] — heavy CI overlap, so parity-within-measurement, not "better".

No verdict is asserted here. Gates run in T13; this note only records what was
measured and under what conditions.
