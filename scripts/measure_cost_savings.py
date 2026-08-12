"""B4 — ``cost_savings_pct`` per deployment candidate, from recorded tokens.

Zero live calls: every arm replays out of ``artifacts/replay/``, and the only
new inputs are the list prices a human stamped into ``config/pricing.yaml`` on
2026-08-12. The arithmetic and the labelling both live in
:mod:`amw.economics.measured_savings`; this script is the driver that pairs the
arms up and writes the artifact.

For each subagent it costs the incumbent (``claude_baseline`` on
``claude-sonnet``) against each deployment candidate's shipping arm — the same
prompt bytes the ladder measured, on the candidate model — item by item over
the full 70.

    .venv/bin/python scripts/measure_cost_savings.py
"""

from __future__ import annotations

import argparse
import json
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
from amw.economics.measured_savings import savings_from_traces  # noqa: E402
from amw.eval.runner import default_dataset_dir, prompt_view  # noqa: E402
from amw.tuning.ablate import (  # noqa: E402
    CAPPED_GEN_MODEL,
    DEPLOYMENT_CANDIDATES,
    SHIPPING_VARIANT,
)

#: Costable arms: every deployment candidate, plus the capped deployment
#: configuration. The capped arm is not a *candidate* — it is the headline
#: candidate with its reasoning budget minimised, and it is priced off the same
#: SKU — but it is its own model key in the replay store, so it costs like one.
COSTABLE = tuple(DEPLOYMENT_CANDIDATES) + (CAPPED_GEN_MODEL,)

SUBAGENTS = ("query_rewriter", "chunk_summarizer", "feature_extractor")
BASELINE_VARIANT = "claude_baseline"
BASELINE_MODEL = "claude-sonnet"
RESULTS = ROOT / "artifacts" / "results"
OUT = RESULTS / "cost_savings.json"
MARKER = RESULTS / "cost_savings.done"


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(message: str) -> None:
    print(f"[{_stamp()}] {message}", flush=True)


def _traces(router, subagent, variant, model, items) -> dict:
    """``item_id -> Trace`` for one arm, replayed.

    A replay miss is fatal rather than skipped: a missing recording would
    silently shrink the paired set and change the saving, which is the sort of
    denominator drift that is invisible in the output.
    """
    out = {}
    for item in items:
        request = build_request(
            subagent, variant, prompt_view(item), item_id=item.item_id, model=model
        )
        out[item.item_id] = router.complete(request)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidates",
        default=",".join(sorted(DEPLOYMENT_CANDIDATES)),
        help="comma-separated costable arms (default: %(default)s)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=OUT,
        help=(
            "output path. Defaults to the shipped artifact; a new measurement "
            "goes to a new file rather than over an existing one "
            "(default: %(default)s)"
        ),
    )
    args = parser.parse_args(argv)
    candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
    unknown = [c for c in candidates if c not in COSTABLE]
    if unknown:
        parser.error(f"not costable arms: {unknown}; known: {list(COSTABLE)}")

    cfg = load_all(customer="demo_patents")
    if not cfg.pricing.is_verified:
        _log(
            "REFUSING: config/pricing.yaml still has "
            f"{len(cfg.pricing.unverified_keys())} slot(s) at VERIFY. A savings "
            "percentage over placeholder prices is a fabricated result."
        )
        return 2

    router = AdapterRouter(mode="replay", models=cfg.models)
    _log(f"prices verified {cfg.pricing.verified_on} by {cfg.pricing.verified_by}")
    _log(f"candidates: {', '.join(candidates)}")

    report: dict = {
        "computed_on": _stamp(),
        "prices_verified_on": str(cfg.pricing.verified_on),
        "baseline_model": BASELINE_MODEL,
        "baseline_variant": BASELINE_VARIANT,
        "mode": "replay (zero live calls)",
        "candidates": {},
    }

    for candidate in candidates:
        _log(f"=== {candidate} ===")
        report["candidates"][candidate] = {}
        for subagent in SUBAGENTS:
            items = read_items(default_dataset_dir() / f"{subagent}.jsonl")
            arm = SHIPPING_VARIANT[subagent]
            base = _traces(router, subagent, BASELINE_VARIANT, BASELINE_MODEL, items)
            cand = _traces(router, subagent, arm, candidate, items)
            savings = savings_from_traces(
                cfg,
                subagent=subagent,
                baseline_variant=BASELINE_VARIANT,
                candidate_variant=arm,
                baseline_model=BASELINE_MODEL,
                candidate_model=candidate,
                baseline_traces=base,
                candidate_traces=cand,
            )
            report["candidates"][candidate][subagent] = savings.model_dump(mode="json")
            estimate = savings.estimate
            if estimate is None:
                _log(f"  {subagent:20s} not computed: {savings.no_estimate_reason}")
                continue
            _log(
                f"  {subagent:20s} {estimate.point:+.1f}% "
                f"[{estimate.lo:+.1f}, {estimate.hi:+.1f}] "
                f"paired n={estimate.n}, dropped {savings.dropped_unpaired} | "
                f"${savings.baseline_total_usd:.4f} -> "
                f"${savings.candidate_total_usd:.4f} over the corpus"
            )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    _log(f"wrote {args.out.resolve().relative_to(ROOT)}")
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
