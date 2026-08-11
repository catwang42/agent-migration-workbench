"""Mechanical Claude-XML → Gemini form, plus the side-by-side that teaches it.

The first thing a customer asks when they see the A0 gap is "so what would you
actually change?". This module answers it without a model call: it is a pure,
deterministic rewrite of a Claude-shaped prompt into the four artefacts a
Gemini request is assembled from —

``system_instruction``
    the instruction block, XML tags folded into Markdown headings;
``markdown_body``
    the user turn, same treatment;
``response_schema``
    the subagent's output contract, from
    :func:`amw.agents.schemas.json_schema`;
``tool_declarations``
    the ``emit_*`` function declaration, from
    :func:`amw.agents.schemas.tool_name` plus the same schema.

Two things this module is deliberately *not*.

**It is not a tuner.** It performs the format half of rung A1 and nothing else:
tags become headings, sections keep their order, and every word of instruction
text survives byte-for-byte. It will not reword an instruction, drop a
few-shot, or fix a defect. When the ladder shows A1 moving a score, the
customer can read this output and see that only the container changed. A
translator that quietly improved the prose would make that rung unreadable.

**It is not the source of the shipped tuned prompts.** Those are versioned
files under ``amw/agents/prompts/`` that a human wrote and reviewed
(``notes/day1_failures.md`` drove them). This is the mechanical starting point
those files were edited *from*, and the teaching artifact that shows the
difference between "ported" and "tuned".

Output-mode exclusivity is preserved, because the Gemini adapter raises
``ConfigError`` on a request carrying both: a translation is emitted for one
mode, and the other field comes back empty.

What "mechanical" means here
----------------------------

A paired ``<tag> … </tag>`` block becomes a Markdown heading whose level is its
nesting depth, and whose title is the tag name humanised (``output_format`` →
``Output format``). Attributes are dropped from the heading and reported. Text
outside any tag is passed through untouched. An unpaired tag is **not**
guessed at: it is left in place verbatim and named in
:attr:`TranslatedPrompt.unconverted_tags`, so a half-translated prompt is
visible rather than silently mangled.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from amw.agents.prompt_packs import (
    VARIANT_SPECS,
    PromptPack,
    PromptPackError,
    find_xml_tags,
    load_pack,
)
from amw.agents.schemas import json_schema, tool_name

__all__ = [
    "OUTPUT_MODES",
    "SectionMapping",
    "TranslatedPrompt",
    "humanise_tag",
    "xml_to_markdown",
    "translate_pack",
    "translate",
    "render_side_by_side",
]

#: The two ways a Gemini request can be made to return structured output.
#: Never both on one request — ``amw/adapters/gemini.py`` rejects that.
OUTPUT_MODES: tuple[str, ...] = ("response_schema", "tool")

#: One paired XML-ish block: ``<name attrs> body </name>``. Non-greedy body
#: with a backreference, so nesting is handled by recursing into ``body``
#: rather than by trying to write a recursive regex.
_BLOCK_RE = re.compile(
    r"<\s*(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)(?P<attrs>\s[^<>]*)?>"
    r"(?P<body>.*?)"
    r"</\s*(?P=name)\s*>",
    re.DOTALL,
)

_ATTR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*\"([^\"]*)\"")

#: Deepest heading Markdown defines. Prompts nest two or three levels
#: (``<examples><example><document>``), so this is a guard, not a limit we
#: expect to hit; past it the heading is emitted as bold text instead.
_MAX_HEADING_LEVEL = 6


def humanise_tag(name: str) -> str:
    """``output_format`` -> ``Output format``. Underscores and dots to spaces."""
    words = re.split(r"[_.\-]+", name.strip())
    text = " ".join(word for word in words if word)
    return text[:1].upper() + text[1:] if text else name


class SectionMapping(BaseModel):
    """One XML construct and the Markdown it became. The teaching table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: "system" or "user" — which turn of the prompt this block lived in.
    where: str
    source_tag: str
    #: Attributes on the opening tag, e.g. ``{"id": "c1"}``. Dropped from the
    #: heading; kept here so a reader can see what was dropped.
    attributes: dict[str, str] = Field(default_factory=dict)
    heading: str
    level: int


def _heading(title: str, level: int) -> str:
    if level > _MAX_HEADING_LEVEL:
        return f"**{title}**"
    return f"{'#' * level} {title}"


