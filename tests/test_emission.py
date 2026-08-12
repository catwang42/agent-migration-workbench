"""Was the emission structurally broken, or just worse?

The Query Rewriter shadow run adjudicates 15 wins for the Gemini candidate. Six
of them are items where the Claude baseline's tool call emitted a structurally
broken object, so "15 wins" is not 15 quality wins. These tests pin the
boundary between the two, because everything downstream — the second
adjudication figure, the caveat text, the alt-clause evidence string — is only
as honest as this predicate.

What is deliberately *not* tested here: any notion of an answer being good
enough. A wrong-but-well-formed payload must come back ``None``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from amw.shadow import emission as E
from amw.traces.schema import LatencyMs, Trace, TraceInput, TraceOutput


def trace(
    payload: dict | list | None = None,
    *,
    subagent: str = "query_rewriter",
    text: str | None = None,
    status: str = "ok",
) -> Trace:
    return Trace(
        trace_id=f"{subagent}-emission-fixture",
        subagent=subagent,
        provenance="synthetic",
        ts=datetime(2026, 8, 11, tzinfo=timezone.utc),
        model="claude-sonnet",
        system_prompt_sha="0" * 16,
        input=TraceInput(messages=["fixture"]),
        output=TraceOutput(text=text, json=payload),
        status=status,  # type: ignore[arg-type]
        error=None if status == "ok" else "deadline exceeded",
        latency_ms=LatencyMs(),
    )


GOOD_QR = {
    "query": "polymer electrolyte separator",
    "intent": "prior_art",
    "filters": {"assignees": ["Acme"]},
}


# ==========================================================================
# the four conditions
# ==========================================================================


def test_a_well_formed_payload_is_not_malformed():
    assert E.malformed_emission("query_rewriter", trace(GOOD_QR)) is None


def test_a_wrong_answer_that_validates_is_a_quality_difference_not_a_defect():
    """The whole point of the predicate: bad content is not a structural fault.

    If this ever starts returning a reason, the "excluding structurally
    malformed" figure silently becomes "excluding items Claude lost", which is
    the exact misuse the exclusion exists to prevent.
    """
    wrong = dict(GOOD_QR, query="battery", intent="prior_art", filters={})
    assert E.malformed_emission("query_rewriter", trace(wrong)) is None


def test_an_errored_call_emitted_nothing():
    reason = E.malformed_emission("query_rewriter", trace(None, status="error"))
    assert reason is not None
    assert "the call failed" in reason
    assert "deadline exceeded" in reason, "the recorded error is quoted, not summarised"


def test_a_response_with_no_json_object_is_malformed():
    reason = E.malformed_emission("query_rewriter", trace(None, text="here you go!"))
    assert reason == "the response carried no JSON object"


def test_a_payload_that_fails_the_frozen_schema_is_malformed():
    reason = E.malformed_emission("query_rewriter", trace({"intent": "prior_art"}))
    assert reason is not None
    assert reason.startswith("the payload does not validate")


def test_the_validation_reason_names_the_field_not_just_the_count():
    """Pydantic's first line is "N validation errors for QueryPlan" — useless.

    A triage row that says only how many things were wrong cannot be checked by
    the reader, so the field path and the message have to survive into the cell.
    """
    reason = E.malformed_emission("query_rewriter", trace({"intent": "prior_art"}))
    assert "query" in reason
    assert "missing" in reason.lower()


def test_double_encoding_is_detected_and_names_the_field():
    payload = {
        "query": json.dumps(GOOD_QR),
        "intent": "prior_art",
        "filters": {},
    }
    reason = E.malformed_emission("query_rewriter", trace(payload))
    assert reason == (
        "the whole payload was re-encoded as a JSON string inside the 'query' field"
    )


def test_double_encoding_is_diagnosed_ahead_of_schema_invalidity():
    """Both conditions hold on the real traces; only one names the mechanism.

    Six of the thirteen malformed Query Rewriter rows are double-encoded *and*
    schema-invalid. Reporting them as "does not validate" would hide that the
    cause is tool-call emission — the thing the org-policy caveat is about.
    """
    payload = {"query": json.dumps(GOOD_QR)}  # missing `intent` as well
    reason = E.malformed_emission("query_rewriter", trace(payload))
    assert reason.startswith("the whole payload was re-encoded")


# ==========================================================================
# the boundary: what is *not* double encoding
# ==========================================================================


def test_a_string_field_holding_unrelated_json_is_not_a_re_encoded_payload():
    payload = dict(GOOD_QR, query='{"foo": 1, "bar": 2}')
    assert E.malformed_emission("query_rewriter", trace(payload)) is None


def test_one_shared_key_is_a_coincidence_not_proof():
    """`MIN_SHARED_KEYS` is 2. A single overlapping name is reachable by chance."""
    payload = dict(GOOD_QR, query='{"query": "nested"}')
    assert E.malformed_emission("query_rewriter", trace(payload)) is None
    assert E.MIN_SHARED_KEYS == 2


def test_plain_prose_in_a_string_field_is_not_parsed_as_json():
    payload = dict(GOOD_QR, query="a query about {curly braces}")
    assert E.malformed_emission("query_rewriter", trace(payload)) is None


def test_no_trace_is_not_a_finding():
    """An absent baseline trace is a coverage gap, not a structural failure."""
    assert E.malformed_emission("query_rewriter", None) is None


# ==========================================================================
# the predicate is per-subagent, off the frozen schemas
# ==========================================================================


@pytest.mark.parametrize(
    "subagent,expected",
    [
        ("query_rewriter", {"query", "intent", "filters"}),
        ("chunk_summarizer", {"summary", "key_points"}),
    ],
)
def test_field_names_come_from_the_frozen_schema(subagent, expected):
    assert E.schema_field_names(subagent) == expected


def test_the_same_bytes_are_judged_against_the_right_subagents_schema():
    """A query plan is malformed *as a summary*, and vice versa."""
    assert E.malformed_emission("query_rewriter", trace(GOOD_QR)) is None
    reason = E.malformed_emission(
        "chunk_summarizer", trace(GOOD_QR, subagent="chunk_summarizer")
    )
    assert reason is not None and reason.startswith("the payload does not validate")
