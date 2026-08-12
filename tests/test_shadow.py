"""T11 — shadow comparison: agreement, triage, latency, the live slice.

Three things these tests exist to protect, in order of how much damage the bug
would do:

1. **Nothing here fabricates a verdict.** An item with no recorded judge call
   must come out ``not_adjudicated``, never ``tie`` — a tie is a measurement
   ("the judge scored both arms equally"), and the gate's alternative clause
   ("wins >= losses") is decided on those counts. Silently folding 36 unjudged
   Query Rewriter items into "tie" would make the alt clause pass on items
   nobody scored.
2. **The prose comparison is named for what it is.** The T11 card asked for
   embedding cosine; there is no embedding backend in an offline replay run, so
   prose is compared with a lexical proxy. These tests pin the name, the
   documented failure mode (a faithful paraphrase reads as a disagreement), and
   the fact that the judge adjudication — not the proxy — is authoritative.
3. **The default path makes no live calls, and the live slice cannot be big.**

The integration tests run against the committed e2e fixture corpus in replay
mode, so they exercise the real store, the real prompt packs and the real
`cmd_shadow`, with no credentials.
"""

from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timezone

import pytest

import cli
from amw.adapters import AdapterRouter
from amw.config import ConfigError, load_all
from amw.datasets.schema import read_items
from amw.eval.judge import CriterionVerdict, JudgeVerdict
from amw.shadow import agreement as A
from amw.shadow import runner as R
from amw.shadow import triage as T
from amw.shadow import cmd_shadow, run_shadow
from amw.traces.schema import LatencyMs, Trace, TraceInput, TraceOutput
from amw.traces.store import ReplayMissError, ReplayStore


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cfg():
    return load_all(customer="demo_patents")


@pytest.fixture(scope="module")
def items():
    return {
        subagent: list(read_items(cli.E2E_DATASET_DIR / f"{subagent}.jsonl"))
        for subagent in ("query_rewriter", "chunk_summarizer", "feature_extractor")
    }


def trace(
    subagent: str = "feature_extractor",
    payload: dict | list | None = None,
    *,
    text: str | None = None,
    status: str = "ok",
    ttft: int | None = None,
    total: int | None = None,
    model: str = "gemini-flash",
) -> Trace:
    return Trace(
        trace_id=f"{subagent}-fixture",
        subagent=subagent,
        provenance="synthetic",
        ts=datetime(2026, 8, 9, tzinfo=timezone.utc),
        model=model,
        system_prompt_sha="0" * 16,
        input=TraceInput(messages=["fixture"]),
        output=TraceOutput(text=text, json=payload),
        status=status,  # type: ignore[arg-type]
        error=None if status == "ok" else "boom",
        latency_ms=LatencyMs(ttft=ttft, total=total),
    )


def verdict(
    item_id: str,
    score: float | None,
    *,
    criteria: list[tuple[str, bool, str]] | None = None,
    overall: str | None = None,
    repeat: int = 1,
    error: str | None = None,
) -> JudgeVerdict:
    if score is None:
        return JudgeVerdict(
            item_id=item_id,
            subagent="feature_extractor",
            repeat=repeat,
            status="error",
            error=error or "replay miss",
        )
    return JudgeVerdict(
        item_id=item_id,
        subagent="feature_extractor",
        repeat=repeat,
        score=score,
        criteria=[
            CriterionVerdict(criterion_id=cid, passed=passed, rationale=why)
            for cid, passed, why in (criteria or [("c1", score == 1.0, "because")])
        ],
        overall_rationale=overall,
    )


class StubJudge:
    """Returns canned verdicts per arm. Records that it was asked, never calls out."""

    def __init__(self, by_arm: dict[str, list[JudgeVerdict]]):
        self.by_arm = by_arm
        self.calls: list[tuple[str, str]] = []

    def score_many(self, requests):
        requests = list(requests)
        arm = requests[0].arm if requests else ""
        self.calls.extend((r.item_id, r.arm) for r in requests)
        return list(self.by_arm.get(arm, []))


# ==========================================================================
# the prose proxy — named honestly, and its failure mode pinned
# ==========================================================================


def test_the_similarity_metric_is_not_called_an_embedding_cosine():
    """The card asked for embedding cosine. Offline replay has no embedding
    backend, so a lexical proxy stands in — and it must not wear the other
    name, or a reader will believe a semantic comparison happened."""
    assert "lexical" in A.LEXICAL_SIMILARITY_NAME
    assert "embedding" not in A.LEXICAL_SIMILARITY_NAME
    assert "cosine" not in A.LEXICAL_SIMILARITY_NAME
    doc = A.lexical_similarity.__doc__.lower()
    assert "stand-in" in doc or "proxy" in doc


