"""T07 verify: every prompt variant renders, compiles, and stays in its lane.

Three properties matter more than the rest, because breaking any of them makes
the ablation ladder measure something other than what it claims:

1. ``gemini_naive`` is the Claude baseline file **byte for byte**. A0 is "the
   XML fed verbatim"; if the naive prompt gets quietly improved, the
   Claude→A0 delta stops being a prompt-format measurement.
2. Baseline and A0 use the **same emission mechanism** (an ``emit_*`` tool), so
   that delta is not partly a mechanism change. Strict ``response_schema``
   arrives at the tuned rung, where the ladder says it belongs
   (``notes/org_policy_structured_outputs.md``).
3. The tuned variants contain **no XML instruction tags**. That is the
   intervention; a tuned prompt that grew ``<rules>`` back is testing nothing.

Everything here runs offline with zero credentials (CLAUDE.md ground rule 4):
loading a pack touches the filesystem and ``config/models.yaml`` only.
"""

from __future__ import annotations

import json
import re

import pytest

from amw.adapters.base import ModelRequest
from amw.adapters.claude_anthropic import _to_json_schema
from amw.agents import prompt_packs as pp
from amw.agents.schemas import SUBAGENTS, json_schema, tool_name

#: The universal ladder: three variants every subagent has. Tests about the
#: ladder's *shape* (baseline → naive → tuned) belong here.
ALL_PACKS = [(subagent, variant) for subagent in SUBAGENTS for variant in pp.VARIANTS]
IDS = [f"{subagent}/{variant}" for subagent, variant in ALL_PACKS]

#: Every pack on disk, including the Feature-Extractor-only 2×2 cells that
#: unbundle A1–A3. Those are real arms that get run against customers' eyes,
#: not fixtures, so the mechanism invariants have to hold for them too.
EVERY_PACK = [(s, v) for s in SUBAGENTS for v in pp.variants_for(s)]
EVERY_IDS = [f"{s}/{v}" for s, v in EVERY_PACK]
TOOL_PACKS = [(s, v) for s, v in EVERY_PACK if pp.VARIANT_SPECS[v].output_mode == "tool"]
TOOL_IDS = [f"{s}/{v}" for s, v in TOOL_PACKS]

#: Deliberately the universal three only. This is a claim about the *tuned
#: rewrite* — the FE ``_schema`` cells keep A0's XML wording on purpose,
#: because their whole job is to move the output mode and nothing else.
SCHEMA_VARIANTS = [v for v in pp.VARIANTS if pp.VARIANT_SPECS[v].output_mode != "tool"]


@pytest.fixture(scope="module")
def packs() -> dict[tuple[str, str], pp.PromptPack]:
    """The universal three per subagent — what ``load_packs()`` returns."""
    return pp.load_packs()


@pytest.fixture(scope="module")
def every_pack() -> dict[tuple[str, str], pp.PromptPack]:
    """Every pack on disk, subagent-specific variants included."""
    return {key: pp.load_pack(*key) for key in EVERY_PACK}


# --------------------------------------------------------------------------
# the files exist and parse
# --------------------------------------------------------------------------


def test_every_subagent_has_all_three_variants(packs):
    assert set(packs) == set(ALL_PACKS)
    assert pp.VARIANTS == ("claude_baseline", "gemini_naive", "gemini_tuned_v1")


def test_subagent_specific_variants_stay_out_of_the_universal_ladder():
    """``VARIANTS`` is phase2's default arm list — a variant only one subagent
    has a file for must not sneak into it, or a plain phase2 run tries to load
    a Query Rewriter prompt that does not and should not exist."""
    assert set(pp.VARIANTS) <= set(pp.ALL_VARIANTS)
    for variant in set(pp.ALL_VARIANTS) - set(pp.VARIANTS):
        assert pp.VARIANT_SPECS[variant].subagents is not None
    assert pp.variants_for("feature_extractor") == pp.ALL_VARIANTS
    assert pp.variants_for("query_rewriter") == pp.VARIANTS
    with pytest.raises(pp.PromptPackError, match="feature_extractor"):
        pp.load_pack("query_rewriter", "gemini_novelty_v1_tool")


