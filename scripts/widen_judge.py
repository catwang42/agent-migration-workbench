"""Top up the judge recordings so a subagent can be judged on the full corpus.

Sizing deviation #2 (``notes/sizing_deviation_2.md``) widens Query Rewriter and
Chunk Summarizer from the 28-item core split to the full 70. The generations
for all 70 were recorded on 2026-08-09/10; only the judge calls for the 42
previously-unjudged items are missing.

The obvious way to get them — run ``phase2 --mode live --judge-all`` — is wrong
here. The replay store's index takes the *last* record for a key
(``amw/traces/store.py``), so a live pass would re-call and supersede the 28
core items that Monday's artifact and the second-judge cross-check were both
built on. Their numbers would stop being reproducible from the store, which is
a worse thing to lose than a few hundred judge calls are to save.

So the judge here runs on a **recorded-first** adapter: replay on a hit, live
only on a miss. Nothing already on disk is touched, and the widened artifact
itself is assembled afterwards by the ordinary CLI in ``--mode replay``.

This is a one-shot recorder, not a fourth mode: it never reaches the eval path,
and ``amw/adapters/__init__.py`` remains the only place ``live|replay|hybrid``
is resolved (CLAUDE.md conventions).

    .venv/bin/python scripts/widen_judge.py query_rewriter chunk_summarizer
"""

from __future__ import annotations

import sys
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

from amw.adapters import AdapterRouter, resolve  # noqa: E402
from amw.adapters.base import ModelAdapter, ModelRequest  # noqa: E402
from amw.config import load_all  # noqa: E402
from amw.eval.judge import JUDGE_ROLE, Judge  # noqa: E402
from amw.eval.runner import run_phase2  # noqa: E402
from amw.traces.schema import Trace  # noqa: E402
from amw.traces.store import ReplayMissError, ReplayStore  # noqa: E402


class RecordedFirstAdapter(ModelAdapter):
    """Serve a recorded call if there is one; otherwise make it live.

    Deliberately *not* registered as a mode. A cache-first adapter is the right
    tool for topping up a corpus and the wrong tool for an eval run, where
    "some of these numbers are from today and some from Sunday" is precisely
    the ambiguity the three-mode table exists to prevent.
    """

    name = "recorded_first"
    mode = "live"

    def __init__(self, model_key: str, *, models, store: ReplayStore) -> None:
        self.store = store
        self.live = resolve(model_key, "live", models=models, store=store)
        self.hits = 0
        self.misses = 0

    def complete(self, request: ModelRequest) -> Trace:
        try:
            recorded = self.store.get(*request.replay_key)
        except ReplayMissError:
            self.misses += 1
            if self.misses % 25 == 0:
                print(
                    f"  ... {self.misses} live judge calls, {self.hits} replayed",
                    flush=True,
                )
            return self.live.complete(request)
        self.hits += 1
        return recorded.model_copy(deep=True)


def main(subagents: tuple[str, ...]) -> int:
    cfg = load_all(customer="demo_patents")
    store = ReplayStore()
    model_key, _spec = cfg.models.for_role(JUDGE_ROLE)

    adapter = RecordedFirstAdapter(model_key, models=cfg.models, store=store)
    judge = Judge(mode="live", models=cfg.models, adapter=adapter)

    print(f"widening {', '.join(subagents)} to the full corpus", flush=True)
    print(f"judge   : {model_key} (recorded-first; live only on a miss)", flush=True)
    print("generations: replayed, never re-run\n", flush=True)

    # write=False: this pass exists to fill the recording gap. The artifact a
    # human reads is assembled afterwards in --mode replay, so that it dates
    # itself by the calls it served rather than by this top-up.
    run_phase2(
        config=cfg,
        mode="replay",
        router=AdapterRouter(mode="replay", models=cfg.models),
        judge=judge,
        subagents=subagents,
        judge_all=subagents,
        write=False,
    )

    print(
        f"\ntop-up complete: {adapter.misses} judge calls made live, "
        f"{adapter.hits} served from existing recordings.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    names = tuple(sys.argv[1:]) or ("query_rewriter", "chunk_summarizer")
    raise SystemExit(main(names))
