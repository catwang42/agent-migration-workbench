"""Root Orchestrator — stub only. Deliberately not evaluated in Act 1.

The reference RAG system has five agents (docs/master_plan.md §5.2): Root
Orchestrator, Query Rewriter, Chunk Summarizer, Feature Extractor, Answer
Drafter. Act 1 evaluates the three utility subagents in
:data:`amw.agents.schemas.SUBAGENTS`. This module exists so the architecture
diagram has a counterpart in code, and so that "why isn't the orchestrator in
the scorecard?" has an answer sitting where someone will look for it.

**Why it is a stub, not an omission.** The recommended migration strategy is to
move high-volume utility subagents first and leave the orchestrator on its
current model until the customer's own Phase 2. Routing quality is a different
measurement problem — multi-turn, stateful, and scored on downstream task
success rather than on a single structured payload — so folding it into the
same gates would produce a number that looks comparable and is not. Ground rule
1 cuts both ways: the honest move is to show no number rather than a
convenient one.

``config/customers/demo_patents.yaml`` marks this agent ``enabled: false``,
``evaluated: false``, ``tier: stub``; its volume profile is present only so the
cost model can price the whole system if a customer asks.

There is deliberately no prompt pack under ``amw/agents/prompts/`` for it: a
prompt file on disk would imply an arm that can be run.
"""

from __future__ import annotations

#: Subagents the orchestrator would route to in the full reference system.
#: Answer Drafter is P1 (the deliberate TUNE_FIRST row) and is not in
#: :data:`amw.agents.schemas.SUBAGENTS` until its tier is green-lit.
ROUTES: tuple[str, ...] = (
    "query_rewriter",
    "chunk_summarizer",
    "feature_extractor",
    "answer_drafter",
)

#: Kept explicit so no runner can pick this agent up by iterating the module.
EVALUATED = False


def route(question: str) -> str:  # pragma: no cover - stub, never executed
    """Placeholder for the routing decision. Intentionally unimplemented.

    Implementing this would mean shipping an evaluated arm that no gate in
    ``config/gates.yaml`` covers.
    """
    raise NotImplementedError(
        "Root Orchestrator is a stub in Act 1: it is not evaluated, has no "
        "prompt pack, and has no gates. See this module's docstring."
    )
