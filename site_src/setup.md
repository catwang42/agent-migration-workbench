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

This is the continuous-integration gate for the whole repository (a different CI
from the confidence ranges the modules quote). It regenerates the dataset from its
seed, replays the recorded baseline, ablation, shadow and judge calls, evaluates
the gates in `config/gates.yaml`, and renders the Migration Readiness Scorecard.
A green exit means the evidence on this site reproduces on your machine.

Then run the metric tests:

```bash
pytest tests/ -q
```

1,012 tests collected; 1,010 pass and 2 skip on the offline path.

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

- Claude runs in region `global`; Gemini and the judge run in `us-central1`.
- That makes `latency_p95` a cross-region comparison, and it renders throughout
  as *"not comparable — region split disclosed"* rather than as a measurement.

Record-on-live is always on and has no off switch: every live call appends a
canonical trace to `artifacts/replay/`, which is how the replay corpus grew in
the first place.

## 5. Where things live

| Path | What is in it |
|---|---|
| `config/gates.yaml` | The six thresholds. The only place a threshold exists. |
| `config/models.yaml` | Every model ID, keyed by access path. No raw IDs in code. |
| `config/pricing.yaml` | Prices. Currently `VERIFY` placeholders — see the note below. |
| `config/customers/` | Customer profiles: domain, seed, volume assumptions. |
| `amw/` | All the logic — adapters, datasets, traces, eval, tuning, shadow, economics, reporting. |
| `amw/eval/judge_prompts/` | The judge prompts, as versioned text files. Read them. |
| `artifacts/replay/` | The recorded trace corpus, 4,408 traces. |
| `artifacts/results/` | The numbers a customer was shown, per run. |
| `notebooks/` | Two notebooks. They import from `amw/` and display; no logic lives here. |

!!! warning "No dollar figure exists yet"

    `config/pricing.yaml` still holds `VERIFY` placeholders and the demo customer
    profile's volumes are illustrative (`volumes_confirmed: false`). Both gates
    have to be cleared by a human before any cost cell renders. Until then every
    cost and savings cell on this site is an em dash — not a zero, not an
    estimate, not a range.

## Next

[Module 01 — Why subagents migrate first](modules/01-why-subagents-first.md)
