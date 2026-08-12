# Module 02 — The reference workload

*~5 min read · [01 Why subagents migrate first](01-why-subagents-first.md) → **02** → [03 Gates as contract](03-gates-as-contract.md)*

---

A migration evaluation is only as good as the corpus it runs on. This one is
synthetic, and it says so on every single item. The interesting question is not
"is it real?" — it is "does it fail in the places a real workload fails, and can
you check that it does?"

## The corpus, in numbers

| Property | Value | Where it comes from |
|---|---|---|
| Customer profile | `demo_patents` — "Demo — Patents RAG" | `config/customers/demo_patents.yaml` |
| Domain | Patents | profile |
| Items per subagent | 70 | `cases_per_subagent: 70` |
| Total items | 210 across three subagents | `datasets/*.jsonl` |
| Judged core split | 28 per subagent, stratified | `judged_core_set: 28` |
| Judge repeats | k=2 | `judge_repeats: 2` |
| Provenance | `synthetic` on 210 of 210 items | per-item field |
| Dataset seed | `20260812` | profile |
| Generator version | `t06.1` | per-item field |
| Distinct templates | 12 Query Rewriter / 8 Chunk Summarizer / 9 Feature Extractor | `datasets/*.jsonl` |

Every item carries `provenance`, `seed` and `generator_version` as fields, not as
a note in a README. A provenance label that is not machine-readable is a label
that stops travelling the moment someone copies a table into a slide.

## The difficulty mix is deliberate

The dataset is not a uniform pile of questions. It is weighted **40 / 25 / 20 / 15**
across `simple` / `multi_hop` / `extraction` / `edge`, which at n=70 lands as:

| Bucket | Target | Items at n=70 (all three subagents) |
|---|---|---|
| `simple` | 40% | 28 |
| `multi_hop` | 25% | 18 |
| `extraction` | 20% | 14 |
| `edge` | 15% | 10 |

The reason for the weighting is stated in the module that implements it
(`amw/datasets/mix.py`): the migration signal is concentrated in the last two
buckets. A model that keeps up on the easy 40% and falls apart on edge cases looks
fine under an unweighted average and is a bad migration.

Two properties are tested directly rather than assumed:

- **The mix holds at any n.** Largest-remainder allocation, so 70 splits
  28/18/14/10 and 10 splits 4/3/2/1 — never losing an item to rounding, never
  silently emptying a bucket at small n.
- **The core split preserves the mix.** The judged core set is the expensive part
  of any run (judge calls × k repeats), so it is a *stratified* sample, not the
  first N items. If the core were the head of the file it would be all `simple`,
  and the judged score would be measured on the easy half.

## Edge cases are designed, not collected

Roughly half the edge cases exist because something is **missing**: "in the last
couple of years" with no date anchor, a patent front page with no filing date, a
follow-up turn that replaces a subject rather than adding to it. The gold answer
for those items is that the model must *not* invent the missing value.

That design choice is what makes the next section load-bearing.

## The realism pass, and the line it may not cross

Template prose reads like a template. So generation optionally routes through the
Gemini adapter for a surface rewrite — recorded and replayable like every other
call in the repo.

The scope of that rewrite is deliberately narrow
(`amw/datasets/surface.py`):

> The model rewrites surface prose. It never authors gold, rubrics, filters, dates,
> classifications or claim structure.

Templates derive the gold answer from the same scenario facts they build the prose
from, so the rewrite only changes how it sounds. Every rewrite is then checked
before it is accepted:

1. Every literal the gold depends on must survive **verbatim**.
2. Nothing the item deliberately **omits** may appear.

Check 2 is the important one. A helpful rewriter that supplies a missing filing
date destroys the edge case without raising a single error. A rejected rewrite is
not a failure — the item falls back to its template prose and records that in
`surface_source`.

!!! note "Zero credentials, still a complete dataset"

    In replay mode a surface-rewrite miss is a fallback, not a crash, so
    `cli.py gen --mode replay` produces a complete and valid dataset on a laptop
    with no ADC. What it does not produce is the naturalised phrasing — and the
    run report says how many items that affected rather than hiding it.

## Regenerate it yourself

```bash
python cli.py gen --customer demo_patents -n 70
```

Same seed, same generator version, same 210 items. Determinism here is not
tidiness — it is what makes "we re-ran it and got the same corpus" a checkable
claim rather than a promise.

---

**Next:** [Module 03 — Gates as contract](03-gates-as-contract.md)

*Source: `config/customers/demo_patents.yaml`, `amw/datasets/mix.py`, `amw/datasets/surface.py`, `datasets/*.jsonl` (counted 2026-08-12).*
