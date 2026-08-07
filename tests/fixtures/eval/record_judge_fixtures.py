#!/usr/bin/env python3
"""Record the judge fixtures with REAL judge calls. Human-run, live only.

    source .venv/bin/activate
    PYTHONPATH=. python tests/fixtures/eval/record_judge_fixtures.py

``tests/test_judge.py`` replays what this writes, so the judge's offline test
suite is exercised against genuine model output rather than against a
hand-authored idea of what a judge might say (CLAUDE.md ground rule 1). Nothing
in the automated suite calls this script; it needs credentials and it costs
money.

Rerun it whenever any of these change, because all of them feed the replay key
``(subagent, model, input_sha)``:

* ``amw/eval/judge_prompts/<version>/`` — system text or user template;
* ``tests/fixtures/eval/judge_cases.json`` — rubrics, or which trace is judged;
* the rendering in ``amw.eval.judge.Judge.build_request``.

Output goes to ``tests/fixtures/eval/replay/judge_<subagent>.jsonl``, never to
``artifacts/replay/``: the workshop corpus is what the customer's numbers come
from, and a test fixture must not be able to end up in it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from tests.fixtures.eval.judge_fixture import (  # noqa: E402
    JUDGE_REPLAY_DIR,
    all_requests,
    fixture_store,
)


def main() -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:  # pragma: no cover - dotenv is in requirements
        pass

    if not os.environ.get("PROJECT_ID"):
        print(
            "PROJECT_ID is unset — this script makes real calls and cannot run "
            "offline. Set it in .env.",
            file=sys.stderr,
        )
        return 2

    from amw.eval.judge import Judge

    JUDGE_REPLAY_DIR.mkdir(parents=True, exist_ok=True)
    store = fixture_store()
    # mode="live" -> resolve() wraps the adapter in RecordingAdapter, so every
    # call lands in the store. Recording is not optional (ground rule 5); the
    # only thing chosen here is which store.
    judge = Judge(mode="live", store=store)
    print(f"judge: {judge.describe()}")
    print(f"recording to: {JUDGE_REPLAY_DIR}")

    requests = all_requests()
    failures = 0
    for request in requests:
        verdict = judge.score(request)
        label = f"{request.item_id} r{request.repeat}/{request.repeats}"
        if verdict.status == "ok":
            failed = verdict.failed_criteria
            print(
                f"  ok   {label}: score={verdict.score:.3f} "
                f"failed={failed or '-'}"
            )
        else:
            failures += 1
            print(f"  FAIL {label}: {verdict.error}")

    print(f"\n{len(requests)} judge call(s); {failures} did not produce a score.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
