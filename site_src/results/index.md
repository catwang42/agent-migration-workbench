# Results

Everything in this section is copied verbatim from the repository by
`python scripts/build_site.py`. Nothing is retyped, reformatted or rounded on the
way in — the only thing the copier adds is a provenance banner naming the source
file. Re-running the pipeline and re-running the copier is the entire refresh
procedure.

!!! note "freeze-v1"

    These artifacts are the frozen run. Every call behind them was executed and
    recorded between 10 Aug 2026, 12:07 AM and 12 Aug 2026, 8:58 PM (SGT,
    Singapore time).

    UTC: `2026-08-09T16:07:15+00:00` → `2026-08-12T12:58:33+00:00`
    { .amw-provenance }

## The artifacts

<ul class="amw-cards">
<li><div class="amw-card">
  <p class="amw-card__title">Migration Readiness Scorecard</p>
  <p><strong>The deployment answer.</strong> The gates evaluated against Gemini
  3.6 Flash with the reasoning budget minimised — the configuration the
  scorecard recommends. Per-subagent gate tables, evidence rows, the "not
  evaluated and why" lists, the two configurations' economics side by side, and
  the provenance footer.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">source: <code>artifacts/results/scorecard_current-capped.md</code></span>
  <a class="md-button" href="../scorecard/">Open</a></p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">How we got here — development generation</p>
  <p>The same six gates on Gemini 2.5 Flash, the generation the adaptation
  ladder and the prompt-optimizer work were done on. Not the recommendation:
  it is the evidence that the instruction rules written here kept working
  unchanged on the deployment generation above.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">source: <code>artifacts/results/scorecard_widened.md</code></span>
  <a class="md-button" href="../scorecard_development_generation/">Open</a></p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">Selection table</p>
  <p>Every ladder rung and gated arm, per subagent, grouped by split, with all
  deterministic metrics beside the judged score.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">source: <code>artifacts/reports/selection_table.md</code></span>
  <a class="md-button" href="../selection_table/">Open</a></p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">Judge cross-check</p>
  <p>Claude Sonnet 5 against Gemini 2.5 Pro on the same recorded outputs and the
  same rubrics: agreement, kappa, and the largest per-item disagreements with
  both rationales.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">source: <code>artifacts/results/crosscheck.md</code></span>
  <a class="md-button" href="../crosscheck/">Open</a></p>
</div></li>
<li><div class="amw-card">
  <p class="amw-card__title">Charts</p>
  <p>Interval charts exported from the executed notebooks — point estimates with
  95% confidence ranges and the gate bound marked.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">source: <code>artifacts/notebooks/*.out.ipynb</code></span>
  <a class="md-button" href="../charts/">Open</a></p>
</div></li>
</ul>

## Reading rules that apply to all of them

- **Every figure came from an executed call.** No number in any artifact is a
  placeholder, an estimate or a projection. Where a value could not be measured,
  the cell says so and names the reason.
- **Every gate is a 95% confidence range bound, not a point estimate.** 10,000
  resamples, seed `20260812`. Lower bound for `min` gates, upper for `max`.
- **Every judge score carries its n and its split.** Two judge scores at
  different n are not directly comparable, and the number alone does not say so.
  Feature Extractor's Claude incumbent is 0.903 on the core 28 and 0.900 on the
  full 70; those are two measurements, not one.
- **Corpus cost is not projected cost.** The dollar figures on the headline
  scorecard are the measured token counts of these exact 70 items, priced at the
  uncached list rates in `config/pricing.yaml` (verified 2026-08-12). They are a
  measurement of what this corpus cost. They are **not** a monthly bill, an
  annual run rate, or a saving at customer scale: the demo profile's call
  volumes are illustrative (`volumes_confirmed: false`), so every
  volume-projected cell still renders *not measured*. A real call rate
  multiplied by anything is the most dangerous number this repo could print, and
  the two are kept in separate columns for exactly that reason.
- **Latency figures are directional.** The deployment probes pin both arms to
  `global`, which is the only thing that opens the gate — but they are n=10 per
  arm, and the incumbent's own p95 moved between the two probes taken three
  hours apart on the same model in the same region (Query Rewriter 3,471 →
  6,893 ms). Latency requires measurement on production infrastructure;
  demo-window figures are directional. The development-generation artifacts have
  no latency figure at all — Claude ran in `global` and Gemini in `us-central1`,
  and that renders as *not comparable*, not as a pass.
- **Nothing here is projected onto a model that was not run.** Claude Opus 5 and
  Gemini 3.1 Pro (preview) are priced in `config/pricing.yaml` so a customer on
  those tiers can be costed without a code change, and that is all they are.

## Regenerate them

```bash
python cli.py e2e --mode replay      # rebuild every artifact from recordings
python scripts/build_site.py         # copy the whitelist into site_src/results/
mkdocs build --strict                # verify, including the leak check
```
