# Exercises

Three exercises. The first two need **no credentials** — they run entirely against
the recorded trace corpus. The third needs Google Cloud ADC.

Finish [Setup](setup.md) first.

---

## Exercise 1 — Replay the whole pipeline

**Goal:** convince yourself that every figure on this site reproduces on your
machine, from recordings, offline.

```bash
source .venv/bin/activate
python cli.py e2e --mode replay
```

This regenerates the dataset from seed `20260812`, replays the recorded baseline,
ablation, shadow and judge calls, evaluates the gates, and renders the scorecard.

**What to look for while it runs**

1. The mode banner. Replay mode announces itself and prints the recording window
   — it never presents a recorded call as a fresh one.
2. The dataset counts: 70 items per subagent, 28 in the core split, every item
   `provenance: synthetic`.
3. The gate evaluation lines. Each gate prints the bound it tested (a 95%
   confidence-range bound,
   not a point estimate) and the result.
4. The unmeasured gates. `cost_savings_pct` and `latency_p95` print a reason, not
   a value.

**Then break it on purpose.** Move one file out of `artifacts/replay/` and re-run:

```bash
mv artifacts/replay/query_rewriter.jsonl /tmp/
python cli.py e2e --mode replay          # expect a failure
mv /tmp/query_rewriter.jsonl artifacts/replay/
```

You should get a `ReplayMissError` naming the exact `(subagent, model, input_sha)`
key it wanted. That is the property worth internalising: a missing recording is a
loud failure, never a quietly substituted nearby trace and never a synthesised
response.

**Also run the metric tests:**

```bash
pytest tests/ -q
```

1,052 collected, 1,049 pass, 3 skip.

---

## Exercise 2 — Re-render the scorecard locally

**Goal:** see that the report is a pure function of the recorded results plus
`config/gates.yaml`, and watch a threshold change propagate.

```bash
# The development-generation card — Claude Sonnet 5 against Gemini 2.5 Flash.
python cli.py scorecard \
  --results artifacts/results/phase2_n70_widened.json \
  --shadow  artifacts/results/shadow_widened.json \
  --crosscheck artifacts/results/crosscheck.json \
  --out /tmp/scorecard_mine.md
diff /tmp/scorecard_mine.md artifacts/results/scorecard_widened.md
```

The frozen artifact was rendered on 2026-08-11, so that `diff` is **not** empty,
and reading it is the exercise. Three kinds of change appear, and telling them
apart is the skill:

- **Presentation.** The renderer now prints the model name beside every arm
  (`Judge score — Claude` became `Judge score — Claude Sonnet 5 (incumbent)`),
  and writes *confidence range* where it used to write *CI*.
- **New measurements since the freeze.** Prices cleared on 2026-08-12, so the
  pricing precondition no longer blocks the cost cells. Feature Extractor's
  `groundedness_delta_pp` now resolves against a supported-claim-rate instrument
  that did not exist on 11 Aug, taking it from 3 of 6 gates to 4 of 6. The
  ablation ladders have gained the deployment-generation rungs measured on
  12 Aug.
- **Changed findings.** There are none. Every gate figure and every confidence
  range on a row present in both renders is identical, digit for digit.

That last line is the claim worth not taking on trust — check it.

The published card is the 11 Aug render, left frozen on purpose: re-rendering it
would silently move a verdict row that a customer may already have screenshotted.
New measurements go in new files.

**Then experiment — on a copy of the config, never on the committed one.**

The interesting experiment is to see how little discretion the verdict logic has.
Try each of these and re-render:

| Change | What you should see |
|---|---|
| Raise `quality_delta_pp.min` from `-2.0` to `-6.0` | Chunk Summarizer and Query Rewriter stop failing that gate; the gates version **hash in the footer changes**, which is the whole point of printing it |
| Lower `shadow_agreement.min` below 0.443 | Query Rewriter's HOLD disappears — and you have just demonstrated why the hash is in the footer |
| Confirm volumes with `--volume query_rewriter:250000` | Run-rate cells unlock on this card only because prices cleared on 2026-08-12; drop a `VERIFY` back into `config/pricing.yaml` and they return to em dashes even with volumes confirmed |

That last row is the one worth sitting with. Two independent human-cleared gates
protect every cost cell. Pricing cleared on 2026-08-12; volumes have not, which
is why the shipping cards carry measured corpus costs and still print an em dash
in every monthly and annual run-rate cell.

**Restore the real config before moving on:**

```bash
git checkout config/gates.yaml
```

Compare the footer hash against `92f9d018432f` to confirm you are back.

---

## Exercise 3 — Run the ADK app and read the delegation trace

**Goal:** see the same prompt packs the bench measured, running inside a real
multi-agent app — and see why the orchestrator around them gets no verdict.

Needs ADC (`gcloud auth application-default login`) and `PROJECT_ID` / `REGION`
in `.env`.

```bash
python cli.py adk-demo --mode live
```

The reference app is a root orchestrator delegating to the three leaf subagents.
Each leaf loads its own shipping prompt arm (`adk_app.SHIPPING_VARIANTS`) — the
same files, byte for byte, that the bench harness measured.

**Read the delegation trace and answer these:**

1. **Which leaf agents did the root call, in what order, and how many times?**
   That sequence is a *decision the root made*. Nothing in this workshop measured
   it.
2. **Where would a retry or a loop show up?** Now ask what metric on the
   scorecard would have caught a root that loops twice as often on hard queries.
   (There isn't one. That is the answer.)
3. **Compare a leaf's output here to its recorded bench output.** Same prompt,
   same model, live rather than replayed.

**Pin one prompt pack across all three leaves** to see the ablation rungs move
inside a running app:

```bash
python cli.py adk-demo --mode live --variant gemini_naive
python cli.py adk-demo --mode live --variant gemini_tuned_v1
```

**The point of the exercise:** the leaves are Level 1 and are measured in full.
The root is Level 3 — when to loop, when to delegate, when to stop — and a
single-call bench harness cannot see any of that. Migrating the root on the
strength of this scorecard would mean extrapolating a Level 1 measurement onto
Level 3 behaviour. The app exists so that the gap is something you can watch
rather than something you have to take on trust.

!!! note "This runs in replay too"

    `python cli.py adk-demo --mode replay` is the zero-credential path and is the
    default. You get the delegation structure and the leaf outputs from
    recordings; what you do not get is a fresh trajectory, which is exactly the
    thing this exercise is about.

---

## Where to go next

- [The scorecard](results/scorecard.md) — the full report, every caveat welded in.
- [The selection table](results/selection_table.md) — every rung at the n it ran on.
- [The cross-check](results/crosscheck.md) — the second judge's disagreements.