@pytest.mark.parametrize(("subagent", "variant"), EVERY_PACK, ids=EVERY_IDS)
def test_pack_is_a_versioned_file_with_content(every_pack, subagent, variant):
    """Prompts are files on disk, not string literals — customers read them."""
    pack = every_pack[(subagent, variant)]
    assert pack.path == pp.prompts_dir() / subagent / f"{variant}.txt"
    assert pack.path.is_file()
    assert pack.text == pack.path.read_text(encoding="utf-8")
    assert pack.system.strip()
    assert pack.user_template.strip()
    assert len(pack.sha256) == 64


def test_no_stray_prompt_files_in_the_pack_directories():
    """A file nobody loads is a prompt nobody reviews."""
    for subagent in SUBAGENTS:
        found = {path.name for path in (pp.prompts_dir() / subagent).glob("*")}
        assert found == {f"{variant}.txt" for variant in pp.variants_for(subagent)}


def test_root_orchestrator_has_no_prompt_pack():
    """Stub only (not evaluated). A prompt file would imply a runnable arm."""
    assert not (pp.prompts_dir() / "root_orchestrator").exists()


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("subagent", "variant"), EVERY_PACK, ids=EVERY_IDS)
def test_variant_renders_with_a_sample_item(every_pack, subagent, variant):
    item = pp.sample_item(subagent)
    rendered = every_pack[(subagent, variant)].render(item)

    assert rendered.system_prompt.strip()
    assert len(rendered.messages) == 1
    assert rendered.messages[0].strip()
    for text in [rendered.system_prompt, *rendered.messages, *rendered.context_chunks]:
        assert "{{" not in text and "}}" not in text


def test_rendered_prompt_carries_the_item_content(packs):
    item = pp.sample_item("query_rewriter")
    for variant in pp.VARIANTS:
        rendered = packs[("query_rewriter", variant)].render(item)
        assert item["question"] in rendered.messages[0]


@pytest.mark.parametrize("variant", pp.VARIANTS)
def test_chunk_ids_reach_the_model(packs, variant):
    """The adapters add no labels, so chunk ids must come from the pack.

    ``tests/test_adapter_parity.py`` pinned ``context_chunks -> one user turn,
    one text unit per chunk, no glue``. Citation coverage is unscoreable if the
    model never sees which chunk is which.
    """
    item = pp.sample_item("chunk_summarizer")
    rendered = packs[("chunk_summarizer", variant)].render(item)

    assert len(rendered.context_chunks) == len(item["chunks"])
    for chunk, text in zip(item["chunks"], rendered.context_chunks):
        assert chunk["chunk_id"] in text
        assert chunk["text"] in text


def test_render_ignores_dataset_bookkeeping(packs):
    """Gold outputs and rubrics travel on the item and must not leak in."""
    item = pp.sample_item("feature_extractor")
    item["gold"] = {"assignee": "LEAKED-GOLD-VALUE"}
    item["rubric"] = ["LEAKED-RUBRIC-CRITERION"]
    item["difficulty"] = "edge"

    rendered = packs[("feature_extractor", "gemini_tuned_v1")].render(item)
    blob = rendered.system_prompt + "".join(rendered.messages)
    assert "LEAKED-GOLD-VALUE" not in blob
    assert "LEAKED-RUBRIC-CRITERION" not in blob


def test_missing_placeholder_names_the_key(packs):
    with pytest.raises(pp.PromptPackError) as excinfo:
        packs[("query_rewriter", "claude_baseline")].render({"quesiton": "typo"})
    assert "question" in str(excinfo.value)


def test_prerendered_dataset_item_is_refused_with_the_fix(packs):
    """T06 items carry ``input.messages`` already rendered; packs render it.

    Accepting a pre-rendered user turn would make every arm show the model the
    same surface, which is exactly what the format rungs are supposed to vary.
    The error has to name the seam rather than guess ``messages[0]``.
    """
    item = {
        "item_id": "qr-0007",
        "input": {"messages": ["Which companies filed ...?"], "chunks": []},
    }
    with pytest.raises(pp.PromptPackError, match="pre-rendered"):
        packs[("query_rewriter", "claude_baseline")].render(item)


