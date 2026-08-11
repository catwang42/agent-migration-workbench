"""amw.agents — the evaluated subagents: their output contract and their prompts.

Two modules, one seam each:

* :mod:`amw.agents.schemas` — the frozen output contract shared by the dataset
  generator, the prompt packs, and the eval engine.
* :mod:`amw.agents.prompt_packs` — the prompt variants per subagent, loaded
  from versioned text files under ``amw/agents/prompts/``. Every subagent has
  the universal three (incumbent Claude, naive A0 swap, tuned Gemini) in
  :data:`VARIANTS`; a subagent may additionally declare its own, which live in
  :data:`ALL_VARIANTS` and are enumerated per subagent by :func:`variants_for`.

Typical use::

    from amw.agents import build_request
    request = build_request("query_rewriter", "gemini_tuned_v1", item)
    trace = adapter.complete(request)

:mod:`amw.agents.root_orchestrator` is a documented stub — see its docstring
for why it is not evaluated.
"""

from amw.agents.prompt_packs import (
    ALL_VARIANTS,
    PLACEHOLDERS,
    VARIANT_SPECS,
    VARIANTS,
    PromptPack,
    PromptPackError,
    RenderedPrompt,
    build_request,
    load_pack,
    load_packs,
    resolve_model,
    sample_item,
    variants_for,
)
from amw.agents.schemas import (
    SUBAGENTS,
    TOOL_NAMES,
    ChunkSummary,
    PatentFeatures,
    QueryPlan,
    json_schema,
    schema_model,
    tool_name,
)

__all__ = [
    "SUBAGENTS",
    "TOOL_NAMES",
    "ChunkSummary",
    "PatentFeatures",
    "QueryPlan",
    "json_schema",
    "schema_model",
    "tool_name",
    "PLACEHOLDERS",
    "VARIANTS",
    "ALL_VARIANTS",
    "VARIANT_SPECS",
    "PromptPack",
    "PromptPackError",
    "RenderedPrompt",
    "build_request",
    "load_pack",
    "load_packs",
    "resolve_model",
    "sample_item",
    "variants_for",
]
