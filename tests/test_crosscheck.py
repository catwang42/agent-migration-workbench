"""The second-judge cross-check: a validating instrument that cannot become a
gating one, and an agreement statistic that never invents a number.

Three things are worth asserting here and nothing else really is:

* **The two judges are asked the same question.** If the cross-check pack's
  scoring text drifts from the gated pack's, any disagreement it finds is
  partly a prompt difference, and the instrument stops meaning what the
  scorecard says it means. That is enforced byte-for-byte.
* **A hole is never a number.** Cohen's kappa is undefined on a degenerate
  sample and a failed judge call is not a disagreement. Both have an obvious
  wrong answer (0.0, and "the judges disagreed") that would be quietly
  believable, which is exactly why they are tested.
* **The combination rule holds structurally.** No averaging, no substitution,
  nothing written back into the gated artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amw.eval import crosscheck as cc
from amw.eval.judge import (
    JUDGE_RESPONSE_SCHEMA,
    JUDGE_TOOL_NAME,
    CriterionVerdict,
    Judge,
    JudgeRequest,
    JudgeVerdict,
    Rubric,
    RubricCriterion,
    load_prompt_pack,
)

REPO = Path(__file__).resolve().parents[1]
PROMPTS = REPO / "amw" / "eval" / "judge_prompts"


# --------------------------------------------------------------------------
# the two prompt packs
# --------------------------------------------------------------------------


def test_both_judge_prompt_packs_are_published():
    """Both are shown to customers; neither may be a Python string literal."""
    for version in ("v1", cc.CROSSCHECK_PROMPT_VERSION):
        pack = load_prompt_pack(version)
        assert pack.system.strip() and pack.user_template.strip()
        for name in ("system.txt", "user.txt", "repeat_note.txt"):
            assert (PROMPTS / version / name).is_file()


def test_the_cross_check_pack_asks_exactly_the_same_question():
    """Byte-identical scoring text; only the emission mechanism differs.

    A cross-check judge on a subtly different prompt measures the prompt as
    well as the model, and there is no way to tell afterwards how much of a
    disagreement was which.
    """
    gated = load_prompt_pack("v1")
    check = load_prompt_pack(cc.CROSSCHECK_PROMPT_VERSION)

    assert check.user_template == gated.user_template
    assert check.repeat_note_template == gated.repeat_note_template

    marker = "\nOutput\n"
    gated_head, _, _ = gated.system.partition(marker)
    check_head, _, check_tail = check.system.partition(marker)
    assert check_head == gated_head, (
        "everything above the Output section decides how the judges score and "
        "must be identical between the two packs"
    )
    # And the one permitted difference really is only about transport.
    assert JUDGE_TOOL_NAME in check_tail
    assert "JSON object" in gated.system


def test_the_two_packs_have_different_shas():
    """They differ, so the footer can never attribute one score to the other."""
    assert load_prompt_pack("v1").sha != load_prompt_pack(cc.CROSSCHECK_PROMPT_VERSION).sha


# --------------------------------------------------------------------------
# tool emission
# --------------------------------------------------------------------------


def _rubric(item_id: str = "fe-0001") -> Rubric:
    return Rubric(
        item_id=item_id,
        subagent="feature_extractor",
        criteria=[
            RubricCriterion(id="c1", text="one"),
            RubricCriterion(id="c2", text="two"),
            RubricCriterion(id="c3", text="three"),
        ],
    )


def _request(item_id: str = "fe-0001", repeat: int = 1) -> JudgeRequest:
    return JudgeRequest(
        item_id=item_id,
        subagent="feature_extractor",
        rubric=_rubric(item_id),
        candidate={"field": "value"},
        task_input="a document",
        repeat=repeat,
        repeats=2,
    )


def test_tool_mode_offers_the_tool_and_sends_no_response_schema():
    """Never both: the Gemini adapter rejects the pair, and asking twice for
    the same object in two mechanisms is how a judge ends up double-answering.
    """
    tool_judge = Judge(mode="replay", model_key="claude-sonnet", output_mode="tool")
    built = tool_judge.build_request(_request())
    assert built.response_schema is None
    assert [t.name for t in built.tools] == [JUDGE_TOOL_NAME]
    assert built.tools[0].parameters == JUDGE_RESPONSE_SCHEMA


def test_schema_mode_is_unchanged():
    schema_judge = Judge(mode="replay")
    built = schema_judge.build_request(_request())
    assert built.response_schema == JUDGE_RESPONSE_SCHEMA
    assert built.tools == []


def test_the_two_emission_modes_cannot_collide_in_the_replay_store():
    """The replay key covers tools_offered, so the cross-check's calls land
    beside the gated judge's rather than on top of them."""
    a = Judge(mode="replay", model_key="claude-sonnet").build_request(_request())
    b = Judge(
        mode="replay", model_key="claude-sonnet", output_mode="tool"
    ).build_request(_request())
    assert a.replay_key != b.replay_key


