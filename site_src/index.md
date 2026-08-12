<div class="amw-hero" markdown>
<p class="amw-hero__eyebrow">Claude → Gemini subagent migration · patents-domain RAG</p>
<h1 class="amw-hero__title">Agent Migration Workbench</h1>
<p class="amw-hero__thesis">
Evidence, not assertions. Decide a model migration the way you would decide a
database migration: pre-register the gates, run both arms over the same corpus,
check the confidence-interval bounds, and publish the verdict the numbers give
you — including the ones that say <em>no</em>.
</p>
<p class="amw-hero__cta">
<a class="md-button md-button--primary" href="setup/">Start the workshop</a>
<a class="md-button" href="#the-modules">Browse modules</a>
</p>
</div>

!!! warning "Draft — refreshes at freeze-v1"

    Every figure on this site is measured, but the run behind it is the
    pre-freeze run (recordings 2026-08-09T16:07:15+00:00 → 2026-08-11T06:20:37+00:00).
    The final run replaces these pages via one command
    (`python scripts/build_site.py`) at content freeze; nothing here is a
    placeholder waiting for a value.

<ul class="amw-chips">
<li><a class="amw-chip" href="modules/01-why-subagents-first/"><span class="amw-chip__num">01</span> Why subagents migrate first</a></li>
<li><a class="amw-chip" href="modules/02-reference-workload/"><span class="amw-chip__num">02</span> The reference workload</a></li>
<li><a class="amw-chip" href="modules/03-gates-as-contract/"><span class="amw-chip__num">03</span> Gates as contract</a></li>
<li><a class="amw-chip" href="modules/04-the-naive-swap/"><span class="amw-chip__num">04</span> The naive swap</a></li>
<li><a class="amw-chip" href="modules/05-adaptation-ladder/"><span class="amw-chip__num">05</span> The adaptation ladder</a></li>
<li><a class="amw-chip" href="modules/06-the-second-judge/"><span class="amw-chip__num">06</span> The second judge</a></li>
<li><a class="amw-chip" href="modules/07-shadow-and-adjudication/"><span class="amw-chip__num">07</span> Shadow &amp; adjudication</a></li>
<li><a class="amw-chip" href="modules/08-the-scorecard/"><span class="amw-chip__num">08</span> The scorecard</a></li>
</ul>

## By the numbers

<ul class="amw-stats">
<li><div class="amw-stat">
  <span class="amw-stat__value">3</span>
  <span class="amw-stat__label">Subagents measured end to end</span>
  <span class="amw-stat__source">Query Rewriter, Chunk Summarizer, Feature Extractor · <code>config/customers/demo_patents.yaml</code></span>
