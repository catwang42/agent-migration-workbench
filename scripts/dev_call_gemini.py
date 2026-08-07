#!/usr/bin/env python3
"""One live Gemini round trip, then prove it replays identically.

This is the T03 verification a human runs once with credentials, standing in
for `cli.py smoke --mode live --backend gemini` until T09/T16 wire that up. It
exists to check the three things no offline test can:

1. the model ID in ``config/models.yaml`` is real in this project + region,
2. record-on-live actually writes to ``artifacts/replay/``,
3. the recorded trace replays byte-identically — which is the whole premise of
   the offline demo path.

Usage::

    source .venv/bin/activate
    python scripts/dev_call_gemini.py                       # default probe
    python scripts/dev_call_gemini.py --schema              # structured output
    python scripts/dev_call_gemini.py --tool                # tool declaration
    python scripts/dev_call_gemini.py --model gemini-pro

Needs PROJECT_ID, REGION and application-default credentials. It writes a real
trace into the corpus under the subagent name ``dev_smoke``, so demo data stays
clean.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from amw.adapters import resolve  # noqa: E402
from amw.adapters.base import ModelRequest, ToolSpec  # noqa: E402
from amw.config import ConfigError, load_all  # noqa: E402
from amw.traces.store import ReplayMissError, ReplayStore  # noqa: E402

SUBAGENT = "dev_smoke"

DEFAULT_PROMPT = (
    "You rewrite patent-search questions into keyword queries. Answer briefly."
)
DEFAULT_MESSAGE = "Find prior art on solid-state battery separators filed after 2019."

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["queries"],
}


def build_request(args: argparse.Namespace) -> ModelRequest:
    return ModelRequest(
        subagent=SUBAGENT,
        model=args.model,
        system_prompt=args.system,
        messages=[args.message],
        context_chunks=(
            ["US10123456B2 — separator comprising a ceramic-coated polyolefin film."]
            if args.context
            else []
        ),
        tools=(
            [
                ToolSpec(
                    name="emit_query_plan",
                    description="Emit the search plan.",
                    parameters=PLAN_SCHEMA,
                )
            ]
            if args.tool
            else []
        ),
        response_schema=PLAN_SCHEMA if args.schema else None,
        provenance="synthetic",
    )


def show(label: str, trace) -> None:
    print(f"\n--- {label} ---")
    print(f"  trace_id   {trace.trace_id}")
    print(f"  status     {trace.status}" + (f"  ({trace.error})" if trace.error else ""))
    print(f"  key        {trace.key}")
    print(
        f"  usage      in={trace.usage.input_tokens} out={trace.usage.output_tokens} "
        f"cached={trace.usage.cached_tokens}"
    )
    print(f"  latency_ms ttft={trace.latency_ms.ttft} total={trace.latency_ms.total}")
    if trace.tool_calls:
        print(f"  tool_calls {[c.name for c in trace.tool_calls]}")
    body = trace.output.json_ if trace.output.json_ is not None else trace.output.text
    print(f"  output     {str(body)[:400]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="gemini-flash", help="key in models.yaml")
    parser.add_argument("--system", default=DEFAULT_PROMPT)
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument("--context", action="store_true", help="include a context chunk")
    parser.add_argument("--tool", action="store_true", help="offer a tool declaration")
    parser.add_argument(
        "--schema", action="store_true", help="request strict structured output"
    )
    args = parser.parse_args(argv)

    if args.tool and args.schema:
        print(
            "error: --tool and --schema are mutually exclusive; Gemini accepts one "
            "structured-output mechanism per call.",
            file=sys.stderr,
        )
        return 2

    try:
        cfg = load_all()
    except ConfigError as exc:
        print(f"config error:\n{exc}", file=sys.stderr)
        return 2

    request = build_request(args)
    store = ReplayStore()

    # mode="live": resolve() wraps the adapter in RecordingAdapter, so the call
    # is written to artifacts/replay/ with no action from this script.
    try:
        adapter = resolve(args.model, "live", models=cfg.models, store=store)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"calling {args.model} live (recording to {store.root}) ...", flush=True)
    try:
        live = adapter.complete(request)
    except ConfigError as exc:
        # Missing PROJECT_ID / REGION lands here: a one-line message, not a
        # traceback, because this is the script a human runs before a workshop.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"error: live call failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    show("live", live)

    if live.status == "error":
        print("\nlive call returned a status:error trace — recorded, but not usable.")
        return 1

    replayer = resolve(args.model, "replay", models=cfg.models, store=ReplayStore())
    try:
        replayed = replayer.complete(request)
    except ReplayMissError as exc:
        print(f"\nFAIL: record-on-live did not land in the corpus: {exc}", file=sys.stderr)
        return 1

    show("replayed", replayed)

    if replayed.to_jsonl_line() != live.to_jsonl_line():
        print("\nFAIL: replayed trace differs from the recorded one.", file=sys.stderr)
        return 1

    window = replayer.recording_window(SUBAGENT)  # type: ignore[attr-defined]
    print(f"\nOK: recorded and replayed byte-identically. Recording window: {window}")
    print(f"     corpus file: {store.path_for(SUBAGENT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