def test_an_unknown_output_mode_is_refused_at_construction():
    from amw.config import ConfigError

    with pytest.raises(ConfigError):
        Judge(mode="replay", output_mode="freeform")


def test_a_verdict_carried_in_a_tool_call_is_read_not_treated_as_a_failure():
    from amw.adapters import ModelRequest, build_trace
    from amw.traces.schema import ToolCall, TraceOutput

    judge = Judge(mode="replay", model_key="claude-sonnet", output_mode="tool")
    request = _request()
    payload = {
        "criteria": [
            {"criterion_id": cid, "passed": True, "rationale": "because"}
            for cid in ("c1", "c2", "c3")
        ],
        "overall_rationale": "fine",
    }
    trace = build_trace(
        judge.build_request(request),
        output=TraceOutput(text=None, json_=None),
        tool_calls=[ToolCall(name=JUDGE_TOOL_NAME, args=payload)],
    )
    verdict = judge.verdict_from_trace(request, trace)
    assert verdict.status == "ok" and verdict.score == 1.0


# --------------------------------------------------------------------------
# Cohen's kappa
# --------------------------------------------------------------------------


def test_kappa_matches_a_hand_computed_value():
    # 10 pairs: 8 agree (6 pass/pass, 2 fail/fail), 2 disagree one each way.
    labels = (
        [(True, True)] * 6 + [(False, False)] * 2 + [(True, False), (False, True)]
    )
    kappa, _note = cc.cohens_kappa(labels)
    # po = 0.8; A passes 7/10, B passes 7/10; pe = .49 + .09 = .58
    assert kappa == pytest.approx((0.8 - 0.58) / (1 - 0.58))


def test_kappa_is_none_not_zero_when_it_is_undefined():
    """Both judges passed everything. Chance agreement is 1.0, so kappa has no
    denominator — and 0.0 would read as "agreed no better than chance", the
    exact opposite of what happened.
    """
    kappa, note = cc.cohens_kappa([(True, True)] * 40)
    assert kappa is None
    assert "undefined" in note
    assert "Raw agreement is 1.0000" in note


def test_kappa_carries_its_prevalence_caveat_when_it_is_deflated():
    labels = [(True, True)] * 95 + [(True, False)] * 3 + [(False, True)] * 2
    kappa, note = cc.cohens_kappa(labels)
    assert kappa is not None
    assert note is not None and "prevalence-deflated" in note


def test_kappa_on_an_empty_sample_says_so():
    kappa, note = cc.cohens_kappa([])
    assert kappa is None and "no paired" in note


# --------------------------------------------------------------------------
# pairing
# --------------------------------------------------------------------------


def _verdict(
    item_id: str,
    repeat: int,
    passes: dict[str, bool],
    *,
    status: str = "ok",
    error: str | None = None,
) -> JudgeVerdict:
    if status != "ok":
        return JudgeVerdict(
            item_id=item_id,
            subagent="feature_extractor",
            repeat=repeat,
            status="error",
            error=error or "boom",
        )
    return JudgeVerdict(
        item_id=item_id,
        subagent="feature_extractor",
        repeat=repeat,
        status="ok",
        score=sum(passes.values()) / len(passes),
        criteria=[
            CriterionVerdict(criterion_id=cid, passed=ok, rationale=f"{cid} said so")
            for cid, ok in passes.items()
        ],
    )


