# Environment finding: partner-model structured outputs are blocked in this org

**Discovered:** 2026-08-07, during the Fan-out 2 seam check on `amw/agents/schemas.py`.
**Scope of this note:** one GCP organization's policy configuration. It is **not**
a statement about Claude, about Vertex AI generally, or about partner models
generally. Wording rules for reuse are at the bottom.

## What happened

A live `messages.create` against `claude-sonnet-5` on Vertex Model Garden, with
`output_config.format = {"type": "json_schema", ...}`, returned HTTP 400:

```
Organization Policy constraint constraints/vertexai.allowedPartnerModelFeatures
violated for `projects/440790012685` attempting to use a disallowed feature
structured_outputs for Partner model claude-sonnet-5. Please contact your
organization administrator to fix this violation by adding
`publishers/anthropic/models/claude-sonnet-5:structured_outputs` to the allowed
values.
status: FAILED_PRECONDITION
```

Identifiers, verbatim, for anyone filing or auditing this:

| Field | Value |
| --- | --- |
| Constraint | `constraints/vertexai.allowedPartnerModelFeatures` |
| Project | `projects/440790012685` (`vital-octagon-19612`) |
| Disallowed feature | `structured_outputs` |
| Model | `claude-sonnet-5` |
| Value that would permit it | `publishers/anthropic/models/claude-sonnet-5:structured_outputs` |
| HTTP status | 400, `FAILED_PRECONDITION` |
| Region | `global` (`CLAUDE_REGION`) |
| Docs | https://docs.cloud.google.com/vertex-ai/generative-ai/docs/control-model-access |

This is an **organization policy**, not a quota. Retries, backoff, and region
changes do not clear it — it is a different failure class from the
`us-central1` 429 recorded in `SPIKES.md` (see [claude-runs-in-global-region]).

## What still works — probed live, same session, same credentials

| Path | Result |
| --- | --- |
| Gemini + `response_schema` | **works** — all three subagent schemas accepted, output validates |
| Claude/Vertex + **tool use** | **works** — `query_rewriter` and `chunk_summarizer` tool calls validated against the frozen schemas on the first attempt |
| Claude/Vertex + structured outputs | **blocked** — the 400 above |

The `feature_extractor` probe returned a well-formed tool call that wrapped the
object in an extra `features` key, so it failed `extra="forbid"`. That is a
prompt-quality artifact of a one-line throwaway system prompt, not a schema or
policy problem — it is precisely what the T07 prompt packs and the T08
schema-validity metric exist to measure.

## Consequence for the build

The Claude baseline arm emits through a per-subagent tool
(`emit_query_plan` / `emit_summary` / `emit_features`) whose `parameters` are the
frozen schema from `amw/agents/schemas.py`.

Gemini's **A0 (naive) rung uses the same tool**, so the Claude-vs-A0 delta is a
prompt-format difference and not a mechanism difference. Gemini's tuned rungs
(A1+) move to `response_schema`, because "strict schema" is already an explicit
intervention on the ablation ladder rather than a hidden advantage. This is the
same reasoning that produced `tests/test_adapter_parity.py` in Fan-out 1: if the
two arms are encoded differently, part of the measured gap is the encoding, and
nothing downstream can separate that back out.

Two things follow, and both should survive into the workshop unchanged:

1. **Nothing on the P0 demo path depends on partner-model structured outputs.**
   The blocked feature is used by no P0 code path. If the policy never changes,
   the demo is unaffected.
2. **The fairness question has an evidenced answer.** If a customer asks "did
   you hand Gemini an enforced schema that Claude never got?", the A0/A1 rungs
   are the answer: A0 runs Gemini under the same tool-call mechanism as Claude,
   and the A0→A1 delta shows exactly what the strict schema is worth.

## Exception request

The org policy exception is being filed by the repo owner, not by this build.

**Cutoff: Monday 2026-08-10, end of day.** If the exception lands after Monday
EOD it is **post-workshop only**. Tuesday is rehearsal and Wednesday is the
workshop; nothing new enters the build on freeze day. A structured-outputs arm
that first executes during rehearsal week is a second thing that can fail, not a
fallback — the same reasoning that keeps `CLAUDE_PATH=anthropic` out of the demo.

If it does land before the cutoff, it is a *bonus* comparison arm, added only if
`pytest tests/` and `python cli.py e2e --mode replay` still pass afterwards.

## Wording rules if this reaches the scorecard or any customer-facing text

Scope it to the environment, every time:

> Under this demo organization's Vertex AI policy configuration
> (`constraints/vertexai.allowedPartnerModelFeatures`), partner-model structured
> outputs were unavailable, so the Claude baseline was measured using tool-call
> structured emission.

Do **not** write "Claude does not support structured outputs on Vertex", or
anything that generalises one org's policy configuration into a product
limitation. The finding is real and worth reporting — it is a genuine
portability consideration for anyone running partner models under org policy
constraints — but it is an *environment* finding, and overstating it would be
the reporting equivalent of a fabricated result.
