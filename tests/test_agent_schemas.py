"""Guards on the frozen output contract in ``amw/agents/schemas.py``.

Three lanes build against these shapes — the dataset generator writes gold
outputs in them, the prompt packs ship them to the providers, the eval engine
scores against them — so a silent change here would show up as a model
difference in the scorecard rather than as a bug.

Two classes of guard:

* **Provider portability.** Vertex Gemini's ``response_schema`` accepts an
  OpenAPI 3.0 subset that does not resolve ``$ref``/``$defs``, which pydantic
  emits for every nested model. ``json_schema()`` inlines them; the tests below
  assert the export is genuinely reference-free, all the way down.
* **Nothing gets lost in the inlining.** ``json_schema()`` strips JSON Schema
  *keywords* the providers ignore. ``title`` is both a keyword and a real field
  on :class:`PatentFeatures`, and an earlier version of the walk dropped the
  field along with the keyword — the schema still compiled, and the extractor
  simply stopped being asked for a patent title. The
  ``exported properties == model fields`` tests below are the regression.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from amw.agents.schemas import (
    SUBAGENT_SCHEMAS,
    SUBAGENTS,
    TOOL_NAMES,
    ChunkSummary,
    PatentFeatures,
    QueryPlan,
    json_schema,
    schema_model,
    tool_name,
)


def walk(node, path="$"):
    """Yield ``(path, node)`` for every mapping in a schema document."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------


def test_registry_covers_exactly_the_evaluated_subagents():
    assert SUBAGENTS == ("query_rewriter", "chunk_summarizer", "feature_extractor")
    assert set(SUBAGENT_SCHEMAS) == set(SUBAGENTS)
    assert set(TOOL_NAMES) == set(SUBAGENTS)
    assert "answer_drafter" not in SUBAGENTS  # P1
    assert "root_orchestrator" not in SUBAGENTS  # stub, never evaluated


def test_models_and_tool_names_are_the_expected_ones():
    assert schema_model("query_rewriter") is QueryPlan
    assert schema_model("chunk_summarizer") is ChunkSummary
    assert schema_model("feature_extractor") is PatentFeatures
    assert tool_name("query_rewriter") == "emit_query_plan"
    assert tool_name("chunk_summarizer") == "emit_summary"
    assert tool_name("feature_extractor") == "emit_features"


def test_unknown_subagent_raises_with_the_known_names():
    for lookup in (schema_model, tool_name, json_schema):
        with pytest.raises(KeyError, match="answer_drafter"):
            lookup("answer_drafter")


# --------------------------------------------------------------------------
# provider portability
# --------------------------------------------------------------------------


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_export_is_ref_free_and_defs_free(subagent):
    """Gemini's response_schema will not resolve a reference. Not one, anywhere."""
    schema = json_schema(subagent)
    offenders = [
        (path, sorted(set(node) & {"$ref", "$defs", "definitions", "$schema"}))
        for path, node in walk(schema)
        if set(node) & {"$ref", "$defs", "definitions", "$schema"}
    ]
    assert offenders == []


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_export_is_json_serialisable_and_a_valid_schema(subagent):
    schema = json_schema(subagent)
    assert json.loads(json.dumps(schema)) == schema
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_export_is_a_fresh_object_each_call(subagent):
    """Callers mutate schemas (a tool's ``parameters``); no shared state."""
    first = json_schema(subagent)
    first["properties"].pop(next(iter(first["properties"])))
    assert json_schema(subagent) != first


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_nullable_unions_are_collapsed(subagent):
    """``X | None`` must not reach Gemini as ``anyOf`` — it rejects the schema."""
    for path, node in walk(json_schema(subagent)):
        assert "anyOf" not in node, f"un-collapsed union at {path}"


def test_optional_field_is_nullable_with_a_concrete_type():
    filing_date = json_schema("feature_extractor")["properties"]["filing_date"]
    assert filing_date["type"] == "string"
    assert filing_date["nullable"] is True


# --------------------------------------------------------------------------
# nothing gets lost in the inlining  (the `title` regression)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_exported_properties_match_the_model_fields(subagent):
    model = schema_model(subagent)
    exported = json_schema(subagent)["properties"]
    assert set(exported) == set(model.model_fields)


def test_patent_features_keeps_its_title_field():
    """Regression: ``title`` is a field here *and* a JSON Schema keyword.

    The keyword-stripping walk once removed both, so the exported schema asked
    the extractor for everything except the patent's title — silently, because
    the schema still compiled and every other field kept working.
    """
    schema = json_schema("feature_extractor")
    assert "title" in schema["properties"]
    assert schema["properties"]["title"]["type"] == "string"
    assert schema["properties"]["title"]["nullable"] is True
    # ...and the annotation form of the keyword is still stripped at node level.
    assert "title" not in {key for key in schema if key != "properties"}


def test_nested_models_keep_their_fields_after_inlining():
    filters = json_schema("query_rewriter")["properties"]["filters"]
    assert set(filters["properties"]) == set(QueryPlan.model_fields["filters"].annotation.model_fields)
    assert filters["properties"]["cpc_codes"]["type"] == "array"

    key_points = json_schema("chunk_summarizer")["properties"]["key_points"]
    assert set(key_points["items"]["properties"]) == {"text", "chunk_ids"}


def test_field_descriptions_survive_the_export():
    """Descriptions are instructions to the model, not decoration."""
    query = json_schema("query_rewriter")["properties"]["query"]
    assert "filters" in query["description"]
    chunk_ids = json_schema("chunk_summarizer")["properties"]["key_points"]["items"][
        "properties"
    ]["chunk_ids"]
    assert "non-empty" in chunk_ids["description"]


def test_required_lists_survive_the_export():
    assert set(json_schema("query_rewriter")["required"]) == {"query", "intent"}
    assert set(json_schema("chunk_summarizer")["required"]) == {"summary", "key_points"}
    assert "required" not in json_schema("feature_extractor")  # every field optional


def test_intent_enum_survives_the_export():
    intent = json_schema("query_rewriter")["properties"]["intent"]
    assert set(intent["enum"]) == {
        "prior_art",
        "ownership",
        "landscape",
        "freedom_to_operate",
        "citation_lookup",
    }


# --------------------------------------------------------------------------
# the models themselves
# --------------------------------------------------------------------------


def test_models_round_trip_a_valid_payload():
    plan = QueryPlan.model_validate(
        {
            "query": "lithium metal anode dendrite suppression",
            "filters": {"assignees": ["Toyota"], "jurisdictions": ["JP"]},
            "intent": "prior_art",
        }
    )
    assert plan.filters.date_from is None
    assert plan.model_dump()["filters"]["cpc_codes"] == []

    summary = ChunkSummary.model_validate(
        {"summary": "s", "key_points": [{"text": "t", "chunk_ids": ["c1"]}]}
    )
    assert summary.key_points[0].chunk_ids == ["c1"]

    assert PatentFeatures.model_validate({}).title is None


def test_unknown_keys_are_refused():
    """A model that invents a key must fail schema validity, not pass quietly.

    This is the failure recorded in ``notes/org_policy_structured_outputs.md``:
    a live Claude probe wrapped the object in an extra ``features`` key.
    """
    with pytest.raises(ValidationError):
        PatentFeatures.model_validate({"features": {"title": "x"}})
    with pytest.raises(ValidationError):
        QueryPlan.model_validate(
            {"query": "q", "intent": "prior_art", "confidence": 0.9}
        )


def test_intent_is_constrained():
    with pytest.raises(ValidationError):
        QueryPlan.model_validate({"query": "q", "intent": "vibes"})