def xml_to_markdown(
    text: str,
    *,
    level: int = 1,
    where: str = "system",
    mappings: list[SectionMapping] | None = None,
) -> str:
    """Fold paired XML blocks in ``text`` into Markdown headings.

    Pure and deterministic: same input, same output, no model call and no
    filesystem access. ``mappings`` collects a :class:`SectionMapping` per
    converted block, in document order, for the side-by-side renderer.

    Text between and around blocks is preserved exactly, so instruction wording
    cannot drift through translation.
    """
    out: list[str] = []
    cursor = 0
    for match in _BLOCK_RE.finditer(text):
        if match.start() < cursor:  # already consumed as part of an outer block
            continue
        out.append(text[cursor : match.start()])
        name = match.group("name")
        attributes = dict(_ATTR_RE.findall(match.group("attrs") or ""))
        title = humanise_tag(name)
        if mappings is not None:
            mappings.append(
                SectionMapping(
                    where=where,
                    source_tag=name,
                    attributes=attributes,
                    heading=title,
                    level=level,
                )
            )
        body = xml_to_markdown(
            match.group("body"), level=level + 1, where=where, mappings=mappings
        ).strip("\n")
        out.append(f"{_heading(title, level)}\n\n{body}\n")
        cursor = match.end()
    out.append(text[cursor:])
    rendered = "".join(out)
    # Collapse runs of blank lines the folding can leave behind. Whitespace is
    # the only thing this function is allowed to change about the wording.
    return re.sub(r"\n{3,}", "\n\n", rendered).strip("\n") + "\n"


