# Objection Handling

**Version:** 2026-08-11 · Numbers from `artifacts/results/phase2_n70.json` (630 calls,
executed 2026-08-09T16:07Z → 2026-08-10T02:45Z, `demo_patents`, synthetic, seed `20260812`).

How to use this: each objection has a **short answer** (say this), the **evidence** (point at
this), and **what not to say**. If an objection isn't here, the correct answer is "that isn't
measured — here's the instrument that would measure it," never an estimate.

---

## The classics

### 1. "You're a Google shop selling a Google model. Why would I trust this comparison?"

**Short answer:** Because the design makes it expensive for me to cheat, and you can check
every mechanism before you look at a single number.

**Evidence:**
- Gates were signed off in segment one, *before* any result appeared. The scorecard footer
  prints a hash of `config/gates.yaml`; if a threshold moved after the run, you can tell.
- Gates test **CI lower bounds**, not point estimates — a harder bar than the point estimate
  Gemini would look better against.
- Claude gets the tuning ladder's benefit of the doubt: the baseline prompt is the
  production Claude prompt, unmodified.
- The run surfaces defects on **both** sides. Claude's QR schema validity came in at 0.814
  against a 0.99 gate. My own tuning made Feature Extractor **worse**. Neither is in here
  because it flatters anybody.
- The whole pipeline is in the repo. Re-run it against your prompts.

**Don't say:** "We're neutral." Nobody believes a vendor claiming neutrality. Claim
*auditability* instead.

### 2. "The judge is a Gemini model grading Gemini's own output."

**Short answer:** Correct, and that's the single biggest methodological weakness on this
scorecard. Here's what bounds it.

**Evidence:**
- The judge is rubric-anchored per item, not a free-form "which is better" — the rubric is
  written from the gold reference before either model runs. Judge prompts are versioned
  text files in `amw/eval/judge_prompts/`; read them.
- The judge is blind to which arm produced the text.
- k=2 repeats per item, bootstrap 95% CIs — self-preference that isn't stable across
  repeats shows up as a wide interval.
- Empirically it doesn't behave like a homer: on Feature Extractor the Gemini judge scored
  **Claude higher** (0.900 [0.868, 0.929] vs Gemini 0.821 [0.787, 0.854]), non-overlapping.
  A biased judge doesn't hand the contested row to the competitor.
- The judge is also the *noisiest* instrument here, which is why no verdict rests on the
  judge alone — deterministic gates are the blocking ones.
- **A Claude judge re-scored the same recorded outputs against the same rubrics** (run
  2026-08-11; `cli.py crosscheck`, report in `artifacts/results/crosscheck.md`). Criterion
  agreement: query_rewriter **98.8%**, chunk_summarizer **99.0%**, feature_extractor
  **92.9%** on all three arms of the full 70. The two judges are never averaged — the Gemini
  judge stays the gated instrument, registered before results.
- The cross-check reproduces the finding that matters: on Feature Extractor the Claude judge
  *also* ranks claude_baseline > gemini_naive > gemini_tuned_v1, so "our tuning made FE
  worse" is not an artefact of one judge.

**What's honest to concede:** the Claude judge is systematically *stricter* on Feature
Extractor — about 3–4 points lower on every arm (e.g. claude_baseline 0.857 vs the gated
0.897). The ranking survives, the absolute level does not. Quote FE judge scores as a
gated-instrument number with the cross-check beside it, never as "the" judged score. And
Cohen's kappa reads lower than the raw agreement because pass rates are high — chance
agreement on the QR/CS samples is already ~0.81 — so kappa is prevalence-deflated here;
read the two together.

**Don't say:** "The judge is unbiased." Don't say "two judges agreed, so the number is
right" either — they agreed on 92.9% of FE criteria and still differ by 4 points on the arm
level.

### 3. "This is synthetic data. My traffic looks nothing like it."

**Short answer:** Agreed — which is why the synthetic corpus is a *methodology* deliverable,
not an answer about your traffic.

**Evidence:**
- Every item carries `provenance: synthetic` and generator seed `20260812`. It's on the
  artifacts, not just in the talk track.
- What synthetic data legitimately establishes: the harness runs, the metrics discriminate,
  the gates fire, and the *shape* of the failure modes (prompt-format sensitivity on QR, the
  novelty-field regression on FE) is real behavior of real models on real calls.
- What it cannot establish: your pass rates. Those need your traces.
- The corpus is deliberately adversarial in the places that matter — the patent domain
  gives long documents, dense numeric limits, and citation-heavy summaries.

**The move:** don't defend the synthetic data. Use the objection to close — hand over
`data_request_onepager.md` and convert it into the Act 2 trace ingest.

### 4. "Are those prices current?"

**Short answer:** Right now, no cell shows a price at all — every cost cell renders an
em-dash, because pricing hasn't been verified for this run.

**Evidence:**
- One file, `config/pricing.yaml`, is the only place a price may exist. Nothing else in the
  codebase may hardcode a rate or a savings percentage.