def test_chunk_pack_without_chunks_fails_loudly(packs):
    with pytest.raises(pp.PromptPackError, match="chunks"):
        packs[("chunk_summarizer", "claude_baseline")].render({"question": "q"})


# --------------------------------------------------------------------------
# A0 is genuinely verbatim
# --------------------------------------------------------------------------


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_naive_is_the_baseline_byte_for_byte(packs, subagent):
    baseline = packs[(subagent, "claude_baseline")]
    naive = packs[(subagent, "gemini_naive")]

    assert naive.text == baseline.text
    assert naive.sha256 == baseline.sha256


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_baseline_and_naive_render_identically(packs, subagent):
    item = pp.sample_item(subagent)
    assert packs[(subagent, "gemini_naive")].render(item) == packs[
        (subagent, "claude_baseline")
    ].render(item)


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_tuned_variant_is_a_real_rewrite(packs, subagent):
    assert packs[(subagent, "gemini_tuned_v1")].text != packs[
        (subagent, "claude_baseline")
    ].text


# --------------------------------------------------------------------------
# format interventions: XML out, Markdown in
# --------------------------------------------------------------------------


@pytest.mark.parametrize("subagent", SUBAGENTS)
@pytest.mark.parametrize("variant", SCHEMA_VARIANTS)
def test_tuned_variant_has_no_xml_instruction_tags(packs, subagent, variant):
    tags = pp.find_xml_tags(packs[(subagent, variant)].text)
    assert tags == [], f"{subagent}/{variant} still contains XML tags: {tags}"


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_baseline_really_is_xml_tagged(packs, subagent):
    """Guards the other direction: a neutral baseline would flatten the delta.

    The workshop exists to show what happens to a Claude-shaped prompt when it
    is ported. If someone quietly rewrites the baseline into clean Markdown,
    every ladder rung measures less than it should.
    """
    tags = set(pp.find_xml_tags(packs[(subagent, "claude_baseline")].text))
    assert {"instructions", "output_format"} <= tags


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_tuned_variant_uses_markdown_headings(packs, subagent):
    text = packs[(subagent, "gemini_tuned_v1")].text
    assert re.search(r"^# Role$", text, re.MULTILINE)
    assert re.search(r"^# Output$", text, re.MULTILINE)


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_tuned_variant_has_exactly_two_few_shots(packs, subagent):
    """Rung A3 is "few-shot recalibration" — two examples, per the task card."""
    text = packs[(subagent, "gemini_tuned_v1")].text
    assert len(re.findall(r"^## Example\b.*$", text, re.MULTILINE)) == 2


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_tuned_variant_splits_system_from_user_content(packs, subagent):
    """A1 is the system-instruction split: instructions in system, item in user."""
    pack = packs[(subagent, "gemini_tuned_v1")]
    placeholders = re.findall(r"\{\{\s*(\w+)\s*\}\}", pack.user_template)
    assert placeholders, "the user section must carry the item"
    # The user turn is the item and (at most) a one-word label, nothing else.
    boilerplate = re.sub(r"\{\{\s*\w+\s*\}\}", "", pack.user_template).strip()
    assert len(boilerplate) <= 16, f"user section carries instructions: {boilerplate!r}"
    assert len(pack.system) > 10 * max(len(pack.user_template), 1)


# --------------------------------------------------------------------------
# output mechanism: exactly one, and the right one
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("subagent", "variant"), EVERY_PACK, ids=EVERY_IDS)
def test_exactly_one_output_mechanism(every_pack, subagent, variant):
    """Gemini raises ConfigError when a request carries tools *and* a schema."""
    pack = every_pack[(subagent, variant)]
    has_tool = bool(pack.tool_specs())
    has_schema = pack.response_schema() is not None
    assert has_tool != has_schema


@pytest.mark.parametrize(("subagent", "variant"), TOOL_PACKS, ids=TOOL_IDS)
def test_tool_variants_offer_the_subagents_emit_tool(every_pack, subagent, variant):
    (spec,) = every_pack[(subagent, variant)].tool_specs()
    assert spec.name == tool_name(subagent)
    assert spec.description.strip()
    assert spec.parameters == json_schema(subagent)


