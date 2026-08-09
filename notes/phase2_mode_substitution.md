# Phase 2 ran live where the plan said hybrid

**Date: 2026-08-09.** Adaptation, not deviation. Recorded here because a
reader comparing the build plan to `artifacts/results/phase2.json` will see
`"mode": "live"` where the fan-out order said `--mode hybrid`, and is entitled
to know why without asking.

## What the plan said

The Fan-out 2 end state was:

> `python cli.py phase2 --mode hybrid -n 10` has produced first real numbers

## What ran

`python cli.py phase2 --mode live -n 10`, 2026-08-09 — 90 subagent calls and
180 judge calls, 0 errors. Results in `artifacts/results/phase2.json`, failure
clusters in [day1_failures.md](day1_failures.md).

## Why

Hybrid means Gemini live, Claude replayed. Replay is keyed on
`(subagent, model, input_sha)`, and the corpus in `datasets/` had been
generated that same day — so **no Claude recording existed for any item in
it**. A hybrid run would not have produced a degraded Claude arm; it would
have raised `ReplayMissError` on the first Claude call and produced no Claude
arm at all. There was nothing to replay yet.

Hybrid is a *demo-mode* spec — it exists so a workshop can show live Gemini
behaviour without re-spending on the Claude baseline. It is not a measurement
requirement. Running both arms live is strictly the stronger measurement:
every number in the artifact is a call made against a real endpoint on the
stated date, with nothing replayed.

So the substitution moved the run in the direction of more evidence, not less,
which is why it was made without stopping to ask.

## Standing ruling on this artifact

Ruled by the project owner on 2026-08-09:

- The live `-n 10` run **stays** as the cited artifact for
  `day1_failures.md`. No `-n 10` re-runs in any mode.
- Its `run_started` and `judge_model` fields are `null`. `run_phase2` read the
  wrong keys out of `Judge.describe()` and never stamped a start time; both
  were fixed in commit `24c8d8d`, which is the same commit that landed the
  artifact — so the file predates its own fix. The nulls are **left as
  produced rather than hand-filled**: a provenance block is either measured or
  empty (ground rule 2). The run date and judge identity are stated in
  `day1_failures.md` from the console output and `config/models.yaml`, clearly
  as prose rather than as artifact fields.
- All future artifacts carry both fields. The n=70 baseline
  (`artifacts/results/phase2_n70.json`) is the first one that does.

## Hybrid from here

Now that the n=10 live run has recorded both arms, hybrid is runnable for
those 10 items, and the n=70 baseline records the rest. Hybrid remains the
intended **demo** mode for Wednesday — see the standing decision that the
demo path is `CLAUDE_PATH=vertex` only.
