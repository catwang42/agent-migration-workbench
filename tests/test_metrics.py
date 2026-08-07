"""Golden tests for amw/eval/metrics.py.

Every expected number below was derived on paper from
``tests/fixtures/eval/metric_cases.json`` and is written here as the exact
fraction, with the tally that produces it in the comment above the assertion.
That is the point of the T08 card: a golden captured by running the code is a
snapshot of whatever bug the code has, not a golden.

Where a value is a repeating decimal the assertion uses ``Fraction`` rather
than an ``approx`` with a hand-typed 0.666…, so a wrong denominator cannot hide
inside a tolerance.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import pytest

from amw.eval import metrics as M
from amw.traces.schema import Trace, TraceInput, TraceOutput

FIXTURES = Path(__file__).parent / "fixtures" / "eval" / "metric_cases.json"
CASES = json.loads(FIXTURES.read_text(encoding="utf-8"))


def frac(outcome: M.MetricOutcome) -> Fraction:
    """The measured value as an exact fraction, for equality (not approx)."""
    assert outcome.measured, f"{outcome.metric} was not measured: {outcome.detail}"
    assert outcome.value is not None
    return Fraction(outcome.value).limit_denominator(10**6)


def make_trace(
    subagent: str,
    payload: dict | None = None,
    *,
    text: str | None = None,
    status: str = "ok",
    error: str | None = None,
) -> Trace:
    return Trace(
        trace_id=f"{subagent}-fixture",
        subagent=subagent,
        provenance="synthetic",
        ts=datetime(2026, 8, 7, tzinfo=timezone.utc),
        model="gemini-flash",
        system_prompt_sha="0" * 16,
        input=TraceInput(messages=["fixture"]),
        output=TraceOutput(text=text, json=payload),
        status=status,  # type: ignore[arg-type]
        error=error,
    )


# ==========================================================================
# MetricOutcome — the "no fabricated number" invariant
# ==========================================================================


def test_outcome_cannot_carry_a_value_it_did_not_measure():
    with pytest.raises(ValueError, match="never be emitted"):
        M.MetricOutcome(
            metric="x", status="not_applicable", value=0.0, detail="undefined"
        )


def test_outcome_cannot_be_ok_without_a_value():
    with pytest.raises(ValueError, match="carries no value"):
        M.MetricOutcome(metric="x", status="ok")


def test_absent_measurement_must_say_why():
    with pytest.raises(ValueError, match="without a detail"):
        M.MetricOutcome(metric="x", status="not_applicable")


# ==========================================================================
# G5 — JSON schema validity
# ==========================================================================


def test_schema_validity_accepts_a_conforming_payload():
    out = M.json_schema_validity("query_rewriter", CASES["qr_schema_valid"])
    assert out.value == 1.0
    assert out.counts == {"valid": 1}


@pytest.mark.parametrize(
    "case",
    ["qr_schema_extra_key", "qr_schema_missing_intent", "qr_schema_bad_intent"],
)
def test_schema_validity_rejects_nonconforming_payloads(case):
    out = M.json_schema_validity("query_rewriter", CASES[case])
    assert out.value == 0.0
    assert out.counts == {"invalid": 1}
    assert out.detail  # the pydantic complaint, for triage


def test_schema_validity_scores_prose_where_json_was_required_as_zero():
    trace = make_trace("query_rewriter", None, text="Here is your query plan!")
    out = M.json_schema_validity("query_rewriter", trace)
    assert out.value == 0.0
    assert out.counts == {"no_json": 1}


def test_schema_validity_counts_a_failed_call_as_invalid_but_flags_it():
    """A call that exhausted its retries produced no parseable response.

    Scoring it 0 is a measurement of what the customer got; excluding it would
    shrink the denominator and flatter whichever backend fell over. It is
    tagged so the rate can be printed with its call-error count beside it.
    """
    trace = make_trace(
        "query_rewriter", None, status="error", error="AdapterError: 503"
    )
    out = M.json_schema_validity("query_rewriter", trace)
    assert out.value == 0.0
    assert out.counts == {"call_error": 1}
    assert "503" in (out.detail or "")


def test_schema_validity_rejects_an_unknown_subagent():
    with pytest.raises(KeyError):
        M.json_schema_validity("answer_drafter", {})


# ==========================================================================
# G1-G4 — filter precision / recall / F1
# ==========================================================================


def test_filter_assertions_flatten_key_by_key():
    """gold: date_from + 2 assignees + 2 jurisdictions + 1 cpc = 6 assertions."""
    gold = CASES["qr_mixed"]["gold"]["filters"]
    assert M.filter_assertions(gold) == {
        ("date_from", "2021-01-01"),
        ("assignees", "toyota"),
        ("assignees", "panasonic"),
        ("jurisdictions", "JP"),
        ("jurisdictions", "US"),
        ("cpc_codes", "H01M10/052"),
    }


def test_filter_assertions_normalise_case_whitespace_and_codes():
    """predicted: '  toyota ' -> toyota, 'jp' -> JP, 'h01m 10/052' -> H01M10/052."""
    pred = CASES["qr_mixed"]["predicted"]["filters"]
    assert M.filter_assertions(pred) == {
        ("date_from", "2021-01-01"),
        ("date_to", "2024-12-31"),
        ("assignees", "toyota"),
        ("assignees", "lg energy solution"),
        ("jurisdictions", "JP"),
        ("cpc_codes", "H01M10/052"),
        ("cpc_codes", "H01M4/62"),
    }


def test_filter_prf_golden_mixed_case():
    """GOLDEN. gold=6 assertions, pred=7, overlap=4.

    tp = 4  {date_from 2021-01-01, assignees toyota, jurisdictions JP,
             cpc H01M10/052}
    fp = 3  {date_to 2024-12-31, assignees lg energy solution, cpc H01M4/62}
    fn = 2  {assignees panasonic, jurisdictions US}

    precision = tp/(tp+fp) = 4/7
    recall    = tp/(tp+fn) = 4/6 = 2/3
    F1        = 2PR/(P+R)  = 2*(4/7)*(2/3) / (4/7 + 2/3)
                           = (16/21) / (26/21) = 16/26 = 8/13
    """
    case = CASES["qr_mixed"]
    out = M.filter_prf(case["gold"], case["predicted"])

    assert out["filter_precision"].counts == {"tp": 4, "fp": 3, "fn": 2}
    assert frac(out["filter_precision"]) == Fraction(4, 7)
    assert frac(out["filter_recall"]) == Fraction(2, 3)
    assert frac(out["filter_f1"]) == Fraction(8, 13)


def test_filter_prf_undefined_precision_is_not_zero():
    """GOLDEN. gold=1 assertion, pred=0.  tp=0, fp=0, fn=1.

    precision = 0/0 -> undefined, reported as not_applicable.
    recall    = 0/1 = 0.0, a real measured miss.
    F1        -> undefined, because precision is.
    """
    case = CASES["qr_no_predicted_filters"]
    out = M.filter_prf(case["gold"], case["predicted"])

    assert out["filter_precision"].status == "not_applicable"
    assert out["filter_precision"].value is None
    assert out["filter_recall"].value == 0.0
    assert out["filter_f1"].status == "not_applicable"
    assert out["filter_precision"].counts == {"tp": 0, "fp": 0, "fn": 1}


def test_filter_prf_undefined_recall_is_not_zero():
    """GOLDEN. gold=0 assertions, pred=1 (an invented assignee).

    tp=0, fp=1, fn=0 -> precision = 0/1 = 0.0; recall = 0/0 -> undefined.
    """
    case = CASES["qr_no_gold_filters"]
    out = M.filter_prf(case["gold"], case["predicted"])

    assert out["filter_precision"].value == 0.0
    assert out["filter_recall"].status == "not_applicable"
    assert out["filter_recall"].counts == {"tp": 0, "fp": 1, "fn": 0}


def test_filter_prf_scores_nothing_when_neither_side_asserts_anything():
    """GOLDEN. tp=fp=fn=0. All three undefined — emphatically not 1.0."""
    case = CASES["qr_both_empty"]
    out = M.filter_prf(case["gold"], case["predicted"])
    assert [o.status for o in out.values()] == ["not_applicable"] * 3
    assert all(o.value is None for o in out.values())


def test_filter_prf_accepts_a_bare_filters_mapping():
    gold = CASES["qr_mixed"]["gold"]["filters"]
    pred = CASES["qr_mixed"]["predicted"]["filters"]
    assert frac(M.filter_prf(gold, pred)["filter_precision"]) == Fraction(4, 7)


# ==========================================================================
# G10 — exact key match
# ==========================================================================


def test_exact_key_match_golden():
    """gold intent prior_art vs predicted landscape -> 0.0; self-match -> 1.0."""
    case = CASES["qr_mixed"]
    assert M.exact_key_match(case["gold"], case["predicted"], "intent").value == 0.0
    assert M.exact_key_match(case["gold"], case["gold"], "intent").value == 1.0


def test_exact_key_match_is_not_applicable_without_a_gold_value():
    assert (
        M.exact_key_match({"query": "x"}, {"intent": "prior_art"}, "intent").status
        == "not_applicable"
    )


# ==========================================================================
# G6-G7 — citation coverage
# ==========================================================================


def test_citation_coverage_golden():
    """GOLDEN. 5 key points, chunks c1..c3 supplied.

    p1 cites [c1]      -> grounded
    p2 cites [c2, c3]  -> grounded
    p3 cites []        -> uncited
    p4 cites [c9]      -> fabricated (c9 was never supplied)
    p5 cites [c1, c9]  -> fabricated (one bad id poisons the point)

    coverage                 = 2/5 = 0.4
    uncited_claim_rate       = 1/5 = 0.2
    fabricated_citation_rate = 2/5 = 0.4     (the three sum to 1.0)
    """
    case = CASES["cs_mixed"]
    out = M.citation_coverage(case["payload"], case["provided_chunk_ids"])

    assert out["citation_coverage"].counts == {
        "total_points": 5,
        "grounded_points": 2,
        "uncited_points": 1,
        "fabricated_citation_points": 2,
    }
    assert out["citation_coverage"].value == 0.4
    assert out["uncited_claim_rate"].value == 0.2
    assert out["fabricated_citation_rate"].value == 0.4
    assert sum(o.value for o in out.values()) == pytest.approx(1.0)


def test_citation_coverage_of_an_empty_summary_is_undefined_not_perfect():
    """GOLDEN. 0 key points -> 0/0. A vacuous 1.0 would top the leaderboard."""
    case = CASES["cs_empty_points"]
    out = M.citation_coverage(case["payload"], case["provided_chunk_ids"])
    assert [o.status for o in out.values()] == ["not_applicable"] * 3
    assert out["citation_coverage"].counts == {"total_points": 0}


def test_citation_coverage_of_a_missing_response_is_a_measured_zero():
    """No response -> coverage 0.0 (measured), diagnostics undefined.

    Coverage has to stay a 0 so a backend that fails half its calls cannot
    report perfect groundedness on the half it answered. The two diagnostic
    rates are genuinely 0/0: a response that made no claim did not make an
    uncited one either.
    """
    out = M.citation_coverage(None, ["c1"])
    assert out["citation_coverage"].value == 0.0
    assert out["citation_coverage"].counts["call_error"] == 1
    assert out["uncited_claim_rate"].status == "not_applicable"
    assert out["fabricated_citation_rate"].status == "not_applicable"


def test_citation_coverage_errors_on_an_unscoreable_structure():
    out = M.citation_coverage({"key_points": "lots"}, ["c1"])
    assert [o.status for o in out.values()] == ["error"] * 3


# ==========================================================================
# G8-G9 — feature extractor: null vs wrong vs right
# ==========================================================================


def test_extraction_field_verdicts_golden():
    """GOLDEN, field by field, over the 8 fields of PatentFeatures.

    title       'Solid-state battery' vs 'solid-state  battery'
                -> correct (whitespace collapsed, case folded)
    assignee    'Toyota Motor Corp.' vs 'Panasonic Corp.'   -> wrong
    filing_date '2021-03-04' vs null                        -> omission
    jurisdiction 'US' vs 'us'   -> correct (code normalisation)
    cpc_codes   ['H01M10/052'] vs ['h01m 10/052'] -> correct (set + code norm)
    technical_field 'solid-state electrolytes' vs null      -> omission
    independent_claim_count 2 vs 3                          -> wrong
    novelty_statement null vs 'A novel sulfide separator...' -> hallucination
    """
    case = CASES["fe_mixed"]
    assert M.extraction_field_verdicts(case["gold"], case["predicted"]) == {
        "title": "correct",
        "assignee": "wrong",
        "filing_date": "omission",
        "jurisdiction": "correct",
        "technical_field": "omission",
        "independent_claim_count": "wrong",
        "novelty_statement": "hallucination",
        "cpc_codes": "correct",
    }


def test_extraction_metrics_golden():
    """GOLDEN, from the verdict tally above.

    correct = 3, correct_abstention = 0, wrong = 2, hallucination = 1,
    omission = 2, unscoreable = 0

    scoreable   = 3+0+2+1+2 = 8
    gold_null   = correct_abstention + hallucination = 0 + 1 = 1
    gold_stated = correct + wrong + omission        = 3 + 2 + 2 = 7
    answered    = correct + wrong + hallucination   = 3 + 2 + 1 = 6

    extraction_accuracy = (3 + 0) / 8 = 3/8  = 0.375
    answered_precision  = 3 / 6      = 1/2  = 0.5
    hallucination_rate  = 1 / 1      = 1.0
    omission_rate       = 2 / 7
    """
    case = CASES["fe_mixed"]
    out = M.extraction_metrics(case["gold"], case["predicted"])

    counts = out["extraction_accuracy"].counts
    assert counts["correct"] == 3
    assert counts["correct_abstention"] == 0
    assert counts["wrong"] == 2
    assert counts["hallucination"] == 1
    assert counts["omission"] == 2
    assert (counts["scoreable"], counts["gold_null"], counts["gold_stated"],
            counts["answered"]) == (8, 1, 7, 6)

    assert frac(out["extraction_accuracy"]) == Fraction(3, 8)
    assert frac(out["answered_precision"]) == Fraction(1, 2)
    assert frac(out["hallucination_rate"]) == Fraction(1, 1)
    assert frac(out["omission_rate"]) == Fraction(2, 7)


def test_abstaining_and_fabricating_are_told_apart_at_equal_accuracy():
    """GOLDEN, and the reason the schema uses null for "not stated".

    gold: title + assignee stated, filing_date + jurisdiction null.
      -> gold_stated = 2, gold_null = 2, scoreable = 4

    ABSTAINER answers nothing:
      omission x2 (title, assignee), correct_abstention x2
      accuracy           = (0 + 2)/4 = 0.5
      answered_precision = 0/0       -> undefined, NOT 0.0
      hallucination_rate = 0/2       = 0.0
      omission_rate      = 2/2       = 1.0

    FABRICATOR answers everything, right on the two stated fields:
      correct x2, hallucination x2
      accuracy           = (2 + 0)/4 = 0.5    <- identical
      answered_precision = 2/4       = 0.5
      hallucination_rate = 2/2       = 1.0
      omission_rate      = 0/2       = 0.0

    Same accuracy, opposite failure modes. Only hallucination_rate and
    answered_precision separate them, which is why one headline number is not
    enough for this subagent.
    """
    case = CASES["fe_abstainer_vs_fabricator"]
    fields = ("title", "assignee", "filing_date", "jurisdiction")

    shy = M.extraction_metrics(case["gold"], case["abstainer"], fields=fields)
    bold = M.extraction_metrics(case["gold"], case["fabricator"], fields=fields)

    assert shy["extraction_accuracy"].value == 0.5
    assert bold["extraction_accuracy"].value == 0.5

    assert shy["answered_precision"].status == "not_applicable"
    assert bold["answered_precision"].value == 0.5

    assert shy["hallucination_rate"].value == 0.0
    assert bold["hallucination_rate"].value == 1.0

    assert shy["omission_rate"].value == 1.0
    assert bold["omission_rate"].value == 0.0


def test_extraction_treats_a_missing_key_as_an_abstention():
    """PatentFeatures defaults its optionals to null, so an omitted key is the
    same claim as an explicit null — and must not read as a wrong answer."""
    verdicts = M.extraction_field_verdicts(
        {"title": "X", "assignee": None}, {"assignee": None}, fields=("title", "assignee")
    )
    assert verdicts == {"title": "omission", "assignee": "correct_abstention"}


def test_extraction_marks_a_field_the_gold_does_not_cover_as_unscoreable():
    out = M.extraction_metrics(
        {"title": "X"}, {"title": "X", "assignee": "Y"}, fields=("title", "assignee")
    )
    counts = out["extraction_accuracy"].counts
    assert counts["unscoreable"] == 1
    assert counts["scoreable"] == 1
    # accuracy is over the scoreable field only: 1/1
    assert out["extraction_accuracy"].value == 1.0
    # the gold states every scoreable field, so there is no room to hallucinate
    assert out["hallucination_rate"].status == "not_applicable"


def test_extraction_hallucination_rate_undefined_when_gold_states_everything():
    out = M.extraction_metrics(
        {"title": "X", "assignee": "Y"},
        {"title": "X", "assignee": "Y"},
        fields=("title", "assignee"),
    )
    assert out["hallucination_rate"].status == "not_applicable"
    assert out["extraction_accuracy"].value == 1.0


# ==========================================================================
# normalisation
# ==========================================================================


def test_normalisation_never_collapses_null_into_empty_string():
    assert M.normalize_scalar(None) is None
    assert M.normalize_scalar("") == ""
    assert M.normalize_scalar("  Solid-State   Battery ") == "solid-state battery"
    assert M.normalize_code(" h01m 10/052 ") == "H01M10/052"
    assert M.normalize_code(None) is None


def test_normalisation_leaves_non_strings_alone():
    assert M.normalize_scalar(2) == 2
    assert M.normalize_scalar(True) is True


# ==========================================================================
# G11 — aggregation keeps its denominator honest
# ==========================================================================


def test_aggregate_counts_what_it_could_not_measure():
    outcomes = [
        M.MetricOutcome(metric="m", status="ok", value=1.0, item_id="i1"),
        M.MetricOutcome(metric="m", status="ok", value=0.0, item_id="i2"),
        M.MetricOutcome(metric="m", status="ok", value=0.5, item_id="i3"),
        M.MetricOutcome(
            metric="m", status="not_applicable", detail="0/0", item_id="i4"
        ),
        M.MetricOutcome(metric="m", status="error", detail="boom", item_id="i5"),
    ]
    sample = M.aggregate(outcomes)
    assert sample.values == [1.0, 0.0, 0.5]
    assert sample.item_ids == ["i1", "i2", "i3"]
    assert sample.n == 3
    assert sample.excluded == {"not_applicable": 1, "error": 1}
    assert sample.n_considered == 5
    assert sample.excluded_items == {"i4": "0/0", "i5": "boom"}


def test_aggregate_surfaces_call_errors_inside_the_scored_items():
    trace = make_trace("query_rewriter", None, status="error", error="503")
    outcomes = [
        M.json_schema_validity("query_rewriter", CASES["qr_schema_valid"], item_id="i1"),
        M.json_schema_validity("query_rewriter", trace, item_id="i2"),
    ]
    sample = M.aggregate(outcomes)
    assert sample.values == [1.0, 0.0]
    # the 0.0 is a measurement, but the reader has to be able to see why
    assert sample.call_errors == 1


def test_aggregate_refuses_to_mix_metrics():
    with pytest.raises(ValueError, match="mixed metrics"):
        M.aggregate(
            [
                M.MetricOutcome(metric="a", status="ok", value=1.0),
                M.MetricOutcome(metric="b", status="ok", value=1.0),
            ]
        )


def test_metric_sample_rejects_ragged_arrays():
    with pytest.raises(ValueError, match="item ids"):
        M.MetricSample(metric="m", values=[1.0, 0.0], item_ids=["i1"])


# ==========================================================================
# composition helper used by T09's runner
# ==========================================================================


def test_deterministic_metrics_picks_the_right_family_per_subagent():
    qr = M.deterministic_metrics(
        "query_rewriter",
        gold=CASES["qr_mixed"]["gold"],
        source=CASES["qr_mixed"]["predicted"],
        item_id="qr-1",
    )
    assert set(qr) == {
        "json_schema_validity",
        "filter_precision",
        "filter_recall",
        "filter_f1",
        "exact_match_intent",
    }
    assert frac(qr["filter_precision"]) == Fraction(4, 7)
    assert all(o.item_id == "qr-1" for o in qr.values())

    cs = M.deterministic_metrics(
        "chunk_summarizer",
        gold=None,
        source=CASES["cs_mixed"]["payload"],
        provided_chunk_ids=CASES["cs_mixed"]["provided_chunk_ids"],
    )
    assert cs["citation_coverage"].value == 0.4

    fe = M.deterministic_metrics(
        "feature_extractor",
        gold=CASES["fe_mixed"]["gold"],
        source=CASES["fe_mixed"]["predicted"],
    )
    assert frac(fe["extraction_accuracy"]) == Fraction(3, 8)


def test_deterministic_metrics_rejects_an_unknown_subagent():
    with pytest.raises(KeyError):
        M.deterministic_metrics("root_orchestrator", gold={}, source={})


def test_extract_payload_never_invents_an_empty_object():
    assert M.extract_payload(None) is None
    assert M.extract_payload(make_trace("query_rewriter", None, text="prose")) is None
    assert M.extract_payload(make_trace("query_rewriter", {"a": 1})) == {"a": 1}