- It ships with `VERIFY` placeholders. A human runs `scripts/refresh_pricing.py`, which sets
  the values plus `verified_on` and the source URLs.
- The footer prints `verified_on`. If it's stale, the reader knows without asking.
- Cost is double-gated: prices must be verified **and** volumes must be customer-confirmed
  before a dollar figure renders. Today volumes are illustrative, so even with verified
  prices the footer would say `volumes: illustrative`.

**Don't say:** a savings percentage from memory. Ever. Not even "roughly."

**And the follow-up you will get — "is Claude cheaper direct from Anthropic?"** The cost cells
price the path this platform actually runs: partner models on Vertex. A customer running the
direct Anthropic API gets re-priced on their real path in the follow-on — the pricing source,
like the baseline mechanism, is a per-customer config field, not a universal claim.

### 5. "These numbers look too good. Real migrations aren't this clean."

**Short answer:** They aren't clean. Three rows on this scorecard are failures, and two of
them are mine.

**Evidence:**
- The naive prompt swap **fails**: QR `exact_match_intent` falls to 0.571 [0.457, 0.686].
  "Just point it at Gemini" is measurably wrong.
- Claude's QR schema validity is **0.814** against a 0.99 blocking gate.
- My tuning made FE **worse** (0.795 < 0.821 naive).
- Where things *do* look good — FE deterministic accuracy at 1.000, CS fabricated citations
  at 0.000 — say so plainly: those instruments are **saturated** on this corpus. A metric
  pinned at its ceiling has stopped discriminating; it isn't evidence of excellence, it's
  evidence the test is too easy. That is itself a finding, and it's an argument for your
  traces.

**Don't say:** "parity" where the CIs don't overlap, or "better" where they do.

---

## The five from this build

### 6. "You measured Claude with tool-use JSON and Gemini with a native enforced schema. That's rigged."

**Short answer:** It's a real asymmetry, it's disclosed in the same cell as every affected
number, and the ladder is built so you can subtract it.

**Evidence:**
- The cause: under this demo organization's Vertex AI policy configuration
  (`constraints/vertexai.allowedPartnerModelFeatures`), partner-model structured outputs were
  unavailable, so the Claude baseline was measured using tool-call structured emission. It's
  a policy setting on **this** project, not a model capability statement.
- Every Claude schema-validity cell (QR 0.814, CS 0.971, FE 0.957) renders with that caveat
  welded into the cell — you can't read the number without reading the mechanism.
- **Fairness by construction:** Gemini's A0 (naive) rung runs under the *same tool-call
  mechanism*. So Claude-vs-A0 isolates prompt format, and A0→A1 isolates what the enforced
  schema is worth. The confound is measured, not assumed away.
- Consequence for the verdict: I do **not** claim Claude's 0.814 is Claude's ceiling. In your
  org, with structured outputs permitted, that number would likely be materially higher — and
  the honest scorecard treatment is that this gate result is about the environment.

**Don't say:** "Claude doesn't support structured outputs on Vertex." That's false and it
will be the only thing they remember.

### 7. "Your tuning made Feature Extractor worse. Why should I trust the rest of the tuning?"

**Short answer:** Because that's the system working. A tuning process that never regresses
isn't being measured.

**Evidence:**
- The numbers: FE judge, naive 0.821 [0.787, 0.854] → tuned_v1 0.795 [0.760, 0.828]. Judged
  on the **full 70**, not the 28-item core, precisely because this row is contested.
- The regression is localised — it lives in the `novelty_statement` field, where the tuned
  prompt's compression instruction traded away substance the judge rewards.
- The instruments **disagree**, and that's the interesting part: on deterministic metrics
  Gemini FE is saturated (extraction accuracy 1.000, schema validity 1.000, omission 0.000,
  hallucination 0.000) and looks like a clean MIGRATE. Only the judge sees the loss. Had we
  run the cheap instrument alone, we'd have shipped a confident wrong recommendation. That
  arithmetic is written out in `notes/counterfactual_scorecard.md`.
