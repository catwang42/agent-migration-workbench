# Day 1 — first phase-2 numbers and where the failures cluster

**Run:** `python cli.py phase2 --mode live -n 10`, 2026-08-09.
**Corpus:** `datasets/`, synthetic, seed 20260812, 10 items per subagent (all
10 are core, so every item was judged).
**Judge:** Gemini 2.5 Pro, prompt v1, k=2 repeats.
**Calls:** 90 subagent + 180 judge, 0 errors.
**Results:** `artifacts/results/phase2.json`.

> `run_started` and `judge_model` are `null` in *this* results file:
> `run_phase2` read the wrong keys out of `Judge.describe()` and never stamped
> a start time. Fixed in the same commit as this note, so the next run
> populates both. They are not hand-filled here — an artifact's provenance
> block is either measured or empty. The run date and judge identity above come
> from this run's console output and `config/models.yaml`.

> Read the headline caveat first: an earlier run of this same command produced
> a *materially different and wrong* Claude baseline. See
> [claude_schema_dialect.md](claude_schema_dialect.md). The numbers below are
> from the re-run after that fix.

## Judge score by arm

| subagent | claude_baseline | gemini_naive (A0) | gemini_tuned_v1 (A1-A3) |
|---|---|---|---|
| query_rewriter | 0.925 [0.800, 1.000] | 0.805 [0.750, 0.875] | 0.875 [0.775, 0.975] |
| chunk_summarizer | 0.975 [0.925, 1.000] | 0.925 [0.850, 1.000] | 0.925 [0.850, 1.000] |
| feature_extractor | 0.879 [0.707, 0.986] | 0.782 [0.692, 0.864] | 0.783 [0.702, 0.860] |

n=10 per arm. These intervals are wide and they overlap; at this subset size
nothing here is a gate decision, and none of it should be shown as one. What
n=10 is good for is telling us **where to look**, which is what the clusters
below do.

## The clusters

`cluster_failures` groups failed rubric criteria by criterion id. Counts are
items out of 10.

### query_rewriter

| criterion | claude | A0 | A1-A3 |
|---|---|---|---|
| `intent_landscape` | 0 | **3** | 2 |
| `publication_number_kept` | 1 | 2 | 2 |
| `no_invented_dates` | 1 | 1 | 1 |
| `cpc_filter` | 1 | 0 | 0 |
| `date_range_exact` | 0 | 1 | 0 |
| `dates_null` | 0 | 1 | 0 |

**`intent_landscape` is the one Gemini-specific cluster and the clearest
tuning target.** Gemini classifies survey-shaped questions as `prior_art`
where the gold is `landscape`. Both sampled cases are unambiguous:

- qr-0001 *"Search for Huawei … filings on federated learning … since the start
  of 2019"* — Gemini got both filters right (`assignees`, `date_from`) and
  still returned `intent: prior_art`. Claude returned `landscape`.
- qr-0010 *"Can you run a **broad search** for European patent docs on …"* —
  same split.

The `<intents>` block defines landscape as "surveying an area, counting
activity, mapping who is doing what". Gemini is reading intent from the
*subject matter* rather than from the analyst's verb. This is a prompt fix, not
a model ceiling — it is exactly what the A1-A3 rung is for, and the tuned rung
already recovers one of the three.

**`publication_number_kept` hits both models** (Claude 1, Gemini 2) and is a
*prompt* defect, not a model one. On qr-0009 and qr-0014 the gold query keeps
the publication number: `"US10869083B2 cited references: silicon-carbon
composite anode OR …"`. Both models dropped it — reasonably, since instruction
3 tells them to write "the technical terms an examiner would use" and says
nothing about preserving identifiers. Fix the prompt, and expect both arms to
improve together.

Singleton worth noting: on qr-0014 **Claude omitted the `filters` object
entirely**, losing the CPC code the analyst named. Gemini kept it. One item.

### chunk_summarizer

| criterion | claude | A0 | A1-A3 |
|---|---|---|---|
| `comparison_cites_both` | 1 | 1 | 1 |
| `conflict_flagged` | 0 | 1 | 1 |
| `difference_cites_both` | 0 | 1 | 1 |

Three items, three criteria, all about **citing both sides of a two-chunk
claim**. cs-0010 fails for every arm including Claude, so it is a task-design
or prompt problem rather than a migration risk. cs-0014 and cs-0017 fail for
both Gemini rungs and not Claude.

Every deterministic metric here is perfect across all three arms
(`citation_coverage` 1.000, `fabricated_citation_rate` 0.000,
`uncited_claim_rate` 0.000, `json_schema_validity` 1.000). Chunk Summarizer is
the strongest migration candidate on this evidence.

### feature_extractor — the important one

| criterion | claude | A0 | A1-A3 |
|---|---|---|---|
| `novelty_statement_correct` | 2 | **9** | **8** |
| `technical_field_correct` | 3 | 4 | 5 |
| `technical_field_inferred` | 1 | 1 | 1 |
| `novelty_from_fragment` | 0 | 1 | 1 |
| `bibliographic_correct` | 1 | 0 | 0 |
| `independent_count_exact` | 1 | 0 | 0 |

