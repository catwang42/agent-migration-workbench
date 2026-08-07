"""amw.agents — the evaluated subagents: their output contract and their prompts.

Two modules, one seam each:

* :mod:`amw.agents.schemas` — the frozen output contract shared by the dataset
  generator, the prompt packs, and the eval engine.
* :mod:`amw.agents.prompt_packs` — the three prompt variants per subagent
  (incumbent Claude, naive A0 swap, tuned Gemini), loaded from versioned text
  files under ``amw/agents/prompts/``.

Typical use::

    from amw.agents import build_request
    request = build_request("query_rewriter", "gemini_tuned_v1", item)
    trace = adapter.complete(request)

:mod:`amw.agents.root_orchestrator` is a documented stub — see its docstring
for why it is not evaluated.
"""

from amw.agents.prompt_packs import (
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
    "VARIANT_SPECS",
    "PromptPack",
    "PromptPackError",
    "RenderedPrompt",
    "build_request",
    "load_pack",
    "load_packs",
    "resolve_model",
    "sample_item",
]
