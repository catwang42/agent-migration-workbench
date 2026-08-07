# SPIKES.md — Day-0 platform go/no-go

Every platform risk in this build was front-loaded into a timeboxed Day-0 spike
(`act1_build_plan.md` §1). This file is the record: status, the evidence it was
decided on, and the P1 scope each verdict locks in or out.

**A verdict here is only as good as the evidence next to it.** Every number and
quoted string below came out of an executed call against a real service. Nothing
is illustrative. A RED spike ships via its pre-written fallback
(`act1_build_plan.md` §3), never via hope, and never by showing output the
service did not produce (ground rule 1).

| Spike | Subject | Status | Decided |
|---|---|---|---|
| S1 | Claude adapter live round trip | **GREEN** | 2026-08-07 |
| S2 | Vertex AI GenAI Evaluation Service | **GREEN** | 2026-08-07 |
| S3 | Vertex AI Prompt Optimizer (VAIPO) | **GREEN** | 2026-08-07 |

Run environment for all three: project `vital-octagon-19612`, Gemini and the two
managed services in `us-central1`, Claude in `global` (see S1 caveat). ADC from
the workbench VM's compute service account. `anthropic` SDK 0.120.2,
`google-cloud-aiplatform[evaluation]` 1.163.0.

---

## S1 — Claude adapter round trip → **GREEN**

**Criterion** (T04 card): one real call on the chosen path, trace recorded,
replayed back byte-identical.

**Path exercised:** `CLAUDE_PATH=vertex` (Vertex AI Model Garden), auth by GCP
ADC, no Anthropic API key. This is the documented demo default —
`act1_build_plan.md` §1 wants traffic staying inside GCP.

> **Standing decision (2026-08-07): the demo path is `CLAUDE_PATH=vertex` only.**
> The direct Anthropic path is implemented and unit-tested but has **never been
> exercised live**. It stays out of every demo flow unless it earns its own live
> round trip during **Tuesday's rehearsal**. It is never first-run on Wednesday
> — a path whose first real call happens in front of the customer is not a
> fallback, it is a second thing that can fail. Unit tests prove the translation
> logic, not that the account, key, and model access exist.

**Evidence** — `resolve("claude-sonnet", "live").complete(...)`, recorded to
`artifacts/replay/spike_s1.jsonl`:

```
adapter: <RecordingAdapter inner=<ClaudeVertexAdapter name=claude_vertex mode=live>>
region:  global | project: vital-octagon-19612
status: ok | error: None
model: claude-sonnet -> id: claude-sonnet-5
output: pong
usage: input_tokens=26 output_tokens=4 cached_tokens=0 | latency: ttft=912 total=946
BYTE-IDENTICAL: True
```

All four GREEN conditions hold: `status: ok`; non-zero input and output tokens;
`latency_ms.ttft` populated, which is what proves the streaming path actually
fired against the real API rather than a buffered response; and the recorded
trace re-serialising identically to the replayed one.

A second round trip (`artifacts/replay/spike_s1_encoding.jsonl`) confirmed the
Messages API accepts the two-consecutive-user-turn shape the adapters emit when
a request carries `context_chunks` — it does, returning `apple, banana` for the
echo probe. This was worth checking rather than assuming: the API historically
required strictly alternating roles, and had it still done so, every
retrieval-carrying subagent call would have 400'd on first contact with real
data instead of in a spike.

**Model IDs verified the same day.** All four IDs in `config/models.yaml` were
confirmed by executed round trip, not by reading a docs page, and
`ids_verified_on: 2026-08-07` is stamped on that basis
(`artifacts/replay/id_verify.jsonl`):

```
claude-opus     claude-opus-5        status=ok
claude-sonnet   claude-sonnet-5      status=ok
gemini-flash    gemini-2.5-flash     status=ok
gemini-pro      gemini-2.5-pro       status=ok
```

Current-generation Claude models take the **bare** first-party ID on Vertex.
There is no vendor prefix — `anthropic.` is Bedrock's convention and 404s here.

### Caveat, carried forward: Claude and Gemini run in different regions

Claude in `us-central1` on this project returns:

```
429 Quota exceeded for aiplatform.googleapis.com/online_prediction_input_tokens_per_minute_per_base_model
    with base model: anthropic-claude-sonnet-5
```

Model Garden quota is per-region *and* per-base-model, so this is not something
the code can route around. `global` works. Gemini and both managed services are
fine in `us-central1`.

