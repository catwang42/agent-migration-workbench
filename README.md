# Agent Migration Workbench

A customer-workshop toolkit for deciding — with measurements, not vibes — whether
high-volume RAG subagents should migrate from Claude to Gemini.

It runs a four-stage pipeline over three subagents (Query Rewriter, Chunk
Summarizer, Feature Extractor):

```
baseline eval  ->  prompt adaptation  ->  shadow comparison  ->  Migration
                   (A0-A4 ablation)                             Readiness
                                                                Scorecard
```

The scorecard is the deliverable. It applies pre-agreed gates from
`config/gates.yaml` and returns one of three verdicts per subagent —
**MIGRATE**, **TUNE_FIRST**, or **HOLD** — with the evidence behind each.

**Delivery deadline: Wed 12 Aug 2026.**

---

## Status

Day 0 is complete; Day 1 (`T06`) is the next task.

| Built | Not built yet |
|---|---|
| Config system with validation (`amw/config.py`) | Dataset generator (`T06`) |
| Canonical trace schema + replay store (`amw/traces/`) | Judge + eval pipeline (`T07`–`T09`) |
| Gemini adapter (`amw/adapters/gemini.py`) | Ablation ladder (`T10`) |
| Claude adapters, Vertex + direct (`amw/adapters/claude_*.py`) | Shadow runner + triage (`T11`) |
| Mode resolution + record-on-live (`amw/adapters/__init__.py`) | Gates, scorecard, economics (`T12`) |
| Day-0 platform spikes, all GREEN (`SPIKES.md`) | Notebooks (`T13`), smoke check (`T16`) |

179 tests pass. **Most `cli.py` subcommands are deliberate stubs** — they exit
non-zero naming the task that will deliver them, rather than pretending to have
run. That is by design; see ground rule 1.

Two things are known-outstanding and affect any live run:

- **`config/pricing.yaml` is 13 × `VERIFY`.** No price or savings figure can be
  printed until `scripts/refresh_pricing.py` has been run against real
  published rates.
- **Claude and Gemini currently run in different regions.** Model Garden quota
  for `anthropic-claude-sonnet-5` is exhausted in `us-central1`, so Claude runs
  in `global`. This makes the `latency_p95` gate a cross-region comparison that
  the scorecard must disclose. Details and the fix in `SPIKES.md`.

---

## Setup

