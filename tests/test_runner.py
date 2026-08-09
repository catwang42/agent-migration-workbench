"""T09 — the phase-2 runner, its lane bridges, and the offline e2e gate.

The e2e tests here run the real CLI entry point against the committed fixture
corpus in replay mode. That is the CI gate CLAUDE.md ground rule 4 requires, so
it is worth testing the *command*, not a hand-assembled imitation of it.
"""

from __future__ import annotations

import json
import os

import pytest

import cli
from amw.config import load_all
from amw.datasets.schema import read_items
from amw.eval.metrics import MetricSample
from amw.eval.runner import (
    MetricReport,
    Phase2Result,
    default_dataset_dir,
    prompt_view,
    rubric_of,
    run_phase2,
)
from amw.traces.schema import Trace
from tests.test_metrics import make_trace


def _trace(*, json_=None, text=None) -> Trace:
    """A minimal ok trace. Only `output` matters to the tests that use it."""
    return make_trace("feature_extractor", json_, text=text)


@pytest.fixture(scope="module")
def cfg():
    return load_all(customer="demo_patents")


@pytest.fixture(scope="module")
def items():
    """One item per subagent from the committed e2e corpus."""
    return {
        subagent: list(read_items(cli.E2E_DATASET_DIR / f"{subagent}.jsonl"))
        for subagent in ("query_rewriter", "chunk_summarizer", "feature_extractor")
    }


# --------------------------------------------------------------------------
# the two cross-lane bridges
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subagent,key",
    [
        ("query_rewriter", "question"),
        ("chunk_summarizer", "question"),
        ("feature_extractor", "document"),
    ],
)
def test_prompt_view_uses_the_key_the_pack_declares(items, subagent, key):
    from amw.agents.prompt_packs import PLACEHOLDERS

    view = prompt_view(items[subagent][0])
    assert key in view
    assert set(PLACEHOLDERS[subagent]) <= set(view)


def test_prompt_view_leaks_neither_gold_nor_rubric(items):
    """The single most damaging bug this runner could have.

    An item's gold output and rubric are the answer key. If either reached a
    prompt the arm would be scoring the dataset's ability to hand over the
    answer, not the model's ability to produce it — and every number downstream
    would be worthless while looking excellent.

    Note what is *not* asserted: that gold *values* are absent from the input.
    For the Feature Extractor they are supposed to be there — the task is to
    pull the title and assignee out of a document that states them. The leak
    that matters is the gold *object* and the rubric, which name the expected
    shape and the grading criteria.
    """
    for subagent, corpus in items.items():
        for item in corpus:
            view = prompt_view(item)
            blob = json.dumps(view, sort_keys=True)
            assert set(view) <= {"question", "document", "chunks"}
            assert item.item_id not in blob
            assert item.difficulty not in blob
            assert item.template_id not in blob
            for criterion in item.rubric:
                assert criterion.id not in blob
                assert criterion.criterion not in blob
            # the serialised answer key, in any of the spellings a careless
            # bridge might use
            assert json.dumps(item.gold, sort_keys=True) not in blob
            for key in item.gold:
                assert f'"{key}":' not in blob


def test_prompt_view_keeps_chunks_addressable(items):
    """Citation coverage scores chunk_ids, so the ids have to survive the trip."""
    item = next(i for i in items["chunk_summarizer"] if i.input.chunks)
    view = prompt_view(item)
    assert [c["chunk_id"] for c in view["chunks"]] == item.input.chunk_ids


def test_rubric_of_renames_criterion_to_text_and_keeps_ids(items):
    item = items["query_rewriter"][0]
    rubric = rubric_of(item)
    assert rubric.item_id == item.item_id
    assert [c.id for c in rubric.criteria] == [c.id for c in item.rubric]
    assert [c.text for c in rubric.criteria] == [c.criterion for c in item.rubric]
    assert all(c.tag is None for c in rubric.criteria)


