"""The Migration Readiness Scorecard: gates -> verdicts -> Markdown.

Three modules, in the order the data flows:

* :mod:`amw.reporting.evidence` — decides, per subagent, which of the six
  gates in ``config/gates.yaml`` this run actually measured, and records a
  reason for every one it did not.
* :mod:`amw.reporting.scorecard` — checks the measurable gates on their CI
  bound via :mod:`amw.eval.stats`, applies the verdict rules read out of
  ``gates.yaml``, and renders the report.
* :mod:`amw.reporting.cells` — the four cells whose caveat is part of the
  measurement, welded together by construction so the number cannot be
  rendered without it.

``cmd_scorecard`` is the entry point ``cli.py scorecard`` calls.
"""

from amw.reporting.cells import (
    CLAUDE_SCHEMA_CAVEAT,
    REGION_SPLIT_DISCLOSURE,
    ClaudeSchemaValidityCell,
    JudgeScoreCell,
)
from amw.reporting.evidence import (
    Regions,
    SameRegionLatencyProbe,
    SubagentEvidence,
    build_evidence,
    collect_samples,
)
from amw.reporting.scorecard import (
    DEFAULT_SHADOW_METRIC,
    SHADOW_METRICS,
    INCOMPLETE,
    PARITY_SENTENCE,
    TAXONOMY_LINE,
    UNDETERMINED,
    Scorecard,
    SubagentVerdict,
    VerdictRules,
    build_scorecard,
    cmd_scorecard,
    decide_verdict,
    render_markdown,
)

__all__ = [
    "DEFAULT_SHADOW_METRIC",
    "SHADOW_METRICS",
    "CLAUDE_SCHEMA_CAVEAT",
    "INCOMPLETE",
    "PARITY_SENTENCE",
    "REGION_SPLIT_DISCLOSURE",
    "TAXONOMY_LINE",
    "UNDETERMINED",
    "ClaudeSchemaValidityCell",
    "JudgeScoreCell",
    "Regions",
    "SameRegionLatencyProbe",
    "Scorecard",
    "SubagentEvidence",
    "SubagentVerdict",
    "VerdictRules",
    "build_evidence",
    "build_scorecard",
    "cmd_scorecard",
    "collect_samples",
    "decide_verdict",
    "render_markdown",
]