class TranslatedPrompt(BaseModel):
    """A Claude prompt in the four pieces a Gemini request is built from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subagent: str
    source_variant: str
    #: Digest of the exact source file translated, for report provenance.
    source_sha256: str
    #: Which structured-output mechanism this translation targets.
    output_mode: str

    system_instruction: str
    markdown_body: str
    #: Set iff ``output_mode == "response_schema"``.
    response_schema: dict[str, Any] | None = None
    #: Populated iff ``output_mode == "tool"``. Gemini function declarations.
    tool_declarations: list[dict[str, Any]] = Field(default_factory=list)

    sections: list[SectionMapping] = Field(default_factory=list)
    #: Tags that could not be paired and were therefore left verbatim.
    unconverted_tags: list[str] = Field(default_factory=list)
    #: Things a human still has to decide. Never numbers, never measurements.
    notes: list[str] = Field(default_factory=list)


def translate_pack(
    pack: PromptPack, *, output_mode: str = "response_schema"
) -> TranslatedPrompt:
    """Translate one loaded pack. The entry point the notebook and CLI use."""
    if output_mode not in OUTPUT_MODES:
        raise PromptPackError(
            f"unknown output_mode {output_mode!r}; expected one of "
            f"{list(OUTPUT_MODES)}."
        )

    mappings: list[SectionMapping] = []
    system = xml_to_markdown(pack.system, where="system", mappings=mappings)
    body = xml_to_markdown(pack.user_template, where="user", mappings=mappings)

    schema = json_schema(pack.subagent)
    tools: list[dict[str, Any]] = []
    response_schema: dict[str, Any] | None = None
    if output_mode == "tool":
        tools = [
            {
                "name": tool_name(pack.subagent),
                # The tool description is prompt text the model reads, so it
                # comes from the source pack when the source had one rather
                # than being invented here.
                "description": (pack.tool_description or "").strip()
                or f"Emit the structured output for {pack.subagent}.",
                "parameters": schema,
            }
        ]
    else:
        response_schema = schema

    notes: list[str] = []
    if output_mode == "response_schema" and pack.tool_description is not None:
        notes.append(
            "The source emits through a tool and this translation targets "
            "response_schema, so the tool description was dropped. Any "
            "instruction that names the tool by name still reads as if the "
            "tool exists — check the output-format section before running it."
        )
    body_tags = find_xml_tags(body)
    if not body_tags and pack.user_template.strip() != body.strip():
        notes.append(
            "The user turn kept a heading of its own. Rung A1 moves all "
            "instruction text into the system turn and leaves the user turn as "
            "the item alone; that trim is a human edit, not a mechanical one."
        )

    leftover = sorted(set(find_xml_tags(system) + body_tags))
    if leftover:
        notes.append(
            f"Tag name(s) {leftover} survive in the text with no block to "
            f"pair with — typically prose cross-references ('the document "
            f"inside the <document> tags') that now point at a Markdown "
            f"heading. Left exactly as written rather than guessed at; reword "
            f"them by hand."
        )

    return TranslatedPrompt(
        subagent=pack.subagent,
        source_variant=pack.variant,
        source_sha256=pack.sha256,
        output_mode=output_mode,
        system_instruction=system,
        markdown_body=body,
        response_schema=response_schema,
        tool_declarations=tools,
        sections=mappings,
        unconverted_tags=leftover,
        notes=notes,
    )


def translate(
    subagent: str,
    variant: str = "claude_baseline",
    *,
    output_mode: str = "response_schema",
) -> TranslatedPrompt:
    """Load a pack by name and translate it."""
    return translate_pack(load_pack(subagent, variant), output_mode=output_mode)


# --------------------------------------------------------------------------
# the teaching artifact
# --------------------------------------------------------------------------

_MECHANISM_BLURB = {
    "response_schema": (
        "an enforced `response_schema` — the model cannot return anything that "
        "does not match it"
    ),
    "tool": (
        "an `emit_*` function declaration — the same mechanism the incumbent "
        "prompt uses, so a migration can change one thing at a time"
    ),
}


def _fence(text: str, language: str = "text") -> str:
    # Prompts contain triple backticks about as often as never, but a fence
    # that a prompt could break is a report that renders wrong on stage.
    ticks = "`" * max(3, max((len(m) for m in re.findall(r"`+", text)), default=0) + 1)
    return f"{ticks}{language}\n{text.rstrip()}\n{ticks}"


def render_side_by_side(translated: TranslatedPrompt, pack: PromptPack) -> str:
    """The Markdown a customer is shown: what changed, and what did not.

    Structured as claim-then-evidence rather than as a line diff. A line diff
    of a prompt rewrite is unreadable on a projector and, worse, it buries the
    single point of the exercise: *the instructions did not change, only their
    container did.* So the construct table comes first, the full before/after
    second, and the output contract — the part that is genuinely new — last.
    """
    if translated.source_sha256 != pack.sha256:
        raise PromptPackError(
            f"render_side_by_side was handed a translation of "
            f"{translated.source_variant} (sha {translated.source_sha256[:12]}) "
            f"and a different pack file (sha {pack.sha256[:12]}). Translate the "
            f"pack you intend to show."
        )

    spec = VARIANT_SPECS[pack.variant]
    lines: list[str] = [
        f"# Prompt translation — {pack.subagent} / {pack.variant}",
        "",
        f"Source: `{pack.path.name}` (sha256 `{pack.sha256[:12]}`), rung "
        f"`{spec.rung}`.",
        "",
        "Mechanical translation only: XML tags become Markdown headings and the "
        "output contract moves out of the prose and into "
        f"{_MECHANISM_BLURB[translated.output_mode]}. **No instruction text was "
        "reworded, reordered, added, or removed.** Everything the ablation "
        "ladder measures beyond this point is a human edit on top of it.",
        "",
        "## Constructs",
        "",
        "| Turn | Claude construct | Becomes |",
        "| --- | --- | --- |",
    ]
    for section in translated.sections:
        attrs = "".join(f' {k}="{v}"' for k, v in sorted(section.attributes.items()))
        lines.append(
            f"| {section.where} | `<{section.source_tag}{attrs}>` | "
            f"`{'#' * section.level} {section.heading}` |"
        )
    if not translated.sections:
        lines.append("| — | (no XML blocks) | — |")

    mechanism_row = (
        "`response_schema` (enforced)"
        if translated.output_mode == "response_schema"
        else f"tool `{translated.tool_declarations[0]['name']}`"
    )
    lines += [
        f"| — | prose output-format instruction | {mechanism_row} |",
        "",
        "## Before — the incumbent prompt",
        "",
        "### system",
        "",
        _fence(pack.system),
        "",
        "### user",
        "",
        _fence(pack.user_template),
        "",
        "## After — the Gemini form",
        "",
        "### system_instruction",
        "",
        _fence(translated.system_instruction, "markdown"),
        "",
        "### user turn",
        "",
        _fence(translated.markdown_body, "markdown"),
        "",
        "## The output contract",
        "",
    ]
    if translated.response_schema is not None:
        lines += [
            "Passed as `response_schema` with `response_mime_type: "
            "application/json`, so the shape is enforced by the endpoint rather "
            "than requested in prose:",
            "",
            _fence(json.dumps(translated.response_schema, indent=2), "json"),
        ]
    else:
        lines += [
            "Passed as a function declaration. The model never executes "
            "anything — the declaration is a schema-shaped slot it fills:",
            "",
            _fence(json.dumps(translated.tool_declarations, indent=2), "json"),
        ]

    if translated.notes:
        lines += ["", "## Still a human's call", ""]
        lines += [f"- {note}" for note in translated.notes]

    lines += [
        "",
        "---",
        "",
        f"Generated from `{pack.path.name}` by `amw.tuning.translator`. No model "
        "call was made to produce this page, and it contains no measurements.",
        "",
    ]
    return "\n".join(lines)
