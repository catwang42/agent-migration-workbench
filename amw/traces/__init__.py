"""Canonical trace record + the replay store built on it.

`amw.traces.schema` is the one schema every model call is recorded in — the
replay foundation for Act 1 and the ingestion target for Act 2 (BYOT).
"""

from amw.traces.schema import (
    LatencyMs,
    ToolCall,
    Trace,
    TraceInput,
    TraceOutput,
    Usage,
    append_jsonl,
    compute_input_sha,
    iter_jsonl,
    read_jsonl,
    sha256_text,
    write_jsonl,
)
from amw.traces.store import ReplayMissError, ReplayStore, default_replay_dir

__all__ = [
    "LatencyMs",
    "ReplayMissError",
    "ReplayStore",
    "ToolCall",
    "Trace",
    "TraceInput",
    "TraceOutput",
    "Usage",
    "append_jsonl",
    "compute_input_sha",
    "default_replay_dir",
    "iter_jsonl",
    "read_jsonl",
    "sha256_text",
    "write_jsonl",
]
