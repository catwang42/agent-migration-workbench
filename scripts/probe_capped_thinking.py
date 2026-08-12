"""A probe, not a verdict: does capping thinking move the bill and the clock?

The 2026-08-12 cost audit (``artifacts/results/cost_audit.md``) established
that Gemini 3.6 Flash is billed for far more output tokens than it returns
characters — 56% of Query Rewriter's billed output is a *floor* on tokens the
model generated and did not emit — and that the workbench never sets a
thinking budget, so every recorded arm ran at the model's default.

That raises exactly one question a customer will ask in the room: is the knob
reachable? This probe answers that and nothing else. It runs **n=10 Query
Rewriter items only**, on the shipping prompt, with the thinking budget
minimised, and records the three things that would move: cost, latency, and
schema validity.

What this is not
----------------
It is **not** a candidate arm and it produces **no verdict**. Ten items on one
subagent cannot re-decide a gate that was measured on seventy across three,
and the capped configuration was never run through the judge, so nothing here
says whether the answers are as good. A capped-thinking arm measured properly
— all three subagents, full corpus, judged, shadow-compared — is the follow-on
measurement, and the report says so wherever this probe appears.

Two guards keep it from contaminating anything:

* Recordings land in ``artifacts/thinking_probe/``, not ``artifacts/replay/``.
  The replay store is keyed on ``(subagent, model, input_sha)`` and a later
  record supersedes an earlier one, so writing capped calls into the main
  corpus would silently overwrite the default-budget recordings the ladder,
  the shadow run and the scorecard are all built on.
* The uncapped side of every comparison is **replayed**, not re-run. It is the
  same recorded call the audit costed, so the two columns differ in the
  thinking budget and in nothing else.

    .venv/bin/python scripts/probe_capped_thinking.py --budget 0 -n 10
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

from amw.adapters import AdapterRouter, RecordingAdapter  # noqa: E402
from amw.adapters.gemini import GeminiAdapter  # noqa: E402
from amw.agents.prompt_packs import build_request  # noqa: E402
from amw.config import load_all  # noqa: E402
from amw.datasets.schema import read_items  # noqa: E402
from amw.economics.measured_savings import call_cost_usd  # noqa: E402
from amw.eval.metrics import deterministic_metrics  # noqa: E402
from amw.eval.runner import default_dataset_dir, prompt_view  # noqa: E402
from amw.traces.store import ReplayStore  # noqa: E402
from amw.tuning.ablate import SHIPPING_VARIANT  # noqa: E402

SUBAGENT = "query_rewriter"
CANDIDATE = "gemini-flash-current"
PROBE_STORE = ROOT / "artifacts" / "thinking_probe"
RESULTS = ROOT / "artifacts" / "results"
OUT = RESULTS / "capped_thinking_probe.json"
MARKER = RESULTS / "capped_thinking_probe.done"


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(message: str) -> None:
    print(f"[{_stamp()}] {message}", flush=True)


def emitted_chars(trace) -> int:
    total = len(trace.output.text or "")
    if trace.output.json_ is not None:
        total += len(
            json.dumps(trace.output.json_, sort_keys=True, separators=(",", ":"))
        )
    return total


def _schema_valid(trace, item) -> float | None:
    """The arm's own ``json_schema_validity`` for one item, or None."""
    outcomes = deterministic_metrics(
        SUBAGENT,
        gold=item.gold,
        source=trace,
        provided_chunk_ids=item.input.chunk_ids,
        item_id=item.item_id,
    )
    outcome = outcomes.get("json_schema_validity")
    return None if outcome is None else outcome.value


