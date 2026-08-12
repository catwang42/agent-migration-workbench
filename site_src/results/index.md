# Results

<span class="amw-draft">Draft — refreshes at freeze-v1</span>

Everything in this section is copied verbatim from the repository by
`python scripts/build_site.py`. Nothing is retyped, reformatted or rounded on the
way in — the only thing the copier adds is a provenance banner naming the source
file. Re-running the pipeline and re-running the copier is the entire refresh
procedure.

!!! warning "Pre-freeze run"

    These artifacts come from the run recorded 10 Aug 2026, 12:07 AM →
    11 Aug 2026, 2:20 PM (SGT, Singapore time). They are measurements, not
    placeholders. They are replaced wholesale by the freeze-v1 run.

    UTC: `2026-08-09T16:07:15+00:00` → `2026-08-11T06:20:37+00:00`
    { .amw-provenance }

## The four artifacts

<ul class="amw-cards">
<li><div class="amw-card">
  <p class="amw-card__title">Migration Readiness Scorecard</p>
  <p>The gates evaluated. Per-subagent gate tables, evidence rows, the "not
  evaluated and why" lists, the economics gates, and the provenance footer.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">source: <code>artifacts/results/scorecard_widened.md</code></span>
  <a class="md-button" href="../scorecard/">Open</a></p>
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
  95% CIs and the gate bound marked.</p>
  <p class="amw-card__meta"><span class="amw-card__readtime">source: <code>artifacts/notebooks/*.out.ipynb</code></span>
  <a class="md-button" href="../charts/">Open</a></p>
</div></li>
</ul>

## Reading rules that apply to all of them

- **Every figure came from an executed call.** No number in any artifact is a
  placeholder, an estimate or a projection. Where a value could not be measured,
  the cell says so and names the reason.
- **Every gate is a 95% confidence range (CI) bound, not a point estimate.** 10,000
  resamples, seed `20260812`. Lower bound for `min` gates, upper for `max`.
- **Every judge score carries its n and its split.** Two judge scores at
  different n are not directly comparable, and the number alone does not say so.
  Feature Extractor's Claude incumbent is 0.903 on the core 28 and 0.900 on the
  full 70; those are two measurements, not one.
- **Cost cells are em dashes.** `config/pricing.yaml` is unverified and volumes
  are unconfirmed. No dollar figure exists anywhere in this section.
- **Latency is not comparable.** Claude in `global`, Gemini in `us-central1`.

## Regenerate them

```bash
python cli.py e2e --mode replay      # rebuild every artifact from recordings
python scripts/build_site.py         # copy the whitelist into site_src/results/
mkdocs build --strict                # verify, including the leak check
```