@pytest.mark.parametrize(
    "left,right,expected",
    [
        # |A∩B| / |A∪B|, hand-computed
        ("solid state battery", "solid state battery", 1.0),
        ("solid state battery", "battery solid state", 1.0),  # order-free
        ("alpha beta", "gamma delta", 0.0),
        ("alpha beta", "alpha gamma", 1 / 3),  # {alpha} / {alpha,beta,gamma}
        ("alpha beta gamma", "alpha beta", 2 / 3),
        ("Alpha, BETA!", "alpha beta", 1.0),  # casefold + punctuation split
        ("alpha alpha beta", "alpha beta", 1.0),  # a set, not a bag
        ("", "", 1.0),  # both said nothing: they agree
        ("alpha", "", 0.0),  # one said nothing: they do not
    ],
)
def test_lexical_similarity_is_token_jaccard(left, right, expected):
    assert A.lexical_similarity(left, right) == pytest.approx(expected)


def test_a_faithful_paraphrase_reads_as_a_disagreement():
    """The documented cost of the proxy, executed rather than asserted in prose.

    Both sentences say the same thing; token overlap does not. This is why the
    item-level rate is reported next to a prose-free `structured_agreement` and
    why the judge adjudication is the authoritative call on prose.
    """
    left = "A separator comprising a ceramic coating on a polymer substrate."
    right = "A polymer substrate bearing a ceramic layer, used as a separator."
    assert A.lexical_similarity(left, right) < A.DEFAULT_PROSE_THRESHOLD


def test_prose_similarity_is_injectable_for_a_real_embedding_backend():
    """The extension point: swap the callable, keep everything else."""
    calls = []

    def always_same(a: str, b: str) -> float:
        calls.append((a, b))
        return 1.0

    result = A.compare_field(
        "novelty_statement",
        "prose",
        "one thing",
        "a completely different thing",
        prose_similarity=always_same,
    )
    assert result.agreed is True
    assert result.similarity == 1.0
    assert calls  # the injected function, not the built-in proxy, decided


# ==========================================================================
# structured comparison — normalisation is reused, not reimplemented
# ==========================================================================


def test_scalar_comparison_reuses_normalize_scalar():
    assert A.compare_field("assignee", "scalar", "  Acme   Corp ", "acme corp").agreed
    assert not A.compare_field("assignee", "scalar", "Acme Corp", "Acme Ltd").agreed


def test_code_comparison_reuses_normalize_code():
    """`normalize_code` is why "h01 m 10/02" and "H01M10/02" are one code."""
    assert A.compare_field("jurisdiction", "code", "h01 m 10/02", "H01M10/02").agreed


def test_sets_compare_without_order_or_duplicates():
    assert A.compare_field(
        "cpc_codes", "code_set", ["H01M10/02", "h01m 4/62"], ["H01M4/62", "H01M10/02"]
    ).agreed
    assert not A.compare_field(
        "cpc_codes", "code_set", ["H01M10/02"], ["H01M10/02", "H01M4/62"]
    ).agreed


def test_absent_and_null_are_the_same_answer():
    """Both arms declined to fill the field. That is agreement, not a hole."""
    left = A.field_values("feature_extractor", {"title": "T"})
    right = A.field_values("feature_extractor", {"title": "T", "assignee": None})
    assert left["assignee"] is None and right["assignee"] is None


def test_prose_below_threshold_disagrees_and_records_the_similarity():
    result = A.compare_field("summary", "prose", "alpha beta", "alpha gamma")
    assert result.agreed is False
    assert result.similarity == pytest.approx(1 / 3)
    assert result.kind == "prose"


# ==========================================================================
# item-level agreement — an unanswered item is never an agreement
# ==========================================================================


def test_two_empty_outputs_are_excluded_not_counted_as_agreement():
    """Both arms failing to produce structured output is a shared failure, not
    a match. Counting it as agreement would let a total outage score 1.00."""
    result = A.compare_item("feature_extractor", "fe-1", trace(), trace())
    assert result.comparable is False
    assert result.agreed is None
    assert result.reason == A.NO_PAYLOAD_EITHER


def test_one_empty_output_is_a_measured_disagreement():
    """This one is real signal: the swap would send the next stage nothing."""
    result = A.compare_item(
        "feature_extractor", "fe-1", trace(payload={"title": "T"}), trace()
    )
    assert result.comparable is True
    assert result.agreed is False
    assert result.empty_arm == "candidate"


