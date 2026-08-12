"""A thin ADK reference app — **demo only, never in the eval path**.

What this is for
----------------

The workshop shows three artefacts of the same three subagents:

1. the **prompt files** under ``amw/agents/prompts/`` — what the model is told;
2. the **bench harness** (``amw/adapters/`` → ``amw/eval/``) — how they are
   measured;
3. **this module** — what they look like once they are wired together as an
   agent application on Google's stack.

(3) exists so the customer can see that the thing we measured and the thing
they would ship are the *same prompts*, not two drifting copies. It answers
"what does this look like in ADK?" in ten minutes of screen time.

Hard boundary: this is not an instrument
----------------------------------------

Nothing here produces a number that appears on a scorecard. ``phase2``,
``ablate``, ``shadow`` and ``scorecard`` run **exclusively** through
``amw/adapters/``, which records a canonical trace for every call
(CLAUDE.md ground rules 1 and 5). An ADK ``Runner`` has its own retry, its own
prompt assembly, and no recording hook into ``artifacts/replay/`` — measuring
through it would produce numbers we cannot reproduce offline. So:

* ``amw/eval/``, ``amw/shadow/``, ``amw/tuning/`` and ``amw/reporting/`` must
  never import this module. ``tests/test_adk_app.py`` asserts that.
* This module has **no replay mode**. It is live-Gemini or it is nothing, and
  the fallback when it is nothing is the diagram plus the prompt files plus the
  harness (see WORKSHOP_RUNBOOK.md).

Single source of truth, enforced
--------------------------------

Every leaf agent's ``instruction`` is
:attr:`~amw.agents.prompt_packs.PromptPack.system` — read off disk through
:func:`~amw.agents.prompt_packs.load_pack` at *agent-construction time*. There
is no prompt text in this file. ``tests/test_adk_app.py::test_instruction_is_the_pack_not_a_copy``
perturbs the pack in memory and asserts the instruction moves with it, so a
copy that merely happens to match today cannot pass.

*Which* pack is :data:`SHIPPING_VARIANTS` — one arm per subagent, the arms the
scorecard reports. Same-file identity is not enough on its own: loading the
right file from the wrong arm would still show the customer a prompt no gate
was evaluated against.

The user turn is rendered through the same pack too, and encoded the way
``amw/adapters/gemini.py`` encodes it (one user ``Content`` of chunk parts,
then one user ``Content`` of message parts, no glue text). That is what
:func:`_render_through_pack` does as a ``before_model_callback``: ADK's own
"here is your JSON input" turn is replaced by the bench's wire format.

Two things ADK adds that we do not control: a short identity preamble
("You are an agent. Your internal name is ...") and, for structured agents, its
own ``response_schema`` plumbing. Both are framework behaviour, disclosed here
rather than hidden.

Zero-credential import
----------------------

``google.adk`` and ``google.genai`` are imported inside functions, never at
module scope, so ``import amw.agents.adk_app`` works on a machine with neither
the SDK nor ADC — ``python cli.py e2e --mode replay`` stays green
(CLAUDE.md ground rule 4). Missing SDK becomes a clear error at the moment the
demo is actually invoked.

Gemini only
-----------

:func:`resolve_demo_model` refuses any model whose ``provider`` in
``config/models.yaml`` is not ``google``. There is no Claude path here: the
incumbent's job in this workshop is to be measured by the harness, not to be
re-implemented in a second framework.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from amw.agents.prompt_packs import (
    CHUNK_PLACEHOLDERS,
    PLACEHOLDERS,
    PromptPack,
    load_pack,
    resolve_model,
)
from amw.agents.schemas import SUBAGENTS, schema_model

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from amw.config import AppConfig

__all__ = [
    "TAXONOMY",
    "ROOT_TAXONOMY",
    "ROOT_NAME",
    "DEMO_VARIANT",
    "SHIPPING_VARIANTS",
    "variant_for",
    "SAMPLE_QUERIES",
    "DelegationStep",
    "DelegationTrace",
    "AdkUnavailableError",
    "instruction_for",
    "resolve_demo_model",
    "retrieve_chunks",
    "build_subagent",
    "build_root_agent",
    "build_app",
    "run_demo",
    "cmd_adk_demo",
]


# --------------------------------------------------------------------------
# taxonomy labels
# --------------------------------------------------------------------------

#: Behaviour-class labels, verbatim from the four rows of
#: ``docs/what_we_measure.md`` §"The canonical taxonomy". They are printed
#: beside each agent in the delegation trace so the demo answers the question
#: the taxonomy table poses — *which row is this, and does it get a verdict?* —
#: agent by agent, on screen.
#:
#: All three evaluated subagents are row 1. That is the deliberate scoping
#: choice the scorecard footer prints; seeing three identical labels next to an
#: ``orchestration`` root is the point, not an oversight.
TAXONOMY: dict[str, str] = {
    "query_rewriter": "prompt-based",
    "chunk_summarizer": "prompt-based",
    "feature_extractor": "prompt-based",
}

#: Row 4. The root gets **no verdict** on this scorecard — trajectory
#: evaluation is the follow-on instrument (``docs/what_we_measure.md``, and
#: ``amw/agents/root_orchestrator.py`` for why the evaluated stub stays a stub).
ROOT_TAXONOMY = "orchestration"

#: Row 3, used for the retrieval step. Labelled so nobody reads the demo's
#: toy lexical lookup as the measured retrieval quality it is not.
RETRIEVAL_TAXONOMY = "retrieval-augmented"

ROOT_NAME = "root_orchestrator"

#: The arm each subagent actually ships, per the 2026-08-11 selection ruling.
#: The demo loads *these* prompts, one per subagent, so the thing on screen is
#: the thing the scorecard measured.
#:
#: It used to be a single variant for all three, back when all three shipped
#: the same rung. They no longer do: Feature Extractor ships the promoted
#: optimizer instruction and Query Rewriter ships the targeted rung. Leaving
#: the demo on ``gemini_tuned_v1`` would have put two prompts on screen that
#: no gate was evaluated against — the exact drift this module's
#: single-source-of-truth mechanism exists to prevent, just one level up.
#:
#: ``tests/test_adk_app.py`` pins each entry against the arm named in the
#: scorecard, so a re-selection that forgets the demo fails there.
SHIPPING_VARIANTS: dict[str, str] = {
    "query_rewriter": "gemini_targeted_v1",
    "chunk_summarizer": "gemini_tuned_v1",
    "feature_extractor": "gemini_optimizer_v1",
}

#: What ``--variant`` falls back to when the workshop pins one arm across all
#: three — used to show A0 beside the shipping arms if asked. Not the default:
#: passing no ``--variant`` gets :data:`SHIPPING_VARIANTS`, per subagent.
DEMO_VARIANT = "gemini_tuned_v1"


def variant_for(subagent: str, variant: str | None = None) -> str:
    """Which pack this subagent loads: the explicit override, or its shipping arm.

    One function so the per-subagent default is resolved in exactly one place
    (the same discipline ``adapters/__init__.py`` applies to modes). ``None``
    means "whatever ships", which is the answer everywhere except a workshop
    A/B where the operator names one arm for all three.
    """
    if variant is not None:
        return variant
    try:
        return SHIPPING_VARIANTS[subagent]
    except KeyError:
        raise KeyError(
            f"no shipping arm declared for {subagent!r}; SHIPPING_VARIANTS "
            f"covers {sorted(SHIPPING_VARIANTS)}"
        ) from None

#: The two queries the DoD requires a live run of. Patents-domain, and shaped
#: to exercise both the summarizer path (needs chunks) and the extractor path.
SAMPLE_QUERIES: tuple[str, ...] = (
    "Which companies filed solid-state battery separator patents in Japan "
    "between 2019 and 2021, and what do the filings claim?",
    "Summarise what is known about perovskite-silicon tandem photovoltaic "
    "efficiency, and pull the structured features out of the source document.",
)

#: Corpus the retrieval tool serves from, in preference order. The committed
#: e2e fixture is the fallback so the tool still works in a fresh checkout that
#: has not run ``cli.py gen``.
_CORPUS_CANDIDATES: tuple[Path, ...] = (
    Path(__file__).resolve().parents[2] / "datasets" / "chunk_summarizer.jsonl",
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "e2e"
    / "datasets"
    / "chunk_summarizer.jsonl",
)

#: How many chunks the retrieval tool hands back. Small on purpose: the demo is
#: about the delegation shape, not about recall@k.
RETRIEVAL_TOP_K = 4


class AdkUnavailableError(RuntimeError):
    """``google-adk`` is not installed, so the demo cannot run.

    Raised only when the demo is invoked. Importing this module never needs
    ADK — see the module docstring.
    """


def _require_adk() -> None:
    try:
        import google.adk  # noqa: F401
    except ImportError as exc:  # pragma: no cover - exercised by hand, not CI
        raise AdkUnavailableError(
            "google-adk is not installed, so `cli.py adk-demo` cannot run. It is "
            "a demo-only dependency: nothing in the eval path needs it, and "
            "`cli.py e2e --mode replay` does not touch it. Install it with "
            "`pip install -r requirements.txt`. Per WORKSHOP_RUNBOOK the "
            "fallback for this segment is the architecture diagram plus the "
            "prompt files plus a harness run."
        ) from exc


# --------------------------------------------------------------------------
# instruction text: the pack, not a copy
# --------------------------------------------------------------------------


def instruction_for(subagent: str, variant: str | None = None) -> str:
    """The ADK ``instruction`` for ``subagent`` — read off disk, every call.

    This is the whole single-source-of-truth mechanism, and it is deliberately
    one line long: the instruction *is*
    ``amw/agents/prompts/{subagent}/{variant}.txt``'s ``=== system ===``
    section, byte for byte. Nothing rewrites, wraps, or reformats it, so there
    is no place for a copy to hide and drift.

    ``variant=None`` loads the subagent's shipping arm — see
    :func:`variant_for`.
    """
    return load_pack(subagent, variant_for(subagent, variant)).system


#: The registry role the demo's backend comes from.
#:
#: Deliberately *not* ``gemini_candidate``. The measured arms answer "does the
#: prompt work hold up", and they were recorded on the development generation;
#: this app answers "what would we deploy", and the answer the scorecard gives
#: is the headline deployment candidate. Routing the demo through its own role
#: means the workshop can re-point the demo without touching a single model a
#: number was measured on — and cannot accidentally do the reverse either.
DEMO_ROLE = "adk_demo"


def resolve_demo_model(variant: str | None = None, cfg: "AppConfig | None" = None) -> str:
    """Provider model ID for the demo, from ``config/models.yaml``.

    No model ID literal appears in this module (CLAUDE.md conventions): the
    :data:`DEMO_ROLE` role names the model and the ``vertex`` access path names
    the ID. Non-Google providers are refused here rather than failing obscurely
    inside ADK — the constraint is "Gemini backend only", so it should read as
    a constraint.

    ``variant`` no longer chooses the backend, because the demo's backend is a
    deployment decision and a variant is a prompt. It is still checked: the
    shipping arms have to keep resolving to a single model, since the root and
    its three leaves share one ``model`` and an arm that quietly moved to
    another role would mean the demo is showing prompts from a mixed set.
    """
    from amw.adapters.gemini import ACCESS_PATH
    from amw.config import ConfigError, load_all

    if cfg is None:
        cfg = load_all()
    wanted = [variant] if variant is not None else sorted(set(SHIPPING_VARIANTS.values()))
    roles = {v: resolve_model(v, cfg.models) for v in wanted}
    if len(set(roles.values())) > 1:
        raise ConfigError(
            f"the demo runs one model for the root and all three leaves, but "
            f"the shipping arms resolve to different models: {roles}. Pin one "
            f"with --variant, or reconcile config/models.yaml."
        )
    # The variant no longer picks the backend, so it has to be refused on its
    # own account: running the Claude arm's prompt bytes on a Gemini backend
    # and calling the result a reference app would show the customer something
    # no gate was evaluated against, which is the drift this module exists to
    # prevent.
    for name, key in roles.items():
        if cfg.models.spec(key).provider != "google":
            raise ConfigError(
                f"the ADK reference app is Gemini-only, but variant {name!r} "
                f"belongs to {key!r} (provider "
                f"{cfg.models.spec(key).provider!r}). The incumbent is measured "
                f"through the adapter harness, not re-implemented here."
            )
    key, spec = cfg.models.for_role(DEMO_ROLE)
    if spec.provider != "google":
        raise ConfigError(
            f"the ADK reference app is Gemini-only, but the {DEMO_ROLE!r} role "
            f"resolves to {key!r} (provider {spec.provider!r})."
        )
    return spec.id_for(ACCESS_PATH)


# --------------------------------------------------------------------------
# delegation trace
# --------------------------------------------------------------------------


@dataclass
class DelegationStep:
    """One thing that actually happened during the run.

    Every field is recorded from an ADK callback as it fires. Nothing here is
    predicted, templated, or filled in afterwards (ground rule 1): if a step is
    in the trace, the framework ran it.
    """

    kind: str  # "agent" | "tool" | "result"
    name: str
    taxonomy: str | None = None
    detail: str = ""
    depth: int = 0
    elapsed_ms: int = 0

    def render(self) -> str:
        pad = "  " * self.depth
        label = f"[{self.taxonomy}] " if self.taxonomy else ""
        head = f"{pad}{label}{self.name}"
        if self.detail:
            return f"{head}\n{pad}    {self.detail}"
        return head


@dataclass
class DelegationTrace:
    """Ordered record of a single ``adk-demo`` invocation."""

    steps: list[DelegationStep] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    final_text: str = ""

    def add(self, kind: str, name: str, **kwargs: Any) -> DelegationStep:
        step = DelegationStep(
            kind=kind,
            name=name,
            elapsed_ms=int((time.monotonic() - self.started) * 1000),
            **kwargs,
        )
        self.steps.append(step)
        return step

    def render(self) -> str:
        lines = [step.render() for step in self.steps]
        return "\n".join(lines)


def _summarise(value: Any, limit: int = 220) -> str:
    """A one-line, truncated view of a tool payload for the trace."""
    if isinstance(value, str):
        text = " ".join(value.split())
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------
# retrieval tool: provided chunks, from the same corpus the bench uses
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _corpus_path() -> Path:
    for candidate in _CORPUS_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "no chunk_summarizer corpus found; looked at "
        + ", ".join(str(p) for p in _CORPUS_CANDIDATES)
        + ". Run `python cli.py gen --customer demo_patents -n 70` first."
    )


def _load_corpus_items() -> list[dict[str, Any]]:
    """The Chunk Summarizer corpus, as ``{item_id, chunks}`` records.

    Read straight from ``datasets/*.jsonl``, and kept grouped **per item**
    rather than flattened into one chunk pool. Two reasons, both about staying
    consistent with dataset items:

    * The chunk shape the tool hands back is then literally ``input.chunks`` —
      ``chunk_id`` + ``text``, exactly what ``amw.eval.runner.prompt_view``
      passes a prompt pack. A demo that invented its own chunk shape would show
      the customer a retrieval contract the bench never tested.
    * An item's chunks come from one patent document. Serving a mixed bag of
      passages from four unrelated patents is not what the corpus represents,
      and it makes the Feature Extractor's "extract from *the* document" step
      unanswerable — correctly so, which is why the first draft of this tool
      never reached that agent.
    """
    items: list[dict[str, Any]] = []
    with _corpus_path().open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            chunks = [
                {"chunk_id": chunk["chunk_id"], "text": chunk["text"]}
                for chunk in record.get("input", {}).get("chunks", [])
            ]
            if chunks:
                items.append({"item_id": record.get("item_id", ""), "chunks": chunks})
    return items


def retrieve_chunks(query: str) -> dict[str, Any]:
    """Retrieve passages from the demo patents corpus for a search query.

    Args:
        query: A search query. Prefer the rewritten query from query_rewriter.

    Returns:
        The matching passages, each with a chunk_id to cite and its text.
    """
    # Deliberately a lexical overlap score over whole corpus items, not an
    # embedding index: this is a *provided-chunks* stand-in so the summarizer
    # has something real and citable to work on. Retrieval quality is the row-3
    # instrument and is explicitly not measured in this workshop
    # (docs/what_we_measure.md), so dressing this up as a retriever would
    # misrepresent what was tested.
    wanted = set(_TOKEN_RE.findall(query.lower()))
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for position, item in enumerate(_load_corpus_items()):
        tokens: set[str] = set()
        for chunk in item["chunks"]:
            tokens |= set(_TOKEN_RE.findall(chunk["text"].lower()))
        overlap = len(wanted & tokens)
        if overlap:
            # -overlap then +position: best first, ties broken by corpus order,
            # so the demo shows the same passages every time it is run.
            scored.append((-overlap, position, item))
    scored.sort(key=lambda row: (row[0], row[1]))
    if not scored:
        return {
            "chunks": [],
            "corpus": _corpus_path().name,
            "provenance": "synthetic",
            "note": "No passage in the demo corpus shares a term with this query.",
        }
    best = scored[0][2]
    return {
        "chunks": best["chunks"][:RETRIEVAL_TOP_K],
        "source_item": best["item_id"],
        "corpus": _corpus_path().name,
        "provenance": "synthetic",
        "note": (
            "All passages come from one document, so feature_extractor can run "
            "on them. Lexical overlap over the committed demo corpus — a "
            "provided-chunks stand-in for the customer's index. Retrieval "
            "quality is not measured in this workshop."
        ),
    }


# --------------------------------------------------------------------------
# input shapes (mirror the prompt packs' placeholders)
# --------------------------------------------------------------------------


def _input_schema(subagent: str) -> type:
    """Pydantic model for what the root must hand this subagent.

    Derived from :data:`~amw.agents.prompt_packs.PLACEHOLDERS` and
    :data:`~amw.agents.prompt_packs.CHUNK_PLACEHOLDERS` rather than written out,
    so a pack that grows a placeholder cannot leave the ADK tool signature
    behind. ``tests/test_adk_app.py`` pins the correspondence.
    """
    from pydantic import BaseModel, Field, create_model

    class _Chunk(BaseModel):
        chunk_id: str = Field(description="ID of a retrieved passage, for citation.")
        text: str = Field(description="The passage text, verbatim.")

    fields: dict[str, Any] = {
        name: (str, Field(description=f"The {name} to process."))
        for name in PLACEHOLDERS[subagent]
    }
    if subagent in CHUNK_PLACEHOLDERS:
        fields["chunks"] = (
            list[_Chunk],
            Field(description="Retrieved passages from retrieve_chunks."),
        )
    return create_model(f"{_camel(subagent)}Input", **fields)


def _camel(subagent: str) -> str:
    return "".join(part.capitalize() for part in subagent.split("_"))


# --------------------------------------------------------------------------
# agents
# --------------------------------------------------------------------------


def _render_through_pack(pack: PromptPack, trace: DelegationTrace):
    """A ``before_model_callback`` that swaps ADK's user turn for the bench's.

    ADK's ``AgentTool`` sends the sub-agent a bare JSON document of its
    ``input_schema``. The bench sends the pack's rendered ``user`` section, plus
    one part per rendered chunk, with no glue text
    (``tests/test_adapter_parity.py`` pins that encoding). This callback makes
    the demo send the second thing, so what the customer sees on screen is the
    prompt surface that was actually measured — not an ADK-flavoured
    approximation of it.

    If the incoming turn is not the JSON we expect, the callback leaves the
    request alone and says so in the trace rather than guessing.
    """

    def callback(callback_context, llm_request):  # noqa: ANN001 - ADK signature
        from google.genai import types

        contents = list(llm_request.contents or [])
        raw = ""
        for content in reversed(contents):
            if content.role == "user" and content.parts:
                raw = "".join(part.text or "" for part in content.parts)
                break
        try:
            item = json.loads(raw)
            if not isinstance(item, Mapping):
                raise ValueError("not an object")
            rendered = pack.render(item)
        except Exception as exc:  # noqa: BLE001 - never break the demo on this
            trace.add(
                "result",
                f"{pack.subagent}: pack rendering skipped",
                depth=3,
                detail=f"{type(exc).__name__}: {exc}",
            )
            return None

        replacement = []
        if rendered.context_chunks:
            replacement.append(
                types.Content(
                    role="user",
                    parts=[types.Part(text=chunk) for chunk in rendered.context_chunks],
                )
            )
        replacement.append(
            types.Content(
                role="user",
                parts=[types.Part(text=message) for message in rendered.messages],
            )
        )
        llm_request.contents = replacement
        return None

    return callback


def _on_agent_start(name: str, taxonomy: str, detail: str, depth: int, trace: DelegationTrace):
    def callback(callback_context):  # noqa: ANN001 - ADK signature
        trace.add("agent", name, taxonomy=taxonomy, detail=detail, depth=depth)
        return None

    return callback


def _on_tool_start(trace: DelegationTrace):
    def callback(tool, args, tool_context):  # noqa: ANN001 - ADK signature
        taxonomy = TAXONOMY.get(tool.name) or (
            RETRIEVAL_TAXONOMY if tool.name == retrieve_chunks.__name__ else None
        )
        trace.add(
            "tool",
            f"→ {tool.name}",
            taxonomy=taxonomy,
            detail=f"args: {_summarise(args)}",
            depth=1,
        )
        return None

    return callback


def _on_tool_end(trace: DelegationTrace):
    def callback(tool, args, tool_context, tool_response):  # noqa: ANN001 - ADK signature
        trace.add(
            "result",
            f"← {tool.name}",
            detail=f"returned: {_summarise(tool_response)}",
            depth=1,
        )
        return None

    return callback


def build_subagent(
    subagent: str, *, model: str, variant: str | None = None, trace: DelegationTrace
):
    """One evaluated subagent as an ADK ``Agent``.

    Instruction from the prompt pack, output schema from
    ``amw/agents/schemas.py``. Those two files are the contract the bench
    measures; this function is a wiring adapter over them and owns neither.

    ``variant=None`` loads this subagent's shipping arm, which is not the same
    arm for all three — see :data:`SHIPPING_VARIANTS`.
    """
    _require_adk()
    from google.adk.agents import Agent

    variant = variant_for(subagent, variant)
    pack = load_pack(subagent, variant)
    return Agent(
        name=subagent,
        model=model,
        description=(
            f"{subagent.replace('_', ' ').title()} — taxonomy row "
            f"'{TAXONOMY[subagent]}', prompt pack {variant}. Returns a "
            f"{schema_model(subagent).__name__} object."
        ),
        # The single source of truth. Not a copy of the pack: the pack.
        instruction=instruction_for(subagent, variant),
        input_schema=_input_schema(subagent),
        output_schema=schema_model(subagent),
        before_agent_callback=_on_agent_start(
            subagent,
            TAXONOMY[subagent],
            f"prompt: {pack.path.relative_to(Path(__file__).resolve().parents[2])} "
            f"(sha256 {pack.sha256[:12]}, mode {pack.output_mode})",
            2,
            trace,
        ),
        before_model_callback=_render_through_pack(pack, trace),
    )


#: The root's instruction is a literal, and that is deliberate.
#:
#: ``amw/agents/root_orchestrator.py`` explains why there is no prompt pack for
#: the orchestrator: "a prompt file on disk would imply an arm that can be
#: run." Nothing about the root is evaluated — no gates cover it, no gold
#: reference exists for routing — so its wording lives here, in demo-only code,
#: where it cannot be mistaken for a measured arm. The three instructions that
#: *are* measured all come off disk.
_ROOT_INSTRUCTION = """\
You are the Root Orchestrator of a patents-domain RAG system. You do not answer
from your own knowledge; you delegate to specialist subagents and to a retrieval
tool, then report what they returned.

For each user question, in order:

1. Call `query_rewriter` with the user's question to get a structured retrieval
   plan (a rewritten query, filters, and an intent).
2. Call `retrieve_chunks` with the rewritten query from step 1.
3. Call `chunk_summarizer` with the original question and the chunks from
   step 2, exactly as they were returned, preserving each chunk_id.
4. If the retrieved passages come from a single patent document, call
   `feature_extractor` once with those passages concatenated as the document.
   If they are clearly from different documents, skip this step and say so.
5. Reply in plain text, in under 120 words: the intent you routed on, the
   summary, and any structured features you extracted. Cite chunk ids the
   summarizer cited. Do not add facts no subagent returned.

Call each subagent at most once. If a subagent returns an error, say which one
failed and stop — do not invent its output.
"""


def build_root_agent(
    *, model: str, variant: str | None = None, trace: DelegationTrace | None = None
):
    """The delegating root, with the three subagents attached as tools.

    Tools rather than ``sub_agents``: an ADK transfer hands control *away*, and
    a transferred-to agent with an ``output_schema`` ends the invocation. The
    demo needs the root to stay in charge across three delegations and then
    speak last, which is what ``AgentTool`` gives.
    """
    _require_adk()
    from google.adk.agents import Agent
    from google.adk.tools.agent_tool import AgentTool

    trace = trace if trace is not None else DelegationTrace()
    subagents = [
        build_subagent(name, model=model, variant=variant, trace=trace)
        for name in SUBAGENTS
    ]
    return Agent(
        name=ROOT_NAME,
        model=model,
        description="Routes a patents question across the three utility subagents.",
        instruction=_ROOT_INSTRUCTION,
        tools=[
            retrieve_chunks,
            # skip_summarization stays False. It is tempting — the subagent
            # already returned a validated object, so re-summarising it burns a
            # model call — but setting it makes ADK hand the tool result
            # straight back as the final response, which ends the invocation
            # after the *first* delegation. The whole point of this segment is
            # the root staying in charge across all three.
            *(AgentTool(agent=agent) for agent in subagents),
        ],
        before_agent_callback=_on_agent_start(
            ROOT_NAME,
            ROOT_TAXONOMY,
            f"model {model} · no verdict on this scorecard (trajectory "
            f"evaluation is the follow-on instrument)",
            0,
            trace,
        ),
        before_tool_callback=_on_tool_start(trace),
        after_tool_callback=_on_tool_end(trace),
    )


def build_app(*, variant: str | None = None, cfg: "AppConfig | None" = None):
    """``(root_agent, trace, model_id)`` — everything a run needs, wired."""
    model = resolve_demo_model(variant, cfg)
    trace = DelegationTrace()
    return build_root_agent(model=model, variant=variant, trace=trace), trace, model


# --------------------------------------------------------------------------
# running
# --------------------------------------------------------------------------


def _export_vertex_env(cfg: "AppConfig | None" = None) -> tuple[str, str]:
    """Point ADK's genai client at the same project/region the adapters use.

    ADK reads ``GOOGLE_GENAI_USE_VERTEXAI`` / ``GOOGLE_CLOUD_PROJECT`` /
    ``GOOGLE_CLOUD_LOCATION``; ``.env`` speaks ``PROJECT_ID`` / ``REGION``.
    Bridged here, at invocation time, so importing this module still touches no
    credentials. Note the demo pins Gemini's region, not ``CLAUDE_REGION`` —
    there is no Claude in this path.

    A registry ``region`` on the demo model wins over ``$REGION``, exactly as
    it does in :mod:`amw.adapters.gemini`. The deployment candidates are not
    served in ``us-central1``, so before this precedence existed here the demo
    sent ``gemini-3.6-flash`` to ``$REGION`` and got a 404 — a live demo that
    fails in front of the customer for a reason the registry already knew.
    """
    from amw.adapters.gemini import _require_env
    from amw.config import load_all

    if cfg is None:
        cfg = load_all()
    _key, spec = cfg.models.for_role(DEMO_ROLE)

    project = _require_env("PROJECT_ID", os.environ.get("PROJECT_ID"))
    location = spec.region or _require_env("REGION", os.environ.get("REGION"))
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "1")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project)
    # `=` not `setdefault`: GOOGLE_CLOUD_LOCATION may already be exported to
    # $REGION by the shell, and a stale value here is the 404 above.
    os.environ["GOOGLE_CLOUD_LOCATION"] = location
    return project, location


async def _run_once_async(query: str, *, variant: str | None, cfg: "AppConfig | None"):
    _require_adk()
    from google.adk.runners import InMemoryRunner
    from google.genai import types

    root, trace, model = build_app(variant=variant, cfg=cfg)
    runner = InMemoryRunner(agent=root, app_name="amw_adk_demo")
    session = await runner.session_service.create_session(
        app_name="amw_adk_demo", user_id="workshop"
    )
    message = types.Content(role="user", parts=[types.Part(text=query)])
    async for event in runner.run_async(
        user_id="workshop", session_id=session.id, new_message=message
    ):
        if event.error_message:
            trace.add(
                "result",
                f"{event.author}: error",
                detail=event.error_message,
                depth=1,
            )
        if event.author == ROOT_NAME and event.content and event.content.parts:
            text = "".join(part.text or "" for part in event.content.parts).strip()
            if text:
                trace.final_text = text
    return trace, model


def run_demo(
    query: str, *, variant: str | None = None, cfg: "AppConfig | None" = None
) -> tuple[DelegationTrace, str]:
    """Run one query end to end on Gemini. Returns ``(trace, model_id)``.

    The single entry point the CLI calls. Live only: there is no replay store
    for ADK-internal calls, and inventing one would be a fabricated result.
    """
    import asyncio

    _require_adk()
    _export_vertex_env(cfg)
    return asyncio.run(_run_once_async(query, variant=variant, cfg=cfg))


def _packs_label(variant: str | None) -> str:
    """What to print for "which prompts is this running".

    Spelled out per subagent when the arms differ, because "prompt pack
    gemini_tuned_v1" on a run where two of the three leaves load something
    else is a caption that contradicts the screen.
    """
    if variant is not None:
        return f"prompt pack {variant} (pinned for all three)"
    return "prompt packs " + ", ".join(
        f"{name}={variant_for(name)}" for name in SUBAGENTS
    )


def _print_run(query: str, trace: DelegationTrace, model: str, variant: str | None) -> None:
    print(f"\n{'=' * 78}\nquery: {query}\n{'=' * 78}")
    print(f"model {model} · {_packs_label(variant)} · live Gemini, not recorded\n")
    print(trace.render() or "(no steps — the run produced no callbacks)")
    print(f"\nfinal answer:\n{trace.final_text or '(none)'}")


def cmd_adk_demo(args, cfg) -> int:
    """``cli.py adk-demo`` — the whole subcommand.

    Lives here, not in ``cli.py``, so the CLI keeps a one-line lazy import and
    a machine with no ``google-adk`` can still run every other subcommand.
    """
    from amw.config import ConfigError

    # None, not DEMO_VARIANT: no --variant means each leaf loads its own
    # shipping arm. Collapsing to one arm here is what used to put a prompt on
    # screen that no gate was evaluated against.
    variant = getattr(args, "variant", None)
    queries: Sequence[str] = getattr(args, "query", None) or list(SAMPLE_QUERIES)

    if getattr(args, "mode", "live") != "live":
        print(
            "adk-demo is live-only: the ADK runtime makes its own model calls and "
            "nothing records them to artifacts/replay/, so a 'replay' ADK run "
            "would be a fabrication. Re-run with --mode live, or use the "
            "WORKSHOP_RUNBOOK fallback (diagram + prompt files + harness).",
        )
        return 2

    failures = 0
    for query in queries:
        try:
            trace, model = run_demo(query, variant=variant, cfg=cfg)
        except (AdkUnavailableError, ConfigError) as exc:
            print(f"adk-demo: {exc}")
            return 2
        _print_run(query, trace, model, variant)
        if not trace.final_text:
            failures += 1

    if failures:
        print(f"\nadk-demo: {failures} query/queries produced no final answer.")
        return 5
    print("\nadk-demo OK — demo-only path; no number here reaches a scorecard.")
    return 0
