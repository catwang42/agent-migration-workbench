"""Prompt packs: the prompt variants each evaluated subagent runs under.

A *pack* is one subagent × one variant. It owns two things that must travel
together:

1. **The prompt text**, which lives on disk in
   ``amw/agents/prompts/{subagent}/{variant}.txt`` — versioned files, because
   customers are shown them in the workshop, and a prompt buried in a Python
   string literal is a prompt nobody reviews.
2. **How the variant constrains output** — a tool call or a ``response_schema``.
   That is not a stylistic detail; it is a rung on the ablation ladder, so it
   is declared per variant in :data:`VARIANT_SPECS` and carried on the pack.

The three universal variants
----------------------------

Every subagent has these three, and only these three are in :data:`VARIANTS`:

===================  ========================  =========================
variant              output mechanism          ladder position
===================  ========================  =========================
``claude_baseline``  tool (``emit_*``)         the incumbent
``gemini_naive``     tool (``emit_*``)         A0 — naive endpoint swap
``gemini_tuned_v1``  ``response_schema``       A1–A3 starting point
===================  ========================  =========================

Two deliberate choices, both from ``notes/org_policy_structured_outputs.md``:

* The Claude baseline emits through a **tool**, not structured outputs. In this
  GCP organization ``constraints/vertexai.allowedPartnerModelFeatures`` blocks
  ``structured_outputs`` for partner models; tool use was verified live and
  works. It is also the more faithful shape — the customer's real Query
  Rewriter already calls ``emit_query_plan``.
* ``gemini_naive`` uses the **same tool mechanism** and the **byte-identical
  prompt file** as the baseline (``tests/test_prompts.py`` asserts both). A0 is
  "the XML fed verbatim", so the Claude→A0 delta measures prompt-format
  transfer and nothing else. The strict schema arrives at the tuned rung, where
  the ladder says it belongs, instead of being smuggled into the baseline.

File format
-----------

One text file per variant, split into sections by ``=== name ===`` marker
lines::

    === system ===      required — becomes ModelRequest.system_prompt
    === chunk ===       optional — rendered once per retrieved chunk
    === user ===        required — becomes the single user message
    === tool_description ===
                        required for tool-mode variants only

Placeholders are ``{{name}}``. The set of names a subagent's templates may use
is fixed by :data:`PLACEHOLDERS` and checked at load time, so a typo is a load
error rather than a literal ``{{questoin}}`` reaching a customer's screen.

Why the chunk section exists
----------------------------

``tests/test_adapter_parity.py`` pinned the request encoding: ``context_chunks``
become one user turn with one text unit per chunk, and **neither adapter adds
glue text, labels, or separators**. So a chunk id marker that the Chunk
Summarizer needs in order to cite that chunk has to come from here. The
baseline wraps each chunk in ``<chunk id="...">``; the tuned variant uses a
Markdown heading. That difference is part of what the ladder measures.

Subagent-specific variants
--------------------------

A variant may declare :attr:`VariantSpec.subagents`, in which case it exists
for those subagents only and is *not* in :data:`VARIANTS`. Three Feature
Extractor variants (T10) do: together with ``gemini_naive`` they form the 2×2
that separates a prompt change from an output-mode change, which the bundled
A1–A3 rung confounded.

============================  ==============  ===================  ================
variant                       prompt          output mechanism     ladder cell
============================  ==============  ===================  ================
``gemini_naive``              naive (XML)     tool                 prompt −, mode −
``gemini_naive_schema``       naive (XML)     ``response_schema``  prompt −, mode +
``gemini_novelty_v1_tool``    naive + rule    tool                 prompt +, mode −
``gemini_novelty_v1_schema``  naive + rule    ``response_schema``  prompt +, mode +
============================  ==============  ===================  ================

Two invariants make those cells readable, and ``tests/test_ablate.py`` pins
both:

* The ``_schema`` files differ from their tool twin **only** in the lines that
  name the emission mechanism, plus the dropped ``tool_description`` section.
  A mode change cannot be made without editing the sentence that tells the
  model how to answer; that edit is the minimum, and nothing else moves with it.
* The novelty files are the *naive* file plus one added
  ``<novelty_statement_rule>`` block. The rung branches from A0, not from
  ``gemini_tuned_v1``, because at n=70 the tuned bundle scores *below* naive on
  this subagent (``notes/phase2_n70_validation.md``) — building on it would
  inherit the regression.

Why they are not in :data:`VARIANTS`: that tuple is the default arm list for
``amw.eval.runner.run_phase2`` and for :func:`load_packs`, and the choices for
``cli.py phase2 --variant``. Putting a Feature-Extractor-only variant in it
would make a plain ``phase2`` run try to load a Query Rewriter prompt file that
does not and should not exist. Use :func:`variants_for` to ask what a given
subagent can actually run, or :data:`ALL_VARIANTS` to enumerate everything.
"""