**`novelty_statement_correct` fails on 9 of 10 Gemini items and 2 of 10 Claude
items.** This single cluster is the entire FE quality gap. Two distinct failure
modes, from sampled items:

1. **Silent abstention.** fe-0003 and fe-0020 — Gemini returned
   `novelty_statement: null` where the source states a clear advance and Claude
   extracted it verbatim-equivalent:

   > gold: *"A positive electrode active material comprising carbon-coated
   > LiFe(1-x)MnxPO4 particles wherein 0.15 <= x <= 0.45 and the carbon coating
   > has a thickness of 2 nm to 6 nm."*
   > claude: same, near-verbatim. gemini: `null`.

2. **The problem instead of the advance.** fe-0017 — Gemini names what the
   disclosure *addresses* and drops every technical limit:

   > gold: *"…an ionisable cationic lipid having an apparent pKa of 6.2 to 6.7
   > present at 45 mol% to 50 mol% of total lipid."*
   > gemini: *"The disclosed arrangement addresses poor endosomal escape
   > limiting the delivered dose of intact mRNA."*
   > claude: keeps both the pKa range and the mol% range.

`technical_field` shows a milder version of the same thing: Gemini restates the
document's opening noun phrase ("lipid nanoparticle composition encapsulating a
messenger RNA") where the gold wants a field name ("ionisable lipid nanoparticle
delivery"). Claude also misses this on 3 items, so it is partly rubric
strictness — but Gemini's answers are systematically the wrong *kind* of
answer, not just the wrong words.

## The headline for the scorecard

**On Feature Extractor, every deterministic metric ranks Gemini above Claude,
and the judge ranks Claude above Gemini — and the judge is the one that is
right.**

| FE metric | claude | A0 | A1-A3 |
|---|---|---|---|
| `extraction_accuracy` | 0.900 | **1.000** | **1.000** |
| `json_schema_validity` | 0.900 | **1.000** | **1.000** |
| `answered_precision` | 1.000 | 1.000 | 1.000 |
| `hallucination_rate` | 0.000 | 0.000 | 0.000 |
| `omission_rate` | 0.111 | **0.000** | **0.000** |
| `judge_score` | **0.879** | 0.782 | 0.783 |

The deterministic metrics cover the six exact-matchable fields, all of which
Gemini gets right. The two fields that carry the analytical value —
*what field is this* and *what is new here* — left exact match on 2026-08-07
(see [fe_open_text_metric_change.md](fe_open_text_metric_change.md)) and are
scored only by the judge. A scorecard built on deterministic metrics alone
would have recommended migrating Feature Extractor on the strength of a clean
sweep, while the subagent quietly stopped saying what each patent contributes.

That reroute was made for a different reason — exact match was filing correct
paraphrases as fabrications — and this is the payoff.

## What Claude's own failures were

Not zero, and worth stating plainly so the baseline is not read as a ceiling:

- fe-0006 returned `{"feature_data": {}}` — a tool call with an invented
  wrapper key and no content. It fails every FE criterion and is the single
  item behind Claude's 0.900 `json_schema_validity` and 0.111 `omission_rate`.
  One of ten, not reproduced elsewhere. Its 181 output tokens do not match the
  ~10-token payload recorded, which is odd enough to re-check on the n=70 run
  before it is characterised as a model behaviour.
- qr-0014: dropped the `filters` object entirely.
- 3 FE items where `technical_field` disagreed with the gold label.

## Caveats on these numbers

1. **n=10 of 70.** Intervals overlap everywhere. Direction only.
2. **The judge disagrees with itself on FE.** `full_agreement_rate` is 0.9 for
   Claude and **0.8** for both Gemini arms, against 1.0 on every QR and CS arm.
   FE open-text judging is the noisiest thing in this run. One sampled item
   (fe-0002, Gemini) reads as a correct novelty statement and was still marked
   failed — worth a look before the FE judge criteria are trusted at gate
   strength.
3. **Cross-region.** Claude runs in `global`, Gemini in `us-central1`, so
   `latency_p95` is not a same-region comparison. The CLI warns on every run;
   the scorecard must disclose it.
4. **Synthetic corpus.** `provenance=synthetic`, seed 20260812. Gemini wrote
   the surface prose of 60 of 70 FE passages during generation, which is a
   plausible confound for FE phrasing metrics and should be named when these
   numbers are shown.
5. **A1-A3 barely moves FE** (0.782 → 0.783). Whatever the tuned rung changes,
   it does not touch the novelty-statement behaviour. That is the first thing
   the ablation ladder should attack.

## Suggested order of attack

1. FE `novelty_statement` — biggest cluster, clearest failure mode, and A1-A3
   currently does nothing for it.
2. QR `intent_landscape` — 3 items, unambiguous, and the tuned rung already
   recovers one.
3. QR `publication_number_kept` — a baseline prompt gap that improves both arms.
4. Re-check fe-0006 on the full n=70 run.

---

# Tuning targets — Monday

