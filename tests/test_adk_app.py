"""The ADK reference app is demo-only, and its prompts are not its own.

Two properties carry the whole card, and each gets a test that fails loudly:

1. **Single source of truth.** Every agent instruction *is* the prompt-pack
   text on disk. Asserting equality is not enough — a hardcoded copy that
   happens to match today passes that and drifts tomorrow. So
   :func:`test_instruction_is_the_pack_not_a_copy` perturbs the pack in memory
   and asserts the instruction moves with it, and
   :func:`test_no_prompt_text_is_copied_into_the_module` asserts no prompt line
   is present in ``adk_app.py`` at all.

2. **Never in the eval path.** ``phase2``/``ablate``/``shadow``/``scorecard``
   run exclusively through ``amw/adapters/`` so every call is recorded and
   replayable (CLAUDE.md ground rules 1, 4, 5). An ADK ``Runner`` has no
   recording hook, so a measurement taken through it could not be reproduced
   offline. :func:`test_eval_path_never_imports_adk_app` pins the boundary.

Everything here runs offline with zero credentials: the module imports without
``google-adk``, and the tests that need real ADK objects skip when it is absent.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from amw.agents import adk_app
from amw.agents import prompt_packs as pp
from amw.agents.schemas import SUBAGENTS, schema_model

adk = pytest.importorskip("google.adk", reason="google-adk is a demo-only dependency")

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = Path(adk_app.__file__).resolve()

#: A model ID is required to construct an ADK Agent but no call is made, so
#: these tests neither need credentials nor spend quota. Taken from the config
#: registry rather than written down, per CLAUDE.md conventions.
MODEL = adk_app.resolve_demo_model()

VARIANTS = [adk_app.DEMO_VARIANT, "gemini_naive"]


def _agent(subagent: str, variant: str = adk_app.DEMO_VARIANT):
    return adk_app.build_subagent(
        subagent,
        model=MODEL,
        variant=variant,
        trace=adk_app.DelegationTrace(),
    )


# --------------------------------------------------------------------------
# 1. the instruction IS the prompt pack
# --------------------------------------------------------------------------


@pytest.mark.parametrize("subagent", SUBAGENTS)
@pytest.mark.parametrize("variant", VARIANTS)
def test_instruction_equals_the_pack_system_section(subagent, variant):
    """Byte-for-byte, with no wrapper, prefix, or reformatting."""
    pack = pp.load_pack(subagent, variant)
    assert _agent(subagent, variant).instruction == pack.system


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_instruction_is_the_pack_not_a_copy(subagent, monkeypatch):
    """Perturb the pack in memory; the instruction must move with it.

    This is the test that distinguishes "loaded from the pack" from "a literal
    that currently matches the pack". Equality alone cannot tell those apart,
    and only one of them is safe to leave in a repo.

    The perturbation is applied to the loader the module actually calls, so a
    hypothetical future ``adk_app`` that cached the text at import time, or
    inlined it, would fail here rather than silently pass.
    """
    real = pp.load_pack
    marker = "\n\nDRIFT-CANARY-98a1: this line exists only inside this test.\n"

    def perturbed(name, variant):
        pack = real(name, variant)
        if name != subagent:
            return pack
        return pack.model_copy(update={"system": pack.system + marker})

    monkeypatch.setattr(adk_app, "load_pack", perturbed)

    assert marker in adk_app.instruction_for(subagent)
    assert marker in _agent(subagent).instruction
    # And only the perturbed subagent moves — the loader is consulted per
    # agent, not once for all of them.
    for other in SUBAGENTS:
        if other != subagent:
            assert marker not in _agent(other).instruction


def test_no_prompt_text_is_copied_into_the_module():
    """No substantive line of any prompt file appears in ``adk_app.py``.

    Belt and braces for the property above: even a copy that is never *used*
    should not sit in this file, because the next person to touch it will not
    know which one is live.
    """
    source = MODULE_PATH.read_text(encoding="utf-8")
    offenders: list[str] = []
    for subagent in SUBAGENTS:
        for variant in pp.variants_for(subagent):
            for line in pp.load_pack(subagent, variant).text.splitlines():
                stripped = line.strip()
                # Short lines and section markers collide with ordinary prose;
                # a copied prompt is detectable from its substantive lines.
                if len(stripped) >= 40 and stripped in source:
                    offenders.append(f"{subagent}/{variant}: {stripped[:70]}")
    assert not offenders, (
        "prompt text is duplicated inside adk_app.py — the instruction must be "
        "read from the pack, never restated:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_output_schema_comes_from_agents_schemas(subagent):
    assert _agent(subagent).output_schema is schema_model(subagent)


@pytest.mark.parametrize("subagent", SUBAGENTS)
def test_input_schema_mirrors_the_pack_placeholders(subagent):
    """The ADK tool signature is derived from the pack's placeholders.

    If a pack grows a placeholder, the tool the root calls has to grow the
    argument, or the demo silently sends the subagent less than the bench does.
    """
    fields = set(_agent(subagent).input_schema.model_fields)
    expected = set(pp.PLACEHOLDERS[subagent])
    if subagent in pp.CHUNK_PLACEHOLDERS:
        expected.add("chunks")
    assert fields == expected


# --------------------------------------------------------------------------
# 2. never in the eval path
# --------------------------------------------------------------------------

#: Packages that produce numbers a customer sees. All of them must reach models
#: only through ``amw/adapters/``.
EVAL_PACKAGES = ("eval", "shadow", "tuning", "reporting", "datasets", "adapters")


def test_eval_path_never_imports_adk_app():
    offenders: list[str] = []
    for package in EVAL_PACKAGES:
        for path in sorted((REPO_ROOT / "amw" / package).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] + [a.name for a in node.names]
                else:
                    continue
                if any("adk_app" in name for name in names):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, (
        "the eval path imports the ADK demo app: " + ", ".join(offenders) + ". "
        "Measurements run exclusively through amw/adapters/, which records every "
        "call to artifacts/replay/; ADK's runtime has no such hook."
    )


def test_module_imports_without_google_adk(monkeypatch):
    """Importing the module must not need the SDK — ground rule 4.

    ``cli.py e2e --mode replay`` has to pass with zero credentials and, on a
    fresh machine, possibly without the demo-only dependency installed.
    """
    import builtins
    import importlib

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("google.adk") or name == "google.adk":
            raise ImportError("blocked by test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    module = importlib.reload(adk_app)
    try:
        with pytest.raises(module.AdkUnavailableError):
            module.build_subagent(
                "query_rewriter",
                model=MODEL,
                variant=adk_app.DEMO_VARIANT,
                trace=module.DelegationTrace(),
            )
    finally:
        monkeypatch.undo()
        importlib.reload(adk_app)


def test_no_module_level_adk_or_genai_import():
    """The guarantee above, pinned structurally as well as behaviourally."""
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    for node in tree.body:  # module level only; nested imports are the point
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        assert not any(
            name.startswith("google.") for name in names
        ), f"{names} imported at module scope; ADK/genai imports must be lazy"


def test_demo_is_gemini_only():
    """A Claude-backed variant is refused with a message that says why."""
    from amw.config import ConfigError

    with pytest.raises(ConfigError, match="Gemini-only"):
        adk_app.resolve_demo_model("claude_baseline")


def test_replay_mode_is_refused_rather_than_faked():
    """No ADK call is recorded, so there is no honest ADK replay (rule 1)."""
    import argparse

    args = argparse.Namespace(mode="replay", variant=None, query=["anything"])
    assert adk_app.cmd_adk_demo(args, None) == 2


# --------------------------------------------------------------------------
# retrieval tool: same chunk shape the bench uses
# --------------------------------------------------------------------------


def test_retrieval_returns_dataset_shaped_chunks():
    result = adk_app.retrieve_chunks(
        "perovskite silicon tandem photovoltaic power conversion efficiency"
    )
    assert result["chunks"], "the demo corpus should match this query"
    assert result["provenance"] == "synthetic"
    for chunk in result["chunks"]:
        assert set(chunk) == {"chunk_id", "text"}
        assert chunk["chunk_id"] and chunk["text"]


def test_retrieval_is_deterministic_and_single_document():
    """Same query, same passages — and all from one corpus item.

    Determinism keeps the demo repeatable on stage. Single-document keeps the
    Feature Extractor's step answerable: "extract features from the document"
    has no honest answer over a mixed bag of four unrelated patents.
    """
    query = "solid electrolyte separator lithium secondary battery"
    first = adk_app.retrieve_chunks(query)
    assert first == adk_app.retrieve_chunks(query)

    items = {
        item["item_id"]: {chunk["chunk_id"] for chunk in item["chunks"]}
        for item in adk_app._load_corpus_items()
    }
    returned = {chunk["chunk_id"] for chunk in first["chunks"]}
    assert returned <= items[first["source_item"]]


def test_retrieval_chunk_shape_matches_prompt_view():
    """The tool's chunks are interchangeable with a dataset item's chunks."""
    from amw.datasets.schema import read_items
    from amw.eval.runner import prompt_view

    path = REPO_ROOT / "tests" / "fixtures" / "e2e" / "datasets" / "chunk_summarizer.jsonl"
    item = next(iter(read_items(path)))
    bench_keys = {key for chunk in prompt_view(item)["chunks"] for key in chunk}
    tool_keys = {
        key
        for chunk in adk_app.retrieve_chunks("perovskite tandem photovoltaic")["chunks"]
        for key in chunk
    }
    assert tool_keys == bench_keys


def test_retrieved_chunks_render_through_the_pack():
    """What retrieval returns is directly renderable by the prompt pack.

    The demo's fidelity claim is that the ADK path puts the *same* text on the
    wire as the bench. That only holds if the tool's output is a valid pack
    item, so this asserts it end to end rather than by inspection.
    """
    hits = adk_app.retrieve_chunks("perovskite tandem photovoltaic efficiency")
    pack = pp.load_pack("chunk_summarizer", adk_app.DEMO_VARIANT)
    rendered = pack.render({"question": "What is claimed?", "chunks": hits["chunks"]})
    assert len(rendered.context_chunks) == len(hits["chunks"])
    for chunk, text in zip(hits["chunks"], rendered.context_chunks):
        assert chunk["chunk_id"] in text


# --------------------------------------------------------------------------
# taxonomy labels come from the workshop docs
# --------------------------------------------------------------------------


def test_taxonomy_labels_exist_in_the_taxonomy_doc():
    """Every label printed on screen is a row of ``docs/what_we_measure.md``.

    The demo's captions and the doc the customer is handed have to agree; a
    label invented here would be a fifth taxonomy row nobody signed off.
    """
    doc = (REPO_ROOT / "docs" / "what_we_measure.md").read_text(encoding="utf-8").lower()
    labels = set(adk_app.TAXONOMY.values()) | {
        adk_app.ROOT_TAXONOMY,
        adk_app.RETRIEVAL_TAXONOMY,
    }
    for label in labels:
        assert f"**{label}**" in doc, f"{label!r} is not a row in what_we_measure.md"


def test_every_evaluated_subagent_has_a_taxonomy_label():
    assert set(adk_app.TAXONOMY) == set(SUBAGENTS)


def test_root_is_not_an_evaluated_subagent():
    """The orchestrator gets no verdict; it must not look like an arm."""
    from amw.agents import root_orchestrator

    assert adk_app.ROOT_NAME not in SUBAGENTS
    assert root_orchestrator.EVALUATED is False
    assert not (pp.prompts_dir() / adk_app.ROOT_NAME).exists()


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def test_root_delegates_to_all_three_subagents_plus_retrieval():
    root, trace, model = adk_app.build_app()
    tool_names = {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in root.tools}
    assert tool_names == set(SUBAGENTS) | {"retrieve_chunks"}
    assert model == MODEL
    assert trace.steps == []


def test_trace_records_only_what_ran():
    """A fresh trace is empty, and rendering it invents nothing."""
    trace = adk_app.DelegationTrace()
    assert trace.render() == ""
    trace.add("agent", "root_orchestrator", taxonomy=adk_app.ROOT_TAXONOMY)
    assert "[orchestration] root_orchestrator" in trace.render()
