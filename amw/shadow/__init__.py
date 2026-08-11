"""Shadow comparison: both backends on the same inputs, then triage.

Three modules, in the order the pipeline uses them:

``runner``
    Feeds one corpus through the baseline arm and the candidate arm and
    collects paired traces. In ``--mode replay`` (the default, and the only
    path the demo needs) this resolves entirely from the recorded corpus and
    makes zero model calls. Also owns the per-backend latency percentiles and
    the cross-region disclosure they must be printed with.
``agreement``
    How often the two arms produced the same thing, per subagent, as an
    :class:`~amw.eval.stats.Estimate` with a bootstrap 95% CI — the input the
    ``shadow_agreement`` gate is checked on (lower bound, ground rule 7).
``triage``
    Win / loss / tie for every disagreement, derived from the judge calls
    phase 2 already recorded. Never issues a new judge call, and never lets an
    unjudged item read as a tie.

Agreement is a *migration-risk* measure, not a quality measure: two arms can
agree on a wrong answer, and a disagreement can be the candidate being right.
That is exactly why the gate has an ``alt`` clause about adjudicated wins, and
why the triage table ships next to the rate rather than under it.

Entry point for ``python cli.py shadow`` is :func:`cmd_shadow`.
"""

from amw.shadow.agreement import (
    AGREEMENT_METRIC,
    DEFAULT_PROSE_THRESHOLD,
    LEXICAL_SIMILARITY_NAME,
    STRUCTURED_AGREEMENT_METRIC,
    FieldComparison,
    FieldRate,
    ItemAgreement,
    SubagentAgreement,
    aggregate_agreement,
    compare_item,
    lexical_similarity,
)
from amw.shadow.runner import (
    DEFAULT_BASELINE_ARM,
    DEFAULT_CANDIDATE_ARM,
    JUDGE_MODE,
    LIVE_SLICE_MAX,
    SHADOW_VERSION,
    ArmShadow,
    LatencyStats,
    ShadowResult,
    SubagentShadow,
    cmd_shadow,
    run_shadow,
)
from amw.shadow.triage import (
    NOT_ADJUDICATED,
    TriageRow,
    TriageSummary,
    adjudicate,
    summarize,
    triage_table_markdown,
)

__all__ = [
    "AGREEMENT_METRIC",
    "DEFAULT_BASELINE_ARM",
    "DEFAULT_CANDIDATE_ARM",
    "DEFAULT_PROSE_THRESHOLD",
    "JUDGE_MODE",
    "LEXICAL_SIMILARITY_NAME",
    "LIVE_SLICE_MAX",
    "NOT_ADJUDICATED",
    "SHADOW_VERSION",
    "STRUCTURED_AGREEMENT_METRIC",
    "ArmShadow",
    "FieldComparison",
    "FieldRate",
    "ItemAgreement",
    "LatencyStats",
    "ShadowResult",
    "SubagentAgreement",
    "SubagentShadow",
    "TriageRow",
    "TriageSummary",
    "adjudicate",
    "aggregate_agreement",
    "cmd_shadow",
    "compare_item",
    "lexical_similarity",
    "run_shadow",
    "summarize",
    "triage_table_markdown",
]
