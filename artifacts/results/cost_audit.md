# Cost audit — is the negative savings figure real?

Computed 2026-08-12T12:01:41+00:00 from recordings only (mode: replay (zero live calls)). Prices as stamped in `config/pricing.yaml`, verified 2026-08-12 by Catherine Wang.

## A. Per-arm cost breakdown

Totals over the full 70-item corpus, per arm. `in $` and `out $` are the two halves of the bill, so the row that dominates is visible rather than inferred.

| Subagent | Arm | Model | n | in tok | out tok | cached | in $/1M | out $/1M | in $ | out $ | total $ |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| query_rewriter | `claude_baseline` | `claude-sonnet` | 70 | 154,557 | 16,318 | 0 | 2 | 10 | $0.3091 | $0.1632 | $0.4723 |
| query_rewriter | `gemini_targeted_v1` | `gemini-flash-current` | 70 | 152,356 | 42,891 | 0 | 1.5 | 7.5 | $0.2285 | $0.3217 | $0.5502 |
| chunk_summarizer | `claude_baseline` | `claude-sonnet` | 70 | 162,164 | 39,245 | 0 | 2 | 10 | $0.3243 | $0.3925 | $0.7168 |
| chunk_summarizer | `gemini_tuned_v1` | `gemini-flash-current` | 70 | 101,593 | 81,549 | 0 | 1.5 | 7.5 | $0.1524 | $0.6116 | $0.7640 |
| feature_extractor | `claude_baseline` | `claude-sonnet` | 70 | 160,609 | 35,645 | 0 | 2 | 10 | $0.3212 | $0.3565 | $0.6777 |
| feature_extractor | `gemini_optimizer_v1` | `gemini-flash-current` | 70 | 103,810 | 90,441 | 0 | 1.5 | 7.5 | $0.1557 | $0.6783 | $0.8340 |

## B. Rate sanity

Every stamped rate, priced against the incumbent and against its own generation's Pro tier. The 2.5-generation rows are the reference: they predate freeze day and every ladder result this week was costed on them.

| Model | in $/1M | out $/1M | cached $/1M | vs Sonnet in | vs Sonnet out | vs own Pro in | vs own Pro out |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `gemini-flash` | 0.3 | 2.5 | 0.03 | 0.15x | 0.25x | 0.24x | 0.25x |
| `gemini-pro` | 1.25 | 10 | 0.125 | 0.62x | 1.00x | — | — |
| `gemini-flash-current` | 1.5 | 7.5 | 0.15 | 0.75x | 0.75x | 0.75x | 0.62x |
| `gemini-flash-35` | 1.5 | 9 | 0.15 | 0.75x | 0.90x | 0.75x | 0.75x |
| `gemini-pro-current` | 2 | 12 | 0.2 | 1.00x | 1.20x | — | — |
| `claude-sonnet` | 2 | 10 | 0.2 | 1.00x | 1.00x | — | — |
| `claude-opus` | 5 | 25 | 0.5 | 2.50x | 2.50x | — | — |

## C. Output tokens: billed vs emitted

`unreturned (floor)` is `max(0, output_tokens - emitted_chars)` summed over calls. No tokenizer emits more tokens than characters, so any positive value is output the model was billed for and did not return — a floor on it, not an estimate. `chars/out tok` is context and is tokenizer-dependent; compact JSON tokenizes densely, so a low value is unremarkable, but a value **below 1.0 is impossible** without unreturned tokens.

| Subagent | Arm | out tok | median out tok | emitted chars | chars/out tok | calls billed > emitted | unreturned (floor) | share of billed output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| query_rewriter | `claude_baseline` on `claude-sonnet` | 16,318 | 188 | 17,531 | 1.07 | 15/70 | 2,507 | 15% |
| query_rewriter | `gemini_targeted_v1` on `gemini-flash-current` | 42,891 | 531 | 18,764 | 0.44 **impossible** | 70/70 | 24,127 | 56% |
| chunk_summarizer | `claude_baseline` on `claude-sonnet` | 39,245 | 535 | 80,484 | 2.05 | 0/70 | 0 | 0% |
| chunk_summarizer | `gemini_tuned_v1` on `gemini-flash-current` | 81,549 | 1140 | 73,963 | 0.91 **impossible** | 43/70 | 11,553 | 14% |
| feature_extractor | `claude_baseline` on `claude-sonnet` | 35,645 | 476 | 33,735 | 0.95 **impossible** | 37/70 | 5,755 | 16% |
| feature_extractor | `gemini_optimizer_v1` on `gemini-flash-current` | 90,441 | 1215.5 | 35,079 | 0.39 **impossible** | 70/70 | 55,362 | 61% |

