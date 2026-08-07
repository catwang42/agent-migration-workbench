# Judge prompts

These files are the judge. They are checked in, versioned by directory, and
shown to customers — "here is the exact text that scored your subagents" is the
answer to *"Gemini judging Gemini vs Claude?"* (master_plan §7). They are files
rather than Python string literals for that reason: a prompt buried in code is
a prompt nobody in the room can read.

## Layout

```
judge_prompts/
  v1/
    system.txt       system_instruction for the judge model
    user.txt         per-item template ($-placeholders, string.Template)
    repeat_note.txt  appended as its own message when k > 1
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