def _summarise(rows: list[dict], label: str) -> dict:
    ok = [r for r in rows if r["status"] == "ok"]
    totals = {
        "arm": label,
        "calls": len(rows),
        "calls_ok": len(ok),
        "input_tokens": sum(r["input_tokens"] for r in ok),
        "output_tokens": sum(r["output_tokens"] for r in ok),
        "emitted_chars": sum(r["emitted_chars"] for r in ok),
        "usd": sum(r["usd"] for r in ok),
        "errors": sorted({r["error"] for r in rows if r["error"]}),
    }
    totals["unreturned_output_tokens_floor"] = sum(
        max(0, r["output_tokens"] - r["emitted_chars"]) for r in ok
    )
    latencies = [r["total_ms"] for r in ok if r["total_ms"] is not None]
    totals["latency_p50_ms"] = statistics.median(latencies) if latencies else None
    valid = [r["schema_valid"] for r in ok if r["schema_valid"] is not None]
    totals["json_schema_validity"] = (sum(valid) / len(valid)) if valid else None
    totals["schema_scored_n"] = len(valid)
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", type=int, default=10, help="items (default: %(default)s)")
    parser.add_argument(
        "--budget",
        type=int,
        default=0,
        help="thinking_budget to request; 0 minimises it (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    cfg = load_all(customer="demo_patents")
    variant = SHIPPING_VARIANT[SUBAGENT]
    items = read_items(default_dataset_dir() / f"{SUBAGENT}.jsonl")[: args.n]
    _log(
        f"=== capped-thinking probe: {SUBAGENT}/{variant} on {CANDIDATE}, "
        f"thinking_budget={args.budget}, n={len(items)} ==="
    )
    _log(f"  recordings -> {PROBE_STORE.relative_to(ROOT)} (main corpus untouched)")

    # location comes from the registry pin, exactly as ``adapters.resolve``
    # does it — the candidate is pinned to ``global`` and a probe that quietly
    # ran somewhere else would not be comparable to the recorded arm.
    region = cfg.models.spec(CANDIDATE).region
    _log(f"  region pin: {region!r}")
    capped_adapter = RecordingAdapter(
        GeminiAdapter(
            models=cfg.models, location=region, thinking_budget=args.budget
        ),
        store=ReplayStore(PROBE_STORE),
    )
    replay = AdapterRouter(mode="replay", models=cfg.models)

    capped_rows: list[dict] = []
    default_rows: list[dict] = []
    for index, item in enumerate(items, start=1):
        request = build_request(
            SUBAGENT, variant, prompt_view(item), item_id=item.item_id, model=CANDIDATE
        )
        for adapter, rows in ((capped_adapter, capped_rows), (replay, default_rows)):
            trace = adapter.complete(request)
            rows.append(
                {
                    "item_id": item.item_id,
                    "status": trace.status,
                    "error": trace.error,
                    "input_tokens": trace.usage.input_tokens,
                    "output_tokens": trace.usage.output_tokens,
                    "emitted_chars": emitted_chars(trace),
                    "total_ms": trace.latency_ms.total,
                    "schema_valid": (
                        _schema_valid(trace, item) if trace.status == "ok" else None
                    ),
                    "usd": (
                        call_cost_usd(cfg.pricing, CANDIDATE, trace)
                        if trace.status == "ok"
                        else 0.0
                    ),
                }
            )
        if index % 5 == 0:
            _log(f"  ... {index}/{len(items)} items")

    capped = _summarise(capped_rows, f"thinking_budget={args.budget}")
    default = _summarise(default_rows, "default thinking budget (recorded)")

    report = {
        "probed_on": _stamp(),
        "kind": "probe — not a verdict, not a candidate arm",
        "subagent": SUBAGENT,
        "variant": variant,
        "model": CANDIDATE,
        "thinking_budget": args.budget,
        "n": len(items),
        "store": str(PROBE_STORE.relative_to(ROOT)),
        "capped": capped,
        "default": default,
        "caveat": (
            f"{len(items)} items on one subagent, unjudged. This says whether "
            f"the thinking budget is reachable and what it costs; it does not "
            f"say whether the capped answers are as good, and it re-decides no "
            f"gate. The follow-on measurement is a capped-thinking arm across "
            f"all three subagents on the full corpus, judged and "
            f"shadow-compared."
        ),
    }
    if capped["calls_ok"] and default["calls_ok"]:
        report["deltas"] = {
            "output_tokens_ratio": (
                capped["output_tokens"] / default["output_tokens"]
                if default["output_tokens"]
                else None
            ),
            "usd_ratio": (
                capped["usd"] / default["usd"] if default["usd"] else None
            ),
            "latency_p50_ratio": (
                capped["latency_p50_ms"] / default["latency_p50_ms"]
                if capped["latency_p50_ms"] and default["latency_p50_ms"]
                else None
            ),
        }

    OUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for block in (default, capped):
        _log(
            f"  {block['arm']:38s} ok={block['calls_ok']}/{block['calls']} "
            f"out_tok={block['output_tokens']:,} "
            f"unreturned_floor={block['unreturned_output_tokens_floor']:,} "
            f"${block['usd']:.4f} p50={block['latency_p50_ms']}ms "
            f"schema={block['json_schema_validity']}"
        )
        if block["errors"]:
            _log(f"    errors (verbatim): {json.dumps(block['errors'])}")
    _log(f"wrote {OUT.relative_to(ROOT)}")
    MARKER.write_text(_stamp() + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # pragma: no cover - the marker must record failures too
        traceback.print_exc()
        MARKER.write_text(f"{_stamp()} FAILED\n", encoding="utf-8")
        raise SystemExit(1)
