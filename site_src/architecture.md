# Architecture & toolchain

*~7 min read. Read this once and the rest of the site stops looking like magic:
every page you are about to read is one stage of the pipeline below.*

---

## The shape of it

The workbench is a measurement rig, not an application. It sends the same work to
two model families, records everything that comes back, scores the recordings
against thresholds agreed in advance, and renders a report that carries its own
caveats.

Five stages, in order. Each writes a file the next one reads, and every file is
committed:

| Stage | What happens | What it writes |
|---|---|---|
| **1. Dataset** | 70 items per worker are generated from a fixed seed, with a rubric per item | `artifacts/datasets/` |
| **2. Baseline eval** | Each arm runs the dataset; a judge model scores the outputs | `artifacts/results/phase2_*.json` |
| **3. Adaptation ladder** | The prompt is rewritten in labelled steps and each step re-measured | `artifacts/results/ablation_*.json` |
| **4. Shadow comparison** | Both arms answer the same shadow slice; disagreements are triaged | `artifacts/results/shadow_*.json` |
| **5. Scorecard** | Thresholds are evaluated and a verdict is produced per worker | `artifacts/results/scorecard*.md` |

Nothing in stage 5 recomputes anything from stages 2–4. It reads their numbers,
compares them to `config/gates.yaml`, and renders. That separation is why a
threshold change can never quietly move a measurement.

## Every call is recorded, and replay is the default

The single most important piece of the toolchain is the trace store.

Every model call — baseline, ladder rung, shadow, judge — is written as one line
of JSON to `artifacts/replay/`, keyed on the worker, the model and a hash of the
exact input. Recording is always on when a call goes to a live endpoint, and
there is no switch to turn it off.

That gives the whole rig three modes, chosen in one place rather than at each
call site:

| Mode | What it does | Credentials |
|---|---|---|
| `replay` | Every call is served from the recorded corpus | none |
| `live` | Every call goes to the endpoint, and is recorded on the way back | Google Cloud |
| `hybrid` | Recorded calls are replayed, new ones go live and are recorded | Google Cloud |

A replay miss is a loud failure that names the exact key it wanted. It never
substitutes a nearby recording and never invents a response. That is what lets
this site say the numbers reproduce offline and mean it literally:
`python cli.py e2e --mode replay` is the pipeline, end to end, with no network.

!!! note "What the input hash covers"

    The recording key folds in the prompt bytes and the tools offered, so a
    changed prompt cannot silently reuse an old answer. It does **not** fold in
    the sampling configuration — which is why a deliberately reconfigured arm,
    such as the reduced-reasoning-budget arm on the recommended model, is
    registered as its own entry in `config/models.yaml` rather than as a flag.
    Two configurations of one model are two rows, and their recordings never
    collide.

## The toolchain

| Layer | What is used | Why it is there |
|---|---|---|
| Runtime | Python 3.11 | Every schema in the repo is a `pydantic` model, so a mismatched runtime fails at import rather than at 3 a.m. |
| Google models | Vertex AI, via the `google-genai` SDK | One access path for Gemini 2.5 Flash, Gemini 3.6 Flash, Gemini 3.5 Flash and the Gemini 2.5 Pro judge |
| Claude models | Vertex AI partner models, via the Anthropic Vertex SDK | Claude Sonnet 5 runs through the same cloud as everything it is compared with |
| Multi-agent demo | Agent Development Kit | A root agent delegating to the three workers, loading the exact prompt files the bench measured |
| Statistics | Bootstrap resampling in `amw/eval/stats.py` | 10,000 resamples, seed `20260812`, 95% confidence ranges on every figure |
| Reporting | Markdown rendered from `pydantic` models | The report is a pure function of the recorded results plus the thresholds file |
| This site | MkDocs Material, built with `--strict` | Pages include repository files directly rather than quoting them, so a page cannot drift from the thing it describes |

Two access paths exist for Claude — Vertex AI and the direct Anthropic endpoint —
and this study ran every arm through Vertex AI. Where you see a Claude model ID
on this site, it is the Vertex AI partner model ID, because that is the one that
was actually called.

## Where the code lives

```
config/         thresholds, model registry, prices, customer profiles
amw/
  adapters/     one interface per provider; the three modes resolve here, once
  datasets/     seeded generation, rubrics, provenance stamping
  traces/       the canonical trace schema and the replay store
  eval/         metrics, bootstrap statistics, judge prompts as versioned files
  tuning/       the adaptation ladder and the prompt optimizer
  shadow/       paired running and disagreement triage
  economics/    cost from recorded tokens and the price file
  reporting/    scorecard assembly, charts, markdown rendering
notebooks/      two notebooks that import from amw/ and display
artifacts/      datasets, recordings, results, reports
```

Four rules hold this together, and each of them is enforced somewhere other than
by good intentions:

- **No model ID in code.** They live in `config/models.yaml`, keyed by access
  path.
- **No threshold in code.** They live in `config/gates.yaml`, and the report
  footer prints that file's version hash.
- **No price in code.** They live in `config/pricing.yaml`, stamped with the date
  a human verified them and the page each rate was read off.
- **No logic in notebooks.** The notebooks import and display. Anything longer
  than a few lines moves into `amw/`.

## What the site build guarantees

This site has a `docs_dir` of `site_src/`. The repository's `docs/` directory
holds internal presenter material and is never published — and because a
configuration setting is a weak promise, the build script greps the *built* HTML
for distinctive phrases from each internal file and exits non-zero on a hit.

Two more checks run at build time:

- **Copied artifacts are copied, not retyped.** The Results pages are byte-for-byte
  copies of files in `artifacts/`, with a provenance banner added above them.
- **Hand-written figures are re-derived.** A number written into a sentence —
  "19 rates still read `VERIFY`" — is recomputed from the config at build time,
  and the build fails if the sentence has drifted.

Charts are drawn at build time from the same artifacts the tables are built from,
so a chart cannot quietly disagree with the table beside it.

## What this architecture cannot see

Worth stating plainly, because the rig is convincing and its blind spot is not
visible from inside it.

Every measurement here is of a **single call**: one input, one output, scored
against a rubric. The three workers are that shape, which is exactly why they
were chosen to migrate first.

What the rig does not measure is the orchestrator above them — when to delegate,
when to retry, when to stop. The Agent Development Kit demo exists so that gap is
something you can watch rather than something you have to take on trust: the
leaves are measured in full, the root gets no verdict, and no number on this site
should be extended to cover it.

---

**Next:** [Module 01 — Why subagents migrate first](modules/01-why-subagents-first.md)