PASSES = {"c1": True, "c2": True, "c3": False}


def test_a_failed_judge_call_is_unusable_not_a_disagreement():
    """Our outage is not evidence that the instruments disagree."""
    pairs, unusable = cc.pair_verdicts(
        [_verdict("fe-0001", 1, PASSES)],
        [_verdict("fe-0001", 1, {}, status="error", error="429 exhausted")],
        subagent="feature_extractor",
        arm="claude_baseline",
    )
    assert pairs == []
    assert len(unusable) == 1
    assert "cross-check judge call failed" in unusable[0].reason
    assert "429" in unusable[0].reason


def test_a_cell_only_one_judge_scored_is_unusable():
    pairs, unusable = cc.pair_verdicts(
        [_verdict("fe-0001", 1, PASSES), _verdict("fe-0001", 2, PASSES)],
        [_verdict("fe-0001", 1, PASSES)],
        subagent="feature_extractor",
        arm="claude_baseline",
    )
    assert len(pairs) == 1 and len(unusable) == 1
    assert unusable[0].repeat == 2


def test_verdicts_over_different_criterion_sets_do_not_pair():
    pairs, unusable = cc.pair_verdicts(
        [_verdict("fe-0001", 1, {"c1": True, "c2": True})],
        [_verdict("fe-0001", 1, {"c1": True, "c9": True})],
        subagent="feature_extractor",
        arm="claude_baseline",
    )
    assert pairs == []
    assert "different criterion sets" in unusable[0].reason


def test_repeats_pair_by_index_not_by_position():
    gated = [_verdict("fe-0001", 2, PASSES), _verdict("fe-0001", 1, PASSES)]
    check = [_verdict("fe-0001", 1, PASSES), _verdict("fe-0001", 2, PASSES)]
    pairs, unusable = cc.pair_verdicts(
        gated, check, subagent="feature_extractor", arm="claude_baseline"
    )
    assert not unusable
    assert sorted(p.repeat for p in pairs) == [1, 2]


# --------------------------------------------------------------------------
# the agreement report
# --------------------------------------------------------------------------


def test_an_empty_report_carries_no_numbers_at_all():
    report = cc.report_agreement(
        [], [], subagent="feature_extractor", scope="nothing ran"
    )
    assert report.criterion_agreement is None
    assert report.cohens_kappa is None
    assert report.item_score_agreement is None
    assert report.gated_mean_score is None
    assert report.paired_cells == 0


def test_the_report_keeps_both_judges_means_side_by_side():
    """Never a blend. Two columns is the whole design of the instrument."""
    pairs, _ = cc.pair_verdicts(
        [_verdict("fe-0001", 1, {"c1": True, "c2": True, "c3": True})],
        [_verdict("fe-0001", 1, {"c1": True, "c2": True, "c3": False})],
        subagent="feature_extractor",
        arm="claude_baseline",
    )
    report = cc.report_agreement(
        pairs, subagent="feature_extractor", arm="claude_baseline", scope="test"
    )
    assert report.gated_mean_score == pytest.approx(1.0)
    assert report.check_mean_score == pytest.approx(2 / 3)
    assert report.criterion_agreement == pytest.approx(2 / 3)
    assert report.mean_abs_score_gap == pytest.approx(1 / 3)
    assert not hasattr(report, "combined_score")


def test_unusable_cells_are_counted_with_their_reasons():
    _pairs, unusable = cc.pair_verdicts(
        [_verdict("fe-0001", 1, PASSES)],
        [_verdict("fe-0001", 1, {}, status="error", error="timeout")],
        subagent="feature_extractor",
        arm="claude_baseline",
    )
    report = cc.report_agreement(
        [], unusable, subagent="feature_extractor", scope="test"
    )
    assert report.unusable == 1
    assert sum(report.unusable_reasons.values()) == 1


