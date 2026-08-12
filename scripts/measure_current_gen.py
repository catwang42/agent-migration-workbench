"""The current-generation campaign: the same prompts, a newer model.

Owner's model-target correction, 2026-08-12. The measured campaign ran on the
2.5-class models and those artifacts stay exactly as they are — they are the
validated evidence spine. But the workshop recommends migrating to the model a
customer would actually deploy today, so two rungs per subagent get measured on
``gemini-flash-current``:

* ``A0-current``    — A0's prompt bytes, unchanged, on the new model. Answers
  "how much of the naive-swap gap does a generation close on its own?"
* ``ship-current``  — the shipping arm's prompt bytes, unchanged. Answers the
  question the scorecard actually asks: does the arm we recommend still clear
  its gates on the model we recommend?

Nothing about the instrument moves. Same corpus, same items, same gates, and
above all the same judge — ``gemini-pro`` (2.5) in us-central1, registered
before any result was seen. A judge that changes on freeze day is not the judge
the comparison was agreed on, and the two generations would stop being
comparable to each other, which is the entire point of running both.

Full 70 (``split="all"``), because these rows sit beside gated rows that were
widened to the full corpus. A core-28 row next to a full-70 row invites exactly
the mix-up the split labels exist to prevent.

Three phases, for the same reason ``measure_fe_rungs.py`` has two.

Phase 1 makes the generations live on the new model and lets record-on-live put
them in ``artifacts/replay/``. Nothing is written to the ladder artifact: an
unjudged rung record appended now would have to be de-duplicated against the
judged one appended later.

Phase 2 judges those recordings. Generations are replayed, never re-run, so the
judged numbers score exactly the outputs phase 1 recorded.

Phase 3 runs the shadow adjudication of ``ship-current`` against
``claude_baseline`` — the same gate, the same corpus, the same incumbent
recordings, a different candidate model.

Writes ``artifacts/results/current_gen.done`` when finished, per the standing
rule that background runs report by marker rather than by being watched.

    .venv/bin/python scripts/measure_current_gen.py
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Same .env load cli.py does. These scripts make live calls without going
# through cli.py, and PROJECT_ID/REGION live in .env, not in the shell.
try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv ships in requirements.txt
    pass
else:
    load_dotenv(ROOT / ".env", override=False)

from amw.config import load_all  # noqa: E402
from amw.eval.judge import JUDGE_ROLE, Judge  # noqa: E402
from amw.shadow.runner import run_shadow  # noqa: E402
from amw.traces.store import ReplayStore  # noqa: E402
from amw.tuning.ablate import (  # noqa: E402
    CURRENT_GEN_MODEL,
    SHIPPING_VARIANT,
    format_rung,
    run_ladder,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from widen_judge import RecordedFirstAdapter  # noqa: E402

SUBAGENTS = ("query_rewriter", "chunk_summarizer", "feature_extractor")
RUNGS = ("A0-current", "ship-current")
#: k comes from config/customers/demo_patents.yaml (judge_repeats: 2), the same
#: place every other judged row got it. Asserted rather than passed, so this
#: script cannot quietly judge the new generation at a different k than the
#: generation it is compared against.
EXPECTED_JUDGE_REPEATS = 2
MARKER = ROOT / "artifacts" / "results" / "current_gen.done"


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(message: str) -> None:
    print(f"[{_stamp()}] {message}", flush=True)


def phase1(cfg) -> None:
    _log(f"=== PHASE 1/3  live generations on {CURRENT_GEN_MODEL}, n=70 ===")
    for subagent in SUBAGENTS:
        _log(f"  {subagent}: {', '.join(RUNGS)}")
        run_ladder(
            subagent,
            mode="live",
            config=cfg,
            rungs=RUNGS,
            split="all",
            run_judge=False,
            write=False,
        )
    _log("generations recorded.")


def phase2(cfg) -> None:
    _log("=== PHASE 2/3  judge the recorded generations (gated judge, k=2) ===")
    store = ReplayStore()
    model_key, _spec = cfg.models.for_role(JUDGE_ROLE)
    _log(f"  judge = {model_key} (unchanged; registered before results)")
    for subagent in SUBAGENTS:
        # One judge per subagent, so a rung's judge window spans that
        # subagent's judging rather than the whole campaign.
        adapter = RecordedFirstAdapter(model_key, models=cfg.models, store=store)
        judge = Judge(mode="live", models=cfg.models, adapter=adapter)
        result = run_ladder(
            subagent,
            mode="replay",
            config=cfg,
            rungs=RUNGS,
            split="all",
            judge=judge,
            write=True,
            append=True,
        )
        _log(f"--- {subagent} ---")
        for record in result.rungs:
            if record.rung not in RUNGS:
                continue
            for line in format_rung(record):
                print(line, flush=True)
        _log(
            f"  judge calls: {adapter.misses} live, {adapter.hits} from recordings"
        )


def phase3(cfg) -> None:
    _log("=== PHASE 3/3  shadow adjudication vs claude_baseline ===")
    store = ReplayStore()
    model_key, _spec = cfg.models.for_role(JUDGE_ROLE)
    for subagent in SUBAGENTS:
        arm = SHIPPING_VARIANT[subagent]
        _log(f"  {subagent}: {arm} on {CURRENT_GEN_MODEL} vs claude_baseline")
        # The arms replay: phase 1 recorded the candidate calls and the
        # baseline recordings are the ones every other adjudication used.
        # Re-running either would compare different calls than the ladder
        # scored.
        #
        # The judge is recorded-first rather than the pinned replay judge. The
        # verdicts triage needs are the ones phase 2 just made, so the expected
        # outcome is hits and zero live calls -- and the miss count printed
        # below is the check on that. Pinning to replay here would instead
        # report a wall of `not_adjudicated` if the lookup did not line up,
        # which is a silent empty adjudication rather than a visible one.
        adapter = RecordedFirstAdapter(model_key, models=cfg.models, store=store)
        judge = Judge(mode="live", models=cfg.models, adapter=adapter)
        result = run_shadow(
            config=cfg,
            mode="replay",
            subagents=(subagent,),
            baseline_arm="claude_baseline",
            candidate_arm=arm,
            candidate_model=CURRENT_GEN_MODEL,
            judge=judge,
            out_path=f"artifacts/results/shadow_current_{subagent}.json",
            triage_path=f"artifacts/results/shadow_triage_current_{subagent}.md",
        )
        _log(
            f"    judge calls: {adapter.misses} live, {adapter.hits} from "
            f"recordings (0 live = phase 2's verdicts covered the adjudication)"
        )
        for sub in result.subagents:
            tri = sub.triage_summary
            _log(
                f"    agreement {sub.agreement.agreement:.3f} "
                f"[{sub.agreement.ci_lower:.3f}, {sub.agreement.ci_upper:.3f}]; "
                f"adjudication {tri.wins}W/{tri.losses}L/{tri.ties}T "
                f"({tri.not_adjudicated} not adjudicated of "
                f"{tri.disagreements} disagreements)"
            )


def main() -> int:
    cfg = load_all(customer="demo_patents")
    k = cfg.customer.dataset.judge_repeats
    if k != EXPECTED_JUDGE_REPEATS:
        _log(
            f"REFUSING: judge_repeats is {k}, not {EXPECTED_JUDGE_REPEATS}. The "
            f"2.5-class rows were judged at k={EXPECTED_JUDGE_REPEATS}; judging "
            f"the current generation at a different k would make the two "
            f"generations non-comparable, which is the whole point of the run."
        )
        return 2
    started = _stamp()
    try:
        phase1(cfg)
        phase2(cfg)
        phase3(cfg)
    except Exception:
        # A marker that says "failed" is worth more than no marker: the
        # consumption point is a marker check, and a missing file is
        # indistinguishable from a run that is still going.
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(
            f"FAILED started={started} failed={_stamp()}\n\n{traceback.format_exc()}"
        )
        traceback.print_exc()
        return 1
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(
        f"ok started={started} finished={_stamp()} "
        f"model={CURRENT_GEN_MODEL} rungs={','.join(RUNGS)} "
        f"subagents={','.join(SUBAGENTS)} split=all judge_repeats={k}\n"
    )
    _log(f"done. marker: {MARKER}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