from __future__ import annotations

import copy
import hashlib
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from amw.adapters.base import ModelRequest, ToolSpec
from amw.agents.schemas import SUBAGENTS, json_schema, tool_name
from amw.config import ModelsConfig
from amw.traces.schema import Provenance

__all__ = [
    "VARIANTS",
    "ALL_VARIANTS",
    "VARIANT_SPECS",
    "VariantSpec",
    "variants_for",
    "PLACEHOLDERS",
    "PromptPack",
    "RenderedPrompt",
    "PromptPackError",
    "prompts_dir",
    "load_pack",
    "load_packs",
    "build_request",
    "resolve_model",
    "sample_item",
    "find_xml_tags",
]


class PromptPackError(ValueError):
    """A prompt file is missing, malformed, or references an unknown field."""


class VariantSpec(BaseModel):
    """What a variant *is*, beyond its text: mechanism, model, ladder rung."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: "tool" -> offer an emit_* tool; "response_schema" -> strict JSON mode.
    output_mode: str
    #: Logical role key from ``config/models.yaml: roles``. Never a model ID.
    model_role: str
    #: Ablation ladder rung(s) this variant serves, for reports.
    rung: str
    description: str
    #: Subagents this variant exists for, or ``None`` for "all of them".
    #: A restricted variant is deliberately absent from :data:`VARIANTS` — see
    #: the module docstring for why that tuple must stay universal.
    subagents: tuple[str, ...] | None = None


#: Variant order is report order: the three universal variants first
#: (incumbent, naive swap, tuned), then any subagent-specific ones.
VARIANT_SPECS: dict[str, VariantSpec] = {
    "claude_baseline": VariantSpec(
        output_mode="tool",
        model_role="claude_baseline",
        rung="baseline",
        description=(
            "The incumbent prompt as the customer wrote it: XML-tagged sections, "
            "everything in the system prompt, output via an emit_* tool call."
        ),
    ),
    "gemini_naive": VariantSpec(
        output_mode="tool",
        model_role="gemini_candidate",
        rung="A0",
        description=(
            "The baseline file byte-for-byte, run on Gemini under the same tool "
            "mechanism. Isolates prompt-format transfer from mechanism change."
        ),
    ),
    "gemini_tuned_v1": VariantSpec(
        output_mode="response_schema",
        model_role="gemini_candidate",
        rung="A1-A3",
        description=(
            "Markdown sections, system/user split, strict response_schema, and "
            "two recalibrated few-shots."
        ),
    ),
    # -- Feature Extractor only: the 2x2 that unbundles A1-A3 (T10) ---------
    "gemini_naive_schema": VariantSpec(
        output_mode="response_schema",
        model_role="gemini_candidate",
        rung="A0-schema",
        subagents=("feature_extractor",),
        description=(
            "A0's prompt with the output mode swapped to an enforced "
            "response_schema. The mode-only cell: it isolates how much of the "
            "A1-A3 movement was the mechanism rather than the wording."
        ),
    ),
    "gemini_novelty_v1_tool": VariantSpec(
        output_mode="tool",
        model_role="gemini_candidate",
        rung="A4-novelty-tool",
        subagents=("feature_extractor",),
        description=(
            "A0 plus a novelty_statement rule (claim 1 is the point of novelty "
            "when there is no discussion section; numeric limits survive) and "
            "one worked example. The prompt-only cell: same tool mechanism as "
            "A0."
        ),
    ),
    "gemini_novelty_v1_schema": VariantSpec(
        output_mode="response_schema",
        model_role="gemini_candidate",
        rung="A4-novelty-schema",
        subagents=("feature_extractor",),
        description=(
            "The novelty prompt under the enforced response_schema. Both "
            "changes at once — the cell the other three are read against."
        ),
    ),
    # -- Query Rewriter only: the targeted rung (Tuesday plan item 3b) ------
    "gemini_targeted_v1": VariantSpec(
        output_mode="response_schema",
        model_role="gemini_candidate",
        rung="A4-targeted",
        subagents=("query_rewriter",),
        description=(
            "A1-A3 plus three rules aimed at the three loss clusters the n=70 "
            "adjudication found: publication numbers survive verbatim in "
            "query; date_to is copied when the analyst names an explicit end "
            "date and expanded only for a bare period; landscape and "
            "ownership are separated by which side of the question is "
            "unknown. Bundled by the owner's ruling — the rung answers "
            "'does targeted tuning fix the identified failures', not 'which "
            "rule earned what'. Same output mode as A1-A3, so the delta is "
            "prompt only."
        ),
    ),
}

#: The variants every subagent has. This is the default arm list for phase2 and
#: :func:`load_packs`, and the ``--variant`` choices on the CLI, so it must not
#: grow a variant that only one subagent has a prompt file for.
VARIANTS: tuple[str, ...] = tuple(
    name for name, spec in VARIANT_SPECS.items() if spec.subagents is None
)

#: Every declared variant, universal and subagent-specific, in report order.
ALL_VARIANTS: tuple[str, ...] = tuple(VARIANT_SPECS)


def variants_for(subagent: str) -> tuple[str, ...]:
    """Variants ``subagent`` has a prompt file for, in report order.

    The universal three, plus anything whose :attr:`VariantSpec.subagents`
    names this subagent. Callers that enumerate packs — the ablation ladder,
    the stray-file guard in ``tests/test_prompts.py`` — should ask here rather
    than assume :data:`VARIANTS` covers everything on disk.
    """
    if subagent not in SUBAGENTS:
        raise PromptPackError(
            f"unknown subagent {subagent!r}; expected one of {list(SUBAGENTS)}."
        )
    return tuple(
        name
        for name, spec in VARIANT_SPECS.items()
        if spec.subagents is None or subagent in spec.subagents
    )

#: Item keys each subagent's templates may reference.
#:
#: ``chunks`` is special: it is a list of mappings rendered through the pack's
#: ``chunk`` section into ``context_chunks``, one per entry. Everything else is
#: substituted into the ``user`` section as text.
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "query_rewriter": ("question",),
    "chunk_summarizer": ("question",),
    "feature_extractor": ("document",),
}

#: Keys each chunk mapping must provide, for subagents that take chunks.
CHUNK_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "chunk_summarizer": ("chunk_id", "text"),
}

_SECTION_RE = re.compile(r"^===[ \t]*([a-z_]+)[ \t]*===[ \t]*$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_LEFTOVER_RE = re.compile(r"\{\{|\}\}")

#: An opening XML-ish instruction tag: ``<instructions>``, ``<chunk id="c1">``.
#: Used by the tuned-variant guard in ``tests/test_prompts.py`` and reusable by
#: the T10 translator, which turns exactly these into Markdown headings.
XML_TAG_RE = re.compile(r"<\s*/?\s*([A-Za-z_][A-Za-z0-9_.-]*)(\s[^<>]*)?>")

_REQUIRED_SECTIONS = ("system", "user")
_KNOWN_SECTIONS = ("system", "user", "chunk", "tool_description")


def find_xml_tags(text: str) -> list[str]:
    """Names of XML-ish tags in ``text``, in order of appearance.

    Markdown prompts must contain none; a tuned variant that grew a ``<rules>``
    tag back is no longer testing what the ladder says it tests.
    """
    return [match.group(1) for match in XML_TAG_RE.finditer(text)]


def prompts_dir() -> Path:
    """Directory holding the versioned prompt files."""
    return Path(__file__).resolve().parent / "prompts"


def _split_sections(text: str, path: Path) -> dict[str, str]:
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        raise PromptPackError(
            f"{path} has no '=== section ===' markers; a prompt file needs at "
            f"least {' and '.join(_REQUIRED_SECTIONS)}."
        )
    if text[: matches[0].start()].strip():
        raise PromptPackError(
            f"{path} has text before its first section marker; every line must "
            f"belong to a section."
        )

    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        name = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip("\n")
        if name not in _KNOWN_SECTIONS:
            raise PromptPackError(
                f"{path} declares unknown section {name!r}; known sections are "
                f"{list(_KNOWN_SECTIONS)}."
            )
        if name in sections:
            raise PromptPackError(f"{path} declares section {name!r} twice.")
        sections[name] = body
    return sections


def _render_template(template: str, values: Mapping[str, Any], *, where: str) -> str:
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            missing.append(key)
            return match.group(0)
        return str(values[key])

    rendered = _PLACEHOLDER_RE.sub(replace, template)
    if missing:
        raise PromptPackError(
            f"{where}: item is missing {sorted(set(missing))}; supplied keys were "
            f"{sorted(values)}."
        )
    if _LEFTOVER_RE.search(rendered):
        raise PromptPackError(
            f"{where}: rendered text still contains '{{{{' or '}}}}', which means a "
            f"malformed placeholder survived into the prompt."
        )
    return rendered


def _reject_prerendered_input(
    item: Mapping[str, Any], *, where: str, subagent: str
) -> None:
    """Fail loudly if handed a dataset item whose user turn is already rendered.

    ``amw/datasets/schema.py::DatasetInput`` (T06) carries ``messages:
    list[str]`` plus a ``context_chunks()`` helper that glues chunks as
    ``[id] text``. A pack cannot consume that: the user-turn wording and the
    chunk-id markup are *part of the variant* — the baseline wraps the question
    in ``<question>`` and each chunk in ``<chunk id="...">``, the tuned variant
    uses Markdown. If the generator pre-renders them, every arm shows the model
    the same surface and the format rungs of the ladder measure less than they
    claim to.

    So this is a seam to reconcile, not a shape to silently accept: guessing
    that ``messages[0]`` is the question would bake an assumption into the
    measurement. The message names the fix.
    """
    nested = item.get("input")
    if not isinstance(nested, Mapping) or "messages" not in nested:
        return
    raise PromptPackError(
        f"{where}: this item looks like a T06 DatasetItem — its model-facing "
        f"text is pre-rendered under input.messages. Prompt packs render the "
        f"user turn and the chunk markers themselves, because that formatting "
        f"differs per variant and is part of what the ablation ladder measures. "
        f"Pass the raw fields instead: {list(PLACEHOLDERS[subagent])}"
        + (
            " plus chunks=[{'chunk_id': ..., 'text': ...}]"
            if subagent in CHUNK_PLACEHOLDERS
            else ""
        )
        + "."
    )


class RenderedPrompt(BaseModel):
    """One item put through one pack — exactly what goes on the wire."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    system_prompt: str
    messages: list[str]
    context_chunks: list[str] = Field(default_factory=list)