</div></li>
<li><div class="amw-stat">
  <span class="amw-stat__value">210</span>
  <span class="amw-stat__label">Corpus items, 70 per subagent</span>
  <span class="amw-stat__source">synthetic, seed <code>20260812</code>, generator <code>t06.1</code> · <code>datasets/*.jsonl</code></span>
</div></li>
<li><div class="amw-stat">
  <span class="amw-stat__value">4,408</span>
  <span class="amw-stat__label">Recorded model calls in the replay corpus</span>
  <span class="amw-stat__source">4,407 <code>ok</code>, 1 <code>error</code> · line count of <code>artifacts/replay/*.jsonl</code></span>
</div></li>
<li><div class="amw-stat">
  <span class="amw-stat__value">2,804</span>
  <span class="amw-stat__label">Of those, recorded judge calls</span>
  <span class="amw-stat__source">762 QR + 624 CS + 1,418 FE · <code>artifacts/replay/judge_*.jsonl</code></span>
</div></li>
<li><div class="amw-stat">
  <span class="amw-stat__value">6</span>
  <span class="amw-stat__label">Gates pre-registered before any result was seen</span>
  <span class="amw-stat__source">version 1, hash <code>92f9d018432f</code> · <code>config/gates.yaml</code></span>
</div></li>
<li><div class="amw-stat">
  <span class="amw-stat__value">1,010</span>
  <span class="amw-stat__label">Tests green, 2 skipped</span>
  <span class="amw-stat__source">1,012 collected · <code>pytest tests/ -q</code></span>
</div></li>
</ul>

## What makes this different

<ul class="amw-cards">
<li><div class="amw-card">
  <p class="amw-card__title">1 — Gates are pre-registered, and the report proves it</p>
  <p>Six thresholds live in <code>config/gates.yaml</code> and are signed off before
  the first result appears. Every scorecard footer prints the file's version hash
  (<code>92f9d018432f</code>), so a reader can tell a threshold did not move after
  the numbers landed. Gates are checked against the <strong>95% CI bound</strong>,
  never the point estimate — which is what licenses the phrase
  <em>quality parity within measurement under pre-agreed gates</em>.</p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">2 — The instrument is matched to the subagent's autonomy level</p>
  <p>A single-call transform, a tool decider, a retrieval chain and a looping
  orchestrator do not decide the same things, so they cannot share one bench. The
  four-row taxonomy in module 01 picks the instrument, and the instrument decides
  whether a verdict is even available. Three Level 1 subagents are measured in
  full here; the rest are named and left unmeasured.</p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">3 — Two vendors' judges, cross-checked, never averaged</p>
  <p>Gemini 2.5 Pro is the gated judge, registered before results. Claude Sonnet 5
  re-scored the same recorded outputs against the same rubrics: criterion agreement
  98.8% / 99.0% / 92.9% (kappa 0.936 / 0.948 / 0.758). The two are never averaged
  and one is never substituted for the other — the cross-check validates the
  instrument, it does not replace it.</p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">4 — Verdicts that can say no</p>
  <p>This run produced no MIGRATE. Query Rewriter is <strong>HOLD</strong> on a
  failed blocking gate. Chunk Summarizer and Feature Extractor are
  <strong>INCOMPLETE</strong> — 4 of 6 and 3 of 6 gates measured, provisionally
  TUNE_FIRST — because a verdict over a subset of the gates is not the verdict that
  was agreed. The orchestrator is deliberately unmeasured and receives no verdict
  at all.</p>
</div></li>
</ul>

## The modules { #the-modules }

Eight pages, roughly five minutes each, in the order the workshop runs them.

<ul class="amw-cards">
<li><div class="amw-card">
  <p class="amw-card__title">01 — Why subagents migrate first</p>
  <p>The four-category taxonomy: what each autonomy level actually decides, which
  instrument measures it, and which rows are allowed a verdict today.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">~5 min read</span>
  <a class="md-button" href="modules/01-why-subagents-first/">Start</a></p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">02 — The reference workload</p>
  <p>70 items per subagent, synthetic and labelled as such, one seed, a 40/25/20/15
  difficulty mix, and a realism pass that is not allowed to touch the answer key.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">~5 min read</span>
  <a class="md-button" href="modules/02-reference-workload/">Start</a></p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">03 — Gates as contract</p>
  <p>A line-by-line read of <code>config/gates.yaml</code>, including the
  <code>alt</code> clause on <code>shadow_agreement</code> and what a sentinel that
  cannot resolve does to a gate.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">~6 min read</span>
  <a class="md-button" href="modules/03-gates-as-contract/">Start</a></p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">04 — The naive swap</p>
  <p>Same prompt bytes, new endpoint. The real per-subagent baseline numbers, and
  the two defects the swap surfaced on <em>both</em> sides.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">~5 min read</span>
  <a class="md-button" href="modules/04-the-naive-swap/">Start</a></p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">05 — The adaptation ladder</p>
  <p>Every rung that was run, at the n it was run on — including the optimizer rung,
  its contamination, and the quoting rule that follows from it.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">~7 min read</span>
  <a class="md-button" href="modules/05-adaptation-ladder/">Start</a></p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">06 — The second judge</p>
  <p>How you bound "a Gemini judge grading Gemini output": rubric anchoring, k=2
  repeats, and a Claude judge re-scoring the same recorded outputs.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">~5 min read</span>
  <a class="md-button" href="modules/06-the-second-judge/">Start</a></p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">07 — Shadow &amp; adjudication</p>
  <p>Why item-level and structured-field agreement differ by 40 points, and how two
  candidate arms with the same schema validity got opposite verdicts.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">~6 min read</span>
  <a class="md-button" href="modules/07-shadow-and-adjudication/">Start</a></p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">08 — The scorecard</p>
  <p>The assembled report with every caveat welded in place: what passed, what
  failed, what was not evaluated, and why no cell contains a dollar figure.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">~5 min read</span>
  <a class="md-button" href="modules/08-the-scorecard/">Start</a></p>
</div></li>
</ul>

## Before you start

### Pre-work required — about 10 minutes

Clone the repo, create the virtualenv, and get
`python cli.py e2e --mode replay` to a green exit. It needs **no credentials, no
project, and no network** — it replays the recorded trace corpus that is committed
in `artifacts/replay/`. Full instructions on the [Setup](setup.md) page. If you
want to run the ADK delegation exercise you will also need Google Cloud ADC, but
every other exercise on this site works offline.

### Who it is for

Engineers and architects who have to defend a model-migration decision to someone
who will push back on it: platform teams costing a high-volume RAG stack, ML
engineers who own the eval harness, and the person who has to sign the change.
Prior exposure to LLM evaluation helps; none of the statistics goes beyond a
bootstrap confidence interval, and module 03 explains that one.

### What this is not

It is not a benchmark, and it is not a claim that one model is better than
another. It is a *method* for deciding a specific migration on a specific
workload, demonstrated on a synthetic patents-domain corpus so that the whole
thing is inspectable. It is not a cost analysis — `config/pricing.yaml` still
holds `VERIFY` placeholders, so every cost and savings cell on this site is an em
dash rather than a number. And it does not cover the orchestrator: a looping,
tool-dispatching agent needs trajectory evaluation in the runtime, and a
single-call bench harness cannot see what it does.
