"""Measure the targeted Query Rewriter rung, A4-targeted, on the full 70.

Tuesday plan item 3b, as ruled on 2026-08-11: three rules in one rung —
publication numbers verbatim in ``query``; an explicitly named ``date_to``
copied rather than expanded; ``landscape`` and ``ownership`` separated by which
side of the question is unknown. Each rule answers a named loss cluster from
the full-70 adjudication. See ``notes/qr_targeted_rung.md`` for what this
number can and cannot be read as — in particular, the rules are bundled and
per-rule credit is not isolated.

Measured at n=70, matching the gated QR row and the widened FE ladder.

Everything here is a miss: the rung is a new variant, so no generation and no
judge call for it exists in ``artifacts/replay/``. It is still run
recorded-first, for one reason — a re-run after a crash mid-way should pick up
where it stopped instead of re-calling and superseding what already landed.

The judge phase waits for the FE widening to finish before it starts. Both draw
on the same Gemini Pro judge quota, and two eval runs racing for it is how you
get a batch of ``status:"error"`` traces in the middle of a published number.
Generations do not contend, so phase 1 does not wait.

    .venv/bin/python scripts/measure_qr_rung.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv ships in requirements.txt
    pass
else:
    load_dotenv(REPO_ROOT / ".env", override=False)

from amw.config import load_all  # noqa: E402
from amw.eval.judge import JUDGE_ROLE, Judge  # noqa: E402
from amw.traces.store import ReplayStore  # noqa: E402
from amw.tuning.ablate import format_rung, run_ladder  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from widen_fe_rungs import RecordedFirstAdapter, RecordedFirstRouter  # noqa: E402

SUBAGENT = "query_rewriter"
RUNGS = ("A4-targeted",)
SPLIT = "all"

#: The FE widening run. Its judge phase and ours must not overlap.
FE_MARKER = REPO_ROOT / "artifacts" / "results" / "fe_rungs_n70.done"
FE_WAIT_SECONDS = 90 * 60


def wait_for_fe() -> None:
    if FE_MARKER.is_file():
        print(f"FE widening already complete ({FE_MARKER.read_text().strip()})",
              flush=True)
        return
    print(
        f"waiting up to {FE_WAIT_SECONDS // 60} min for {FE_MARKER} "
        f"before drawing on Gemini Pro judge quota",
        flush=True,
    )
    deadline = time.monotonic() + FE_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(30)
        if FE_MARKER.is_file():
            print(f"FE widening complete ({FE_MARKER.read_text().strip()}) — "
                  f"judging now", flush=True)
            return
    print(
        "timed out waiting for the FE widening; judging anyway. If this run "
        "shows call errors, that contention is the first thing to check.",
        flush=True,
    )


def main() -> int:
    cfg = load_all(customer="demo_patents")
    store = ReplayStore()

    print(f"=== PHASE 1/2  generations for {', '.join(RUNGS)} at n=70 ===",
          flush=True)
    router = RecordedFirstRouter(models=cfg.models, store=store)
    run_ladder(
        SUBAGENT,
        mode="live",
        config=cfg,
        rungs=RUNGS,
        split=SPLIT,
        router=router,
        run_judge=False,
        write=False,
    )
    print(f"generations: {router.misses} live, {router.hits} from recordings.\n",
          flush=True)

    wait_for_fe()

    print("\n=== PHASE 2/2  judge it ===", flush=True)
    model_key, _spec = cfg.models.for_role(JUDGE_ROLE)
    adapter = RecordedFirstAdapter(model_key, models=cfg.models, store=store)
    judge = Judge(mode="live", models=cfg.models, adapter=adapter)
    result = run_ladder(
        SUBAGENT,
        mode="replay",
        config=cfg,
        rungs=RUNGS,
        split=SPLIT,
        judge=judge,
        write=True,
        append=True,
    )
    for record in result.rungs:
        for line in format_rung(record):
            print(line, flush=True)
    print(f"\njudge calls: {adapter.misses} live, {adapter.hits} from recordings.",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