def test_disagreeing_fields_names_only_the_fields_that_differ():
    baseline = trace(payload={"title": "A Widget", "assignee": "Acme"})
    candidate = trace(payload={"title": "A Widget", "assignee": "Globex"})
    result = A.compare_item("feature_extractor", "fe-1", baseline, candidate)
    assert result.disagreeing_fields == ["assignee"]
    assert result.agreed is False


def test_structured_agreement_ignores_prose():
    baseline = trace(payload={"title": "A Widget", "technical_field": "alpha beta"})
    candidate = trace(payload={"title": "A Widget", "technical_field": "gamma delta"})
    result = A.compare_item("feature_extractor", "fe-1", baseline, candidate)
    assert result.agreed is False
    assert result.structured_agreed is True


# ==========================================================================
# aggregation — an Estimate the gate can be checked on
# ==========================================================================


def _items_for(n_agree: int, n_disagree: int):
    out = []
    for i in range(n_agree):
        out.append(
            A.compare_item(
                "feature_extractor",
                f"fe-a{i}",
                trace(payload={"title": "T"}),
                trace(payload={"title": "T"}),
            )
        )
    for i in range(n_disagree):
        out.append(
            A.compare_item(
                "feature_extractor",
                f"fe-d{i}",
                trace(payload={"title": "T"}),
                trace(payload={"title": f"other {i}"}),
            )
        )
    return out


def test_aggregate_produces_an_estimate_with_a_bounded_interval():
    result = A.aggregate_agreement(
        _items_for(6, 4),
        subagent="feature_extractor",
        baseline_arm="claude_baseline",
        candidate_arm="gemini_tuned_v1",
    )
    assert result.point == pytest.approx(0.6)
    est = result.agreement
    assert est is not None
    assert est.metric == A.AGREEMENT_METRIC
    assert est.lo <= est.point <= est.hi
    assert est.n == 10


def test_the_interval_is_deterministic_for_a_seed():
    first, second = (
        A.aggregate_agreement(
            _items_for(6, 4),
            subagent="feature_extractor",
            baseline_arm="claude_baseline",
            candidate_arm="gemini_tuned_v1",
            seed=seed,
        ).agreement
        for seed in (4242, 4242)
    )
    assert (first.lo, first.hi) == (second.lo, second.hi)


def test_one_item_gets_a_point_and_no_interval():
    """Resampling one value 10,000 times returns [x, x], which looks like a
    precise measurement and is not one (same rule as MetricReport)."""
    result = A.aggregate_agreement(
        _items_for(1, 0),
        subagent="feature_extractor",
        baseline_arm="claude_baseline",
        candidate_arm="gemini_tuned_v1",
    )
    assert result.point == 1.0
    assert result.agreement is None
    assert result.no_interval_reason


def test_excluded_items_leave_the_denominator():
    items = _items_for(2, 0) + [A.compare_item("feature_extractor", "x", trace(), trace())]
    result = A.aggregate_agreement(
        items,
        subagent="feature_extractor",
        baseline_arm="claude_baseline",
        candidate_arm="gemini_tuned_v1",
    )
    assert result.n_items == 3
    assert result.n_compared == 2
    assert result.point == 1.0
    assert result.excluded == {A.NO_PAYLOAD_EITHER: 1}


def test_field_rates_report_similarity_only_where_similarity_was_used():
    result = A.aggregate_agreement(
        _items_for(2, 2),
        subagent="feature_extractor",
        baseline_arm="claude_baseline",
        candidate_arm="gemini_tuned_v1",
    )
    by_field = {r.field: r for r in result.field_rates}
    assert by_field["title"].mean_similarity is None
    assert by_field["technical_field"].kind == "prose"
    assert by_field["technical_field"].mean_similarity is not None


# ==========================================================================
# triage — the coverage hole must never read as a tie
# ==========================================================================


def _disagreement(item_id: str, fields=("technical_field",)):
    baseline = trace(payload={"title": "T", "technical_field": "alpha beta"})
    candidate = trace(payload={"title": "T", "technical_field": "gamma delta"})
    result = A.compare_item("feature_extractor", item_id, baseline, candidate)
    assert result.disagreeing_fields == list(fields)
    return result, baseline, candidate


