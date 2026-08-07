"""Judge tests — replay-only, zero credentials.

The judge responses replayed here are **real** ``gemini-pro`` calls, recorded by
``tests/fixtures/eval/record_judge_fixtures.py`` against real recorded subagent
outputs in ``artifacts/replay/``. Nothing in this file hand-writes what a judge
"would have said": a hand-authored judge response would make these tests a
tautology, and CLAUDE.md ground rule 1 says numbers come from executed calls.

The traces that are hand-built are the *failure* traces — a judge that errored,
a judge that returned prose, a judge that scored the wrong criteria. Those are
malformed inputs to the parser, not results, so building them is legitimate and
provoking them live is not practical.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from amw.config import ConfigError, load_all
from amw.eval import judge as J
from amw.eval.stats import aggregate_repeats
from amw.traces.schema import Trace, TraceInput, TraceOutput, sha256_text
from amw.traces.store import ReplayMissError, ReplayStore
from tests.fixtures.eval.judge_fixture import (
    all_requests,
    fixture_store,
    judge_requests,
    load_cases,
    rubric_for,
)

CASES = {case["item_id"]: case for case in load_cases()}


@pytest.fixture(scope="module")
def replay_judge() -> J.Judge:
    """A judge wired to the fixture corpus. No credentials, no network."""
    return J.Judge(mode="replay", store=fixture_store())


def request_for(item_id: str, repeat: int = 1) -> J.JudgeRequest:
    requests = judge_requests(CASES[item_id])
    return requests[repeat - 1]


def fake_trace(request: J.JudgeRequest, judge: J.Judge, **overrides) -> Trace:
    """A trace whose key matches ``request``, with a caller-chosen payload."""
    model_request = judge.build_request(request)
    fields = dict(
        trace_id=f"{request.item_id}-fake",
        subagent=model_request.subagent,
        provenance="synthetic",
        ts=datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc),
        model=model_request.model,
        system_prompt_sha=sha256_text(model_request.system_prompt),
        input=TraceInput(messages=list(model_request.messages)),
    )
    fields.update(overrides)
    return Trace(**fields)


# ==========================================================================
# prompt pack
# ==========================================================================


def test_prompts_are_files_on_disk_not_string_literals():
    root = J.prompt_pack_dir()
    assert root.is_dir()
    assert {p.name for p in root.glob("*.txt")} == {
        "system.txt",
        "user.txt",
        "repeat_note.txt",
    }


def test_prompt_pack_loads_and_hashes():
    pack = J.load_prompt_pack()
    assert pack.version == "v1"
    assert pack.system.strip()
    assert len(pack.sha) == 12
    # Stable: the sha travels on every verdict and into the report footer.
    assert J.load_prompt_pack().sha == pack.sha


def test_user_template_consumes_every_declared_field():
    """A placeholder the template forgets means the judge scores blind."""
    pack = J.load_prompt_pack()
    for field in J.USER_TEMPLATE_FIELDS:
        assert f"${field}" in pack.user_template, field


def test_render_user_rejects_an_unfilled_placeholder():
    pack = J.load_prompt_pack()
    with pytest.raises(KeyError):
        pack.render_user(subagent="query_rewriter")


def test_unknown_prompt_version_is_a_config_error():
    with pytest.raises(ConfigError, match="no judge prompt pack"):
        J.load_prompt_pack("v99")


def test_response_schema_stays_in_step_with_the_verdict_model():
    """The schema is written out flat (Gemini will not resolve $ref), so it can
    drift from CriterionVerdict unless something checks."""
    item = J.JUDGE_RESPONSE_SCHEMA["properties"]["criteria"]["items"]
    assert set(item["properties"]) == set(J.CriterionVerdict.model_fields)
    assert set(item["required"]) == set(J.CriterionVerdict.model_fields)
    # No $ref/$defs anywhere: Gemini's response_schema is an OpenAPI-3 subset.
    blob = repr(J.JUDGE_RESPONSE_SCHEMA)
    assert "$ref" not in blob and "$defs" not in blob


# ==========================================================================
# rubric contract
# ==========================================================================


def test_rubric_requires_three_to_five_criteria():
    def build(n: int) -> J.Rubric:
        return J.Rubric(
            item_id="i1",
            subagent="query_rewriter",
            criteria=[J.RubricCriterion(id=f"c{i}", text=f"t{i}") for i in range(n)],
        )

    for n in (3, 4, 5):
        assert len(build(n).criteria) == n
    for n in (0, 2, 6):
        with pytest.raises(ValueError, match="criteria"):
            build(n)


def test_rubric_rejects_duplicate_criterion_ids():
    with pytest.raises(ValueError, match="duplicate criterion ids"):
        J.Rubric(
            item_id="i1",
            subagent="query_rewriter",
            criteria=[
                J.RubricCriterion(id="c1", text="a"),
                J.RubricCriterion(id="c1", text="b"),
                J.RubricCriterion(id="c3", text="c"),
            ],
        )


def test_rubric_renders_bracketed_ids_for_the_prompt():
    rubric = rubric_for(CASES["qr-fx-002"])
    lines = rubric.render().splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("[c1] ")


def test_request_rejects_a_rubric_for_a_different_item():
    with pytest.raises(ValueError, match="rubric is for item"):
        J.JudgeRequest(
            item_id="other",
            subagent="query_rewriter",
            rubric=rubric_for(CASES["qr-fx-002"]),
            candidate={},
        )


# ==========================================================================
# wiring: model from config, replay with no credentials
# ==========================================================================


def test_judge_model_comes_from_the_config_role_not_a_literal(replay_judge):
    models = load_all().models
    expected_key, expected_spec = models.for_role("judge")
    assert replay_judge.model_key == expected_key
    # Pro-class Gemini, per the T08 card — asserted via config, not a literal.
    assert expected_spec.provider == "google"
    assert "Pro" in expected_spec.display_name
    described = replay_judge.describe()
    assert described["judge_model_key"] == expected_key
    assert described["judge_mode"] == "replay"
    assert described["judge_prompt_sha"] == J.load_prompt_pack().sha


def test_no_model_id_is_hardcoded_in_the_judge_module():
    source = (J.__file__ and open(J.__file__, encoding="utf-8").read()) or ""
    for literal in ("gemini-2.5", "claude-3", "claude-sonnet-4", "publishers/"):
        assert literal not in source, literal


def test_judge_calls_land_in_their_own_replay_namespace():
    assert J.judge_subagent_name("query_rewriter") == "judge_query_rewriter"
    request = request_for("qr-fx-001")
    built = J.Judge(mode="replay", store=fixture_store()).build_request(request)
    assert built.subagent == "judge_query_rewriter"
    assert built.item_id == "qr-fx-001-r1"


def test_the_judge_is_not_told_which_arm_it_is_grading():
    """Judge neutrality: an arm label in the prompt would let the judge favour
    a backend. The label is carried on the request for provenance only."""
    request = request_for("qr-fx-001")
    assert request.arm == "gemini_naive"
    built = J.Judge(mode="replay", store=fixture_store()).build_request(request)
    blob = built.system_prompt + "\n".join(built.messages)
    assert "gemini_naive" not in blob
    assert "gemini" not in blob.lower()
    assert "claude" not in blob.lower()


def test_repeats_do_not_collide_on_one_replay_key():
    """input_sha covers prompt + messages + chunks + tools, but not the repeat
    index — so without the repeat note both k=2 passes would key identically
    and the store would serve one recording twice."""
    judge = J.Judge(mode="replay", store=fixture_store())
    r1 = judge.build_request(request_for("qr-fx-002", 1))
    r2 = judge.build_request(request_for("qr-fx-002", 2))
    assert r1.input_sha != r2.input_sha
    assert r1.replay_key != r2.replay_key

    # A single-repeat request carries no repeat note at all.
    single = judge.build_request(request_for("qr-fx-001"))
    assert len(single.messages) == 1
    assert len(r1.messages) == 2


# ==========================================================================
# scoring real recorded calls
# ==========================================================================


@pytest.mark.parametrize(
    "item_id,repeat,expected_score,expected_failures",
    [
        # qr-fx-001: the real judge failed c1 — the rewritten query restates the
        # assignee "Toyota" instead of leaving it to filters.assignees. 4 of 5.
        ("qr-fx-001", 1, 4 / 5, ["c1"]),
        ("cs-fx-001", 1, 1.0, []),
        ("fe-fx-001", 1, 1.0, []),
        ("qr-fx-002", 1, 1.0, []),
        ("qr-fx-002", 2, 1.0, []),
    ],
)
def test_replayed_judge_scores(
    replay_judge, item_id, repeat, expected_score, expected_failures
):
    verdict = replay_judge.score(request_for(item_id, repeat))
    assert verdict.status == "ok"
    assert verdict.score == pytest.approx(expected_score)
    assert verdict.failed_criteria == expected_failures
    assert verdict.repeat == repeat
    assert verdict.item_id == item_id


def test_score_is_the_fraction_of_rubric_criteria_passed(replay_judge):
    """The headline number must decompose into which criteria held."""
    verdict = replay_judge.score(request_for("qr-fx-001"))
    rubric = rubric_for(CASES["qr-fx-001"])
    assert [c.criterion_id for c in verdict.criteria] == rubric.ids
    passed = sum(1 for c in verdict.criteria if c.passed)
    assert verdict.score == passed / len(rubric.criteria)


def test_every_criterion_verdict_carries_a_rationale(replay_judge):
    """T09 groups failures by criterion; a bare boolean cannot be triaged."""
    for request in all_requests():
        verdict = replay_judge.score(request)
        assert verdict.status == "ok", verdict.error
        for criterion in verdict.criteria:
            assert criterion.rationale.strip(), (request.item_id, criterion)
        assert verdict.overall_rationale


def test_verdicts_carry_provenance_for_the_report_footer(replay_judge):
    verdict = replay_judge.score(request_for("cs-fx-001"))
    assert verdict.judge_model == replay_judge.model_key
    assert verdict.prompt_version == "v1"
    assert verdict.prompt_sha == J.load_prompt_pack().sha
    assert verdict.trace_id == "cs-fx-001-r1"
    assert verdict.trace_ts.startswith("2026-")


def test_the_whole_fixture_batch_replays_offline(replay_judge, monkeypatch):
    """Ground rule 4: no credentials, no network. Blow up on any env the live
    path would need, then score every fixture."""
    for var in ("PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    verdicts = replay_judge.score_many(all_requests())
    assert len(verdicts) == 5
    assert all(v.status == "ok" for v in verdicts)


# ==========================================================================
# failure paths — an error, never a zero
# ==========================================================================


def test_a_failed_judge_call_records_an_error_and_no_score(replay_judge):
    request = request_for("qr-fx-001")
    trace = fake_trace(
        request, replay_judge, status="error", error="429 RESOURCE_EXHAUSTED"
    )
    verdict = replay_judge.verdict_from_trace(request, trace)
    assert verdict.status == "error"
    assert verdict.score is None
    assert "429" in verdict.error
    assert verdict.criteria == []


def test_a_verdict_cannot_be_constructed_as_a_failed_zero():
    """The model itself forbids the shape ground rule 1 warns about."""
    with pytest.raises(ValueError, match="must not produce"):
        J.JudgeVerdict(
            item_id="i1", subagent="query_rewriter", status="error",
            score=0.0, error="boom",
        )
    with pytest.raises(ValueError, match="no score"):
        J.JudgeVerdict(item_id="i1", subagent="query_rewriter", status="ok")


def test_prose_instead_of_json_is_an_error(replay_judge):
    request = request_for("qr-fx-001")
    trace = fake_trace(
        request,
        replay_judge,
        output=TraceOutput(text="Overall this looks pretty good to me."),
    )
    verdict = replay_judge.verdict_from_trace(request, trace)
    assert verdict.status == "error"
    assert verdict.score is None
    assert "no JSON object" in verdict.error


def test_json_embedded_in_the_text_field_is_still_parsed(replay_judge):
    """Some adapters return the JSON as text. That is not a failure."""
    request = request_for("qr-fx-002")
    trace = fake_trace(
        request,
        replay_judge,
        output=TraceOutput(
            text=(
                '{"criteria": ['
                '{"criterion_id": "c1", "passed": true, "rationale": "ok"},'
                '{"criterion_id": "c2", "passed": false, "rationale": "missing"},'
                '{"criterion_id": "c3", "passed": true, "rationale": "ok"}'
                '], "overall_rationale": "mostly fine"}'
            )
        ),
    )
    verdict = replay_judge.verdict_from_trace(request, trace)
    assert verdict.status == "ok"
    assert verdict.score == pytest.approx(2 / 3)
    assert verdict.failed_criteria == ["c2"]


def test_a_partial_rubric_is_an_error_not_a_smaller_denominator(replay_judge):
    """Scoring 2 of 3 criteria and calling it 1.0 would quietly make this item
    easier than every other item in the run."""
    request = request_for("qr-fx-002")
    trace = fake_trace(
        request,
        replay_judge,
        output=TraceOutput(
            json={
                "criteria": [
                    {"criterion_id": "c1", "passed": True, "rationale": "ok"},
                    {"criterion_id": "c2", "passed": True, "rationale": "ok"},
                ],
                "overall_rationale": "fine",
            }
        ),
    )
    verdict = replay_judge.verdict_from_trace(request, trace)
    assert verdict.status == "error"
    assert verdict.score is None
    assert "rubric asks for" in verdict.error


def test_a_criterion_the_rubric_never_asked_for_is_an_error(replay_judge):
    request = request_for("qr-fx-002")
    trace = fake_trace(
        request,
        replay_judge,
        output=TraceOutput(
            json={
                "criteria": [
                    {"criterion_id": "c1", "passed": True, "rationale": "ok"},
                    {"criterion_id": "c2", "passed": True, "rationale": "ok"},
                    {"criterion_id": "c9", "passed": True, "rationale": "invented"},
                ],
                "overall_rationale": "fine",
            }
        ),
    )
    assert replay_judge.verdict_from_trace(request, trace).status == "error"


def test_a_score_with_no_reason_is_an_error(replay_judge):
    request = request_for("qr-fx-002")
    trace = fake_trace(
        request,
        replay_judge,
        output=TraceOutput(
            json={
                "criteria": [
                    {"criterion_id": "c1", "passed": True, "rationale": "ok"},
                    {"criterion_id": "c2", "passed": True, "rationale": "   "},
                    {"criterion_id": "c3", "passed": True, "rationale": "ok"},
                ],
                "overall_rationale": "fine",
            }
        ),
    )
    verdict = replay_judge.verdict_from_trace(request, trace)
    assert verdict.status == "error"
    assert "cannot be triaged" in verdict.error


def test_an_empty_criteria_array_is_an_error(replay_judge):
    request = request_for("qr-fx-002")
    trace = fake_trace(
        request,
        replay_judge,
        output=TraceOutput(json={"criteria": [], "overall_rationale": "n/a"}),
    )
    assert replay_judge.verdict_from_trace(request, trace).status == "error"


def test_a_malformed_criterion_entry_is_an_error(replay_judge):
    request = request_for("qr-fx-002")
    trace = fake_trace(
        request,
        replay_judge,
        output=TraceOutput(json={"criteria": ["c1 passed"], "overall_rationale": "x"}),
    )
    verdict = replay_judge.verdict_from_trace(request, trace)
    assert verdict.status == "error"
    assert "unparseable" in verdict.error


def test_a_replay_miss_raises_on_score_but_not_on_score_many(tmp_path):
    """A bare score() should tell you the corpus is missing. A batch should
    lose the items it cannot replay, not the run."""
    empty = J.Judge(mode="replay", store=ReplayStore(root=tmp_path))
    request = request_for("qr-fx-001")

    with pytest.raises(ReplayMissError):
        empty.score(request)

    verdicts = empty.score_many([request])
    assert len(verdicts) == 1
    assert verdicts[0].status == "error"
    assert verdicts[0].score is None
    assert "replay miss" in verdicts[0].error


def test_a_misconfigured_run_is_not_papered_over_as_error_verdicts():
    with pytest.raises(ConfigError):
        J.Judge(mode="replay", model_key="gemini-ultra-9")


# ==========================================================================
# hand-off to stats and to triage
# ==========================================================================


def test_verdicts_to_repeat_scores_orders_by_repeat_and_keeps_none():
    verdicts = [
        J.JudgeVerdict(
            item_id="i1", subagent="query_rewriter", repeat=2, score=0.5,
            criteria=[J.CriterionVerdict(criterion_id="c1", passed=False, rationale="x")],
        ),
        J.JudgeVerdict(
            item_id="i1", subagent="query_rewriter", repeat=1, score=1.0,
            criteria=[J.CriterionVerdict(criterion_id="c1", passed=True, rationale="x")],
        ),
        J.JudgeVerdict(
            item_id="i2", subagent="query_rewriter", repeat=1,
            status="error", error="timeout",
        ),
    ]
    scores = J.verdicts_to_repeat_scores(verdicts)
    assert scores == {"i1": [1.0, 0.5], "i2": [None]}

    agg = aggregate_repeats(scores, expected_k=2)
    assert agg.item_means == {"i1": 0.75}
    assert agg.failed_repeats == 1
    assert "i2" in agg.dropped_items


def test_replayed_repeats_flow_into_the_repeat_aggregate(replay_judge):
    """End to end on real recordings: two genuine judge passes over qr-fx-002."""
    verdicts = replay_judge.score_many(judge_requests(CASES["qr-fx-002"]))
    scores = J.verdicts_to_repeat_scores(verdicts)
    assert scores == {"qr-fx-002": [1.0, 1.0]}
    agg = aggregate_repeats(scores, expected_k=2)
    assert agg.item_means == {"qr-fx-002": 1.0}
    # The two real passes agreed, which is a measurement, not an assumption.
    assert agg.full_agreement_rate == 1.0
    assert agg.mean_within_item_sd == 0.0


def test_cluster_failures_groups_on_the_rubric_tag(replay_judge):
    verdicts = replay_judge.score_many(all_requests())
    rubrics = {case["item_id"]: rubric_for(case) for case in load_cases()}
    clusters = J.cluster_failures(verdicts, rubrics)
    # qr-fx-001 c1 is tagged query_core and is the only real failure in the set.
    assert clusters == {"query_core": ["qr-fx-001"]}


def test_cluster_failures_falls_back_to_the_criterion_id():
    verdict = J.JudgeVerdict(
        item_id="i1", subagent="query_rewriter", score=0.0,
        criteria=[J.CriterionVerdict(criterion_id="c1", passed=False, rationale="x")],
    )
    assert J.cluster_failures([verdict]) == {"c1": ["i1"]}


def test_error_verdicts_are_not_counted_as_model_failures():
    """An outage is our fault; it must not show up in a failure cluster."""
    verdict = J.JudgeVerdict(
        item_id="i1", subagent="query_rewriter", status="error", error="timeout"
    )
    assert J.cluster_failures([verdict]) == {}