def test_rerouted_fe_fields_get_a_judge_criterion(items):
    """technical_field and novelty_statement left exact match on 2026-08-07.
    They have to be scored somewhere or a fabricated one goes uncaught."""
    from amw.eval.metrics import FE_JUDGED_FIELDS

    saw = set()
    for item in items["feature_extractor"]:
        criteria = {c.id: c for c in rubric_of(item).criteria}
        for field in FE_JUDGED_FIELDS:
            key = f"{field}_correct"
            if item.gold.get(field) is None:
                assert key not in criteria, "nothing to be right about"
                continue
            saw.add(field)
            assert criteria[key].tag == "fe_field_label"
            # the reference has to reach the judge, and the judge has to be
            # told that different words for the same thing are correct
            assert str(item.gold[field]) in criteria[key].text
            assert "CORRECT" in criteria[key].text
    assert saw == set(FE_JUDGED_FIELDS), "the corpus never exercised both fields"


def test_judge_candidate_unwraps_the_trace_container():
    """`TraceOutput` is a container; the judge renders what it is handed.

    Handing it the container raised `TypeError: Object of type TraceOutput is
    not JSON serializable` on the first judge call of the first live run. The
    e2e gate missed it because that gate runs with `run_judge=False`.
    """
    from amw.eval.runner import judge_candidate

    assert judge_candidate(_trace(json_={"rewritten": "x"})) == {"rewritten": "x"}
    assert judge_candidate(_trace(json_=[1, 2])) == [1, 2]
    # prose where a tool call was demanded is a wrong-format *answer*, not a
    # non-answer — json_schema_validity is what scores the format
    assert judge_candidate(_trace(text="a prose answer")) == "a prose answer"
    # structured payload wins when both are present
    assert judge_candidate(_trace(json_={"a": 1}, text="chatter")) == {"a": 1}
    # nothing at all stays None so the judge prints its absence marker
    assert judge_candidate(_trace()) is None
    assert judge_candidate(_trace(text="   ")) is None


def test_a_judge_request_built_from_a_real_trace_renders(items, cfg):
    """The end of the bridge that actually broke: request -> rendered prompt."""
    from amw.eval.judge import Judge, JudgeRequest
    from amw.eval.runner import judge_candidate

    item = items["feature_extractor"][0]
    trace = _trace(json_={"title": "A Widget", "assignee": None})
    request = JudgeRequest(
        item_id=item.item_id,
        subagent=item.subagent,
        rubric=rubric_of(item),
        candidate=judge_candidate(trace),
        task_input=list(item.input.messages),
        context_chunks=item.input.context_chunks(),
        reference=item.gold,
        repeat=1,
        repeats=2,
        arm="claude_baseline",
    )
    judge = Judge(mode="replay", models=cfg.models)
    rendered = judge.build_request(request)  # must not raise
    assert "A Widget" in json.dumps(rendered.model_dump(mode="json"))


def test_other_subagents_get_no_fe_criteria(items):
    for subagent in ("query_rewriter", "chunk_summarizer"):
        for item in items[subagent]:
            assert all(c.tag != "fe_field_label" for c in rubric_of(item).criteria)


# --------------------------------------------------------------------------
# never fabricate a number
# --------------------------------------------------------------------------


def test_empty_sample_reports_no_number_rather_than_zero():
    report = MetricReport.of(
        MetricSample(metric="filter_f1", excluded={"no_gold_filters": 3}), seed=1
    )
    assert report.point is None
    assert report.estimate is None
    assert report.n == 0
    assert report.n_excluded == 3


def test_single_observation_reports_a_mean_but_no_interval():
    """n=1 has a real mean and no width. Resampling one value 10k times would
    return [x, x], which looks like a precise measurement and is not one."""
    report = MetricReport.of(
        MetricSample(metric="filter_f1", values=[0.5], item_ids=["qr-0000"]), seed=1
    )
    assert report.point == 0.5
    assert report.estimate is None
    assert report.n == 1


