"""Measure the three unmeasured Feature Extractor rungs (Tuesday plan item 3).

``A0-schema``, ``A4-novelty-tool`` and ``A4-novelty-schema`` exist in the ladder
but have never been run. They answer two separate questions:

* the **mode confound** — does an enforced ``response_schema``, isolated from
  any prompt change, move the FE judged score? (``A0`` → ``A0-schema``)
* the **prompt effect** — does the hand-tuned novelty rule close the judged gap
  to ``claude_baseline``'s 0.900? (``A0`` → ``A4-novelty-tool``)

Run in two phases on purpose.

Phase 1 makes the 210 generations live on Gemini Flash and lets record-on-live
put them in ``artifacts/replay/``. Nothing is written to the ladder artifact:
an unjudged rung record appended now would have to be de-duplicated against the
judged one appended later.

Phase 2 judges them. The judge is Gemini **Pro**, which is the same quota the
concurrent judge-widening job (B2) is drawing on, so phase 2 waits for that
job's completion marker before it starts. Waiting is cheaper than discovering
the contention as a wall of 429s, and the generations — the part that does not
contend — are already done by then. If the marker never appears the phase
starts anyway once the timeout expires; a stalled sibling job should delay this
work, not cancel it.

Generations are replayed in phase 2, never re-run, so the judged numbers score
exactly the outputs phase 1 recorded.

    .venv/bin/python scripts/measure_fe_rungs.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Same .env load cli.py does. These scripts make live calls without going
# through cli.py, and PROJECT_ID/REGION live in .env, not in the shell.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv ships in requirements.txt
    pass
else:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

from amw.config import load_all  # noqa: E402
from amw.eval.judge import JUDGE_ROLE, Judge  # noqa: E402
from amw.traces.store import ReplayStore  # noqa: E402
from amw.tuning.ablate import format_rung, run_ladder  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from widen_judge import RecordedFirstAdapter  # noqa: E402

SUBAGENT = "feature_extractor"
RUNGS = ("A0-schema", "A4-novelty-tool", "A4-novelty-schema")

#: B2 writes this when its judge work is finished and the Pro quota is free.
B2_MARKER = Path("artifacts/results/phase2_widened.done")
#: Start anyway after this long. B2 is a sibling, not a dependency.
B2_WAIT_SECONDS = 90 * 60


def wait_for_b2() -> None:
    if B2_MARKER.is_file():
        print(f"B2 already complete ({B2_MARKER.read_text().strip()})", flush=True)
        return
    print(
        f"waiting up to {B2_WAIT_SECONDS // 60} min for {B2_MARKER} "
        f"before drawing on Gemini Pro judge quota",
        flush=True,
    )
    deadline = time.monotonic() + B2_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(30)
        if B2_MARKER.is_file():
            print(f"B2 complete ({B2_MARKER.read_text().strip()}) — judging now", flush=True)
            return
    print("timed out waiting for B2; judging anyway", flush=True)


def main() -> int:
    cfg = load_all(customer="demo_patents")

    print(f"=== PHASE 1/2  live generations for {', '.join(RUNGS)} ===", flush=True)
    run_ladder(
        SUBAGENT,
        mode="live",
        config=cfg,
        rungs=RUNGS,
        run_judge=False,
        write=False,
    )
    print("generations recorded.\n", flush=True)

    wait_for_b2()

    print(f"\n=== PHASE 2/2  judge the recorded generations ===", flush=True)
    store = ReplayStore()
    model_key, _spec = cfg.models.for_role(JUDGE_ROLE)
    adapter = RecordedFirstAdapter(model_key, models=cfg.models, store=store)
    # One judge across all three rungs, so each RungRecord's judge window spans
    # the whole phase rather than that rung alone. The generation windows, which
    # are what date the outputs being scored, stay per-rung.
    judge = Judge(mode="live", models=cfg.models, adapter=adapter)

    result = run_ladder(
        SUBAGENT,
        mode="replay",
        config=cfg,
        rungs=RUNGS,
        judge=judge,
        write=True,
        append=True,
    )
    for record in result.rungs:
        for line in format_rung(record):
            print(line, flush=True)

    print(
        f"\njudge calls: {adapter.misses} live, {adapter.hits} from recordings.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
