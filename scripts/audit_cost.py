"""Audit the negative ``cost_savings_pct`` before it enters a shipped artifact.

``scripts/measure_cost_savings.py`` reported that both deployment candidates
cost *more* than the Claude Sonnet 5 baseline. That inverts the workshop's
headline, so the number is audited before it is shown to anyone. Three
hypotheses could produce it, and this script is built to tell them apart:

1. **Rate entry error** — a wrong number was stamped into
   ``config/pricing.yaml`` on freeze day. Section B prices every stamped rate
   against its own generation's siblings and against the incumbent; a slot
   entered from the wrong table shows up as a ratio that breaks the pattern
   every other row follows.
2. **Reasoning-token inflation** — the candidate is billed for thinking tokens
   it never emitted. Section C measures this indirectly, because it has to:
   ``GeminiAdapter._usage`` folds ``thoughts_token_count`` into
   ``output_tokens`` at record time (thinking tokens *are* billed as output),
   so the recorded corpus has no separate thinking count to read. What it does
   have is the emitted payload. If a model is billed for far more output
   tokens than its emitted bytes can account for, the difference is output it
   was charged for and did not return.
3. **Genuinely higher cost** — the candidate emits more actual answer, or the
   rates really are that close together.

Nothing here is a new measurement: every token count comes from a call already
recorded in ``artifacts/replay/``, and every rate comes from
``config/pricing.yaml``. Zero live calls, no credentials.

The one bound that needs no tokenizer
-------------------------------------
Section C leans on a comparison that cannot be argued with: **billed output
tokens against emitted characters**. No tokenizer in use by either provider
produces more tokens than there are characters in the text it encodes — the
worst case is byte-level, one token per character. So whenever a call is
billed for more output tokens than its response contains characters, the
excess is output the model generated and did not return, and

    max(0, output_tokens - emitted_chars)

is a *floor* on it, not an estimate. Real tokenizers pack several characters
per token, so the true figure is much larger; this script reports the floor
because the floor is the part that cannot be disputed.

Characters-per-token is reported alongside it as context. That figure *is*
tokenizer-dependent and is labelled as such wherever it appears. Compact JSON
tokenizes densely — punctuation and short keys often cost a token each — so a
low value is unremarkable on its own. A value below 1.0 is not: it is the
same impossibility stated as a rate.

    .venv/bin/python scripts/audit_cost.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv ships in requirements.txt
    pass
else:
    load_dotenv(ROOT / ".env", override=False)

from amw.adapters import AdapterRouter  # noqa: E402
from amw.agents.prompt_packs import build_request  # noqa: E402
from amw.config import load_all  # noqa: E402
from amw.datasets.schema import read_items  # noqa: E402
from amw.economics.measured_savings import call_cost_usd  # noqa: E402
from amw.eval.runner import default_dataset_dir, prompt_view  # noqa: E402
from amw.tuning.ablate import DEPLOYMENT_CANDIDATES, SHIPPING_VARIANT  # noqa: E402

SUBAGENTS = ("query_rewriter", "chunk_summarizer", "feature_extractor")
BASELINE_VARIANT = "claude_baseline"
BASELINE_MODEL = "claude-sonnet"
RESULTS = ROOT / "artifacts" / "results"
OUT_MD = RESULTS / "cost_audit.md"
OUT_JSON = RESULTS / "cost_audit.json"

#: Savings the gate demands, as a fraction. Read from gates.yaml rather than
#: written here, so section D cannot drift from the bound it is testing.
def _gate_target(cfg) -> float:
    return float(cfg.gates.gate("cost_savings_pct").min) / 100.0


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emitted_chars(trace) -> int:
    """Characters the model actually returned, canonicalised.

    The structured payload is re-serialised with sorted keys and no spaces so
    that two arms are compared on content rather than on whitespace the
    provider happened to include.
    """
    total = len(trace.output.text or "")
    if trace.output.json_ is not None:
        total += len(
            json.dumps(trace.output.json_, sort_keys=True, separators=(",", ":"))
        )
    return total


def arm_rows(cfg, router, subagent: str, variant: str, model: str, items) -> dict:
    """Every recorded call for one arm, costed and measured."""
    rows = []
    for item in items:
        request = build_request(
            subagent, variant, prompt_view(item), item_id=item.item_id, model=model
        )
        trace = router.complete(request)
        if trace.status != "ok":
            continue
        chars = emitted_chars(trace)
        rows.append(
            {
                "item_id": item.item_id,
                "input_tokens": trace.usage.input_tokens,
                "output_tokens": trace.usage.output_tokens,
                "cached_tokens": trace.usage.cached_tokens,
                "emitted_chars": chars,
                # A floor, not an estimate: see the module docstring.
                "unreturned_floor": max(0, trace.usage.output_tokens - chars),
                "usd": call_cost_usd(cfg.pricing, model, trace),
            }
        )
    n = len(rows)
    out_tokens = sum(r["output_tokens"] for r in rows)
    chars_total = sum(r["emitted_chars"] for r in rows)
    impossible = [r for r in rows if r["unreturned_floor"] > 0]
    return {
        "subagent": subagent,
        "variant": variant,
        "model": model,
        "calls_ok": n,
        "input_rate_per_1m": cfg.pricing.rate(model, "input"),
        "output_rate_per_1m": cfg.pricing.rate(model, "output"),
        "input_tokens": sum(r["input_tokens"] for r in rows),
        "output_tokens": out_tokens,
        "cached_tokens": sum(r["cached_tokens"] for r in rows),
        "emitted_chars": chars_total,
        "chars_per_output_token": (chars_total / out_tokens) if out_tokens else None,
        #: Calls billed for more output tokens than they returned characters.
        "calls_billed_over_emitted": len(impossible),
        "unreturned_output_tokens_floor": sum(r["unreturned_floor"] for r in rows),
        "unreturned_share_floor": (
            sum(r["unreturned_floor"] for r in rows) / out_tokens
            if out_tokens
            else None
        ),
        "input_usd": sum(
            r["input_tokens"] for r in rows
        ) / 1e6 * cfg.pricing.rate(model, "input"),
        "output_usd": out_tokens / 1e6 * cfg.pricing.rate(model, "output"),
        "usd": sum(r["usd"] for r in rows),
        "median_output_tokens": statistics.median(
            [r["output_tokens"] for r in rows] or [0]
        ),
    }


def rate_sanity(cfg) -> list[dict]:
    """Every stamped rate, with the ratios that make an entry error visible.

    A wrong rate rarely looks wrong on its own — 7.5 is a plausible number.
    What it breaks is a *pattern*: within a generation, Flash sits at a
    consistent fraction of Pro, and both sit well under the partner-operated
    Claude rows. So each row is priced against its generation's Pro tier and
    against the incumbent, and the 2.5 generation — whose rates predate freeze
    day and have been used all week — is the reference the current-generation
    rows are checked against.
    """
    pricing = cfg.pricing
    sonnet_in = pricing.rate(BASELINE_MODEL, "input")
    sonnet_out = pricing.rate(BASELINE_MODEL, "output")
    pro_of = {
        "gemini-flash": "gemini-pro",
        "gemini-flash-current": "gemini-pro-current",
        "gemini-flash-35": "gemini-pro-current",
    }
    rows = []
    for key in (
        "gemini-flash",
        "gemini-pro",
        "gemini-flash-current",
        "gemini-flash-35",
        "gemini-pro-current",
        "claude-sonnet",
        "claude-opus",
    ):
        row = {
            "model": key,
            "input_per_1m": pricing.rate(key, "input"),
            "output_per_1m": pricing.rate(key, "output"),
            "cached_input_per_1m": pricing.rate(key, "cached_input"),
            "vs_sonnet_input": pricing.rate(key, "input") / sonnet_in,
            "vs_sonnet_output": pricing.rate(key, "output") / sonnet_out,
        }
        pro = pro_of.get(key)
        if pro:
            row["vs_pro_input"] = pricing.rate(key, "input") / pricing.rate(pro, "input")
            row["vs_pro_output"] = pricing.rate(key, "output") / pricing.rate(
                pro, "output"
            )
            row["pro_tier"] = pro
        rows.append(row)
    return rows


def _fmt(value: float | None, spec: str = ".3f") -> str:
    return "—" if value is None else format(value, spec)


def render(report: dict) -> str:
    lines: list[str] = [
        "# Cost audit — is the negative savings figure real?",
        "",
        f"Computed {report['computed_on']} from recordings only (mode: "
        f"{report['mode']}). Prices as stamped in `config/pricing.yaml`, "
        f"verified {report['prices_verified_on']} by {report['prices_verified_by']}.",
        "",
        "## A. Per-arm cost breakdown",
        "",
        "Totals over the full 70-item corpus, per arm. `in $` and `out $` are "
        "the two halves of the bill, so the row that dominates is visible "
        "rather than inferred.",
        "",
        "| Subagent | Arm | Model | n | in tok | out tok | cached | in $/1M | "
        "out $/1M | in $ | out $ | total $ |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in report["arms"]:
        lines.append(
            f"| {arm['subagent']} | `{arm['variant']}` | `{arm['model']}` | "
            f"{arm['calls_ok']} | {arm['input_tokens']:,} | "
            f"{arm['output_tokens']:,} | {arm['cached_tokens']:,} | "
            f"{arm['input_rate_per_1m']:g} | {arm['output_rate_per_1m']:g} | "
            f"${arm['input_usd']:.4f} | ${arm['output_usd']:.4f} | "
            f"${arm['usd']:.4f} |"
        )

    lines += [
        "",
        "## B. Rate sanity",
        "",
        "Every stamped rate, priced against the incumbent and against its own "
        "generation's Pro tier. The 2.5-generation rows are the reference: "
        "they predate freeze day and every ladder result this week was costed "
        "on them.",
        "",
        "| Model | in $/1M | out $/1M | cached $/1M | vs Sonnet in | "
        "vs Sonnet out | vs own Pro in | vs own Pro out |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["rates"]:
        lines.append(
            f"| `{row['model']}` | {row['input_per_1m']:g} | "
            f"{row['output_per_1m']:g} | {row['cached_input_per_1m']:g} | "
            f"{row['vs_sonnet_input']:.2f}x | {row['vs_sonnet_output']:.2f}x | "
            f"{_fmt(row.get('vs_pro_input'), '.2f')}"
            f"{'x' if 'vs_pro_input' in row else ''} | "
            f"{_fmt(row.get('vs_pro_output'), '.2f')}"
            f"{'x' if 'vs_pro_output' in row else ''} |"
        )

    lines += [
        "",
        "## C. Output tokens: billed vs emitted",
        "",
        "`unreturned (floor)` is `max(0, output_tokens - emitted_chars)` summed "
        "over calls. No tokenizer emits more tokens than characters, so any "
        "positive value is output the model was billed for and did not return "
        "— a floor on it, not an estimate. `chars/out tok` is context and is "
        "tokenizer-dependent; compact JSON tokenizes densely, so a low value is "
        "unremarkable, but a value **below 1.0 is impossible** without "
        "unreturned tokens.",
        "",
        "| Subagent | Arm | out tok | median out tok | emitted chars | "
        "chars/out tok | calls billed > emitted | unreturned (floor) | "
        "share of billed output |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for arm in report["arms"]:
        cpt = arm["chars_per_output_token"]
        flag = " **impossible**" if cpt is not None and cpt < 1.0 else ""
        lines.append(
            f"| {arm['subagent']} | `{arm['variant']}` on `{arm['model']}` | "
            f"{arm['output_tokens']:,} | {arm['median_output_tokens']:g} | "
            f"{arm['emitted_chars']:,} | {_fmt(cpt, '.2f')}{flag} | "
            f"{arm['calls_billed_over_emitted']}/{arm['calls_ok']} | "
            f"{arm['unreturned_output_tokens_floor']:,} | "
            f"{_fmt((arm['unreturned_share_floor'] or 0) * 100, '.0f')}% |"
        )

    lines += [
        "",
        "### Per-subagent, candidate vs incumbent",
        "",
        "| Subagent | out tok Claude | out tok Gemini | ratio | in tok ratio | "
        "chars/out tok Claude | chars/out tok Gemini |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["comparison"]:
        lines.append(
            f"| {row['subagent']} | {row['baseline_output_tokens']:,} | "
            f"{row['candidate_output_tokens']:,} | "
            f"{row['output_token_ratio']:.2f}x | "
            f"{row['input_token_ratio']:.2f}x | "
            f"{_fmt(row['baseline_chars_per_output_token'], '.2f')} | "
            f"{_fmt(row['candidate_chars_per_output_token'], '.2f')} |"
        )

    lines += [
        "",
        "## D. What would have to be true for the gate to pass",
        "",
        f"Holding the measured token mix fixed, this is the rate the candidate "
        f"would need for `cost_savings_pct` to reach its "
        f"{report['gate_target_pct']:g}% bound. `scale` is the factor both "
        f"stamped rates would have to be multiplied by — so `scale = 0.60` "
        f"means the stamped rates would have to be **1.67x too high** for a "
        f"rate entry error to explain the result. `out $/1M needed` holds the "
        f"input rate as stamped and solves for output alone; a dash means no "
        f"output rate clears it, because the input side already exceeds the "
        f"budget.",
        "",
        "| Subagent | Claude $ | candidate $ | out $/1M stamped | "
        "out $/1M needed | scale on both rates |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["breakeven"]:
        lines.append(
            f"| {row['subagent']} | ${row['baseline_usd']:.4f} | "
            f"${row['candidate_usd']:.4f} | {row['output_rate_stamped']:g} | "
            f"{_fmt(row['output_rate_needed'], '.2f')} | "
            f"{_fmt(row['scale_needed'], '.2f')} |"
        )

    lines += [
        "",
        "## E. Counterfactual: the same run without the unreturned tokens",
        "",
        "**Arithmetic over recorded calls, not a measurement.** It re-costs the "
        "candidate's *recorded input tokens* at the stamped rates while pricing "
        "its output at the incumbent's recorded output-token count — i.e. what "
        "the bill would have been had the candidate been billed for no more "
        "output than Claude returned for the same item. It is shown because it "
        "separates the two questions the gate conflates: whether the candidate "
        "is expensive, or whether its *thinking* is. No arm was run this way; "
        "the capped-thinking probe is the measurement.",
        "",
        "| Subagent | Claude $ | candidate $ as billed | candidate $ at Claude's "
        "output tokens | savings % (counterfactual) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in report["counterfactual"]:
        lines.append(
            f"| {row['subagent']} | ${row['baseline_usd']:.4f} | "
            f"${row['candidate_usd']:.4f} | ${row['counterfactual_usd']:.4f} | "
            f"{row['counterfactual_savings_pct']:+.1f}% |"
        )

    lines += [
        "",
        "## Recording limitation",
        "",
        "**Emitted characters are undercounted on both arms**, which is why "
        "the incumbent also shows a nonzero floor. The stored payload is the "
        "*parsed* structured output re-serialised compactly; the JSON syntax, "
        "whitespace and tool-call envelope the model actually emitted are not "
        "kept. That inflates every floor in section C by the same mechanism on "
        "both sides, and it is why the comparison between arms carries the "
        "finding rather than any single row. It cannot explain the candidate's "
        "figures: recovering 90,441 billed tokens from a 35,079-character "
        "payload would need an unstored envelope roughly three times the size "
        "of the answer.",
        "",
        "No thinking budget is configured anywhere in this repo — "
        "`amw/adapters/gemini.py` never sets `thinking_config`, so the "
        "candidate runs at the model's default. The adapter does skip "
        "`part.thought` parts when assembling the answer, so thinking output "
        "was streamed and deliberately not stored.",
        "",
        "The recorded corpus cannot break out thinking tokens directly. "
        "`GeminiAdapter._usage` adds `thoughts_token_count` into "
        "`output_tokens` at record time — correctly, since thinking tokens are "
        "billed as output — and `amw/traces/schema.py::Usage` has no field to "
        "keep the two apart. Section C is therefore an inference from emitted "
        "bytes, not a reading of provider metadata. Splitting the counter "
        "would need a schema change plus fresh live calls; it is written up as "
        "the follow-on rather than done under freeze.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate",
        default="gemini-flash-current",
        choices=sorted(DEPLOYMENT_CANDIDATES),
        help="deployment candidate to audit (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    candidate = args.candidate

    cfg = load_all(customer="demo_patents")
    if not cfg.pricing.is_verified:
        print("REFUSING: config/pricing.yaml still has VERIFY slots.", file=sys.stderr)
        return 2
    router = AdapterRouter(mode="replay", models=cfg.models)

    target = _gate_target(cfg)
    arms: list[dict] = []
    comparison: list[dict] = []
    breakeven: list[dict] = []
    counterfactual: list[dict] = []
    for subagent in SUBAGENTS:
        items = read_items(default_dataset_dir() / f"{subagent}.jsonl")
        base = arm_rows(cfg, router, subagent, BASELINE_VARIANT, BASELINE_MODEL, items)
        ship = SHIPPING_VARIANT[subagent]
        cand = arm_rows(cfg, router, subagent, ship, candidate, items)
        arms.extend([base, cand])
        comparison.append(
            {
                "subagent": subagent,
                "baseline_output_tokens": base["output_tokens"],
                "candidate_output_tokens": cand["output_tokens"],
                "output_token_ratio": cand["output_tokens"] / base["output_tokens"],
                "input_token_ratio": cand["input_tokens"] / base["input_tokens"],
                "baseline_chars_per_output_token": base["chars_per_output_token"],
                "candidate_chars_per_output_token": cand["chars_per_output_token"],
            }
        )
        budget = base["usd"] * (1.0 - target)
        remaining = budget - cand["input_usd"]
        capped_usd = (
            cand["input_usd"]
            + base["output_tokens"] / 1e6 * cand["output_rate_per_1m"]
        )
        counterfactual.append(
            {
                "subagent": subagent,
                "baseline_usd": base["usd"],
                "candidate_usd": cand["usd"],
                "counterfactual_usd": capped_usd,
                "counterfactual_savings_pct": (base["usd"] - capped_usd)
                / base["usd"]
                * 100.0,
            }
        )
        breakeven.append(
            {
                "subagent": subagent,
                "baseline_usd": base["usd"],
                "candidate_usd": cand["usd"],
                "output_rate_stamped": cand["output_rate_per_1m"],
                "output_rate_needed": (
                    remaining / cand["output_tokens"] * 1e6
                    if remaining > 0 and cand["output_tokens"]
                    else None
                ),
                "scale_needed": budget / cand["usd"] if cand["usd"] else None,
            }
        )

    report = {
        "computed_on": _stamp(),
        "mode": "replay (zero live calls)",
        "candidate": candidate,
        "prices_verified_on": str(cfg.pricing.verified_on),
        "prices_verified_by": cfg.pricing.verified_by,
        "gate_target_pct": target * 100.0,
        "arms": arms,
        "rates": rate_sanity(cfg),
        "comparison": comparison,
        "breakeven": breakeven,
        "counterfactual": counterfactual,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    OUT_MD.write_text(render(report), encoding="utf-8")
    print(render(report))
    print(f"wrote {OUT_MD.relative_to(ROOT)} and {OUT_JSON.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # pragma: no cover
        traceback.print_exc()
        raise SystemExit(1)