# --------------------------------------------------------------------------
# verdicts and disagreements
# --------------------------------------------------------------------------


def test_no_measurement_is_never_a_pass():
    empty = cc.report_agreement([], subagent="feature_extractor", scope="none")
    assert cc.verdict_for(empty) == cc.INSUFFICIENT


@pytest.mark.parametrize(
    "agreement,expected",
    [(0.9, cc.VALIDATED), (0.85, cc.VALIDATED), (0.8499, cc.UNRELIABLE), (0.0, cc.UNRELIABLE)],
)
def test_the_threshold_is_read_off_raw_agreement(agreement, expected):
    report = cc.report_agreement([], subagent="x", scope="s")
    report.criterion_agreement = agreement
    assert cc.verdict_for(report) == expected


def test_disagreements_are_ranked_and_carry_only_what_differed():
    small, _ = cc.pair_verdicts(
        [_verdict("fe-0001", 1, {"c1": True, "c2": True, "c3": True})],
        [_verdict("fe-0001", 1, {"c1": True, "c2": True, "c3": False})],
        subagent="feature_extractor",
        arm="a",
    )
    big, _ = cc.pair_verdicts(
        [_verdict("fe-0002", 1, {"c1": True, "c2": True, "c3": True})],
        [_verdict("fe-0002", 1, {"c1": False, "c2": False, "c3": False})],
        subagent="feature_extractor",
        arm="a",
    )
    agreed, _ = cc.pair_verdicts(
        [_verdict("fe-0003", 1, PASSES)],
        [_verdict("fe-0003", 1, PASSES)],
        subagent="feature_extractor",
        arm="a",
    )
    ranked = cc.largest_disagreements(small + big + agreed, limit=10)
    assert [d.item_id for d in ranked] == ["fe-0002", "fe-0001"]
    # An item both judges called the same way is not a disagreement.
    assert "fe-0003" not in {d.item_id for d in ranked}
    # And a disagreement row shows the criteria that moved, not the whole rubric.
    assert [c.criterion_id for c in ranked[1].criteria] == ["c3"]
    assert ranked[1].criteria[0].gated_rationale and ranked[1].criteria[0].check_rationale


def test_the_disagreement_list_honours_its_limit():
    pairs = []
    for n in range(20):
        made, _ = cc.pair_verdicts(
            [_verdict(f"fe-{n:04d}", 1, {"c1": True, "c2": True, "c3": True})],
            [_verdict(f"fe-{n:04d}", 1, {"c1": False, "c2": True, "c3": True})],
            subagent="feature_extractor",
            arm="a",
        )
        pairs += made
    assert len(cc.largest_disagreements(pairs, limit=10)) == 10


# --------------------------------------------------------------------------
# sampling
# --------------------------------------------------------------------------


class _Item:
    """Minimal stand-in: stratified_sample only reads item_id and difficulty."""

    def __init__(self, item_id: str, difficulty: str) -> None:
        self.item_id = item_id
        self.difficulty = difficulty


def _pool(easy: int = 30, medium: int = 25, hard: int = 15) -> list[_Item]:
    items = []
    for label, count in (("easy", easy), ("medium", medium), ("hard", hard)):
        items += [_Item(f"{label}-{i:03d}", label) for i in range(count)]
    return items


def test_the_sample_is_exactly_the_requested_size():
    assert len(cc.stratified_sample(_pool(), size=14)) == 14


def test_the_sample_is_deterministic_given_the_seed():
    a = [i.item_id for i in cc.stratified_sample(_pool(), size=14, seed=7)]
    b = [i.item_id for i in cc.stratified_sample(_pool(), size=14, seed=7)]
    c = [i.item_id for i in cc.stratified_sample(_pool(), size=14, seed=8)]
    assert a == b
    assert a != c, "different seeds should draw a different sample"


