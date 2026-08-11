# Judge prompts

These files are the judge. They are checked in, versioned by directory, and
shown to customers — "here is the exact text that scored your subagents" is the
answer to *"Gemini judging Gemini vs Claude?"* (master_plan §7). They are files
rather than Python string literals for that reason: a prompt buried in code is
a prompt nobody in the room can read.

## Layout

```
judge_prompts/
  v1/              THE GATED JUDGE (Gemini 2.5 Pro). Registered pre-results.
    system.txt       system_instruction for the judge model
    user.txt         per-item template ($-placeholders, string.Template)
    repeat_note.txt  appended as its own message when k > 1
  v1_crosscheck/   THE VALIDATING JUDGE (Claude, via Vertex Model Garden).
    system.txt       same scoring text as v1; tool emission instead of JSON
    user.txt         byte-identical to v1/user.txt
    repeat_note.txt  byte-identical to v1/repeat_note.txt
```

Loaded by `amw.eval.judge.load_prompt_pack(version="v1")`, which also computes
a sha256 over the pack so every score can name the exact prompt text that
produced it.

## Editing rules

1. **Never edit a published version in place.** A recorded judge trace is keyed
   on the hash of the text that produced it; editing `v1` after a run makes the
   scorecard footer lie about what was asked. Copy the directory to `v2` and
   change `DEFAULT_PROMPT_VERSION`.
2. **Placeholders are `$name`** (`string.Template`), not `{name}`, because the
   rendered material is full of JSON braces. `judge.py` uses `substitute()`,
   which raises on an unknown or missing placeholder — a typo fails at render
   time, not as a silently blank section.
3. `user.txt` must supply every placeholder in
   `amw.eval.judge.USER_TEMPLATE_FIELDS`; `tests/test_judge.py` asserts this.

## Why there are two packs

`v1_crosscheck` is the second-judge cross-check (`amw/eval/crosscheck.py`): a
Claude-class judge re-scoring the same recorded outputs against the same
rubrics, so *"Gemini judged the Gemini-vs-Claude comparison"* has an answer made
of measurements rather than assurances.

It is a **separate pack rather than a `--model` flag on `v1`** for one reason:
under this demo organization's Vertex AI policy configuration
(`constraints/vertexai.allowedPartnerModelFeatures`), partner-model structured
outputs were unavailable, so a Claude judge on Vertex emits its verdict through
an `emit_judge_verdict` tool call rather than a strict JSON response. That is a
change to the prompt text — the "Output" section — and editing `v1` in place to
say it would have broken rule 1 above.

Everything that decides *how an item scores* is byte-identical between the two
packs, and `tests/test_crosscheck.py` asserts it: `user.txt` and
`repeat_note.txt` in full, and everything in `system.txt` above the `Output`
heading. If the two ever drift, a disagreement between the judges stops being
attributable to the models and the instrument loses its meaning.

The two packs hash differently, so every score names which one produced it.
`v1` remains the gated instrument; `v1_crosscheck` validates and is never
averaged with it or substituted for it.

## Why `repeat_note.txt` exists

`config/customers/demo_patents.yaml` asks for `judge_repeats: 2`, and the
replay store is keyed on `(subagent, model, input_sha)` where `input_sha`
covers the prompt, the messages, the context and the tools — but **not** the
repeat index and not the temperature. Two repeats with byte-identical prompts
therefore collide on one replay key: the second recording overwrites the first,
and a replayed run reports perfect judge agreement it never measured.

Labelling each pass makes the passes distinct calls with distinct keys, so both
are recorded and both replay. The wording deliberately tells the model to score
independently, which is what we want from a repeat anyway.
