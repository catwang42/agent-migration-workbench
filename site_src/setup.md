# Setup

**Time:** about 10 minutes. **Credentials required:** none.

The whole evidence pipeline runs offline. Every model call this workshop shows you
was executed once, recorded as a canonical trace, and committed to
`artifacts/replay/`. Replay mode replays *those recorded calls* and prints the
recording window on screen — it never generates a number.

That is the point of doing setup first: you are not taking the results on trust,
you are re-deriving them on your own laptop.

## 1. Clone and create the environment

```bash
git clone https://github.com/catwang42/agent-migration-workbench.git
cd agent-migration-workbench

python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Python 3.11 is expected. Every schema in the repo — traces, dataset items,
metrics, gate results — is a `pydantic` model, so a mismatched Python will fail
loudly at import rather than quietly at runtime.

## 2. Configure the environment file

```bash
cp .env.example .env
```

For the offline path, the only value that matters is:

```bash
CLAUDE_PATH=replay
```

`PROJECT_ID` and `REGION` are only read when you ask for a live or hybrid run.
Leave them as they are for now.

## 3. Run the pipeline with zero credentials

```bash
python cli.py e2e --mode replay
```

This is the continuous-integration gate for the whole repository (a different sense of the
word from the confidence ranges the modules quote). It regenerates the dataset from its
seed, replays the recorded baseline, ablation, shadow and judge calls, evaluates
the gates in `config/gates.yaml`, and renders the Migration Readiness Scorecard.
A green exit means the evidence on this site reproduces on your machine.

Then run the metric tests:

```bash
pytest tests/ -q
```

1,052 tests collected; 1,049 pass and 3 skip on the offline path.

!!! note "What replay mode is, precisely"

    A `ReplayStore` keyed on `(subagent, model, input_sha)`, reading JSONL trace
    files one trace per line (`amw/traces/schema.py`). A miss raises
    `ReplayMissError` naming the key it wanted — it never substitutes a nearby
    trace and never synthesises a response. If replay is green, every input on
    the site was actually sent to a model and every output actually came back.

## 4. Optional — the live path

You only need this for the ADK delegation exercise and for re-running anything
against a live endpoint.

```bash
gcloud auth application-default login    # a human does this, interactively
python cli.py smoke --mode live -n 2     # two calls, both backends
```

Set `PROJECT_ID` and `REGION` in `.env` first. Two regional facts to know before
you read any latency number:

- The development-generation arms were recorded with Claude in region `global`
  and Gemini and the judge in `us-central1`, because the `us-central1` partner
  quota for Claude was exhausted. Every latency figure from those runs is a
  cross-region comparison, and renders as *"not comparable — region split
  disclosed"* rather than as a measurement.
- The deployment candidates were probed separately with **both arms pinned to
  `global`**. Those probes are the only input that opens the `latency_p95` gate,
  and the probe script refuses to record a result if the two arms did not in
  fact land in the same region.

Record-on-live is always on and has no off switch: every live call appends a
canonical trace to `artifacts/replay/`, which is how the replay corpus grew in
the first place.

## 5. Where things live

| Path | What is in it |
|---|---|
| `config/gates.yaml` | The six thresholds. The only place a threshold exists. |
| `config/models.yaml` | Every model ID, keyed by access path. No raw IDs in code. |
| `config/pricing.yaml` | Prices. The only place a price exists — see the note below. |
| `config/customers/` | Customer profiles: domain, seed, volume assumptions. |
| `amw/` | All the logic — adapters, datasets, traces, eval, tuning, shadow, economics, reporting. |
| `amw/eval/judge_prompts/` | The judge prompts, as versioned text files. Read them. |
| `artifacts/replay/` | The recorded trace corpus, 4,408 traces. |
| `artifacts/results/` | The numbers a customer was shown, per run. |
| `notebooks/` | Two notebooks. They import from `amw/` and display; no logic lives here. |

!!! warning "No dollar figure exists yet"

    Cost is behind two independent gates. **Prices** cleared on 2026-08-12, when a
    human walked `scripts/refresh_pricing.py` rate by rate against the Vertex AI
    pricing page and stamped `verified_on` + `verified_by`. **Volumes** are still
    open: the demo customer profile's call rates are illustrative
    (`volumes_confirmed: false`), and a real call rate multiplied by anything is
    the most dangerous number this repo could print. Until a customer confirms
    their volumes, every cost and savings cell on this site says *not measured* —
    not a zero, not an estimate, not a range.

## Models used in this workshop

| Part it plays | Model |
|---|---|
| Incumbent — what the migration is measured against | **Claude Sonnet 5** (`claude-sonnet-5`, Vertex AI partner models) |
| Development generation — where the tuning ladder and the prompt-optimizer work were done | **Gemini 2.5 Flash** (`gemini-2.5-flash`) |
| Deployment candidate, headline — what the scorecard recommends | **Gemini 3.6 Flash** (`gemini-3.6-flash`) |
| Deployment candidate, second column | **Gemini 3.5 Flash** (`gemini-3.5-flash`) |
| Gated judge — registered before any result was seen, never changed | **Gemini 2.5 Pro** (`gemini-2.5-pro`) |
| Cross-check judge — re-scores a sample of the gated judge's verdicts | **Claude Sonnet 5** (`claude-sonnet-5`) |
| Follow-on candidate — priced, documented, never run | **Gemini 3.1 Pro (preview)** (`gemini-3.1-pro-preview`) |

Prompts were tuned on the development generation and then validated, unchanged,
on the deployment generations. That portability is itself a finding, and it is
why both generations appear throughout this site.

Full detail — access path, region, and the window each model's calls were
recorded in — is on [Models in this study](models-in-this-study.md).

## Next

[Module 01 — Why subagents migrate first](modules/01-why-subagents-first.md)
