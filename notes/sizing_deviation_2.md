# Sizing deviation #2 — the judged split widened to the full corpus

**Date: 2026-08-11.** Decided and recorded **before any widened result was
seen**. That ordering is the whole point of writing this file at launch rather
than at landing: a sizing change chosen after looking at the numbers is not a
sizing change, it is a result.

## The deviation

> Core split widened to full corpus to complete triage adjudication and narrow
> delta CIs; decided before results were seen.

Query Rewriter and Chunk Summarizer were judged on the **28-item core split**
× k=2 (registered in T08). They are now judged on the **full 70** × k=2, all
three arms. Feature Extractor was already at full 70 — that was
[deviation #1](phase2_n70_validation.md#sizing-one-registered-deviation-no-silent-ones)
— so after this change all nine arms share one judged denominator.

## Why

Two concrete defects in the Monday artifact, both caused by the narrow split:

1. **36 of the 60 QR shadow disagreements could not be adjudicated at all.**
   They fell outside the judged split, so `amw/shadow/triage.py` labelled them
   `not_adjudicated` — an absence of measurement, not a tie. The pre-registered
   `shadow_agreement` alt clause (*"wins >= losses over adjudicated
   disagreements"*) was therefore being evaluated on 24 of 60 rows. A clause
   decided on 40% of the evidence is not the clause that was registered.
2. **CS `quality_delta` fails at a CI lower bound of −2.679 on judged n=28.**
   Whether that is a real regression or the width of a small sample is exactly
   the question a wider n answers. It may still fail. If it does, it ships as
   an honest row.

## What this does *not* license

Widening n narrows a confidence interval; it does not move a threshold. The
gates in `config/gates.yaml` are unchanged, still checked on CI lower bounds,
and the verdict is whatever the widened data says. Nothing here is a
re-registration of a gate, and no result seen after this file was written may
be used to revise it.

## How it is machine-recorded

Same mechanism as deviation #1, so a reader cannot miss it:

* `JudgeReport.split` now carries `"all"` on all nine arms, with
  `items_scored` alongside it.
* `Phase2Result.notes` carries the runner's generated widening note naming
  every widened subagent.
* The widened artifact is written to a **new path**
  (`artifacts/results/phase2_n70_widened.json`); Monday's
  `artifacts/results/phase2_n70.json` is left byte-for-byte intact so the two
  can be diffed rather than confused.

## Provenance of the widened numbers

The generations are **not** re-run: every arm's outputs are replayed from the
recordings made 2026-08-09/2026-08-10. Only the judge calls for the 42
previously-unjudged items per subagent are new.

Those new judge calls were made by a top-up recorder
(`scripts/widen_judge.py`) which replays any call already in
`artifacts/replay/` and calls live **only** on a miss. Two consequences worth
stating:

* No existing recording is superseded, so Monday's artifact and the
  second-judge cross-check both remain reproducible from the store.
* Once the top-up finishes, the widened artifact is assembled in
  `--mode replay` by the ordinary CLI, so every number in it is reproducible
  offline with zero credentials.
