"""Strict structured-output schemas for the three evaluated subagents.

This module is the **frozen contract between three lanes**: T06 generates gold
reference outputs against these shapes, T07 renders them into prompt packs —
either as a ``response_schema`` or as the ``parameters`` of an ``emit_*`` tool
(see :data:`TOOL_NAMES`) — and T08 scores real outputs against them. If each lane
invented its own shape the metrics would measure disagreement about the schema
rather than disagreement about the models, so the shapes live here and here
only.

Adding a field is a breaking change to recorded traces and to every gold output
already generated. Do not do it casually.

Provider portability
--------------------

Both backends take JSON Schema, but Vertex Gemini's ``response_schema`` accepts
an OpenAPI 3.0 subset that does **not** resolve ``$ref``/``$defs``. Pydantic
emits those for any nested model. :func:`json_schema` therefore inlines all
definitions and strips the keywords neither provider needs, so one dict works
on both paths — see ``tests/test_agent_schemas.py``, which asserts the output is
``$ref``-free and round-trips through a live-shaped request.

Design notes tied to the metrics in ``amw/eval/metrics.py``:

* Query Rewriter carries filters as explicit fields, not free text, because the
  filter precision/recall metric needs to compare them key by key. A date range
  buried in a query string cannot be scored.
* Chunk Summarizer attaches ``chunk_ids`` to every key point rather than one
  citation list per summary, because citation coverage asks whether *each claim*
  is grounded in a provided chunk.
* Feature Extractor uses ``null`` for "not present in the source" rather than
  omitting the key or inventing a value. An extractor that fabricates a
  plausible assignee is worse than one that abstains, and the metric has to be
  able to tell those apart (ground rule 1).
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "QueryPlan",
    "ChunkSummary",
    "PatentFeatures",
    "SUBAGENT_SCHEMAS",
    "schema_model",
    "json_schema",
    "SUBAGENTS",
    "TOOL_NAMES",
    "tool_name",
]

#: The three subagents evaluated in Act 1, in report order. Answer Drafter is
#: P1 and Root Orchestrator is a stub — neither is scored, so neither is here.
SUBAGENTS: tuple[str, ...] = ("query_rewriter", "chunk_summarizer", "feature_extractor")


class _Strict(BaseModel):
    """Refuse unknown fields. A model that invents a key should fail the
    schema-validity metric, not be quietly accepted."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# query_rewriter
# --------------------------------------------------------------------------


class QueryFilters(_Strict):
    """Structured constraints lifted out of the user's question.

    Every field is optional because most questions constrain only one or two
    dimensions. ``null`` means "the user did not ask for this" — which is a
    scoreable answer, and different from getting it wrong.
    """

    date_from: str | None = Field(
        default=None, description="Inclusive lower bound on filing date, YYYY-MM-DD."
    )
    date_to: str | None = Field(
        default=None, description="Exclusive upper bound on filing date, YYYY-MM-DD."
    )
    assignees: list[str] = Field(
        default_factory=list, description="Organisations named in the question."
    )
    jurisdictions: list[str] = Field(
        default_factory=list,
        description="Two-letter patent office codes, e.g. US, EP, JP, CN, WO.",
    )
    cpc_codes: list[str] = Field(
        default_factory=list, description="CPC classification codes, e.g. H01M10/052."
    )


class QueryPlan(_Strict):
    """Output of the Query Rewriter — the ``emit_query_plan`` payload."""

    query: str = Field(
        description="The rewritten search query: technical terms and synonyms, "
        "no filter text (filters belong in the filters object)."
    )
    filters: QueryFilters = Field(default_factory=QueryFilters)
    intent: Literal[
        "prior_art",
        "ownership",
        "landscape",
        "freedom_to_operate",
        "citation_lookup",
    ] = Field(description="What the user is trying to accomplish.")


# --------------------------------------------------------------------------
# chunk_summarizer
# --------------------------------------------------------------------------


class KeyPoint(_Strict):
    """One claim, plus the chunks it came from.

    ``chunk_ids`` is what makes the citation-coverage metric possible: a point
    citing no chunk, or citing a chunk that was never supplied, is ungrounded.
    """

    text: str = Field(description="A single factual claim, one sentence.")
    chunk_ids: list[str] = Field(
        description="IDs of the provided chunks supporting this claim. Must be "
        "non-empty and must only reference chunks that were supplied."
    )


class ChunkSummary(_Strict):
    """Output of the Chunk Summarizer."""

    summary: str = Field(description="Two to four sentences over all chunks.")
    key_points: list[KeyPoint] = Field(
        description="Each distinct claim in the summary, with its sources."
    )


# --------------------------------------------------------------------------
# feature_extractor
# --------------------------------------------------------------------------


class PatentFeatures(_Strict):
    """Output of the Feature Extractor.

    Nullable fields mean "not stated in the source". Abstaining is correct
    behaviour and must stay distinguishable from a wrong answer.
    """

    title: str | None = Field(default=None)
    assignee: str | None = Field(default=None)
    filing_date: str | None = Field(default=None, description="YYYY-MM-DD if stated.")
    jurisdiction: str | None = Field(default=None, description="Two-letter office code.")
    cpc_codes: list[str] = Field(default_factory=list)
    technical_field: str | None = Field(
        default=None, description="One short phrase, e.g. 'solid-state electrolytes'."
    )
    independent_claim_count: int | None = Field(
        default=None, description="Only if countable from the source."
    )
    novelty_statement: str | None = Field(
        default=None, description="One sentence, quoted or closely paraphrased."
    )