**Added 2026-08-09**, from the clusters above. Ruled by the project owner as
the setup for Fan-out 3. These are directions for the ablation ladder, not
results; nothing below has been measured.

## (a) A new FE rung aimed at `novelty_statement`

A1–A3 move FE by **0.001** (0.782 → 0.783). Whatever those rungs change, they
do not touch the behaviour that accounts for the entire FE gap. So this is a
**new rung driven by the failure cluster**, not a re-tune of the existing
ones — the ladder has no rung that addresses it and adding effort to A1–A3
would be tuning against something that is already saturated.

### The diagnosis

The tuned pack's abstention rule reads:

> `null` means "this document does not state it". It is a correct, expected
> answer and is scored as such. Never infer a value from the subject matter,
> the company, the technology, or anything you happen to know about this
> patent family. A plausible fabrication is the worst output you can produce.

That rule is right, and it is the reason `hallucination_rate` is 0.000 across
all three arms. It is also the cause of the biggest cluster in the run, because
the field table pairs it with "return null when **the document states no point
of novelty**" — and Gemini reads that literally as *no prose passage
discussing novelty*.

**fe-0003 is the clean worked example.** The document is a JPO translation with
no discussion section at all — bibliographic header, then claims:

> Claims:
> 1. An all-solid-state lithium secondary battery, comprising a solid
>    electrolyte layer comprising an argyrodite-type Li6PS5Cl having a median
>    particle diameter D50 of 0.8 um to 2.5 um.

The gold `novelty_statement` is that claim, near-verbatim. Claude returned it.
Gemini returned `null`.

Gemini is not being careless — it is applying the abstention rule correctly to
a document that never uses the word "novel". What it is missing is a **domain
convention the prompt never states: in a patent, independent claim 1 *is* the
point of novelty.** Claude supplies that convention from prior knowledge. A
migration cannot rely on the target model happening to share it.

### What the rung must do

Two requirements, from the two observed failure modes:

1. **Never `null` when the source states an advance.** Name the fallback
   order explicitly — a prose novelty/summary passage if present, otherwise
   independent claim 1. Reserve `null` for a document with neither.
2. **Preserve the numeric limits.** fe-0017 is the other mode: Gemini named
   what the disclosure *addresses* ("poor endosomal escape limiting the
   delivered dose of intact mRNA") and dropped every quantity, where the gold
   keeps "apparent pKa of 6.2 to 6.7" and "45 mol% to 50 mol%". A novelty
   statement without its limits is not a weaker answer, it is a different and
   wrong kind of answer — it describes the problem, not the invention.

### What it must not do

It must not weaken the abstention rule in general. `hallucination_rate` is
0.000 on every arm and that is the property the whole FE evaluation exists to
protect (ground rule 1). The rung narrows *where* abstention applies for one
field; it does not license inference anywhere. **Watch `hallucination_rate`
and `answered_precision` as the guard metrics on this rung** — if either moves
off 0.000/1.000, the rung has bought judge score with fabrication and must be
rejected regardless of what the judge says.

## (b) The VAIPO rung optimises against the judged metric

When the VAIPO rung runs, its objective is the **judged** FE score, not
`extraction_accuracy`.

This is not a preference. Deterministic FE accuracy is already **1.000 for
both Gemini arms** — there is no headroom, and an optimiser pointed at it
would be optimising a saturated metric while the actual defect sits in the two
fields that metric does not cover. It would report success and change nothing.
`technical_field` and `novelty_statement` left exact match on 2026-08-07 and
are judge-only; they are also the two fields carrying the analytical value.

Cost note: judged objectives mean judge calls inside the optimisation loop, so
this rung is materially more expensive per iteration than a rung scored
deterministically. Size it before launching it.

## (c) Dual-judge cross-check on FE runs FIRST

**Before the FE judged gap is treated as real, and therefore before (a) or (b)
is tuned against it.**

The reason is in this run's own numbers. FE `full_agreement_rate` is **0.8 for
both Gemini arms and 0.9 for Claude**, against **1.0 on every QR and CS arm**.
The FE judge disagrees with itself between its two repeats on a fifth of
Gemini items — it is by a wide margin the noisiest measurement here. One
sampled item (fe-0002, Gemini) reads as a correct novelty statement and was
still marked failed.

So the 0.879-vs-0.782 gap is currently resting on the least reliable
instrument in the workbench, and every downstream decision — which rung to
build, whether the counterfactual in
[counterfactual_scorecard.md](counterfactual_scorecard.md) holds, whether FE
gets a MIGRATE verdict — inherits that. Confirming the gap with a second judge
is cheaper than tuning against an artifact of the first one.

The n=70 baseline judges FE on all 70 items rather than the 28-item core
split, specifically so this question has enough data to settle; QR and CS stay
on the registered core split. That widening is recorded in the results file's
`notes` and in each arm's `judge.split`.

**If the two judges disagree on the direction of the FE gap, that is the
finding**, and it is a more important one than any tuning result — it would
mean the workbench cannot yet measure the thing the whole FE analysis turns
on. Say so plainly rather than picking the judge that agrees with the story.
