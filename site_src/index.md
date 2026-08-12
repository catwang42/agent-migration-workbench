---
hide:
  - navigation
  - toc
---

<section class="amw-hero" markdown="1">
<div class="amw-hero__grid" markdown="1">
<div class="amw-hero__intro" markdown="1">

# Agent Migration Workbench

<p class="amw-hero__lead">Migrate agent workloads on evidence, not assertions. Pre-register the gates, run both models over the same corpus, read the confidence-interval bounds, and publish the verdict the numbers give you — including the ones that say <em>no</em>.</p>

<div class="amw-hero__cta" markdown="span">
[Start the walkthrough](setup.md){ .md-button .md-button--primary }
[Browse the results](results/index.md){ .md-button }
</div>

</div>
<div class="amw-hero__pipe" markdown="1">
<div class="amw-pipeline" markdown="1">
<div class="amw-pipeline__chip" markdown="span"><span class="amw-pipeline__num">01</span><span class="amw-pipeline__ic">:material-source-branch:</span><span>Why subagents migrate first</span></div>
<div class="amw-pipeline__chip" markdown="span"><span class="amw-pipeline__num">02</span><span class="amw-pipeline__ic">:material-database-outline:</span><span>The reference workload</span></div>
<div class="amw-pipeline__chip" markdown="span"><span class="amw-pipeline__num">03</span><span class="amw-pipeline__ic">:material-file-sign:</span><span>Gates as contract</span></div>
<div class="amw-pipeline__chip" markdown="span"><span class="amw-pipeline__num">04</span><span class="amw-pipeline__ic">:material-swap-horizontal:</span><span>The naive swap</span></div>
<div class="amw-pipeline__chip" markdown="span"><span class="amw-pipeline__num">05</span><span class="amw-pipeline__ic">:material-stairs-up:</span><span>The adaptation ladder</span></div>
<div class="amw-pipeline__chip" markdown="span"><span class="amw-pipeline__num">06</span><span class="amw-pipeline__ic">:material-scale-balance:</span><span>The second judge</span></div>
<div class="amw-pipeline__chip" markdown="span"><span class="amw-pipeline__num">07</span><span class="amw-pipeline__ic">:material-compare:</span><span>Shadow &amp; adjudication</span></div>
<div class="amw-pipeline__chip" markdown="span"><span class="amw-pipeline__num">08</span><span class="amw-pipeline__ic">:material-clipboard-check-outline:</span><span>The scorecard</span></div>
</div>
</div>
</div>
</section>

!!! warning "Draft — refreshes at freeze-v1"

    Every figure on this site is measured, but the run behind it is the
    pre-freeze run (recordings 2026-08-09T16:07:15+00:00 → 2026-08-11T06:20:37+00:00).
    The final run replaces these pages via one command
    (`python scripts/build_site.py`) at content freeze; nothing here is a
    placeholder waiting for a value.