### Per-subagent, candidate vs incumbent

| Subagent | out tok Claude | out tok Gemini | ratio | in tok ratio | chars/out tok Claude | chars/out tok Gemini |
| --- | --- | --- | --- | --- | --- | --- |
| query_rewriter | 16,318 | 42,891 | 2.63x | 0.99x | 1.07 | 0.44 |
| chunk_summarizer | 39,245 | 81,549 | 2.08x | 0.63x | 2.05 | 0.91 |
| feature_extractor | 35,645 | 90,441 | 2.54x | 0.65x | 0.95 | 0.39 |

## D. What would have to be true for the gate to pass

Holding the measured token mix fixed, this is the rate the candidate would need for `cost_savings_pct` to reach its 30% bound. `scale` is the factor both stamped rates would have to be multiplied by — so `scale = 0.60` means the stamped rates would have to be **1.67x too high** for a rate entry error to explain the result. `out $/1M needed` holds the input rate as stamped and solves for output alone; a dash means no output rate clears it, because the input side already exceeds the budget.

| Subagent | Claude $ | candidate $ | out $/1M stamped | out $/1M needed | scale on both rates |
| --- | --- | --- | --- | --- | --- |
| query_rewriter | $0.4723 | $0.5502 | 7.5 | 2.38 | 0.60 |
| chunk_summarizer | $0.7168 | $0.7640 | 7.5 | 4.28 | 0.66 |
| feature_extractor | $0.6777 | $0.8340 | 7.5 | 3.52 | 0.57 |

## E. Counterfactual: the same run without the unreturned tokens

**Arithmetic over recorded calls, not a measurement.** It re-costs the candidate's *recorded input tokens* at the stamped rates while pricing its output at the incumbent's recorded output-token count — i.e. what the bill would have been had the candidate been billed for no more output than Claude returned for the same item. It is shown because it separates the two questions the gate conflates: whether the candidate is expensive, or whether its *thinking* is. No arm was run this way; the capped-thinking probe is the measurement.

| Subagent | Claude $ | candidate $ as billed | candidate $ at Claude's output tokens | savings % (counterfactual) |
| --- | --- | --- | --- | --- |
| query_rewriter | $0.4723 | $0.5502 | $0.3509 | +25.7% |
| chunk_summarizer | $0.7168 | $0.7640 | $0.4467 | +37.7% |
| feature_extractor | $0.6777 | $0.8340 | $0.4231 | +37.6% |

## Recording limitation

**Emitted characters are undercounted on both arms**, which is why the incumbent also shows a nonzero floor. The stored payload is the *parsed* structured output re-serialised compactly; the JSON syntax, whitespace and tool-call envelope the model actually emitted are not kept. That inflates every floor in section C by the same mechanism on both sides, and it is why the comparison between arms carries the finding rather than any single row. It cannot explain the candidate's figures: recovering 90,441 billed tokens from a 35,079-character payload would need an unstored envelope roughly three times the size of the answer.

No thinking budget is configured anywhere in this repo — `amw/adapters/gemini.py` never sets `thinking_config`, so the candidate runs at the model's default. The adapter does skip `part.thought` parts when assembling the answer, so thinking output was streamed and deliberately not stored.

The recorded corpus cannot break out thinking tokens directly. `GeminiAdapter._usage` adds `thoughts_token_count` into `output_tokens` at record time — correctly, since thinking tokens are billed as output — and `amw/traces/schema.py::Usage` has no field to keep the two apart. Section C is therefore an inference from emitted bytes, not a reading of provider metadata. Splitting the counter would need a schema change plus fresh live calls; it is written up as the follow-on rather than done under freeze.