def _adjudicate_one(item, *, baseline_score, candidate_score, judged=True, **kw):
    disagreement, baseline, candidate = _disagreement(item.item_id)
    judge = StubJudge(
        {
            "claude_baseline": [verdict(item.item_id, baseline_score, **kw)],
            "gemini_tuned_v1": [verdict(item.item_id, candidate_score, **kw)],
        }
    )
    rows = T.adjudicate(
        "feature_extractor",
        [disagreement],
        items={item.item_id: item},
        baseline_traces={item.item_id: baseline},
        candidate_traces={item.item_id: candidate},
        baseline_arm="claude_baseline",
        candidate_arm="gemini_tuned_v1",
        judge=judge,
        repeats=1,
        judged_items=[item.item_id] if judged else [],
        judged_split="all",
    )
    return rows[0], judge


@pytest.mark.parametrize(
    "baseline_score,candidate_score,expected",
    [(0.5, 1.0, T.WIN), (1.0, 0.5, T.LOSS), (0.75, 0.75, T.TIE)],
)
def test_verdicts_follow_the_recorded_scores(
    items, baseline_score, candidate_score, expected
):
    item = items["feature_extractor"][0]
    row, _ = _adjudicate_one(
        item, baseline_score=baseline_score, candidate_score=candidate_score
    )
    assert row.verdict == expected
    assert row.baseline_score == baseline_score
    assert row.candidate_score == candidate_score


def test_an_item_outside_the_judged_split_is_labelled_not_guessed(items):
    """QR and CS were judged on the 28-item core split. The other 42 items have
    no verdict, and 42 silent ties would decide the gate's alt clause."""
    item = items["feature_extractor"][0]
    row, judge = _adjudicate_one(item, baseline_score=1.0, candidate_score=0.5, judged=False)
    assert row.verdict == T.NOT_ADJUDICATED
    assert row.verdict != T.TIE
    assert T.OUTSIDE_SPLIT in row.reason
    assert judge.calls == [], "an unjudged item must not provoke a judge call"


def test_a_missing_recorded_verdict_is_not_a_tie(items):
    """A replay miss comes back as an error verdict, which has no score. The
    row says so instead of scoring 0 vs 0 and calling it even."""
    item = items["feature_extractor"][0]
    row, _ = _adjudicate_one(
        item, baseline_score=None, candidate_score=None, error="replay miss: nope"
    )
    assert row.verdict == T.NOT_ADJUDICATED
    assert "no recorded judge verdict" in row.reason
    assert row.baseline_score is None and row.candidate_score is None


def test_no_judge_at_all_still_produces_labelled_rows(items):
    item = items["feature_extractor"][0]
    disagreement, baseline, candidate = _disagreement(item.item_id)
    rows = T.adjudicate(
        "feature_extractor",
        [disagreement],
        items={item.item_id: item},
        baseline_traces={item.item_id: baseline},
        candidate_traces={item.item_id: candidate},
        baseline_arm="claude_baseline",
        candidate_arm="gemini_tuned_v1",
        judge=None,
        repeats=1,
        judged_items=[item.item_id],
        judged_split="all",
    )
    assert rows[0].verdict == T.NOT_ADJUDICATED
    assert rows[0].reason


def test_the_rationale_is_quoted_from_the_recording_and_names_the_loser(items):
    """A rationale that says "the candidate output" is ambiguous in a table
    where a win means the *baseline* was the one criticised — the judge called
    both arms "the candidate output" in its own prompt."""
    item = items["feature_extractor"][0]
    row, _ = _adjudicate_one(
        item,
        baseline_score=0.5,
        candidate_score=1.0,
        criteria=[("novelty_statement_correct", False, "the statement omits claim 1")],
    )
    assert row.verdict == T.WIN
    assert "claude_baseline" in row.rationale
    assert "the statement omits claim 1" in row.rationale
    assert "novelty_statement_correct" in row.rationale


def test_a_rationale_is_one_line_with_escaped_pipes():
    long = "x | y " + "z" * 400
    line = T._one_line(f"a\nb {long}")
    assert "\n" not in line
    assert "|" not in line.replace("\\|", "")
    assert len(line) <= T._MAX_RATIONALE_CHARS + 1


def test_summary_counts_not_adjudicated_separately(items):
    item = items["feature_extractor"][0]
    win, _ = _adjudicate_one(item, baseline_score=0.5, candidate_score=1.0)
    unjudged, _ = _adjudicate_one(
        item, baseline_score=0.5, candidate_score=1.0, judged=False
    )
    summary = T.summarize("feature_extractor", [win, unjudged])
    assert summary.disagreements == 2
    assert summary.wins == 1
    assert summary.ties == 0
    assert summary.not_adjudicated == 1
    assert summary.adjudicated == 1
    assert summary.wins_ge_losses is True


