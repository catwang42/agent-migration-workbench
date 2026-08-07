#!/usr/bin/env python3
"""SPIKE-S2 — Vertex AI GenAI Evaluation Service hello-world.

Timeboxed probe, per TASKS.md T05: run ONE rubric metric over 3 canned items
and parse scores + rationales into plain dicts.

  GREEN = parsed results printed (real scores from a real service call).
  RED   = the exact error is captured and written into SPIKES.md; we move on
          to the pre-decided fallback (local judge only) and never fake the
          managed service's output.

This is spike scaffolding, not demo-path code. If S2 goes GREEN the real
integration is a P1 task; if RED, this file is the evidence.

Usage:
    PROJECT_ID=... REGION=... python scripts/spike_s2_vertex_eval.py
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone

# Three canned Query Rewriter items: one good rewrite, one that drops the date
# filter, one that changes the intent outright. If the metric works, the
# scores should not all be identical.
ITEMS = [
    {
        "case": "faithful_rewrite",
        "prompt": (
            "Original user query: 'prior art on solid-state battery separators "
            "filed after 2019'\n"
            "Rewritten search query produced by the subagent:"
        ),
        "response": (
            "solid-state battery separator prior art, filing_date >= 2019-01-01"
        ),
    },
    {
        "case": "drops_date_filter",
        "prompt": (
            "Original user query: 'prior art on solid-state battery separators "
            "filed after 2019'\n"
            "Rewritten search query produced by the subagent:"
        ),
        "response": "solid-state battery separator prior art",
    },
    {
        "case": "intent_drift",
        "prompt": (
            "Original user query: 'prior art on solid-state battery separators "
            "filed after 2019'\n"
            "Rewritten search query produced by the subagent:"
        ),
        "response": "lithium-ion cathode manufacturing cost trends 2024",
    },
]


def main() -> int:
    project = os.environ.get("PROJECT_ID")
    region = os.environ.get("REGION")
    if not project or not region:
        print("PROJECT_ID and REGION must be set", file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc)
    print(f"SPIKE-S2 start {started.isoformat()}  project={project} region={region}")

    try:
        import pandas as pd
        import vertexai
        from vertexai.evaluation import (
            EvalTask,
            PointwiseMetric,
            PointwiseMetricPromptTemplate,
        )

        vertexai.init(project=project, location=region)

        metric = PointwiseMetric(
            metric="query_intent_preservation",
            metric_prompt_template=PointwiseMetricPromptTemplate(
                criteria={
                    "intent_preservation": (
                        "The rewritten query preserves the technical subject "
                        "matter of the original user query."
                    ),
                    "filter_fidelity": (
                        "Any date, assignee, or jurisdiction constraint stated "
                        "in the original query is carried over into the "
                        "rewritten query."
                    ),
                },
                rating_rubric={
                    "1": "Both criteria are satisfied.",
                    "0": "At least one criterion is violated.",
                },
                input_variables=["prompt"],
            ),
        )

        dataset = pd.DataFrame(
            {
                "prompt": [item["prompt"] for item in ITEMS],
                "response": [item["response"] for item in ITEMS],
            }
        )

        task = EvalTask(dataset=dataset, metrics=[metric])
        result = task.evaluate()

        table = result.metrics_table
        print("\n--- raw metrics_table columns ---")
        print(list(table.columns))

        score_col = next(c for c in table.columns if c.endswith("/score"))
        expl_col = next(c for c in table.columns if c.endswith("/explanation"))

        parsed = []
        for item, (_, row) in zip(ITEMS, table.iterrows()):
            parsed.append(
                {
                    "case": item["case"],
                    "response": item["response"],
                    "score": row[score_col],
                    "rationale": str(row[expl_col]),
                }
            )

        print("\n--- parsed into plain dicts (the S2 GREEN criterion) ---")
        print(json.dumps(parsed, indent=2, default=str))
        print("\n--- summary_metrics ---")
        print(json.dumps(dict(result.summary_metrics), indent=2, default=str))

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"\nSPIKE-S2 RESULT: GREEN  ({elapsed:.1f}s, {len(parsed)} items parsed)")
        return 0

    except Exception:
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"\nSPIKE-S2 RESULT: RED  ({elapsed:.1f}s)")
        print("--- exact error ---")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
