# CLAUDE.md — Agent Migration Workbench (Act 1)

## What this repo is
A customer-workshop toolkit that evaluates migrating high-volume RAG subagents (Query Rewriter, Chunk Summarizer, Feature Extractor) from Claude to Gemini: baseline eval → prompt adaptation (ablation ladder) → shadow comparison → gates-based Migration Readiness Scorecard. Delivery deadline: **Wed Aug 12**. Work strictly from `TASKS.md` in order; scope tiers are defined in `act1_build_plan.md`.

The full-system design — including Act 2 (BYOT real-trace ingestion) — lives in `docs/master_plan.md`. Read it once for architectural context (module shapes, naming, where Act 2 plugs into `traces/` and `eval/`), then treat it as reference only: it is **not** a work order, and nothing from it enters the build unless it appears in `TASKS.md`.

## Non-negotiable ground rules
1. **No fabricated results, ever.** Every number shown to a customer comes from an executed model call or an explicit calculation over recorded calls. Replay mode replays *previously recorded real calls* and labels itself on screen with the recording date. Never generate placeholder metric values that could be mistaken for measurements.
2. **Provenance labels everywhere.** Every dataset item carries `provenance: synthetic|customer` and a generator seed. Every report footer prints provenance, run date, region, gates version hash.
3. **Prices only from `config/pricing.yaml`.** Never hardcode a price or a savings % anywhere else. Ship `VERIFY` placeholders; `scripts/refresh_pricing.py` sets values + `verified_on` + source URLs.
4. **Everything must run in replay mode with zero credentials.** Any feature that only works live is incomplete. `python cli.py e2e --mode replay` must pass before every commit.
5. **Record-on-live is always on.** Every live call appends a canonical trace to `artifacts/replay/`. Do not add a flag to disable it.
6. **Scope guard.** Build P0 in TASKS order. P1 only after its Day-0 spike is marked GREEN in `SPIKES.md`. Never start P2. If a task balloons past ~2× estimate, stop and flag instead of gold-plating.
7. **Parity language.** Reports say "quality parity within measurement under pre-agreed gates," never "zero quality drop." Gates check **CI lower bounds**.
8. **Thin notebooks.** All logic lives in `amw/`; notebooks import and display. If a notebook cell exceeds ~15 lines of logic, move it into the library.

## Setup
```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
gcloud auth application-default login          # human does this
cp .env.example .env                           # PROJECT_ID, REGION, CLAUDE_PATH=vertex|anthropic|replay
```

## Commands
```bash
python cli.py gen --customer demo_patents -n 70     # synthetic dataset + rubrics
python cli.py phase2 --mode hybrid -n 10            # baseline eval (subset)
python cli.py ablate --subagent query_rewriter      # A0–A4 ladder
python cli.py shadow --mode hybrid                  # shadow run + triage
python cli.py scorecard                             # gates → verdicts → markdown report
python cli.py e2e --mode replay                     # full offline pipeline (CI gate)
python cli.py smoke --mode live -n 2                # pre-demo health check
pytest tests/                                       # golden-fixture metric tests
```

## Conventions
- Python 3.11, `pydantic` models for every schema (traces, dataset items, metrics, gate results).
- Adapters implement `amw/adapters/base.py::ModelAdapter`; modes `live|replay|hybrid` resolved in one place (`adapters/__init__.py`), never per-callsite.
- Canonical trace schema: `amw/traces/schema.py` — JSONL, one trace per line; the replay store is keyed on `(subagent, model, input_sha)`.
- All model IDs come from `config/models.yaml`; all thresholds from `config/gates.yaml`. No literals in code.
- Judge prompts live in `amw/eval/judge_prompts/` as versioned text files (they get shown to customers).
- Deterministic seeds everywhere (`config/customers/*.yaml: seed`).
- Errors from live calls: retry ×2 with backoff, then record a `status:"error"` trace and continue the batch — a single flaky call must not kill an eval run.

## Definition of done (every task)
The task card's verify command passes **and** `pytest tests/ && python cli.py e2e --mode replay` still pass. Commit per task with the task ID in the message.

## Repo map (abridged)
```
config/{models,pricing,gates}.yaml, config/customers/
amw/{adapters,agents,datasets,traces,eval,tuning,shadow,economics,reporting}/
notebooks/01_baseline_and_tuning.ipynb, 02_shadow_scorecard.ipynb
scripts/refresh_pricing.py   artifacts/replay/   tests/   docs/
cli.py  TASKS.md  SPIKES.md  WORKSHOP_RUNBOOK.md
```