def test_summary_has_no_opinion_when_nothing_was_adjudicated(items):
    item = items["feature_extractor"][0]
    unjudged, _ = _adjudicate_one(
        item, baseline_score=0.5, candidate_score=1.0, judged=False
    )
    summary = T.summarize("feature_extractor", [unjudged])
    assert summary.wins_ge_losses is None, "0 >= 0 is not evidence of parity"


def test_the_markdown_table_has_the_columns_the_card_asked_for(items):
    item = items["feature_extractor"][0]
    row, _ = _adjudicate_one(item, baseline_score=0.5, candidate_score=1.0)
    table = T.triage_table_markdown([row], summaries=[T.summarize("feature_extractor", [row])])
    header = next(line for line in table.splitlines() if line.startswith("| item "))
    for column in ("item", "subagent", "disagreeing field(s)", "verdict", "judged split"):
        assert column in header
    assert item.item_id in table
    assert "technical_field" in table
    assert "not_adjudicated` rows are **not ties" in table


# ==========================================================================
# latency — a percentile that says nothing when it measured nothing
# ==========================================================================


def test_percentile_of_nothing_is_none_not_zero():
    assert R.percentile([], 95) is None
    assert R.percentile([1.0], 95) == 1.0
    assert R.percentile([0.0, 10.0], 50) == 5.0


def test_latency_ignores_traces_that_recorded_no_timing():
    stats = R._latency_stats(
        "claude_baseline",
        "claude-sonnet",
        [trace(ttft=100, total=1000), trace(ttft=None, total=None)],
        region="global",
        region_source="env:CLAUDE_REGION",
        disclosure="d",
    )
    assert stats.n_traces == 2
    assert stats.n_with_total == 1
    assert stats.total_p50_ms == 1000
    assert stats.ttft_p95_ms == 100


def test_claude_region_comes_from_its_own_variable(cfg, monkeypatch):
    """Claude runs in `global` because us-central1 quota is exhausted, while
    Gemini runs in us-central1. One REGION cannot describe both."""
    monkeypatch.setenv("CLAUDE_REGION", "global")
    monkeypatch.setenv("REGION", "us-central1")
    claude_key, _ = cfg.models.for_role("claude_baseline")
    gemini_key, _ = cfg.models.for_role("gemini_candidate")
    assert R.arm_region(claude_key, models=cfg.models, customer_region="x") == (
        "global",
        "env:CLAUDE_REGION",
    )
    assert R.arm_region(gemini_key, models=cfg.models, customer_region="x") == (
        "us-central1",
        "env:REGION",
    )


def test_region_falls_back_to_the_customer_profile(cfg, monkeypatch):
    monkeypatch.delenv("CLAUDE_REGION", raising=False)
    monkeypatch.delenv("REGION", raising=False)
    key, _ = cfg.models.for_role("gemini_candidate")
    region, source = R.arm_region(key, models=cfg.models, customer_region="europe-west4")
    assert region == "europe-west4"
    assert "customers" in source


def test_a_cross_region_pair_is_disclosed_as_such():
    text = R._latency_disclosure(
        baseline_region="global",
        candidate_region="us-central1",
        baseline_model="claude-sonnet",
        candidate_model="gemini-flash",
    )
    assert "CROSS-REGION" in text
    assert "global" in text and "us-central1" in text
    same = R._latency_disclosure(
        baseline_region="us-central1",
        candidate_region="us-central1",
        baseline_model="a",
        candidate_model="b",
        baseline_source="env:REGION",
    )
    assert "CROSS-REGION" not in same
    assert "Same region" in same


def test_an_unset_claude_region_is_not_a_same_region_claim(cfg, monkeypatch):
    """The quiet way to publish a false like-for-like p95: leave CLAUDE_REGION
    unset, let both arms inherit REGION, and print "same region"."""
    monkeypatch.delenv("CLAUDE_REGION", raising=False)
    monkeypatch.setenv("REGION", "us-central1")
    claude_key, _ = cfg.models.for_role("claude_baseline")
    region, source = R.arm_region(claude_key, models=cfg.models, customer_region="x")
    assert region == "us-central1"
    assert "unverified" in source
    text = R._latency_disclosure(
        baseline_region=region,
        candidate_region="us-central1",
        baseline_model="claude-sonnet",
        candidate_model="gemini-flash",
        baseline_source=source,
    )
    assert "REGION UNVERIFIED" in text
    assert "Same region" not in text


