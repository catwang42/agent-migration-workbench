"""Re-run the four new Feature Extractor rungs on the full 70.

Owner's ruling, 2026-08-11: "the FE scorecard row is measured at n=70, so the
rungs that answer it must be too. The A4-optimizer result cannot ship on a
smaller sample than the regression it resolves."

The four rungs were first measured on the ladder's 28-item core split
(``scripts/measure_fe_rungs.py`` and the optimizer run). The other three rungs
already have full-70 twins in the gated artifact — ``baseline`` is
``claude_baseline`` 0.900, ``A0`` is ``gemini_naive`` 0.821, ``A1-A3`` is
``gemini_tuned_v1`` 0.795, all n=70 in ``phase2_n70_widened.json`` — so
widening these four makes the whole FE ladder readable at one n instead of two.

Two phases, for the same reason ``measure_fe_rungs.py`` has two: generations do
not contend for judge quota, judging does.

Nothing already recorded is re-called. Both phases run **recorded-first** —
replay on a hit, live only on a miss — so the 28 core items keep the exact
traces the published core-28 numbers were computed from. A plain live pass
would re-record them, and the replay store's index takes the *last* record for
a key, so Monday's artifact and the second-judge cross-check would both stop
being reproducible from the store. Only the 42 new items go over the wire.

``A4-optimizer`` is not in the static ladder: it is registered at run time from
the instruction VAIPO returned. It is rehydrated here out of
``artifacts/results/optimizer_feature_extractor.json`` and re-installed through
the same :func:`~amw.tuning.optimizer.installed_rung` seam the live run used,
so this scores the same instruction rather than a re-optimized one.

CONTAMINATION, and why this run discloses more than the last one did. The
optimizer's 12 training items miss the core 28 entirely, so at n=28 the rung
was scored on genuinely held-out items. All 12 are inside the full 70. At n=70
the rung is therefore scored on 12 items whose gold answers the optimizer saw —
17% of the split. That is disclosed, not corrected: dropping them would give
this one rung a different denominator from every other rung, which is worse
than a labelled overlap. ``installed_rung`` now hands the ladder the whole
training set rather than a pre-computed intersection, so the overlap shows up
in ``RungRecord.leaked_example_items`` and in the artifact notes. Read
A4-optimizer's n=70 score with that in mind, and read the n=28 score — which is
clean — beside it.

    .venv/bin/python scripts/widen_fe_rungs.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv ships in requirements.txt
    pass
else:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

from amw.adapters import AdapterRouter  # noqa: E402
from amw.adapters.base import ModelAdapter  # noqa: E402
from amw.config import load_all  # noqa: E402
from amw.eval.judge import JUDGE_ROLE, Judge  # noqa: E402
from amw.traces.store import ReplayStore  # noqa: E402
from amw.tuning.ablate import format_rung, run_ladder  # noqa: E402
from amw.tuning.optimizer import (  # noqa: E402
    OptimizerResult,
    default_result_path,
    installed_rung,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from widen_judge import RecordedFirstAdapter  # noqa: E402

SUBAGENT = "feature_extractor"
STATIC_RUNGS = ("A0-schema", "A4-novelty-tool", "A4-novelty-schema")
SPLIT = "all"


class RecordedFirstRouter(AdapterRouter):
    """:class:`AdapterRouter`, but every model resolves recorded-first.

    Same one-shot-recorder rationale as ``widen_judge.RecordedFirstAdapter``:
    it never reaches the eval path, and ``amw/adapters/__init__.py`` stays the
    only place ``live|replay|hybrid`` is resolved.
    """

    def __init__(self, *, models, store: ReplayStore) -> None:
        super().__init__("live", models=models, store=store)
        self.recorded_first: dict[str, RecordedFirstAdapter] = {}

    def for_model(self, model: str) -> ModelAdapter:
        if model not in self.recorded_first:
            self.recorded_first[model] = RecordedFirstAdapter(
                model, models=self.models, store=self.store
            )
            self._adapters[model] = self.recorded_first[model]
        return self.recorded_first[model]

    @property
    def hits(self) -> int:
        return sum(a.hits for a in self.recorded_first.values())

    @property
    def misses(self) -> int:
        return sum(a.misses for a in self.recorded_first.values())


def _generate(rungs, cfg, store) -> RecordedFirstRouter:
    router = RecordedFirstRouter(models=cfg.models, store=store)
    run_ladder(
        SUBAGENT,
        mode="live",
        config=cfg,
        rungs=rungs,
        split=SPLIT,
        router=router,
        run_judge=False,
        write=False,
    )
    return router


def _judge(rungs, cfg, store):
    model_key, _spec = cfg.models.for_role(JUDGE_ROLE)
    adapter = RecordedFirstAdapter(model_key, models=cfg.models, store=store)
    judge = Judge(mode="live", models=cfg.models, adapter=adapter)
    result = run_ladder(
        SUBAGENT,
        mode="replay",
        config=cfg,
        rungs=rungs,
        split=SPLIT,
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
    return result


def main() -> int:
    cfg = load_all(customer="demo_patents")
    store = ReplayStore()

    print(f"=== PHASE 1/4  generations for {', '.join(STATIC_RUNGS)} at n=70 ===",
          flush=True)
    router = _generate(STATIC_RUNGS, cfg, store)
    print(f"generations: {router.misses} live, {router.hits} from recordings.\n",
          flush=True)

    print("=== PHASE 2/4  judge them ===", flush=True)
    _judge(STATIC_RUNGS, cfg, store)

    artifact = default_result_path(SUBAGENT)
    result = OptimizerResult.model_validate_json(artifact.read_text(encoding="utf-8"))
    target, run = result.selection.target, result.run
    if target is None or not run.delivered:
        print(
            f"\nA4-optimizer skipped: {artifact} has status {run.status!r}. "
            f"Nothing was optimized, so there is no instruction to re-run and "
            f"no number is reported for it at n=70.",
            flush=True,
        )
        return 0

    print("\n=== PHASE 3/4  generations for A4-optimizer at n=70 ===", flush=True)
    print(f"re-installing the instruction from run {run.run_id} "
          f"({run.instruction_path})", flush=True)
    with installed_rung(target, run) as rung:
        router = _generate([rung.rung], cfg, store)
        print(f"generations: {router.misses} live, {router.hits} from recordings.\n",
              flush=True)
        print("=== PHASE 4/4  judge A4-optimizer ===", flush=True)
        _judge([rung.rung], cfg, store)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