# --------------------------------------------------------------------------
# registry + provider-portable schema export
# --------------------------------------------------------------------------

SUBAGENT_SCHEMAS: dict[str, type[_Strict]] = {
    "query_rewriter": QueryPlan,
    "chunk_summarizer": ChunkSummary,
    "feature_extractor": PatentFeatures,
}


def schema_model(subagent: str) -> type[_Strict]:
    """Pydantic model for a subagent. Raises ``KeyError`` on an unknown name."""
    try:
        return SUBAGENT_SCHEMAS[subagent]
    except KeyError:
        raise KeyError(
            f"unknown subagent {subagent!r}; expected one of {list(SUBAGENT_SCHEMAS)}"
        ) from None


#: Name of the tool a subagent emits its structured output through.
#:
#: Lives here rather than in the prompt packs because three lanes need it and
#: none of them should re-derive it: T07 declares the tool, the adapters record
#: ``tools_offered``/``tool_calls`` under this name, and T08 has to find the
#: call in a trace to score it. ``emit_query_plan`` is the customer's own name
#: for the Query Rewriter tool (docs/master_plan.md §5.9).
#:
#: The Claude baseline and the Gemini A0 rung both emit this way: in this GCP
#: organization ``constraints/vertexai.allowedPartnerModelFeatures`` blocks
#: partner-model structured outputs, while tool use works — see
#: ``notes/org_policy_structured_outputs.md``. Tuned Gemini rungs move to
#: ``response_schema``, which is an explicit rung on the ablation ladder.
TOOL_NAMES: dict[str, str] = {
    "query_rewriter": "emit_query_plan",
    "chunk_summarizer": "emit_summary",
    "feature_extractor": "emit_features",
}


def tool_name(subagent: str) -> str:
    """Emission tool name for a subagent. Raises ``KeyError`` if unknown."""
    try:
        return TOOL_NAMES[subagent]
    except KeyError:
        raise KeyError(
            f"unknown subagent {subagent!r}; expected one of {list(TOOL_NAMES)}"
        ) from None


#: Keywords pydantic emits that the providers' schema subsets do not consume.
#: Dropping them keeps the payload small and avoids provider-side rejections.
#:
#: These are *keywords*, not names. ``title`` is both — a JSON Schema annotation
#: and a real field on :class:`PatentFeatures`. So the walk below only consults
#: this set for schema nodes, never for the keys of a ``properties`` map, which
#: are user-chosen field names.
_DROP_KEYS = frozenset({"$defs", "$schema", "additionalProperties", "title", "default"})

#: Keywords whose value is a mapping of *names* to subschemas. Their keys are
#: data, so recurse into the values but leave the keys strictly alone.
_NAME_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions"})


def _inline(node: Any, defs: dict[str, Any]) -> Any:
    """Recursively resolve ``$ref`` against ``defs`` and drop unused keywords."""
    if isinstance(node, list):
        return [_inline(item, defs) for item in node]
    if not isinstance(node, dict):
        return node

    if "$ref" in node:
        # "#/$defs/QueryFilters" -> defs["QueryFilters"]
        name = node["$ref"].rsplit("/", 1)[-1]
        if name not in defs:  # pragma: no cover - pydantic always emits the def
            raise KeyError(f"unresolvable $ref {node['$ref']!r}")
        resolved = _inline(copy.deepcopy(defs[name]), defs)
        # A `$ref` can carry siblings — pydantic puts a field-level description
        # next to it. Those are more specific than the referenced model's own,
        # so they win.
        siblings = _inline({k: v for k, v in node.items() if k != "$ref"}, defs)
        return {**resolved, **siblings} if isinstance(resolved, dict) else resolved

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _DROP_KEYS:
            continue
        if key in _NAME_MAPS and isinstance(value, dict):
            out[key] = {name: _inline(sub, defs) for name, sub in value.items()}
            continue
        # anyOf: [X, {"type": "null"}] is how pydantic spells `X | None`.
        # Both providers accept nullable via the collapsed branch; keeping the
        # union intact makes Gemini reject the schema outright.
        if key == "anyOf" and isinstance(value, list):
            non_null = [b for b in value if b.get("type") != "null"]
            if len(non_null) == 1:
                out.update(_inline(non_null[0], defs))
                out["nullable"] = True
                continue
        out[key] = _inline(value, defs)
    return out


def json_schema(subagent: str) -> dict[str, Any]:
    """Provider-portable JSON Schema for a subagent's output.

    Fully inlined: no ``$ref``, no ``$defs``. Safe to hand to Gemini's
    ``response_schema`` and to Claude's ``output_config.format.schema``.
    """
    raw = schema_model(subagent).model_json_schema()
    defs = raw.get("$defs", {})
    return _inline(raw, defs)