def test_every_latency_number_carries_the_disclosure(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_REGION", "global")
    monkeypatch.setenv("REGION", "us-central1")
    result = run_shadow(
        config=cfg,
        mode="replay",
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=tmp_path / "s.json",
        triage_path=tmp_path / "s.md",
        run_judge=False,
    )
    for shadow in result.subagents:
        for arm in (shadow.baseline, shadow.candidate):
            assert "CROSS-REGION" in arm.latency.disclosure
            assert arm.latency.region_source


# ==========================================================================
# the run — offline, and honest about being offline
# ==========================================================================


def test_replay_run_writes_a_result_that_revalidates(cfg, tmp_path):
    out = tmp_path / "shadow.json"
    result = run_shadow(
        config=cfg,
        mode="replay",
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=out,
        triage_path=tmp_path / "triage.md",
    )
    reloaded = R.ShadowResult.model_validate_json(out.read_text())
    assert reloaded == result
    assert {s.subagent for s in result.subagents} == {
        "query_rewriter",
        "chunk_summarizer",
        "feature_extractor",
    }
    assert (tmp_path / "triage.md").read_text().startswith("## Disagreement triage")


def test_a_replayed_result_says_when_its_calls_were_recorded(cfg, tmp_path):
    """Ground rule 1: the number is dated by the recording, not by today."""
    result = run_shadow(
        config=cfg,
        mode="replay",
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=tmp_path / "s.json",
        triage_path=tmp_path / "s.md",
        run_judge=False,
    )
    assert result.run_started is None
    assert result.recorded_from and result.recorded_to
    assert result.recorded_from <= result.recorded_to


def test_the_result_says_the_prose_metric_is_a_proxy(cfg, tmp_path):
    result = run_shadow(
        config=cfg,
        mode="replay",
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=tmp_path / "s.json",
        triage_path=tmp_path / "s.md",
        run_judge=False,
    )
    assert result.similarity_metric == A.LEXICAL_SIMILARITY_NAME
    blob = " ".join(result.notes)
    assert "not an embedding cosine" in blob
    assert "structured_agreement" in blob


def test_the_judge_stays_in_replay_whatever_the_arms_do(cfg, tmp_path):
    """Triage reads recorded verdicts. A live judge here would spend money
    mid-demo and produce verdicts the scorecard's quality numbers never saw."""
    assert R.JUDGE_MODE == "replay"
    result = run_shadow(
        config=cfg,
        mode="replay",
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=tmp_path / "s.json",
        triage_path=tmp_path / "s.md",
    )
    assert result.judge_mode == "replay"


def test_provenance_and_seeds_reach_the_footer(cfg, tmp_path):
    result = run_shadow(
        config=cfg,
        mode="replay",
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=tmp_path / "s.json",
        triage_path=tmp_path / "s.md",
        run_judge=False,
    )
    assert result.provenance == "synthetic"
    assert result.dataset_seed and result.generator_version
    assert result.bootstrap_seed
    assert result.customer == "demo_patents"


def test_two_identical_arms_are_refused(cfg, tmp_path):
    with pytest.raises(ConfigError, match="compares two backends"):
        run_shadow(
            config=cfg,
            mode="replay",
            dataset_dir=cli.E2E_DATASET_DIR,
            candidate_arm="claude_baseline",
            write=False,
        )


def test_an_unknown_arm_is_refused(cfg):
    with pytest.raises(ConfigError, match="unknown arm"):
        run_shadow(
            config=cfg, mode="replay", candidate_arm="gemini_vibes_v9", write=False
        )


def test_a_subagent_specific_arm_is_refused_across_all_subagents(cfg):
    """`gemini_targeted_v1` is a query_rewriter prompt and only that.

    Running it against all three would silently compare two subagents on a
    prompt they do not have. The error has to name the subagents that *do*
    have it, because the fix is `--subagent`, not a different arm.
    """
    with pytest.raises(ConfigError, match="has no prompt for"):
        run_shadow(
            config=cfg,
            mode="replay",
            candidate_arm="gemini_targeted_v1",
            write=False,
        )


def test_a_subagent_specific_arm_is_accepted_for_its_own_subagent(cfg, tmp_path):
    """The alt clause is decided on adjudicated wins, so a targeted rung has
    to be shadowable against the incumbent — otherwise it can be measured on
    the ladder and never adjudicated, which is the evidence the gate needs.

    The e2e fixture corpus has no ``gemini_targeted_v1`` recordings, so this
    gets as far as the replay store and stops there. That is the assertion:
    the run reached call resolution, which means the arm cleared validation.
    A ``ConfigError`` here would mean it never got that far.
    """
    with pytest.raises(ReplayMissError):
        run_shadow(
            config=cfg,
            mode="replay",
            subagents=["query_rewriter"],
            dataset_dir=cli.E2E_DATASET_DIR,
            candidate_arm="gemini_targeted_v1",
            out_path=tmp_path / "s.json",
            triage_path=tmp_path / "s.md",
            run_judge=False,
        )


def test_a_missing_dataset_says_which_command_makes_one(cfg, tmp_path):
    with pytest.raises(FileNotFoundError, match="cli.py gen"):
        run_shadow(config=cfg, mode="replay", dataset_dir=tmp_path, write=False)


# ==========================================================================
# the live slice — opt-in, capped, and through the ordinary router
# ==========================================================================


class SpyRouter(AdapterRouter):
    """A real router with a stub adapter behind it.

    Subclassed rather than duck-typed on purpose: `complete_many`, `describe`
    and `served_window` stay the real implementations, so this exercises the
    same code path a live run takes — minus the model call.
    """

    def __init__(self, tmp_path, **kw):
        super().__init__(store=ReplayStore(tmp_path), **kw)
        self.requests = []

    def for_model(self, model: str):
        spy = self

        class _Stub:
            name = "stub"
            mode = spy.mode

            def complete(self, request):
                spy.requests.append(request)
                return trace(
                    request.subagent,
                    {"title": f"{request.model}:{request.item_id}"},
                    model=request.model,
                    ttft=100,
                    total=900,
                )

        return _Stub()


def test_live_slice_is_off_by_default(cfg, tmp_path):
    result = run_shadow(
        config=cfg,
        mode="replay",
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=tmp_path / "s.json",
        triage_path=tmp_path / "s.md",
        run_judge=False,
    )
    assert result.live_slice == 0
    assert result.subagents[0].items == 4  # the whole fixture corpus


def test_live_slice_limits_the_calls_to_n_items_per_subagent(cfg, tmp_path):
    router = SpyRouter(tmp_path, mode="live", models=cfg.models)
    result = run_shadow(
        config=cfg,
        mode="live",
        dataset_dir=cli.E2E_DATASET_DIR,
        router=router,
        live_slice=2,
        out_path=tmp_path / "s.json",
        triage_path=tmp_path / "s.md",
        run_judge=False,
    )
    # 2 items x 2 arms x 3 subagents, and not one call more
    assert len(router.requests) == 12
    assert all(s.items == 2 for s in result.subagents)
    assert result.live_slice == 2
    assert any("demonstration slice" in note for note in result.notes)


def test_live_slice_is_capped(cfg):
    with pytest.raises(ConfigError, match="exceeds the cap"):
        run_shadow(
            config=cfg,
            mode="live",
            live_slice=R.LIVE_SLICE_MAX + 1,
            dataset_dir=cli.E2E_DATASET_DIR,
            write=False,
        )


def test_a_live_run_still_carries_a_run_date(cfg, tmp_path):
    router = SpyRouter(tmp_path, mode="live", models=cfg.models)
    result = run_shadow(
        config=cfg,
        mode="live",
        dataset_dir=cli.E2E_DATASET_DIR,
        router=router,
        live_slice=1,
        out_path=tmp_path / "s.json",
        triage_path=tmp_path / "s.md",
        run_judge=False,
    )
    assert result.run_started is not None


def test_the_shadow_lane_owns_no_adapter_of_its_own():
    """Record-on-live is a property of how adapters are obtained (ground rule
    5). This module must therefore never construct one: everything goes
    through AdapterRouter, which wraps every live adapter in RecordingAdapter.
    """
    source = (R.__file__, T.__file__, A.__file__)
    for path in source:
        text = open(path, encoding="utf-8").read()
        code = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("#")
        )
        # crude but load-bearing: no direct adapter construction, no record flag
        assert "GeminiAdapter" not in code
        assert "RecordingAdapter" not in code
        assert "import resolve" not in code and "resolve," not in code
        assert "record=" not in code