- What happens next is the ladder doing its job: a **novelty rung branched from the naive
  arm** (not from tuned_v1 — you don't build on the regression), with the instruction that
  `novelty_statement` is never null when the source states an advance, numeric limits are
  preserved, and one worked example is given. Run under both output modes to separate the
  prompt change from the output-mode change, since A1–A3 moved both at once.
- Until that's measured, the hypothesis is labelled a hypothesis and FE does not get a clean
  MIGRATE.

**The framing to land:** "The value of this harness isn't that it makes Gemini look good.
It's that it caught my own regression before it reached your production."

### 8. "Gemini's latency looks better/worse — which is it?"

**Short answer:** Neither. That cell isn't a measurement and it renders as
*"not comparable — region split disclosed."*

**Evidence:**
- Claude ran in region `global` because the `us-central1` partner-model quota was exhausted.
  Gemini and the judge ran in `us-central1`. Different regions, different network paths.
- The observed p95s exist in the artifact (Claude Sonnet 7,105 ms; Gemini Flash 8,843 ms;
  judge Gemini Pro 19,056 ms) but they are cross-region, unloaded, and single-run. Quoting
  them as a comparison would be exactly the kind of number this workbench exists to refuse.
- The `latency_p95` gate uses a sentinel (`claude_baseline_p95`) that resolves against a
  measured same-region baseline. Until a same-region probe runs, it doesn't resolve, and the
  cell says so instead of guessing.
- Fix is cheap and scheduled: a same-region probe once partner quota is available in
  `us-central1`. Quality and cost gates are unaffected by the region split.

**Don't say:** "roughly comparable," "in the same ballpark," or any softened version. The
cell says not comparable; so do you.

### 9. "Isn't this just prompt tuning with extra steps?"

**Short answer:** Prompt tuning is one segment of it. The deliverable is a *classification
of your agent estate* plus the matching instrument for each class — and, crucially, a refusal
to issue verdicts where the instrument doesn't reach.

**Evidence — walk the taxonomy table:**

| Subagent pattern | Level | Instrument | Verdict today |
|---|---|---|---|
| Prompt-based | 1 — transform | Bench harness (gold refs, deterministic metrics, rubric judge, bootstrap CIs) | **Yes — measured in full** |
| Tool-calling | 2 — tool decider | Tool-call quality scoring (selection, arguments, sequence) | Partial — formatting measured now, selection follow-on |
| Retrieval-augmented | 2/3 | Retrieval + groundedness trajectory evaluation | Follow-on |
| Orchestration | 3 — looping | Trajectory evaluation in the runtime | Follow-on — **HOLD, no verdict** |

- "If this were just prompt tuning, I'd have given you four verdicts. I'm giving you one
  class of verdict and three named instruments, because the bench harness that answers row 1
  cannot answer row 4. Using it anyway wouldn't give you a weak answer — it'd give you a
  confident wrong one."
- The follow-on scope is the rows without verdicts. That's a real engagement, not a
  consolation prize.

### 10. "You also ran Google's own managed evaluation service. Which set of numbers is real?"

**Short answer:** Both are real; only one is gated. The internal harness is the instrument
the gates are checked against, registered before results. The Vertex Gen AI Evaluation
Service is a second opinion reported *beside* it.

**Evidence:**
- The managed service re-scored the **same recorded outputs** — generations are replayed,
  only the scoring is live — so the two instruments are talking about the same answers, not
  two different runs.
- It was pointed at *our* rubrics (uploaded as rubric group `amw_dataset_rubric` from
  `amw.eval.runner.rubric_of`), so its autorater scored the same criteria ours did.
- The two are never averaged and never substituted. That's enforced in the type system, not
  by convention: managed and internal figures are different frozen classes with a `Literal`
  source, and the record that holds them owns no numeric field — there is nowhere in the
  schema for a blended number to live. The side-by-side table is long-format, one row per
  instrument, so no two columns invite subtraction.
- `cli.py vertex-eval --mode replay` re-renders the committed artifact with no credentials.

**What's honest to concede — say both of these first:**
- **The managed autorater's model is chosen by the service.** It is the one model in this
  pipeline whose ID is *not* pinned in `config/models.yaml`. Everything else in the
  workbench resolves its model from that file; this doesn't, and can't.
- **Its loss clustering returned no clusters, on every arm.** The call completes without
  error and returns nothing — reproduced with 26 failing rubric verdicts across 28 cases and
  under three different taxonomy settings. The table says `no_clusters_returned` with the
  failing-verdict count beside it. That is a statement about the tool, not a clean bill of
  health, and the loss clusters a customer actually gets are the internal harness's.

**Don't say:** "Google's own service confirms our numbers." It scored the same outputs with
a different instrument and mostly agreed. That is worth something and it is not confirmation.

---

## Two you should raise before they do

**The gated metrics are full-70; the ablation ladder is core-28.** All three subagents'
gated judge scores now come from the full 70 — QR and CS were widened on 2026-08-11 as
registered sizing deviation #2, decided before the widened results were seen. The ablation
ladder is a separate, smaller instrument: every rung, old and new, is judged on the 28-item
core split, which is what makes the rungs comparable *to each other*. So the FE ladder's
incumbent reads 0.903 (core-28) while the scorecard's FE baseline reads 0.900 (full 70).
Those are two measurements that happen to agree to 0.3 pp, not one number. Every judge score
on screen carries its n and split. Quote a ladder number against a ladder number and a gated
number against a gated number — never one against the other.

**A1–A3 was one bundled rung; it no longer is.** The prompt change and the output-mode change
originally moved together, so the ladder could not attribute the A0→A1 delta between them.
The mode diagnostic has now run: A0 (tool) 0.837 [0.791, 0.882] against A0-schema
(`response_schema`, same prompt bytes) 0.826 [0.773, 0.875] — −1.1 pp with CIs almost fully
overlapping. **An enforced `response_schema`, isolated from any prompt change, does not move
the FE judge score.** So the drop to A1–A3's 0.807 is the prompt restructure and the
recalibrated few-shots, not the output mode. Say the direction honestly: the mode is not
exonerated by a null result, it is simply not the explanation for this delta.