<div class="amw-stats amw-stats--5" markdown="1">
<div class="amw-card amw-stat" markdown="1">
<div class="amw-stat__icon" markdown="span">:material-robot-outline:</div>
<div class="amw-stat__num">3</div>
<div class="amw-stat__label">Subagents measured end to end</div>
<span class="amw-stat__source">Query Rewriter, Chunk Summarizer, Feature&nbsp;Extractor · <code>config/customers/demo_patents.yaml</code></span>
</div>
<div class="amw-card amw-stat" markdown="1">
<div class="amw-stat__icon" markdown="span">:material-file-document-multiple-outline:</div>
<div class="amw-stat__num">210</div>
<div class="amw-stat__label">Corpus items, 70 per subagent</div>
<span class="amw-stat__source">Synthetic, seed <code>20260812</code>, generator <code>t06.1</code> · <code>wc -l datasets/*.jsonl</code></span>
</div>
<div class="amw-card amw-stat" markdown="1">
<div class="amw-stat__icon" markdown="span">:material-record-circle-outline:</div>
<div class="amw-stat__num">5,193</div>
<div class="amw-stat__label">Recorded live model calls</div>
<span class="amw-stat__source">5,186 <code>ok</code> / 7 <code>error</code>, all replayable · <code>artifacts/replay/*.jsonl</code></span>
</div>
<div class="amw-card amw-stat" markdown="1">
<div class="amw-stat__icon" markdown="span">:material-check-decagram-outline:</div>
<div class="amw-stat__num">1,011</div>
<div class="amw-stat__label">Tests passing</div>
<span class="amw-stat__source">2 skipped, 0 failing · <code>pytest tests/</code></span>
</div>
<div class="amw-card amw-stat" markdown="1">
<div class="amw-stat__icon" markdown="span">:material-compare:</div>
<div class="amw-stat__num">210</div>
<div class="amw-stat__label">Judged pairs in the shadow run</div>
<span class="amw-stat__source">Full corpus, <code>split=all</code>; 177 disagreed and went to adjudication</span>
</div>
</div>

<h2 class="amw-section-title">What makes this different</h2>
<p class="amw-section-sub">Four ideas run through every module. This is where "we swapped the model and it seemed fine" stops and this workshop starts.</p>

<div class="amw-diffs" markdown="1">
<div class="amw-card amw-diff" markdown="1">
### Gates are pre-registered, and the report proves it
Six thresholds live in `config/gates.yaml`, signed off before the first result appears. Every scorecard footer prints the file's version hash — `92f9d018432f` — so a reader can tell no threshold moved after the numbers landed. Gates read the **95% CI bound**, never the point estimate.
</div>
<div class="amw-card amw-diff" markdown="1">
### The instrument is matched to the subagent's autonomy level
A single-call transform, a tool decider, a retrieval chain and a looping orchestrator do not decide the same things, so they cannot share one bench. The taxonomy in module 01 picks the instrument, and the instrument decides whether a verdict is even available.
</div>
<div class="amw-card amw-diff" markdown="1">
### Two vendors' judges, cross-checked, never averaged
Gemini 2.5 Pro is the gated judge, registered before results. Claude Sonnet 5 re-scored the same recorded outputs: criterion agreement 98.8% / 99.0% / 92.9%, kappa 0.936 / 0.948 / 0.758. The cross-check validates the instrument; it does not replace it.
</div>
<div class="amw-card amw-diff" markdown="1">
### Verdicts that can say no
This run produced no MIGRATE. Query Rewriter is **HOLD** on a failed blocking gate; the other two are **INCOMPLETE** — a verdict over a subset of the gates is not the verdict that was agreed. The orchestrator is deliberately unmeasured and gets no verdict at all.
</div>
</div>

<h2 class="amw-section-title">The eight modules</h2>
<p class="amw-section-sub">Roughly five minutes each, in the order the workshop runs them. Each one assumes the last.</p>

<div class="amw-modules" markdown="1">
<div class="amw-card amw-module" markdown="1">
<div class="amw-module__head" markdown="1">
<div class="amw-module__icon" markdown="span">:material-source-branch:</div>
<p class="amw-module__title">01 — Why subagents migrate first</p>
</div>
<p class="amw-module__desc">The four-category taxonomy: what each autonomy level actually decides, which instrument measures it, and which rows are allowed a verdict today.</p>
<div class="amw-module__foot" markdown="span">
<span class="amw-module__time">~5 min read</span>
[Start :material-arrow-right:](modules/01-why-subagents-first.md){ .amw-module__link }
</div>
</div>

<div class="amw-card amw-module" markdown="1">
<div class="amw-module__head" markdown="1">
<div class="amw-module__icon" markdown="span">:material-database-outline:</div>
<p class="amw-module__title">02 — The reference workload</p>
</div>
<p class="amw-module__desc">70 items per subagent, synthetic and labelled as such, one seed, a 40/25/20/15 difficulty mix, and a realism pass that is not allowed to touch the answer key.</p>
<div class="amw-module__foot" markdown="span">
<span class="amw-module__time">~5 min read</span>
[Start :material-arrow-right:](modules/02-reference-workload.md){ .amw-module__link }
</div>
</div>

<div class="amw-card amw-module" markdown="1">
<div class="amw-module__head" markdown="1">
<div class="amw-module__icon" markdown="span">:material-file-sign:</div>
<p class="amw-module__title">03 — Gates as contract</p>
</div>
<p class="amw-module__desc">A line-by-line read of <code>config/gates.yaml</code>, including the <code>alt</code> clause on <code>shadow_agreement</code> and what a sentinel that cannot resolve does to a gate.</p>
<div class="amw-module__foot" markdown="span">
<span class="amw-module__time">~6 min read</span>
[Start :material-arrow-right:](modules/03-gates-as-contract.md){ .amw-module__link }
</div>
</div>

<div class="amw-card amw-module" markdown="1">
<div class="amw-module__head" markdown="1">
<div class="amw-module__icon" markdown="span">:material-swap-horizontal:</div>
<p class="amw-module__title">04 — The naive swap</p>
</div>
<p class="amw-module__desc">Same prompt bytes, new endpoint. The real per-subagent baseline numbers, and the two defects the swap surfaced on <em>both</em> sides.</p>
<div class="amw-module__foot" markdown="span">
<span class="amw-module__time">~5 min read</span>
[Start :material-arrow-right:](modules/04-the-naive-swap.md){ .amw-module__link }
</div>
</div>

<div class="amw-card amw-module" markdown="1">
<div class="amw-module__head" markdown="1">
<div class="amw-module__icon" markdown="span">:material-stairs-up:</div>
<p class="amw-module__title">05 — The adaptation ladder</p>
</div>
<p class="amw-module__desc">Every rung that was run, at the n it was run on — including the optimizer rung, its contamination, and the quoting rule that follows from it.</p>
<div class="amw-module__foot" markdown="span">
<span class="amw-module__time">~7 min read</span>
[Start :material-arrow-right:](modules/05-adaptation-ladder.md){ .amw-module__link }
</div>
</div>

<div class="amw-card amw-module" markdown="1">
<div class="amw-module__head" markdown="1">
<div class="amw-module__icon" markdown="span">:material-scale-balance:</div>
<p class="amw-module__title">06 — The second judge</p>
</div>
<p class="amw-module__desc">How you bound "a Gemini judge grading Gemini output": rubric anchoring, k=2 repeats, and a Claude judge re-scoring the same recorded outputs.</p>
<div class="amw-module__foot" markdown="span">
<span class="amw-module__time">~5 min read</span>
[Start :material-arrow-right:](modules/06-the-second-judge.md){ .amw-module__link }
</div>
</div>

<div class="amw-card amw-module" markdown="1">
<div class="amw-module__head" markdown="1">
<div class="amw-module__icon" markdown="span">:material-compare:</div>
<p class="amw-module__title">07 — Shadow &amp; adjudication</p>
</div>
<p class="amw-module__desc">Why item-level and structured-field agreement differ by 40 points, and how two candidate arms with the same schema validity got opposite verdicts.</p>
<div class="amw-module__foot" markdown="span">
<span class="amw-module__time">~6 min read</span>
[Start :material-arrow-right:](modules/07-shadow-and-adjudication.md){ .amw-module__link }
</div>
</div>

<div class="amw-card amw-module amw-module--core" markdown="1">
<div class="amw-module__head" markdown="1">
<div class="amw-module__icon" markdown="span">:material-clipboard-check-outline:</div>
<p class="amw-module__title">08 — The scorecard</p>
<div class="amw-badge" markdown="span">:material-star: Core</div>
</div>
<p class="amw-module__desc">The assembled report with every caveat welded in place: what passed, what failed, what was not evaluated, and why no cell contains a dollar figure.</p>
<div class="amw-module__foot" markdown="span">
<span class="amw-module__time">~5 min read</span>
[Start :material-arrow-right:](modules/08-the-scorecard.md){ .amw-module__link }
</div>
</div>
</div>

<h2 class="amw-section-title">Before you start</h2>

<div class="amw-notes" markdown="1">
<div class="amw-card amw-note" markdown="1">
<div class="amw-note__head" markdown="span">:material-clock-outline: Pre-work required</div>
<p>About ten minutes: clone the repo, create the virtualenv, and get <code>python cli.py e2e --mode replay</code> to a green exit. It needs <strong>no credentials, no project and no network</strong> — it replays the trace corpus committed in <code>artifacts/replay/</code>. Full instructions on the <a href="setup/">Setup</a> page.</p>
</div>
<div class="amw-card amw-note" markdown="1">
<div class="amw-note__head" markdown="span">:material-account-check-outline: Who it's for</div>
<p>Engineers and architects who have to defend a model-migration decision to someone who will push back on it: platform teams costing a high-volume RAG stack, ML engineers who own the eval harness, and the person who signs the change. No statistics beyond a bootstrap confidence interval, and module 03 explains that one.</p>
</div>
<div class="amw-card amw-note" markdown="1">
<div class="amw-note__head" markdown="span">:material-close-circle-outline: What this is not</div>
<p>Not a benchmark and not a claim that one model beats another. It is a <em>method</em> for deciding one migration on one workload, demonstrated on a synthetic corpus so the whole thing is inspectable. Not a cost analysis either — <code>config/pricing.yaml</code> still holds <code>VERIFY</code> placeholders, so every cost cell renders as an em dash.</p>
</div>
</div>