class PromptPack(BaseModel):
    """One subagent × one variant: its text, its templates, its mechanism."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subagent: str
    variant: str
    path: Path
    #: The raw file, unmodified. Shown to customers; diffed by the translator.
    text: str
    system: str
    user_template: str
    chunk_template: str | None = None
    tool_description: str | None = None

    # -- variant policy ----------------------------------------------------

    @property
    def spec(self) -> VariantSpec:
        return VARIANT_SPECS[self.variant]

    @property
    def output_mode(self) -> str:
        return self.spec.output_mode

    @property
    def model_role(self) -> str:
        return self.spec.model_role

    @property
    def sha256(self) -> str:
        """Digest of the prompt file — for report provenance, not for keying."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def tool_specs(self) -> list[ToolSpec]:
        """The ``emit_*`` tool, or nothing for response_schema variants."""
        if self.output_mode != "tool":
            return []
        return [
            ToolSpec(
                name=tool_name(self.subagent),
                description=(self.tool_description or "").strip(),
                parameters=json_schema(self.subagent),
            )
        ]

    def response_schema(self) -> dict[str, Any] | None:
        """The strict schema, or ``None`` for tool variants.

        Never both: ``amw/adapters/gemini.py`` raises ``ConfigError`` if a
        request carries tools and a response schema together.
        """
        if self.output_mode != "response_schema":
            return None
        return json_schema(self.subagent)

    # -- rendering ---------------------------------------------------------

    def render(self, item: Mapping[str, Any]) -> RenderedPrompt:
        """Render one dataset item. Extra keys on ``item`` are ignored.

        Dataset items carry ids, difficulty tags, gold outputs and rubrics that
        the model must never see; only the keys in :data:`PLACEHOLDERS` (plus
        ``chunks``) are read.
        """
        where = f"{self.subagent}/{self.variant}"
        _reject_prerendered_input(item, where=where, subagent=self.subagent)
        context_chunks: list[str] = []
        if self.chunk_template is not None:
            chunks = item.get("chunks")
            if chunks is None:
                raise PromptPackError(
                    f"{where}: item has no 'chunks'; this pack renders one context "
                    f"chunk per entry and cannot run without them."
                )
            for position, chunk in enumerate(chunks):
                context_chunks.append(
                    _render_template(
                        self.chunk_template, chunk, where=f"{where} chunk[{position}]"
                    )
                )
        message = _render_template(self.user_template, item, where=where)
        return RenderedPrompt(
            system_prompt=self.system,
            messages=[message],
            context_chunks=context_chunks,
        )

    def build_request(
        self,
        item: Mapping[str, Any],
        *,
        model: str,
        provenance: Provenance = "synthetic",
        item_id: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> ModelRequest:
        """Render ``item`` and wrap it in a provider-agnostic request.

        ``model`` is a *logical key* from ``config/models.yaml`` (the adapters
        resolve it to a provider ID); :func:`build_request` will look it up from
        the variant's role for you.
        """
        rendered = self.render(item)
        return ModelRequest(
            subagent=self.subagent,
            model=model,
            system_prompt=rendered.system_prompt,
            messages=rendered.messages,
            context_chunks=rendered.context_chunks,
            tools=self.tool_specs(),
            response_schema=self.response_schema(),
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            provenance=provenance,
            item_id=item_id or _item_id(item),
        )


def _item_id(item: Mapping[str, Any]) -> str | None:
    for key in ("item_id", "id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _validate(pack: PromptPack) -> PromptPack:
    allowed = set(PLACEHOLDERS[pack.subagent])
    used = set(_PLACEHOLDER_RE.findall(pack.user_template))
    unknown = used - allowed
    if unknown:
        raise PromptPackError(
            f"{pack.path}: user section uses unknown placeholder(s) {sorted(unknown)}; "
            f"{pack.subagent} items provide {sorted(allowed)}."
        )

    takes_chunks = pack.subagent in CHUNK_PLACEHOLDERS
    if takes_chunks and pack.chunk_template is None:
        raise PromptPackError(
            f"{pack.path}: {pack.subagent} is given retrieved chunks, so the pack "
            f"needs a '=== chunk ===' section. The adapters add no labels of their "
            f"own (tests/test_adapter_parity.py), so chunk ids must come from here."
        )
    if not takes_chunks and pack.chunk_template is not None:
        raise PromptPackError(
            f"{pack.path}: {pack.subagent} is not given chunks, so a "
            f"'=== chunk ===' section would never render."
        )
    if pack.chunk_template is not None:
        chunk_allowed = set(CHUNK_PLACEHOLDERS[pack.subagent])
        chunk_unknown = set(_PLACEHOLDER_RE.findall(pack.chunk_template)) - chunk_allowed
        if chunk_unknown:
            raise PromptPackError(
                f"{pack.path}: chunk section uses unknown placeholder(s) "
                f"{sorted(chunk_unknown)}; chunks provide {sorted(chunk_allowed)}."
            )

    if pack.output_mode == "tool" and not (pack.tool_description or "").strip():
        raise PromptPackError(
            f"{pack.path}: variant {pack.variant!r} emits through the "
            f"{tool_name(pack.subagent)!r} tool, so it needs a "
            f"'=== tool_description ===' section — the description is prompt text "
            f"the model reads."
        )
    if pack.output_mode != "tool" and pack.tool_description is not None:
        raise PromptPackError(
            f"{pack.path}: variant {pack.variant!r} uses response_schema, so a "
            f"tool description would never be sent."
        )
    if not pack.system.strip():
        raise PromptPackError(f"{pack.path}: system section is empty.")
    if not pack.user_template.strip():
        raise PromptPackError(f"{pack.path}: user section is empty.")
    return pack


@lru_cache(maxsize=None)
def load_pack(subagent: str, variant: str) -> PromptPack:
    """Load and validate one pack. Cached — prompt files do not change at runtime."""
    if subagent not in SUBAGENTS:
        raise PromptPackError(
            f"unknown subagent {subagent!r}; expected one of {list(SUBAGENTS)}."
        )
    if variant not in VARIANT_SPECS:
        raise PromptPackError(
            f"unknown variant {variant!r}; expected one of {list(VARIANT_SPECS)}."
        )
    allowed = VARIANT_SPECS[variant].subagents
    if allowed is not None and subagent not in allowed:
        # Caught here rather than as "no prompt file at ...", because the
        # missing file is the symptom: this variant is declared for one
        # subagent and asking another for it is a caller bug, not a gap on
        # disk that someone should fill in.
        raise PromptPackError(
            f"variant {variant!r} exists for {list(allowed)} only, not for "
            f"{subagent!r}; {subagent} has {list(variants_for(subagent))}."
        )
    path = prompts_dir() / subagent / f"{variant}.txt"
    if not path.is_file():
        raise PromptPackError(f"no prompt file at {path}.")
    text = path.read_text(encoding="utf-8")
    sections = _split_sections(text, path)
    for required in _REQUIRED_SECTIONS:
        if required not in sections:
            raise PromptPackError(f"{path}: missing required '=== {required} ===' section.")
    return _validate(
        PromptPack(
            subagent=subagent,
            variant=variant,
            path=path,
            text=text,
            system=sections["system"],
            user_template=sections["user"],
            chunk_template=sections.get("chunk"),
            tool_description=sections.get("tool_description"),
        )
    )


def load_packs(
    subagents: tuple[str, ...] = SUBAGENTS, variants: tuple[str, ...] = VARIANTS
) -> dict[tuple[str, str], PromptPack]:
    """Every ``(subagent, variant)`` pack, loaded and validated."""
    return {
        (subagent, variant): load_pack(subagent, variant)
        for subagent in subagents
        for variant in variants
    }


def resolve_model(variant: str, models: ModelsConfig | None = None) -> str:
    """Logical model key for a variant, via ``config/models.yaml: roles``.

    No model ID ever appears in this module: the variant names a *role*
    (``claude_baseline`` / ``gemini_candidate``) and the registry decides which
    model that is today.
    """
    if variant not in VARIANT_SPECS:
        raise PromptPackError(
            f"unknown variant {variant!r}; expected one of {list(VARIANT_SPECS)}."
        )
    if models is None:
        from amw.config import load_all

        models = load_all().models
    key, _spec = models.for_role(VARIANT_SPECS[variant].model_role)
    return key


def build_request(
    subagent: str,
    variant: str,
    item: Mapping[str, Any],
    *,
    model: str | None = None,
    models: ModelsConfig | None = None,
    provenance: Provenance = "synthetic",
    item_id: str | None = None,
    max_output_tokens: int | None = None,
    temperature: float | None = None,
) -> ModelRequest:
    """One dataset item + one variant -> one ready-to-run :class:`ModelRequest`.

    The single call the eval runner, the ablation ladder, and the shadow runner
    should use. ``model`` overrides the variant's role lookup (the escalation
    rung swaps ``gemini-flash`` for ``gemini-pro`` without touching prompts).
    """
    pack = load_pack(subagent, variant)
    return pack.build_request(
        item,
        model=model or resolve_model(variant, models),
        provenance=provenance,
        item_id=item_id,
        max_output_tokens=max_output_tokens,
        temperature=temperature,
    )


#: Minimal, schema-shaped items for tests, notebook smoke cells, and sibling
#: lanes that need a stand-in before the T06 generator lands. These are
#: *inputs*, never outputs: no gold answers and no metric values live here.
_SAMPLE_ITEMS: dict[str, dict[str, Any]] = {
    "query_rewriter": {
        "item_id": "qr-sample-000",
        "question": (
            "Which companies filed solid-state battery separator patents in "
            "Japan between 2019 and 2021?"
        ),
    },
    "chunk_summarizer": {
        "item_id": "cs-sample-000",
        "question": "What separator materials are described, and by whom?",
        "chunks": [
            {
                "chunk_id": "c1",
                "text": (
                    "JP 2020-118342 A (assignee: Panasonic Holdings) describes a "
                    "ceramic-coated polyolefin separator for solid-state cells."
                ),
            },
            {
                "chunk_id": "c2",
                "text": (
                    "The coating is applied at 3 micrometres and is reported to "
                    "raise thermal shutdown onset by 20 degrees Celsius."
                ),
            },
        ],
    },
    "feature_extractor": {
        "item_id": "fe-sample-000",
        "document": (
            "Japan Patent Office JP 2020-118342 A\n"
            "Title: Ceramic-coated separator for solid-state secondary battery\n"
            "Assignee: Panasonic Holdings Corporation\n"
            "Filed: July 9, 2019\n"
            "CPC: H01M50/446\n"
            "The invention concerns separators for solid-state secondary "
            "batteries. The 3 micrometre ceramic coating raises thermal "
            "shutdown onset relative to uncoated polyolefin separators.\n"
            "What is claimed is:\n"
            "1. A separator comprising ...\n"
            "2. The separator of claim 1, wherein ...\n"
        ),
    },
}


def sample_item(subagent: str) -> dict[str, Any]:
    """A representative input item for ``subagent`` — a fresh copy each call."""
    try:
        item = _SAMPLE_ITEMS[subagent]
    except KeyError:
        raise PromptPackError(
            f"no sample item for {subagent!r}; expected one of {list(_SAMPLE_ITEMS)}."
        ) from None
    return copy.deepcopy(item)