def test_run_shadow_asks_the_router_for_the_mode_it_was_given(cfg, monkeypatch, tmp_path):
    seen = {}
    real = R.AdapterRouter

    def spy(mode, **kw):
        seen["mode"] = mode
        return SpyRouter(tmp_path, mode=mode, models=kw.get("models"))

    monkeypatch.setattr(R, "AdapterRouter", spy)
    run_shadow(
        config=cfg,
        mode="hybrid",
        dataset_dir=cli.E2E_DATASET_DIR,
        live_slice=1,
        run_judge=False,
        write=False,
    )
    assert seen["mode"] == "hybrid"
    assert real is AdapterRouter


# ==========================================================================
# the CLI contract
# ==========================================================================


def _args(**kw) -> Namespace:
    base = dict(mode="replay", customer="demo_patents")
    base.update(kw)
    return Namespace(**base)


def test_cmd_shadow_runs_from_a_minimal_namespace(cfg, tmp_path, capsys):
    """The main session may wire only `--mode` and `--customer` at first; every
    other flag has to be optional for the command to work at all."""
    code = cmd_shadow(
        _args(
            dataset_dir=str(cli.E2E_DATASET_DIR),
            out=str(tmp_path / "s.json"),
            triage_out=str(tmp_path / "s.md"),
            no_judge=True,
        ),
        cfg,
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "shadow_agreement" in out
    assert "REPLAY" in out
    assert "recorded" in out


def test_cmd_shadow_prints_the_cross_region_caveat(cfg, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_REGION", "global")
    monkeypatch.setenv("REGION", "us-central1")
    cmd_shadow(
        _args(
            dataset_dir=str(cli.E2E_DATASET_DIR),
            out=str(tmp_path / "s.json"),
            triage_out=str(tmp_path / "s.md"),
            no_judge=True,
        ),
        cfg,
    )
    out = capsys.readouterr().out
    assert "CROSS-REGION" in out
    assert "lexical" in out or "proxy" in out


def test_cmd_shadow_reports_a_replay_miss_instead_of_traceback(cfg, monkeypatch, tmp_path):
    class Missing(SpyRouter):
        def for_model(self, model: str):
            raise ReplayMissError("feature_extractor", model, "deadbeef")

    monkeypatch.setattr(R, "AdapterRouter", lambda mode, **kw: Missing(tmp_path, mode=mode))
    code = cmd_shadow(_args(dataset_dir=str(cli.E2E_DATASET_DIR), no_judge=True), cfg)
    assert code == 5


def test_a_single_subagent_string_is_not_iterated_letter_by_letter(cfg, tmp_path, capsys):
    """`--subagent query_rewriter` arrives as a string, and `tuple("qr...")` is
    fourteen subagents named 'q', 'u', 'e'..."""
    code = cmd_shadow(
        _args(
            subagent="feature_extractor",
            dataset_dir=str(cli.E2E_DATASET_DIR),
            out=str(tmp_path / "s.json"),
            triage_out=str(tmp_path / "s.md"),
            no_judge=True,
        ),
        cfg,
    )
    assert code == 0
    result = R.ShadowResult.model_validate_json((tmp_path / "s.json").read_text())
    assert [s.subagent for s in result.subagents] == ["feature_extractor"]


def test_the_cli_already_advertises_this_command():
    """`cli.py` is main-session-owned; this asserts the contract, not the wiring."""
    assert "shadow" in cli.COMMANDS


# ==========================================================================
# the real corpus — the deliverable, not a fixture
# ==========================================================================


@pytest.mark.skipif(
    not (R.default_dataset_dir() / "feature_extractor.jsonl").exists(),
    reason="the n=70 corpus is generated, not committed",
)
def test_the_real_corpus_agreement_is_measured_over_every_item(cfg, tmp_path):
    """No numbers asserted here — they are measurements, and pinning them would
    turn a re-recording into a test failure. What is asserted is that every
    item was compared and every disagreement was accounted for."""
    result = run_shadow(
        config=cfg,
        mode="replay",
        out_path=tmp_path / "s.json",
        triage_path=tmp_path / "s.md",
    )
    for shadow in result.subagents:
        a = shadow.agreement
        assert a.n_items == shadow.items
        assert a.n_compared + sum(a.excluded.values()) == a.n_items
        summary = shadow.triage_summary
        assert summary.disagreements == a.n_compared - a.n_agreed
        assert (
            summary.wins + summary.losses + summary.ties + summary.not_adjudicated
            == summary.disagreements
        )
        if shadow.judged_split == "core":
            assert summary.not_adjudicated > 0, "42 unjudged items must be labelled"


def test_json_artifact_is_readable_by_a_downstream_lane(cfg, tmp_path):
    """T12 reads this file. It must be plain JSON with the gate input on top."""
    out = tmp_path / "s.json"
    run_shadow(
        config=cfg,
        mode="replay",
        dataset_dir=cli.E2E_DATASET_DIR,
        out_path=out,
        triage_path=tmp_path / "s.md",
        run_judge=False,
    )
    data = json.loads(out.read_text())
    first = data["subagents"][0]
    assert data["shadow_version"] == R.SHADOW_VERSION
    assert first["agreement"]["agreement"]["metric"] == A.AGREEMENT_METRIC
    assert "lo" in first["agreement"]["agreement"]
    assert first["baseline"]["latency"]["disclosure"]
