#!/usr/bin/env python3
"""SPIKE-S3 — Vertex AI Prompt Optimizer (VAIPO) hello-world.

Timeboxed probe, per TASKS.md T05: one optimization iteration on a toy
instruction with a 5-item eval set; retrieve the candidate prompt.

  GREEN = a candidate prompt comes back from a real service call.
  RED   = exact error captured into SPIKES.md; the ablation ladder then tops
          out at A4 hand-tuned and we reference VAIPO as the automation path
          WITHOUT showing numbers it did not produce (act1_build_plan.md §3).

Which VAIPO surface this proves matters, so be precise about it:

  * `client.prompt_optimizer.optimize_prompt(...)` — SYNCHRONOUS instruction
    optimizer. Takes a prompt plus a few-shot examples dataframe, returns a
    suggested prompt in seconds. THIS is what the spike exercises, because it
    is what the task card describes (toy instruction + small eval set -> a
    candidate) and it fits a 90-minute timebox.
  * `client.prompt_optimizer.optimize(method="VAPO", ...)` — the data-driven
    optimizer that runs as a long-lived Vertex CustomJob against a GCS bucket.
    NOT exercised here. If rung A4' ever needs the full data-driven loop, that
    is a separate go/no-go.

Usage:
    PROJECT_ID=... REGION=... python scripts/spike_s3_vaipo.py
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime, timezone

# Toy instruction, deliberately underspecified — a real optimizer should have
# something to improve.
TOY_INSTRUCTION = (
    "Rewrite the user's patent search question into a search query."
)

# 5-item eval set: question -> the target rewrite we want. Patents domain, to
# match the Query Rewriter subagent.
EXAMPLES = [
    (
        "prior art on solid-state battery separators filed after 2019",
        "solid-state battery separator prior art, filing_date >= 2019-01-01",
    ),
    (
        "who owns the patents on CRISPR base editing",
        "CRISPR base editing, field:assignee, group_by:assignee",
    ),
    (
        "show me Samsung foldable display patents from the last three years",
        "foldable display, assignee:Samsung, filing_date >= 2023-01-01",
    ),
    (
        "any European filings about perovskite solar cell encapsulation",
        "perovskite solar cell encapsulation, jurisdiction:EP",
    ),
    (
        "what did Google file on transformer attention before 2020",
        "transformer attention mechanism, assignee:Google, "
        "filing_date < 2020-01-01",
    ),
]


def main() -> int:
    project = os.environ.get("PROJECT_ID")
    region = os.environ.get("REGION")
    if not project or not region:
        print("PROJECT_ID and REGION must be set", file=sys.stderr)
        return 2

    started = datetime.now(timezone.utc)
    print(f"SPIKE-S3 start {started.isoformat()}  project={project} region={region}")

    try:
        import pandas as pd
        import vertexai
        from google import genai
        from google.genai import types as gtypes
        from vertexai._genai.types import common as vtypes

        print(f"eval set: {len(EXAMPLES)} items")
        print(f"toy instruction: {TOY_INSTRUCTION!r}\n")

        # The optimizer needs `model_response` — what the CURRENT, unoptimized
        # instruction actually produces. Generate those with real calls rather
        # than hand-writing plausible-looking outputs: an invented failure
        # signal would make the candidate prompt meaningless (ground rule 1).
        gclient = genai.Client(vertexai=True, project=project, location=region)
        model_responses = []
        for question, _ in EXAMPLES:
            reply = gclient.models.generate_content(
                model="gemini-2.5-flash",
                contents=question,
                config=gtypes.GenerateContentConfig(
                    system_instruction=TOY_INSTRUCTION, temperature=0
                ),
            )
            model_responses.append((reply.text or "").strip())
            print(f"  baseline out: {model_responses[-1][:90]}")

        examples = pd.DataFrame(
            {
                "prompt": [q for q, _ in EXAMPLES],
                "model_response": model_responses,
                "target_response": [a for _, a in EXAMPLES],
            }
        )

        client = vertexai.Client(project=project, location=region)
        response = client.prompts.optimize(
            prompt=TOY_INSTRUCTION,
            config=vtypes.OptimizeConfig(
                optimization_target=(
                    vtypes.OptimizeTarget.OPTIMIZATION_TARGET_FEW_SHOT_TARGET_RESPONSE
                ),
                examples_dataframe=examples,
            ),
        )

        parsed = getattr(response, "parsed_response", None)
        suggested = getattr(parsed, "suggested_prompt", None) if parsed else None

        print("--- parsed_response fields ---")
        if parsed is not None:
            print(list(getattr(parsed, "model_fields", {}).keys()) or dir(parsed))

        print("\n--- CANDIDATE PROMPT (the S3 GREEN criterion) ---")
        print(suggested if suggested else "(none returned)")

        if parsed is not None:
            extras = {
                k: v
                for k, v in dict(parsed).items()
                if k != "suggested_prompt" and v
            }
            if extras:
                print("\n--- other parsed fields ---")
                print(json.dumps(extras, indent=2, default=str)[:3000])

        if not suggested:
            print("\n--- raw_text_response (truncated) ---")
            print(str(getattr(response, "raw_text_response", ""))[:2000])

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if suggested:
            print(f"\nSPIKE-S3 RESULT: GREEN  ({elapsed:.1f}s, candidate retrieved)")
            return 0
        print(f"\nSPIKE-S3 RESULT: RED  ({elapsed:.1f}s, no candidate prompt returned)")
        return 1

    except Exception:
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"\nSPIKE-S3 RESULT: RED  ({elapsed:.1f}s)")
        print("--- exact error ---")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