def test_baseline_and_naive_share_the_mechanism():
    """Otherwise part of the Claude→A0 gap is the mechanism, not the prompt."""
    baseline = pp.VARIANT_SPECS["claude_baseline"]
    naive = pp.VARIANT_SPECS["gemini_naive"]
    assert baseline.output_mode == naive.output_mode == "tool"
    assert pp.VARIANT_SPECS["gemini_tuned_v1"].output_mode == "response_schema"


@pytest.mark.parametrize(("subagent", "variant"), TOOL_PACKS, ids=TOOL_IDS)
def test_tool_prompts_name_the_tool_they_call(every_pack, subagent, variant):
    assert tool_name(subagent) in every_pack[(subagent, variant)].text


@pytest.mark.parametrize(("subagent", "variant"), EVERY_PACK, ids=EVERY_IDS)
def test_schema_compiles_and_serialises(every_pack, subagent, variant):
    """"Schemas compile": JSON-serialisable and a valid schema document."""
    pack = every_pack[(subagent, variant)]
    schema = pack.response_schema()
    if schema is None:
        (spec,) = pack.tool_specs()
        schema = spec.parameters

    assert json.loads(json.dumps(schema)) == schema
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(schema)


# --------------------------------------------------------------------------
# request construction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("subagent", "variant"), EVERY_PACK, ids=EVERY_IDS)
def test_build_request_produces_a_runnable_request(subagent, variant):
    item = pp.sample_item(subagent)
    request = pp.build_request(subagent, variant, item)

    assert isinstance(request, ModelRequest)
    assert request.subagent == subagent
    assert request.item_id == item["item_id"]
    assert not (request.tools and request.response_schema is not None)
    assert request.input_sha  # replay key derivable without a live call


def test_build_request_resolves_the_model_from_config_roles():
    """No model IDs in code: the variant names a role, models.yaml decides."""
    from amw.config import load_all

    models = load_all().models
    claude_key, _ = models.for_role("claude_baseline")
    gemini_key, _ = models.for_role("gemini_candidate")

    assert pp.build_request("query_rewriter", "claude_baseline", pp.sample_item("query_rewriter")).model == claude_key
    assert pp.build_request("query_rewriter", "gemini_naive", pp.sample_item("query_rewriter")).model == gemini_key
    assert pp.build_request("query_rewriter", "gemini_tuned_v1", pp.sample_item("query_rewriter")).model == gemini_key


def test_model_override_supports_the_escalation_rung():
    request = pp.build_request(
        "query_rewriter", "gemini_tuned_v1", pp.sample_item("query_rewriter"), model="gemini-pro"
    )
    assert request.model == "gemini-pro"


def test_replay_key_is_stable_across_calls_and_differs_by_variant():
    item = pp.sample_item("chunk_summarizer")
    naive = pp.build_request("chunk_summarizer", "gemini_naive", item)
    again = pp.build_request("chunk_summarizer", "gemini_naive", item)
    tuned = pp.build_request("chunk_summarizer", "gemini_tuned_v1", item)

    assert naive.replay_key == again.replay_key
    assert naive.replay_key != tuned.replay_key


def test_baseline_and_naive_differ_only_by_model():
    """Same prompt, same tools, same input_sha — only the backend changes."""
    item = pp.sample_item("query_rewriter")
    baseline = pp.build_request("query_rewriter", "claude_baseline", item)
    naive = pp.build_request("query_rewriter", "gemini_naive", item)

    assert baseline.input_sha == naive.input_sha
    assert baseline.model != naive.model


def test_temperature_is_left_unset_by_default():
    """Claude 400s on temperature; Gemini's adapter defaults it to 0 itself."""
    request = pp.build_request("query_rewriter", "claude_baseline", pp.sample_item("query_rewriter"))
    assert request.temperature is None


