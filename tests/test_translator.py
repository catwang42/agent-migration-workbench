"""T10 verify: the Claude-XML → Gemini translator.

The property that matters most is a negative one: **the translator changes the
container and nothing else.** Rung A1 on the ablation ladder is "the same
instructions, in Markdown, split into a system instruction". If the translator
were also allowed to reword, tighten, or drop a line, A1 would measure a prompt
rewrite of unknown size and the ladder would stop being an ablation.

So most of what follows asserts that instruction text survives byte-for-byte
through translation, and that the four artefacts a Gemini request needs come
out the far side ready to send.

Everything here runs offline: the translator makes no model call and reads only
``config/models.yaml`` (through the pack loader) and prompt files on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amw.agents import prompt_packs as pp
from amw.agents.schemas import SUBAGENTS, json_schema, tool_name
from amw.tuning import translator as T

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tuning" / "toy_claude_prompt.txt"


@pytest.fixture(scope="module")
def toy_pack() -> pp.PromptPack:
    """The fixture prompt, wrapped in a PromptPack without touching the registry.

    Built by hand rather than dropped into ``amw/agents/prompts/`` on purpose: a
    file in there is a *shipped* prompt that the stray-file guard and the whole
    ladder would pick up. This one is a test fixture and says so in its own
    first line.
    """
    text = FIXTURE.read_text(encoding="utf-8")
    sections = pp._split_sections(text, FIXTURE)
    return pp.PromptPack(
        subagent="query_rewriter",
        variant="claude_baseline",
        path=FIXTURE,
        text=text,
        system=sections["system"],
        user_template=sections["user"],
        tool_description=sections["tool_description"],
    )


# --------------------------------------------------------------------------
# the pure conversion
# --------------------------------------------------------------------------


def test_humanise_tag_reads_like_a_heading():
    assert T.humanise_tag("output_format") == "Output format"
    assert T.humanise_tag("instructions") == "Instructions"
    assert T.humanise_tag("tool-description") == "Tool description"


def test_paired_block_becomes_a_heading_at_its_nesting_depth():
    markdown = T.xml_to_markdown("<outer>\n<inner>\nbody\n</inner>\n</outer>\n")
    assert markdown.splitlines() == ["# Outer", "", "## Inner", "", "body"]


def test_text_outside_tags_is_passed_through_untouched():
    source = "preamble line\n\n<rules>\nrule one\n</rules>\n\ntrailing line\n"
    markdown = T.xml_to_markdown(source)
    assert markdown.startswith("preamble line")
    assert markdown.rstrip().endswith("trailing line")
    assert "rule one" in markdown


def test_an_unpaired_tag_is_left_verbatim_rather_than_guessed_at():
    """Half-translating a prompt silently is worse than not translating it."""
    markdown = T.xml_to_markdown("see the <payload> tags for details\n")
    assert "<payload>" in markdown
    assert "# Payload" not in markdown


def test_attributes_are_reported_rather_than_folded_into_the_heading():
    mappings: list[T.SectionMapping] = []
    markdown = T.xml_to_markdown('<chunk id="c1">\ntext\n</chunk>\n', mappings=mappings)
    assert markdown.startswith("# Chunk\n")
    assert mappings[0].attributes == {"id": "c1"}


def test_conversion_is_deterministic():
    source = FIXTURE.read_text(encoding="utf-8")
    assert T.xml_to_markdown(source) == T.xml_to_markdown(source)


# --------------------------------------------------------------------------
# the fixture prompt -> the expected structure
# --------------------------------------------------------------------------


def test_fixture_translates_to_the_expected_section_structure(toy_pack):
    translated = T.translate_pack(toy_pack)

    assert [(s.where, s.source_tag, s.level) for s in translated.sections] == [
        ("system", "instructions", 1),
        ("system", "output_format", 1),
        ("system", "examples", 1),
        ("system", "example", 2),
        ("system", "payload", 3),
        ("system", "answer", 3),
        ("user", "payload", 1),
    ]
    assert "# Instructions" in translated.system_instruction
    assert "# Output format" in translated.system_instruction
    assert "## Example" in translated.system_instruction
    assert "### Payload" in translated.system_instruction


def test_every_instruction_line_survives_translation(toy_pack):
    """The load-bearing assertion: A1 changes the container, not the content."""
    translated = T.translate_pack(toy_pack)
    body = f"{translated.system_instruction}\n{translated.markdown_body}"
    source = f"{toy_pack.system}\n{toy_pack.user_template}"

    for line in source.splitlines():
        stripped = line.strip()
        if not stripped or pp.XML_TAG_RE.fullmatch(stripped):
            continue
        assert stripped in body, f"translation dropped or reworded: {stripped!r}"


def test_the_item_placeholder_reaches_the_user_turn(toy_pack):
    translated = T.translate_pack(toy_pack)
    assert "{{question}}" in translated.markdown_body


def test_inline_tag_references_are_flagged_for_a_human(toy_pack):
    """`the thing inside the <payload> tags` is now a heading; someone must say so."""
    translated = T.translate_pack(toy_pack)
    assert "payload" in translated.unconverted_tags
    assert any("reword" in note for note in translated.notes)


# --------------------------------------------------------------------------
# the output contract
# --------------------------------------------------------------------------


def test_response_schema_mode_emits_the_schema_and_no_tool(toy_pack):
    translated = T.translate_pack(toy_pack, output_mode="response_schema")
    assert translated.response_schema == json_schema("query_rewriter")
    assert translated.tool_declarations == []


def test_tool_mode_emits_a_declaration_and_no_schema(toy_pack):
    translated = T.translate_pack(toy_pack, output_mode="tool")
    assert translated.response_schema is None
    (declaration,) = translated.tool_declarations
    assert declaration["name"] == tool_name("query_rewriter")
    assert declaration["parameters"] == json_schema("query_rewriter")
    # The tool description is prompt text; it comes from the source pack.
    assert declaration["description"] == toy_pack.tool_description.strip()


@pytest.mark.parametrize("output_mode", T.OUTPUT_MODES)
def test_never_both_mechanisms_at_once(toy_pack, output_mode):
    """The Gemini adapter raises ConfigError on a request carrying both."""
    translated = T.translate_pack(toy_pack, output_mode=output_mode)
    assert (translated.response_schema is not None) != bool(
        translated.tool_declarations
    )


def test_dropping_the_tool_is_called_out_not_done_quietly(toy_pack):
    translated = T.translate_pack(toy_pack, output_mode="response_schema")
    assert any("tool description was dropped" in note for note in translated.notes)


def test_unknown_output_mode_is_rejected(toy_pack):
    with pytest.raises(pp.PromptPackError, match="unknown output_mode"):
        T.translate_pack(toy_pack, output_mode="freeform")


# --------------------------------------------------------------------------
# the real packs
# --------------------------------------------------------------------------


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_every_shipped_baseline_translates(subagent):
    translated = T.translate(subagent)

    assert translated.subagent == subagent
    assert translated.source_variant == "claude_baseline"
    assert translated.source_sha256 == pp.load_pack(subagent, "claude_baseline").sha256
    assert translated.system_instruction.strip()
    assert translated.markdown_body.strip()
    assert translated.response_schema == json_schema(subagent)


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_translated_blocks_become_headings_not_leftover_tags(subagent):
    """Every *paired* block converts; only inline references may survive."""
    translated = T.translate(subagent)
    converted = {section.source_tag for section in translated.sections}
    assert {"instructions", "output_format"} <= converted
    assert "instructions" not in translated.unconverted_tags


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_translation_output_is_json_serialisable(subagent):
    """It is written into reports and notebooks, so it has to round-trip."""
    translated = T.translate(subagent)
    dumped = translated.model_dump_json()
    assert json.loads(dumped)["subagent"] == subagent


# --------------------------------------------------------------------------
# the teaching artifact
# --------------------------------------------------------------------------


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_side_by_side_shows_both_sides_and_the_contract(subagent):
    pack = pp.load_pack(subagent, "claude_baseline")
    page = T.render_side_by_side(T.translate_pack(pack), pack)

    assert page.startswith(f"# Prompt translation — {subagent} / claude_baseline")
    assert "## Before — the incumbent prompt" in page
    assert "## After — the Gemini form" in page
    assert "## The output contract" in page
    assert "| Turn | Claude construct | Becomes |" in page
    # The source text is shown verbatim, which is the point of a side-by-side.
    assert pack.system.strip() in page
    assert pack.sha256[:12] in page


def test_side_by_side_states_that_no_measurement_is_on_the_page():
    """It is a customer-facing page produced without a single model call."""
    pack = pp.load_pack("query_rewriter", "claude_baseline")
    page = T.render_side_by_side(T.translate_pack(pack), pack)
    assert "no measurements" in page
    assert "No model call was made" in page or "No instruction text was" in page


def test_side_by_side_refuses_a_mismatched_pack():
    """Showing translation A next to prompt B would teach the wrong thing."""
    translated = T.translate("query_rewriter")
    other = pp.load_pack("feature_extractor", "claude_baseline")
    with pytest.raises(pp.PromptPackError, match="Translate the pack you intend"):
        T.render_side_by_side(translated, other)
