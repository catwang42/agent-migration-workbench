"""Was an arm's output *structurally* malformed, as opposed to merely worse?

Why this exists
---------------

The Query Rewriter shadow run adjudicates 15 wins for the Gemini candidate
against the Claude baseline, which clears the ``shadow_agreement`` gate's
pre-registered ``alt`` clause. Read alone, "15 wins" says Gemini wrote better
query plans 15 times. Ten of those items are something else: the Claude call
emitted a payload that was structurally broken — the whole object re-encoded as
a JSON string inside one field, or a required field missing — so the judge was
comparing a working output against a broken one. That is a real difference a
customer would experience, and it is *not* a claim about rewriting quality.

Both figures are reported everywhere the adjudication appears, per the
2026-08-11 ruling. The alt clause holds either way (15W/3L overall, and the
quality-only tally), which is exactly why quoting only the flattering one would
be gratuitous as well as wrong.

This is the same artifact as the mandatory org-policy caveat printed beside the
Claude baseline: under this demo organization's Vertex AI policy configuration
(``constraints/vertexai.allowedPartnerModelFeatures``), partner-model
structured outputs were unavailable, so the Claude baseline was measured using
tool-call structured emission. Tool-call emission is where double-encoding
happens. Same cause, same treatment: disclosed, counted, and never used to
inflate a win rate.

What counts as malformed
------------------------

Four conditions, checked in order, each returning a one-line reason:

1. the call errored, so nothing was emitted;
2. the response carried no JSON object at all;
3. the payload does not validate against the subagent's frozen schema;
4. **double encoding** — a *string* field whose contents parse as a JSON object
   that itself carries this subagent's own field names. That is the emission
   putting the whole payload where one value belongs. It passes (3), because
   the field really is a string, which is why it needs its own check.

Everything else is ``None``: the emission worked and any disagreement is about
content. Note what is deliberately *not* here — no threshold, no heuristic
about whether an output is "good enough". A wrong-but-well-formed answer is a
quality difference and is counted as one.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from amw.agents.schemas import schema_model
from amw.eval.metrics import extract_payload
from amw.traces.schema import Trace

__all__ = [
    "MIN_SHARED_KEYS",
    "malformed_emission",
    "schema_field_names",
]

#: How many of the subagent's own field names an embedded object must carry
#: before it is called a re-encoded payload rather than a coincidence. Two,
#: because one shared key is reachable by accident — a ``query`` field whose
#: value is a JSON object with a ``query`` key is odd but not proof — and every
#: subagent schema here has at least three fields.
MIN_SHARED_KEYS = 2


def schema_field_names(subagent: str) -> frozenset[str]:
    """Top-level field names in the subagent's frozen response schema."""
    schema = schema_model(subagent).model_json_schema()
    return frozenset(schema.get("properties", {}))


def _reencoded_field(subagent: str, payload: Mapping[str, Any]) -> str | None:
    """Name of the field the whole payload was stuffed into, if any."""
    names = schema_field_names(subagent)
    for field, value in payload.items():
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text.startswith("{"):
            continue
        try:
            inner = json.loads(text)
        except ValueError:
            continue
        if isinstance(inner, dict) and len(names & set(inner)) >= MIN_SHARED_KEYS:
            return field
    return None


def malformed_emission(subagent: str, trace: Trace | None) -> str | None:
    """One-line reason this emission is structurally broken, or ``None``.

    ``None`` means the arm emitted a well-formed, schema-valid payload. It does
    **not** mean the payload was right.
    """
    if trace is None:
        return None
    if trace.status == "error":
        return f"the call failed, so nothing was emitted: {trace.error}"
    payload = extract_payload(trace)
    if payload is None:
        return "the response carried no JSON object"

    # Double encoding is checked *first*. It is the more specific diagnosis and
    # it is not implied by the other one: a re-encoded payload may validate
    # perfectly well (a string field holding a JSON string is still a string),
    # and when it does not, "does not validate" hides the mechanism.
    field = _reencoded_field(subagent, payload)
    if field is not None:
        return (
            f"the whole payload was re-encoded as a JSON string inside the "
            f"{field!r} field"
        )
    try:
        schema_model(subagent).model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - any validation failure is malformed
        return "the payload does not validate: " + _first_complaint(exc)
    return None


def _first_complaint(exc: Exception) -> str:
    """The field and the reason, not just "N validation errors for X".

    Pydantic's first line is a count and a model name, which is useless in a
    triage table. The field path and the message are on the next two lines.
    """
    lines = [line.strip() for line in f"{exc}".splitlines() if line.strip()]
    head = lines[0] if lines else type(exc).__name__
    detail = " ".join(lines[1:3])
    return f"{head} ({detail})" if detail else head