Handled with a `CLAUDE_REGION` env var that the Vertex Claude adapter resolves
ahead of `REGION`, so moving Claude does not drag Gemini with it. **This is a
measurement caveat, not just a config detail:** the `latency_p95` gate in
`config/gates.yaml` compares candidate latency against the Claude baseline, and
a cross-region split means that comparison is not like-for-like. `cli.py`
warns before any live run, and the scorecard must disclose it rather than
present a same-region measurement. Quality and cost gates are unaffected —
tokens and rubric scores do not care where the endpoint is.

The clean fix is a quota increase for `anthropic-claude-sonnet-5` in
`us-central1`. **Worth requesting before the workshop**; until then the split
stands and gets disclosed.

### Second caveat, found 2026-08-07: partner-model structured outputs are blocked

Discovered while verifying the frozen schemas in `amw/agents/schemas.py` against
both providers. Claude on Vertex with `output_config.format` returns:

```
400 FAILED_PRECONDITION — Organization Policy constraint
constraints/vertexai.allowedPartnerModelFeatures violated for
`projects/440790012685` attempting to use a disallowed feature
structured_outputs for Partner model claude-sonnet-5
```

This is an **org policy**, not a quota — retries, backoff, and region changes do
not clear it, which makes it a different failure class from the 429 above.

Probed live in the same session: Gemini + `response_schema` works for all three
subagent schemas; Claude/Vertex **tool use** works and its tool calls validated
against the frozen schemas on the first attempt. So the Claude baseline emits
through a per-subagent tool (`emit_query_plan` / `emit_summary` /
`emit_features`), and Gemini's A0 rung uses the same tool so the Claude-vs-A0
delta is not partly a mechanism delta. Gemini's tuned rungs use
`response_schema`, since "strict schema" is already an explicit rung on the
ablation ladder.

**Nothing on the P0 demo path depends on the blocked feature.** The exception
request is with the repo owner; **cutoff is Monday 2026-08-10 EOD** — if it
lands later it is post-workshop only, because nothing new enters the build on
rehearsal or freeze day.

Full detail, verbatim identifiers, and the wording rules for any customer-facing
mention: `notes/org_policy_structured_outputs.md`. Short version of those rules
— scope it to *this org's policy configuration*, never generalise it into a
claim about partner models or about Claude on Vertex.

---

## S2 — Vertex AI GenAI Evaluation Service → **GREEN**

**Criterion** (T05 card): one rubric metric over 3 canned items, scores and
rationales parsed into plain dicts. Timebox 90 min — **used 11.6s of service
time**, well inside it.

**Probe:** `scripts/spike_s2_vertex_eval.py`. A `PointwiseMetric` named
`query_intent_preservation` with two criteria (intent preservation, filter
fidelity) and a binary rubric, run over three Query Rewriter items chosen so a
working metric *cannot* score them all alike: one faithful rewrite, one that
drops the date filter, one that changes the subject outright.

**Evidence:**

```
--- raw metrics_table columns ---
['prompt', 'response', 'query_intent_preservation/explanation',
 'query_intent_preservation/score']

--- summary_metrics ---
{"row_count": 3,
 "query_intent_preservation/mean": 0.3333333333333333,
 "query_intent_preservation/std": 0.5773502691896257}
```

Per-item scores were `1.0 / 0.0 / 0.0` — the discrimination the item design was
there to test — with rationales naming the right cause in each case (the
dropped `filing_date` constraint; the shift to an unrelated subject). Parsing
into plain dicts by locating the `/score` and `/explanation` columns works and
is stable enough to build on.

**P1 unlocked:** *Vertex GenAI Eval Service rubric metrics + loss clustering*
(`act1_build_plan.md` §2, P1 row 1). Not started — P1 work begins only after P0
is complete and with explicit approval.

Note for whoever picks it up: this proves the service *runs and parses*. It does
not prove rubric metrics agree with our local judge, which is a separate
question the P1 task has to answer with a real comparison, not an assumption.

---

## S3 — Vertex AI Prompt Optimizer (VAIPO) → **GREEN**

**Criterion** (T05 card): one optimization iteration on a toy instruction with a
5-item eval set, candidate prompt retrieved. Timebox 90 min — **used 124.1s**.

**Which VAIPO surface this proves, precisely.** There are two, and they are not
interchangeable:

* `client.prompts.optimize(...)` — the **synchronous** instruction optimizer.
  Prompt plus a few-shot examples dataframe in, suggested prompt out, in
  seconds. **This is what went GREEN**, and it is what the card describes.
* `optimize(method="VAPO", ...)` — the data-driven optimizer that runs as a
  long-lived Vertex CustomJob against a GCS bucket. **Not exercised.** If rung
  A4′ ever needs the full data-driven loop, that is a separate go/no-go and this
  GREEN does not cover it.

