"""Shared loader for the T08 judge fixtures.

Imported by both ``tests/test_judge.py`` and
``tests/fixtures/eval/record_judge_fixtures.py`` so the requests that were
recorded and the requests that are replayed are built by the same code. If they
drifted apart the replay key would change and every fixture would miss.
"""

from __future__ import annotations

import json
from pathlib import Path

from amw.eval.judge import JudgeRequest, Rubric, RubricCriterion
from amw.traces.schema import Trace
from amw.traces.store import ReplayStore

FIXTURE_DIR = Path(__file__).resolve().parent
REPO_ROOT = FIXTURE_DIR.parents[2]

#: Where the recorded judge calls live. Deliberately *not* artifacts/replay/:
#: that corpus belongs to the workshop run, and test fixtures must not be able
#: to contaminate the numbers a customer sees.
JUDGE_REPLAY_DIR = FIXTURE_DIR / "replay"

#: The live subagent corpus these fixtures judge. Read-only here.
SUBAGENT_REPLAY_DIR = REPO_ROOT / "artifacts" / "replay"

CASES_PATH = FIXTURE_DIR / "judge_cases.json"


def load_cases() -> list[dict]:
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return data["cases"]


def source_trace(case: dict) -> Trace:
    """The real recorded subagent call this case judges."""
    path = SUBAGENT_REPLAY_DIR / f"{case['subagent']}.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        trace = Trace.from_jsonl_line(line)
        if trace.trace_id == case["source_trace_id"]:
            return trace
    raise LookupError(
        f"{path} has no trace {case['source_trace_id']!r}; the fixture points at "
        "a recording that is no longer in the corpus"
    )


def rubric_for(case: dict) -> Rubric:
    return Rubric(
        item_id=case["item_id"],
        subagent=case["subagent"],
        criteria=[RubricCriterion(**c) for c in case["rubric"]],
    )


def judge_requests(case: dict) -> list[JudgeRequest]:
    """Every repeat of one case, in order."""
    trace = source_trace(case)
    candidate = trace.output.json_ if trace.output.json_ is not None else trace.output.text
    repeats = int(case.get("repeats", 1))
    return [
        JudgeRequest(
            item_id=case["item_id"],
            subagent=case["subagent"],
            rubric=rubric_for(case),
            candidate=candidate,
            task_input=list(trace.input.messages),
            context_chunks=list(trace.input.context_chunks),
            reference=case.get("reference"),
            repeat=repeat,
            repeats=repeats,
            arm=case.get("arm"),
        )
        for repeat in range(1, repeats + 1)
    ]


def all_requests() -> list[JudgeRequest]:
    return [request for case in load_cases() for request in judge_requests(case)]


def fixture_store() -> ReplayStore:
    return ReplayStore(root=JUDGE_REPLAY_DIR)