# --------------------------------------------------------------------------
# the packs survive translation into real provider payloads
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def models():
    from amw.config import load_all

    return load_all().models


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_tuned_request_builds_a_gemini_config(models, subagent):
    """The strict schema has to be accepted by the real SDK, not just by us."""
    types = pytest.importorskip("google.genai.types")
    from amw.adapters.gemini import GeminiAdapter

    request = pp.build_request(subagent, "gemini_tuned_v1", pp.sample_item(subagent))
    config = GeminiAdapter(models=models)._build_config(request, types)

    assert config.response_schema == json_schema(subagent)
    assert config.response_mime_type == "application/json"
    assert not config.tools
    assert config.system_instruction == request.system_prompt


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_naive_request_builds_a_gemini_tool_config(models, subagent):
    types = pytest.importorskip("google.genai.types")
    from amw.adapters.gemini import GeminiAdapter

    request = pp.build_request(subagent, "gemini_naive", pp.sample_item(subagent))
    config = GeminiAdapter(models=models)._build_config(request, types)

    (tool,) = config.tools
    (declaration,) = tool.function_declarations
    assert declaration.name == tool_name(subagent)
    assert config.response_schema is None


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_baseline_request_builds_claude_kwargs_with_the_prompt_verbatim(models, subagent):
    from amw.adapters.claude_vertex import ClaudeVertexAdapter

    pack = pp.load_pack(subagent, "claude_baseline")
    request = pp.build_request(subagent, "claude_baseline", pp.sample_item(subagent))
    kwargs = ClaudeVertexAdapter(models, client=object())._request_kwargs(request)

    assert kwargs["system"] == pack.system  # pass-through is load-bearing
    (tool,) = kwargs["tools"]
    assert tool["name"] == tool_name(subagent)
    # Same schema, in Claude's dialect. `json_schema()` emits OpenAPI 3.0 for
    # Gemini's response_schema; `nullable` is not a JSON Schema keyword, so the
    # adapter rewrites it to a type union on the way out. This assertion used
    # to compare against the untranslated schema and so pinned the bug in
    # place — see amw.adapters.claude_anthropic._to_json_schema.
    assert tool["input_schema"] == _to_json_schema(json_schema(subagent))
    assert "nullable" not in json.dumps(tool["input_schema"])
    # Structured outputs are blocked for partner models in this org; the tool
    # is the emission mechanism. See notes/org_policy_structured_outputs.md.
    assert "output_config" not in kwargs


# --------------------------------------------------------------------------
# loader errors
# --------------------------------------------------------------------------


def test_unknown_subagent_and_variant_are_rejected():
    with pytest.raises(pp.PromptPackError, match="unknown subagent"):
        pp.load_pack("answer_drafter", "claude_baseline")
    with pytest.raises(pp.PromptPackError, match="unknown variant"):
        pp.load_pack("query_rewriter", "gemini_tuned_v2")


def test_unmarked_and_unknown_sections_are_rejected(tmp_path):
    path = tmp_path / "x.txt"
    with pytest.raises(pp.PromptPackError, match="no '=== section ==='"):
        pp._split_sections("just some text", path)
    with pytest.raises(pp.PromptPackError, match="before its first section"):
        pp._split_sections("preamble\n=== system ===\nhi\n", path)
    with pytest.raises(pp.PromptPackError, match="unknown section"):
        pp._split_sections("=== systm ===\nhi\n", path)
    with pytest.raises(pp.PromptPackError, match="twice"):
        pp._split_sections("=== system ===\na\n=== system ===\nb\n", path)


def test_malformed_placeholder_does_not_reach_the_prompt():
    with pytest.raises(pp.PromptPackError, match="malformed placeholder"):
        pp._render_template("hello {{ na-me }}", {"name": "x"}, where="unit")


def test_sample_item_is_a_copy_not_the_shared_dict():
    first = pp.sample_item("query_rewriter")
    first["question"] = "mutated"
    assert pp.sample_item("query_rewriter")["question"] != "mutated"


def test_placeholders_declared_for_every_subagent():
    assert set(pp.PLACEHOLDERS) == set(SUBAGENTS)
    for subagent in SUBAGENTS:
        item = pp.sample_item(subagent)
        assert set(pp.PLACEHOLDERS[subagent]) <= set(item)