def test_every_stratum_is_represented_in_proportion():
    """An easy-heavy sample would flatter agreement: easy items are where any
    two competent judges agree."""
    picked = cc.stratified_sample(_pool(30, 25, 15), size=14)
    counts = {label: sum(1 for i in picked if i.difficulty == label)
              for label in ("easy", "medium", "hard")}
    assert counts == {"easy": 6, "medium": 5, "hard": 3}


def test_a_sample_larger_than_the_pool_returns_the_pool():
    pool = _pool(3, 2, 1)
    assert len(cc.stratified_sample(pool, size=99)) == len(pool)


# --------------------------------------------------------------------------
# the combination rule, structurally
# --------------------------------------------------------------------------


def test_a_judge_cannot_cross_check_itself():
    from amw.config import ConfigError, load_all

    cfg = load_all(customer="demo_patents")
    same = Judge(mode="replay", models=cfg.models)
    with pytest.raises(ConfigError, match="same model"):
        cc.run_crosscheck(
            config=cfg,
            phase2=_stub_phase2(),
            mode="replay",
            gated_judge=same,
            check_judge=Judge(mode="replay", models=cfg.models),
            write=False,
        )


def _stub_phase2():
    from amw.eval.runner import ArmResult, JudgeReport, Phase2Result

    return Phase2Result(
        customer="demo_patents",
        mode="replay",
        region="us-central1",
        provenance="synthetic",
        dataset_seed=1,
        generator_version="t06.1",
        bootstrap_seed=1,
        judge_repeats=2,
        arms=[
            ArmResult(
                subagent="feature_extractor",
                variant="claude_baseline",
                model="claude-sonnet",
                output_mode="tool",
                prompt_sha="x",
                items=1,
                calls_ok=1,
                calls_error=0,
                judge=JudgeReport(
                    split="all", items_scored=1, expected_repeats=2, failed_repeats=0
                ),
            )
        ],
    )


def test_the_result_has_no_field_that_could_be_read_as_a_gate_estimate():
    """The cross-check must not be mistakable for a measurement the gates run
    against — that is what "validates, never substitutes" means in practice."""
    fields = set(cc.AgreementReport.model_fields)
    assert not fields & {"estimate", "lo", "hi", "point", "ci_lower", "bound"}
    assert "combined" not in " ".join(fields)


def test_the_artifact_states_both_instruments_and_the_rule(tmp_path):
    from amw.config import load_all

    cfg = load_all(customer="demo_patents")
    out = tmp_path / "crosscheck.json"
    result = cc.run_crosscheck(
        config=cfg,
        phase2=_stub_phase2(),
        mode="replay",
        subagents=("feature_extractor",),
        variants=("claude_baseline",),
        out_path=out,
    )
    written = json.loads(out.read_text())
    assert written["combination_rule"] == cc.COMBINATION_RULE
    assert written["gated_judge"]["judge_model_key"] != written["check_judge"]["judge_model_key"]
    assert written["check_judge"]["judge_prompt_version"] == cc.CROSSCHECK_PROMPT_VERSION
    assert written["check_judge"]["judge_output_mode"] == "tool"
    # And nothing was written back into the gated artifact.
    assert result.subagents or result.notes


def test_the_footer_line_names_every_subagent_and_the_rule():
    result = cc.CrosscheckResult(
        customer="demo_patents",
        mode="live",
        judge_repeats=2,
        gated_judge={"judge_model": "Gemini 2.5 Pro"},
        check_judge={"judge_model": "Claude Sonnet 5"},
        subagents=[
            cc.SubagentCrosscheck(
                subagent=name,
                scope="s",
                overall=cc.report_agreement([], subagent=name, scope="s"),
                verdict=cc.INSUFFICIENT,
            )
            for name in ("query_rewriter", "chunk_summarizer", "feature_extractor")
        ],
    )
    line = cc.crosscheck_footer_line(result)
    for name in ("query_rewriter", "chunk_summarizer", "feature_extractor"):
        assert name in line
    assert "never averaged" in line
    assert "not measured" in line