def test_two_observations_get_an_interval():
    report = MetricReport.of(
        MetricSample(
            metric="filter_f1", values=[0.0, 1.0], item_ids=["qr-0000", "qr-0001"]
        ),
        seed=1,
    )
    assert report.estimate is not None
    assert report.estimate.lo <= report.estimate.point <= report.estimate.hi
    assert report.estimate.n == 2


def test_cli_prints_not_measured_not_a_zero():
    line = cli._format_value("filter_f1", None, None, 0, {"no_gold_filters": 3})
    assert "not measured" in line
    assert "0.000" not in line


# --------------------------------------------------------------------------
# the offline gate
# --------------------------------------------------------------------------


def test_e2e_fixture_corpus_is_committed():
    """Regenerating it on the fly would miss every recorded call — replay is
    keyed on the request bytes. It is a fixture, not an artifact."""
    assert cli.E2E_DATASET_DIR.is_dir()
    for subagent in ("query_rewriter", "chunk_summarizer", "feature_extractor"):
        assert (cli.E2E_DATASET_DIR / f"{subagent}.jsonl").exists()


def test_e2e_replay_passes_with_no_credentials(monkeypatch, capsys):
    """Ground rule 4, executed: the whole path, offline, zero credentials."""
    for var in ("PROJECT_ID", "REGION", "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "load_env", lambda: None)  # don't let .env re-supply them

    assert cli.main(["e2e", "--mode", "replay"]) == 0
    out = capsys.readouterr().out
    assert "0 call errors" in out


def test_phase2_result_validates_against_its_model(tmp_path, cfg):
    out = tmp_path / "phase2.json"
    result = run_phase2(
        config=cfg,
        mode="replay",
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=out,
        run_judge=False,
    )
    reloaded = Phase2Result.model_validate_json(out.read_text())
    assert reloaded == result
    assert len(reloaded.arms) == 9


def test_phase2_result_carries_the_provenance_the_footer_needs(cfg, tmp_path):
    """Ground rule 2: a footer can only state provenance if the run recorded it."""
    result = run_phase2(
        config=cfg,
        mode="replay",
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=tmp_path / "p.json",
        run_judge=False,
    )
    assert result.provenance == "synthetic"
    assert result.customer == "demo_patents"
    assert result.mode == "replay"
    assert result.region
    assert result.dataset_seed
    assert result.generator_version
    assert result.bootstrap_seed


def test_every_arm_reports_its_prompt_sha(cfg, tmp_path):
    """Two arms differing only in prompt must be distinguishable after the fact."""
    result = run_phase2(
        config=cfg,
        mode="replay",
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=tmp_path / "p.json",
        run_judge=False,
    )
    assert all(arm.prompt_sha for arm in result.arms)
    baseline = {a.subagent: a for a in result.arms if a.variant == "claude_baseline"}
    naive = {a.subagent: a for a in result.arms if a.variant == "gemini_naive"}
    # T07 asserts these two packs are byte-identical, so the shas must match —
    # that is what makes the Claude/A0 comparison a model delta, not a prompt one.
    for subagent, arm in baseline.items():
        assert arm.prompt_sha == naive[subagent].prompt_sha


def test_phase2_missing_dataset_says_how_to_make_one(cfg, tmp_path):
    with pytest.raises(FileNotFoundError, match="cli.py gen"):
        run_phase2(
            config=cfg,
            mode="replay",
            dataset_dir=tmp_path / "nope",
            out_path=tmp_path / "p.json",
            run_judge=False,
        )


def test_unimplemented_commands_still_refuse_rather_than_pretend(capsys):
    assert cli.main(["ablate", "--mode", "replay"]) == 3
    assert "not implemented" in capsys.readouterr().err


def test_smoke_runs_offline(capsys):
    assert cli.main(["smoke", "--mode", "replay", "-n", "1"]) == 0
    assert "smoke OK" in capsys.readouterr().out


def test_default_dataset_dir_is_not_the_fixture_dir():
    """A real run must never silently score the 4-item e2e corpus."""
    assert default_dataset_dir() != cli.E2E_DATASET_DIR
