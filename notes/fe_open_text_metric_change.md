# Feature Extractor: open-text fields moved off exact match

**Date of change: 2026-08-07. Made pre-baseline** — before any comparative
number existed. No Claude-vs-Gemini figure was ever computed under the old
rule, so nothing previously reported changes and nothing needs restating.

## What changed

`technical_field` and `novelty_statement` left `FE_SCALAR_FIELDS` in
`amw/eval/metrics.py` and are now scored by the rubric judge, 0/1 per field.
They are listed in `FE_JUDGED_FIELDS`; `FE_ALL_FIELDS` still names the full
schema surface. Every other field is unchanged and still exact-matched.

`amw/eval/runner.py::rubric_of` appends one criterion per rerouted field that
the gold actually states, tagged `fe_field_label`. The criterion hands the
judge the reference value and says explicitly that a different wording for the
same thing is correct, and that an answer the source does not support is not.

Where the gold is `null` for one of these fields, no criterion is added: there
is no label to be right about.

## Why

Found during the T06 realism review of ten sample items per subagent.

The schema asks these two fields for a characterisation — "one short phrase",
"one sentence, quoted or closely paraphrased" — not a verbatim lift. Exact
match cannot score that. Three of ten sample items made it concrete:

| item | source says | gold `technical_field` |
|---|---|---|
| fe-0007 | "beam management", "hierarchical codebook", "large antenna arrays" | `millimetre-wave hybrid beamforming` |
| fe-0008 | "removing carbon dioxide from a flue gas", "hindered amine" | `amine-based post-combustion carbon capture` |
| fe-0004 | "LiFe(1-x)MnxPO4", "olivine phosphates" | `lithium iron phosphate cathodes` |

Each gold is a correct label. So is "beam management in wireless networks" for
fe-0007 — and exact match scored it `wrong`. Across the sample, 20 of 80
scoreable fields (25%) were open text, so a quarter of `extraction_accuracy`
was measuring phrasing agreement rather than extraction.

The more serious problem was `hallucination_rate`. A correct in-source
paraphrase of `novelty_statement` was tallied as a `hallucination`, filing it
beside genuine fabrication. Separating those two is the entire reason the
schema uses `null` for "not stated" and the entire reason that metric exists
(CLAUDE.md ground rule 1). A metric that conflates them is worse than no
metric, because it reads as evidence.

## Effect on the comparison

The change is symmetric across arms — every arm is scored the same way — so it
moves no Claude-vs-Gemini delta. What it moves is the absolute
`extraction_accuracy`, upward, and the composition of `hallucination_rate`,
which now counts only fields where fabrication is the thing being detected.

## Supporting change

`amw/eval/judge.py`'s rubric validator capped criteria at 3–5. That bound
exists to catch a malformed rubric from T06's generator (master_plan §5.3), so
it now counts only generator-authored criteria; criteria tagged in
`EVAL_ADDED_TAGS` are excluded, under a new hard total ceiling of 8. The
generator contract is unchanged and still enforced.

## Verification

- Golden fixture `tests/fixtures/eval/metric_cases.json::fe_mixed` reshaped so
  the six exact-matched fields still cover all five verdicts, and so the
  payload still contains both misread cases — a correct label in different
  words, and an assertion where the gold is null.
- Goldens in `tests/test_metrics.py` rederived on paper, not captured from a
  run of the code under test.
- `pytest tests/` — 489 passed. `python cli.py e2e --mode replay` — green.

Authorised by the project owner on 2026-08-07 with this scope, as part of the
T06 realism review sign-off.
