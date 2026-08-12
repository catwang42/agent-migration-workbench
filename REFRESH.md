# Refreshing the public site

The public workshop companion lives at
<https://catwang42.github.io/agent-migration-workbench/>. Its sources are
`site_src/` (hand-written pages) plus `site_src/results/` (copied artifacts).

**The published `docs_dir` is `site_src`, never `docs/`.** `docs/` is internal
presenter material. `scripts/build_site.py` greps the built output for distinctive
phrases from every internal file and fails the build if any of them leak.

---

## The one-liner

After the freeze-v1 run has produced fresh artifacts:

```bash
source .venv/bin/activate && python cli.py e2e --mode replay && python scripts/build_site.py && mkdocs gh-deploy --strict
```

That is: regenerate the artifacts → copy the whitelist into `site_src/results/`
→ build strictly, run the leak check, and publish to `gh-pages`.

To rehearse without publishing, stop one step short:

```bash
python scripts/build_site.py          # copies, builds to a temp dir, runs the leak check
```

`build_site.py` never deploys. `gh-deploy` is always a deliberate, separate,
human-run command.

---

## What each step does

| Step | Effect |
|---|---|
| `python cli.py e2e --mode replay` | Regenerates `artifacts/results/*` and `artifacts/reports/selection_table.md` from the recorded corpus. Zero credentials. |
| `python scripts/build_site.py` | Copies the whitelist into `site_src/results/`, regenerates `results/charts.md`, then builds to a temp dir and greps the output for internal content. Idempotent — running it twice prints `unchanged`. |
| `mkdocs build --strict` | Verifies without publishing. Broken links and pages missing from `nav` are errors. |
| `mkdocs gh-deploy --strict` | Builds and pushes to the `gh-pages` branch. **Human-run only.** |

Install the docs toolchain once:

```bash
pip install -r requirements-docs.txt
```

Those deps are deliberately **not** in `requirements.txt` — nothing in `amw/`,
`cli.py` or `tests/` imports mkdocs, and the CI gate must not grow a docs
dependency.

---

## What is copied, and what is not

The whitelist is the two lists at the top of `scripts/build_site.py`. It is
explicit on purpose: there is no glob over `artifacts/` and no glob over `docs/`,
so a new internal file cannot be published by appearing in a directory.

Currently published:

| Source | Published as |
|---|---|
| `artifacts/results/scorecard_widened.md` | `results/scorecard.md` |
| `artifacts/reports/selection_table.md` | `results/selection_table.md` |
| `artifacts/results/crosscheck.md` | `results/crosscheck.md` |
| `artifacts/notebooks/01_baseline_and_tuning.out.ipynb` image 0 | `results/charts/json_schema_validity.png` |
| `artifacts/notebooks/02_shadow_scorecard.out.ipynb` image 0 | `results/charts/shadow_agreement_structured.png` |

Copied artifacts are inserted byte-for-byte; the only addition is a provenance
banner naming the source file, placed just after the source's own H1.

### Two charts are excluded, and both need re-checking at freeze

`EXCLUDED_CHARTS` in `scripts/build_site.py` holds two images from
`01_baseline_and_tuning.out.ipynb` that are **stale, not wrong** — the notebook
was last executed 2026-08-11 03:03, before the widening and before the B3/B4
rungs ran:

- **image 1**, the judge-score interval chart, was rendered on the core-28 split.
  It shows `query_rewriter` / `claude_baseline` at roughly 0.911, which is the
  core-28 incumbent; the shipping full-70 figure is 0.886. Publishing it beside
  the full-70 tables would invite exactly the cross-split comparison the
  selection table forbids.
- **image 2**, the Feature Extractor ladder chart, prints "not measured" for
  `A0-schema`, `A4-novelty-tool` and `A4-novelty-schema` — all three have since
  been measured — and omits `A4-optimizer` entirely.

**At freeze: re-execute both notebooks, then re-check the exclusions.** Both
charts become publishable the moment notebook 01 is re-run against the current
results. Move them from `EXCLUDED_CHARTS` to `CHART_WHITELIST` with captions, and
confirm the image indices — `build_site.py` fails loudly rather than guessing if
the notebook's chart order has changed.

Re-execution is a notebook step, not a `cli.py` step:

```bash
jupyter nbconvert --to notebook --execute notebooks/01_baseline_and_tuning.ipynb \
  --output-dir artifacts/notebooks --output 01_baseline_and_tuning.out.ipynb
jupyter nbconvert --to notebook --execute notebooks/02_shadow_scorecard.ipynb \
  --output-dir artifacts/notebooks --output 02_shadow_scorecard.out.ipynb
```

!!! note
    `artifacts/notebooks/` is **not** tracked by git (`.gitignore` keeps only
    `replay/`, `results/`, `optimizer/`, `reports/`, `backup/`). The extracted
    PNGs under `site_src/results/charts/` **are** tracked, so the site builds on a
    clean clone — but a clean clone cannot re-extract them without re-executing
    the notebooks first.

---

## What still has to change by hand at freeze

`build_site.py` refreshes the Results section. It does not touch the hand-written
pages, and three things in them are dated:

1. **The draft badges.** Every pre-freeze figure carries
   `<span class="amw-draft">Draft — refreshes at freeze-v1</span>`. Remove them
   once the figures are final:

   ```bash
   grep -rn "amw-draft" site_src/
   ```

   The badge is also emitted by `build_site.py` (the `DRAFT_BADGE` constant and
   `DocEntry.draft`); flip `draft=False` there rather than editing the copies.

2. **The recording window** — `2026-08-09T16:07:15+00:00 →
   2026-08-11T06:20:37+00:00` appears on the home page and in
   `results/index.md`. It comes from the scorecard footer.

3. **The stat counters on the home page.** Recompute rather than assume:

   ```bash
   wc -l datasets/*.jsonl                                  # corpus items
   cat artifacts/replay/*.jsonl | wc -l                    # recorded model calls
   cat artifacts/replay/judge_*.jsonl | wc -l              # recorded judge calls
   pytest tests/ -q                                        # test count
   ```

   Every counter on the site names the artifact it came from. Keep it that way —
   a figure whose source cannot be named does not go on the site (CLAUDE.md
   ground rule 1).

## Cost figures

There are none, anywhere, and this is enforced by policy rather than by taste:
`config/pricing.yaml` has 13 rates reading `VERIFY` and `verified_on: null`, and
the customer profile carries `volumes_confirmed: false`. Until **both** are
cleared by a human, every cost and savings cell renders as an em dash — never a
number, never a zero. When they clear, re-run `cli.py e2e` and the artifacts will
carry the figures in themselves; do not type one onto a page.
