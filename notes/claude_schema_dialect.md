# The Claude baseline was being handed a Gemini-dialect schema

**Found 2026-08-09, on the first phase-2 run that produced comparative
numbers. Fixed in `3ebe6cb` before any figure left this repo.**

## Symptom

The first `phase2 --mode live -n 10` put the Feature Extractor Claude baseline
below both Gemini arms on every extraction metric:

| metric | claude_baseline | gemini_naive | gemini_tuned_v1 |
|---|---|---|---|
| `json_schema_validity` | **0.600** | 1.000 | 1.000 |
| `extraction_accuracy` | **0.850** | 1.000 | 1.000 |
| `hallucination_rate` | **0.958** (n=4) | 0.000 | 0.000 |
| `answered_precision` | **0.833** | 1.000 | 1.000 |

Read at face value that is a migration slam dunk: the incumbent fails schema
validation 40% of the time and fabricates almost everywhere it is measurable.
It was not true.

## Cause

`amw/agents/schemas.py::json_schema` emits the **OpenAPI 3.0** dialect. It has
to: pydantic spells `str | None` as `anyOf: [{"type": "string"}, {"type":
"null"}]`, and Gemini's `response_schema` rejects that union outright, so the
branch is collapsed to `{"type": "string", "nullable": true}`.

That schema was then passed verbatim into Claude's tool `input_schema`, which
is plain **JSON Schema**. `nullable` is not a JSON Schema keyword. Unknown
keywords are ignored rather than rejected, so no error surfaced anywhere — the
field simply read as a non-nullable `string` or `integer`.

The Feature Extractor's whole design rests on nullability: seven of its eight
fields are nullable, and `null` means "the source does not state this".
Abstaining correctly is the behaviour `hallucination_rate` and
`answered_precision` exist to measure. Claude was told, in effect, that
abstention was not representable — and did the reasonable thing with the only
type it was offered:

```json
{"independent_claim_count": "null", "assignee": "null", "title": "null"}
```

The string `"null"`. Four of ten items. Each one then failed pydantic
validation (`int_parsing`, for the integer field), scored 0 on
`json_schema_validity`, and — because a string is an asserted value where the
gold is `null` — was filed as a **fabrication**.

So the model correctly identified the field as absent, said so in the only way
the schema allowed, and was scored as having invented data.

## Why it went unnoticed

Three separate guards should have caught this and each had a reason not to:

1. **`json_schema_validity` did catch it** — that is what 0.600 was telling us.
   It reads as a model-quality number, so it is easy to accept as a finding
   rather than investigate as a bug. The tell was the *combination*: a frontier
   model failing schema validity 40% of the time while a smaller, cheaper model
   scored 1.000.
2. **`tests/test_prompts.py` asserted the untranslated schema reached Claude.**
   The test was written from the same false premise as the code and pinned the
   bug in place. It now asserts the translation.
3. **The replay key cannot see it.** `input_sha` hashes `tools_offered`, which
   is a list of tool **names**, not their schemas. Two calls offering the same
   tool name under materially different schemas collide onto one key.

## Fix

`amw/adapters/claude_anthropic._to_json_schema` rewrites `nullable: true` into
JSON Schema's `{"type": ["string", "null"]}` on the way out, for both
`input_schema` and `output_config.format.schema`. It is mechanical and
lossless, so both arms are still asked for the same thing — the property the
whole baseline depends on. Gemini's path is untouched.

Re-measured on the e2e fixture immediately after: `json_schema_validity`
1.000, `extraction_accuracy` 1.000, `hallucination_rate` 0.000,
`answered_precision` 1.000.

Query Rewriter has 2 nullable fields and was also affected (it happened not to
need abstention on these 10 items). Chunk Summarizer has none.

## Recordings

Claude traces for `query_rewriter` and `feature_extractor` recorded under the
old dialect were **purged** from `artifacts/replay/` and re-recorded. Because
`input_sha` hashes tool names only, those recordings kept a valid-looking
replay key: left in place they would have been served silently by every future
`--mode replay|hybrid` run, and the fix would have had no visible effect
offline. `chunk_summarizer` recordings are unaffected and were kept.

## Open item for the owner

`input_sha` not covering tool schemas is the hole that made this silent, and it
is still open. Closing it means folding the schema bytes into
`compute_input_sha`, which changes **every** key and invalidates the entire
recorded corpus, including the committed e2e fixture. That is a real cost with
a real benefit and it is not a call this note makes. Flagged for decision.

## Workshop relevance

This belongs in the customer conversation, not just the changelog. It is a
concrete instance of the failure mode the whole engagement is meant to guard
against: **a migration harness that quietly disadvantages the incumbent.** A
team running a naive A/B here would have concluded Gemini extracts better than
Claude, when the measured gap was entirely a schema dialect in the test rig.
Any parity claim is only as good as the fairness of the baseline arm.