Requires Python 3.11.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in PROJECT_ID, REGION
```

For live runs you also need Google Cloud Application Default Credentials:

```bash
gcloud auth application-default login
```

On a GCE VM with a suitably scoped service account this is already provided by
the metadata server and the login step can be skipped.

**You do not need any of the above to run the test suite or replay mode.** That
is a hard requirement, not a convenience — see ground rule 4.

---

## Running it

```bash
python cli.py gen --customer demo_patents -n 70   # synthetic dataset + rubrics
python cli.py phase2 --mode hybrid -n 10          # baseline eval (subset)
python cli.py ablate --subagent query_rewriter    # A0-A4 prompt ladder
python cli.py shadow --mode hybrid                # shadow run + triage
python cli.py scorecard                           # gates -> verdicts -> report
python cli.py e2e --mode replay                   # full offline pipeline
python cli.py smoke --mode live -n 2              # pre-demo health check
pytest tests/                                     # 179 tests, no credentials
```

### Execution modes

Every subcommand takes `--mode`, defaulting to `replay`.

| Mode | Gemini | Claude | Credentials |
|---|---|---|---|
| `replay` | replayed | replayed | **none** |
| `hybrid` | live | replayed | GCP ADC |
| `live` | live | live | GCP ADC (+ API key if `CLAUDE_PATH=anthropic`) |

`hybrid` is the workshop default. The Claude baseline was recorded once;
replaying it keeps the comparison stable and avoids re-billing a measurement
that should not move.

Mode is resolved in exactly one place — `amw/adapters/__init__.py::resolve` —
and never at a call site. Per-callsite mode checks are how a "replay" run ends
up making one real call from some forgotten branch.

### Recording is not optional

Every live call appends a canonical trace to `artifacts/replay/`. There is no
flag to disable it, and adding one would be a bug. `resolve()` wraps every live
adapter in `RecordingAdapter` on the way out, so recording is a property of how
adapters are obtained rather than something an adapter could forget to do.

Today's live run is tomorrow's offline demo. Error traces are recorded too — a
dropped failure silently shrinks an eval's denominator and flatters whichever
model failed.

### Claude access paths

`CLAUDE_PATH` in `.env` selects one of three:

- `vertex` — Vertex AI Model Garden, authenticated by GCP ADC, no API key.
  **The documented default**: traffic stays inside GCP.
- `anthropic` — direct Anthropic API. Needs `ANTHROPIC_API_KEY`.
- `replay` — no Claude calls at all; the baseline is served from
  `artifacts/replay/`.

Set `CLAUDE_REGION` when Claude needs a different region from Gemini, as it
currently does here. It falls back to `REGION` when unset.

---

## Ground rules

These are non-negotiable and are enforced in code where possible. The full list
is in `CLAUDE.md`; these four shape almost every design decision in the repo.

**1. No fabricated results, ever.** Every number a customer sees comes from an
executed model call or an explicit calculation over recorded calls. Replay mode
replays *previously recorded real calls* and labels itself on screen with the
recording date. A model output that was supposed to be JSON and isn't gets
recorded as text with `json: null`, never repaired — downstream scores a real
schema miss.

**2. Provenance everywhere.** Every dataset item carries
`provenance: synthetic|customer` and a generator seed. Every report footer
prints provenance, run date, region, and the gates version hash.

**3. Prices only from `config/pricing.yaml`.** Never hardcode a price or a
savings percentage anywhere else.

**4. Replay mode must work with zero credentials.** Any feature that only works
live is incomplete. Provider SDKs are imported lazily inside adapters so
`import amw.adapters` succeeds with nothing installed and nothing configured;
tests assert this.

One more worth internalising because it shows up in customer-facing text:
reports say **"quality parity within measurement under pre-agreed gates"**,
never "zero quality drop." Gates check **CI lower bounds**, not point estimates.

---

## Repo map

```
cli.py                    entry point; every subcommand names its owning task
config/
  models.yaml             model IDs per access path; no IDs in code
  pricing.yaml            all prices; VERIFY until refresh_pricing.py runs
  gates.yaml              thresholds + verdict rules; no thresholds in code
  customers/*.yaml        per-customer seed, domain, volumes, region
amw/
  config.py               pydantic loaders; the enforcement point
  adapters/               base contract, Gemini, Claude x2, replay, resolution
  traces/                 canonical Trace schema + ReplayStore
  datasets/ eval/ tuning/ shadow/ economics/ reporting/    (T06+)
artifacts/replay/         recorded traces, keyed (subagent, model, input_sha)
scripts/
  refresh_pricing.py      interactive price updater; stamps verified_on
  spike_s2_*, spike_s3_*  Day-0 platform probes (evidence for SPIKES.md)
tests/                    179 tests, all offline
```

### Where to read next

- **`CLAUDE.md`** — ground rules, conventions, definition of done. Read first.
- **`TASKS.md`** — the work order. Tasks are executed strictly in order.
- **`act1_build_plan.md`** — scope tiers (P0/P1/P2), gate rationale, fallbacks.
- **`SPIKES.md`** — Day-0 platform verdicts and the caveats they carry forward.
- **`docs/master_plan.md`** — full-system architecture including Act 2.
  **Reference only** — nothing in it enters the build unless it appears in
  `TASKS.md`.

---

## Conventions

- Python 3.11, pydantic v2 with `extra="forbid"` on every schema.
- Model IDs from `config/models.yaml`, thresholds from `config/gates.yaml`,
  prices from `config/pricing.yaml`. No literals in code.
- Adapters implement `amw/adapters/base.py::ModelAdapter`.
- Traces are JSONL, one per line; the replay store is keyed on
  `(subagent, model, input_sha)`.
- Judge prompts live in `amw/eval/judge_prompts/` as versioned text files —
  they get shown to customers.
- Deterministic seeds everywhere.
- Live call errors: retry ×2 with backoff, then record a `status:"error"` trace
  and continue. One flaky call must not kill an eval run.
- Notebooks stay thin: all logic lives in `amw/`, notebooks import and display.

**Definition of done for any task:** its verify command passes, **and**
`pytest tests/ && python cli.py e2e --mode replay` still pass. One commit per
task, task ID first in the message.
