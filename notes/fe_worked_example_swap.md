# The FE novelty rungs' worked example was swapped off the corpus

**Date: 2026-08-11.** Done **before** either novelty rung was ever measured, on
the owner's instruction. Recorded here because a prompt file that changed
between a decision and a measurement is exactly the kind of thing a customer is
entitled to see the history of.

## What was wrong

`amw/agents/prompts/feature_extractor/gemini_novelty_v1_tool.txt` and
`…_schema.txt` both taught the `novelty_statement` rule with a worked example
lifted from **corpus item `fe-0003`** (JP 2019-165530 A, Helion Elektrolyte
GmbH, argyrodite Li6PS5Cl, D50 0.8–2.5 µm). `fe-0003` is one of the 70 items
these rungs are scored on, and it is in the 28-item core split. The example's
`<novelty_statement>` block was, verbatim, that item's gold answer.

So the two rungs were shown one of their own answer keys. Any judged score they
earned on `fe-0003` would have measured recall of the prompt, not extraction
from the document — and the ladder's whole claim is that A4-novelty isolates
the effect of *the rule*, not of a memorised item.

This was disclosed rather than hidden from the start:
`amw/tuning/ablate.py::FEW_SHOT_ITEM_IDS` drove
`RungRecord.leaked_example_items`, so any run would have named the overlap in
its artifact. Disclosure was the right floor. It was not a fix.

## What replaced it

A freshly authored example, written for this file and not drawn from the
corpus: a clamp-on ultrasonic transit-time flow meter (EP 3 742 118 A1,
Wrenfield Instrumentation Ltd). The corpus covers twelve technical fields —
solid electrolytes, LFP cathodes, silicon anodes, PEM electrolysis, perovskite
tandems, microLED transfer, FMCW LiDAR, mmWave beamforming, surface-code QEC,
federated learning, amine carbon capture, low-GWP heat pumps. Industrial
ultrasonic flow metering is none of them, and neither the applicant nor the
publication number appears anywhere in `datasets/`.

It teaches the identical lesson, deliberately: a document with no discussion
section, so step 2 of the rule applies and claim 1 *is* the statement of
novelty. It is a slightly harder example than the one it replaces — three
numeric limits to carry through (wedge angle range, centre frequency,
transit-time resolution) rather than one particle-size range.

Everything else in both files is byte-identical to what it was. The two rungs
still differ from each other only in output mode (tool vs `response_schema`),
which is the contrast they exist to measure.

## Consequences

* `FEW_SHOT_ITEM_IDS` is now empty for both variants, so a measured run will
  report `leaked_example_items: []` — a measured zero, not an absent check.
* The rungs' prompt shas change, so nothing recorded under the old text can be
  replayed as if it were the new text. Neither rung had been measured, so
  nothing is invalidated.
* `A0-schema` is unaffected: it never quoted a corpus item.
