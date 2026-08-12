"""The capped-thinking shipping arms, full 70 per subagent, on the record.

Registered 2026-08-12 as the **recommended deployment configuration**: the
headline candidate (Gemini 3.6 Flash) running the arm each subagent actually
ships, with the reasoning budget minimised. The configuration is committed —
``config/models.yaml: gemini-flash-current-capped`` — so nothing about it is a
flag someone remembered to pass.

Why this arm exists
-------------------
The 2026-08-12 cost audit (``artifacts/results/cost_audit.md``) established
that the candidate was billed for 2.08-2.63x Claude's output tokens while
emitting the same or fewer characters, with a floor of 14-61% of billed output
never returned to the caller. The n=10 probe
(``artifacts/results/capped_thinking_probe.json``) showed the budget is
reachable. This run is the measurement that probe was not: full corpus, all
three subagents, recorded, judged.

Two phases, deliberately split
------------------------------
Phase 1 records the generations. From those, and only those, tonight's render
takes **cost and schema validity** — both are properties of the generation
itself, computed from recorded token counts and a schema parse, with no judge
in the loop.

Phase 2 judges. It runs after every generation has landed, because the judge
draws on Gemini Pro quota that the generations do not contend for, and because
a judge phase that dies half-way should not be able to take the generations
with it. Until it lands, the capped configuration's **quality cells ship
"judging in progress"** — they are not estimated, defaulted, or borrowed from
the default-budget arm.

Nothing here supersedes anything. The capped arm is its own model key, so its
recordings sit beside the default-budget recordings in ``artifacts/replay/``
rather than on top of them, and every rendered cell can name which
configuration produced it.

    setsid nohup .venv/bin/python scripts/measure_capped_arms.py &
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
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
from amw.tuning.ablate import (  # noqa: E402
    CAPPED_GEN_MODEL,
    format_rung,
    run_ladder,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from widen_fe_rungs import RecordedFirstAdapter, RecordedFirstRouter  # noqa: E402

SUBAGENTS = ("query_rewriter", "chunk_summarizer", "feature_extractor")
RUNGS = ("ship-current-capped",)
SPLIT = "all"

RESULTS = REPO_ROOT / "artifacts" / "results"
GEN_MARKER = RESULTS / "capped_arms_generations.done"
JUDGE_MARKER = RESULTS / "capped_arms_judged.done"


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(message: str) -> None:
    print(f"[{_stamp()}] {message}", flush=True)


def main() -> int:
    cfg = load_all(customer="demo_patents")
    store = ReplayStore()
    spec = cfg.models.spec(CAPPED_GEN_MODEL)
    _log(
        f"=== capped-thinking arms: {spec.display_name} "
        f"({spec.id_for('vertex')}) in {spec.region!r}, "
        f"thinking_budget={spec.thinking_budget} ==="
    )

    # ---- phase 1: generations -------------------------------------------
    # Recorded-first, so a crash mid-corpus resumes instead of re-calling and
    # superseding what already landed.
    _log(f"=== PHASE 1/2  generations, n=70 x {len(SUBAGENTS)} subagents ===")
    for subagent in SUBAGENTS:
        router = RecordedFirstRouter(models=cfg.models, store=store)
        run_ladder(
            subagent,
            mode="live",
            config=cfg,
            rungs=RUNGS,
            split=SPLIT,
            router=router,
            run_judge=False,
            write=False,
        )
        _log(
            f"  {subagent}: {router.misses} live, {router.hits} from recordings"
        )
    GEN_MARKER.write_text(_stamp() + "\n", encoding="utf-8")
    _log(f"generations complete -> {GEN_MARKER.name}")
    _log("cost + schema validity are computable NOW; quality is not.")

    # ---- phase 2: judging ------------------------------------------------
    _log("\n=== PHASE 2/2  judge the capped arms ===")
    model_key, _spec = cfg.models.for_role(JUDGE_ROLE)
    for subagent in SUBAGENTS:
        adapter = RecordedFirstAdapter(model_key, models=cfg.models, store=store)
        judge = Judge(mode="live", models=cfg.models, adapter=adapter)
        result = run_ladder(
            subagent,
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
                _log(f"  {line}")
        _log(
            f"  {subagent}: judge {adapter.misses} live, "
            f"{adapter.hits} from recordings"
        )
    JUDGE_MARKER.write_text(_stamp() + "\n", encoding="utf-8")
    _log(f"judging complete -> {JUDGE_MARKER.name}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # pragma: no cover - a failed run must leave a marker
        traceback.print_exc()
        (RESULTS / "capped_arms_FAILED.done").write_text(
            _stamp() + "\n", encoding="utf-8"
        )
        raise SystemExit(1)