**Probe:** `scripts/spike_s3_vaipo.py`, on a deliberately underspecified
instruction (`"Rewrite the user's patent search question into a search query."`)
with 5 patents-domain examples. The `model_response` column was generated by
**real** `gemini-2.5-flash` calls at temperature 0, not hand-written: the
optimizer works from the gap between current and target output, so invented
"current" outputs would have produced a candidate prompt optimizing against a
failure that never happened — a fabricated result wearing a real result's
clothes (ground rule 1).

**Evidence:** a candidate prompt came back with
`optimization_type: "target_response_optimization_with_target_response"`, plus
three structured `applicable_guidelines` entries diagnosing the toy
instruction's actual weaknesses:

1. Incorrect Response Format and Verbosity
2. Target Discrepancy: Missing Structured Query Definition
3. Missing Logic for Date and Entity Translation

**Honest caveat, and the useful part of the result.** The returned candidate
asserts that "after 2019" should become `filing_date >= 2020-01-01`. My target
said `>= 2019-01-01`. The optimizer over-generalized from the one example in the
set that used a `before` boundary ("before 2020" → `< 2020-01-01`) and applied
its exclusive-boundary logic to an inclusive case. Five examples, one of them
ambiguous, and the optimizer confidently encoded the wrong rule.

That is not a strike against the mechanism — it is evidence the mechanism works
*as specified* and that **the target set is the thing that needs care**. It also
argues directly against ever putting a VAIPO candidate on the demo path without
measuring it on held-out items first. A rung that looks tuned and is subtly
wrong is worse than no rung.

**P1 unlocked:** *VAIPO as ablation rung A4′* (`act1_build_plan.md` §2, P1 row
2). Not started, same conditions as S2.

---

## What this locks

**P0 is unaffected either way** and remains the whole commitment: A0–A4 with
hand-tuned rungs driven by real failure clusters, local judge, shadow
comparison, gates, scorecard. No spike verdict changes that, by design — that is
what "nothing unspiked appears in the demo path" buys.

Three GREEN means no fallback from `act1_build_plan.md` §3 is triggered and **no
P1 item is locked off by spike failure**. The P1 list stands as written:

| P1 item | Gate | Status |
|---|---|---|
| Vertex Eval Service rubric metrics + loss clustering | S2 | unlocked by S2 GREEN |
| VAIPO as ablation rung A4′ | S3 | unlocked by S3 GREEN, with the target-set caveat above |
| Dual-judge cross-check | none (cheap once `judge.py` exists) | available |
| Answer Drafter subagent | none | available |
| Live context-caching call | none | available |
| HTML scorecard; root-orchestrator runnable stub | none | available |

**Unlocked is not scheduled.** P1 work starts only after P0 is complete in
TASKS order, and only with explicit approval (CLAUDE.md ground rule 6). The
priority order if time allows is unchanged: Vertex metrics → dual-judge →
Answer Drafter → VAIPO rung → caching live demo → HTML scorecard.

### Open items carried out of Day 0

* **Claude quota in `us-central1`** — request an increase for
  `anthropic-claude-sonnet-5`. Until then the run is cross-region and the
  latency gate is disclosed as such.
* **`google-cloud-aiplatform[evaluation]`** is installed here and used by the
  S2 probe, but is deliberately **not** in `requirements.txt`. Adding it now
  would make every P0 install carry a package the demo path does not use.

  > **Standing decision (2026-08-07):** if the S2 P1 item is greenlit, adding
  > this dependency to `requirements.txt` is **part of that task's definition of
  > done**, not a follow-up. The task is not done until a clean-environment
  > `pip install -r requirements.txt` followed by `pytest tests/` and
  > `python cli.py e2e --mode replay` passes. A feature that works only on the
  > machine it was written on is the same failure as a feature that works only
  > live (ground rule 4).
* **`config/pricing.yaml` is still 13 × `VERIFY`.** No spike touched it and
  nothing may print a price or a savings % until
  `scripts/refresh_pricing.py` has been run (ground rule 3).
* **Partner-model structured outputs exception** — filed by the repo owner
  against `constraints/vertexai.allowedPartnerModelFeatures`. **Cutoff Monday
  2026-08-10 EOD.** Landing before the cutoff makes it an optional bonus arm
  (added only if the full suite and `e2e --mode replay` still pass); landing
  after makes it post-workshop. Either way the P0 path is unaffected — see
  `notes/org_policy_structured_outputs.md`.
