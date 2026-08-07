"""Canonical trace schema — the record of one model call.

Everything in this workbench is built on this one record. It is:

* the **replay foundation** — a recorded trace can be served back instead of
  re-calling the model, which is what lets `cli.py e2e --mode replay` run with
  zero credentials;
* the **evidence trail** — every number in a customer report traces back to a
  line of JSONL here, which is what makes "no fabricated results" checkable
  rather than aspirational;
* the **Act 2 foundation** — real customer traces convert *into* this shape, so
  BYOT is converters only, no refactor (act1_build_plan.md, preamble).

Serialization is deliberately canonical: :meth:`Trace.to_jsonl_line` is
byte-stable, so a recorded corpus round-trips unchanged and a schema change
shows up as a diff instead of a silent reinterpretation.

Field shape follows docs/master_plan.md §5.9::

    {"trace_id": "qr-000123", "subagent": "query_rewriter", "provenance": "customer",
     "ts": "2026-08-03T10:22:41Z", "model": "claude-sonnet", "system_prompt_sha": "ab12…",
     "input": {"messages": ["…"], "context_chunks": ["…"]},
     "tools_offered": ["emit_query_plan"],
     "tool_calls": [{"name": "emit_query_plan", "args": {"…": "…"}}],
     "output": {"text": null, "json": {"…": "…"}},
     "usage": {"input_tokens": 812, "output_tokens": 196, "cached_tokens": 0},
     "latency_ms": {"ttft": 410, "total": 890}}
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Provenance",
    "TraceStatus",
    "TraceInput",
    "ToolCall",
    "TraceOutput",
    "Usage",
    "LatencyMs",
    "Trace",
    "sha256_text",
    "compute_input_sha",
    "read_jsonl",
    "write_jsonl",
    "append_jsonl",
]

Provenance = Literal["synthetic", "customer"]
TraceStatus = Literal["ok", "error"]


class _Base(BaseModel):
    # extra="forbid": an unrecognised key in a recorded corpus is a schema
    # drift bug, and we want it at load time, not as a silently dropped field.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def sha256_text(text: str) -> str:
    """Short content hash used for `system_prompt_sha`."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class TraceInput(_Base):
    messages: list[str]
    context_chunks: list[str] = Field(default_factory=list)


class ToolCall(_Base):
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class TraceOutput(_Base):
    text: str | None = None
    # `json` shadows a BaseModel attribute, so the Python name is json_ and the
    # wire name stays `json` via the alias. Always serialize with by_alias=True.
    json_: dict[str, Any] | list[Any] | None = Field(default=None, alias="json")


class Usage(_Base):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class LatencyMs(_Base):
    ttft: int | None = None
    total: int | None = None


def compute_input_sha(
    *,
    system_prompt_sha: str,
    messages: Iterable[str],
    context_chunks: Iterable[str] = (),
    tools_offered: Iterable[str] = (),
) -> str:
    """The replay key's input component.

    Note what is hashed: the system prompt's hash is folded in alongside the
    messages. This is required, not incidental — the ablation ladder replays the
    *same* dataset item against A0…A4, which differ only in the system prompt.
    Hashing the messages alone would collide every rung onto one cache entry and
    replay the wrong output. `tools_offered` is included for the same reason:
    the A2 rung's whole change is offering a strict-schema tool.
    """
    payload = {
        "system_prompt_sha": system_prompt_sha,
        "messages": list(messages),
        "context_chunks": list(context_chunks),
        "tools_offered": sorted(tools_offered),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class Trace(_Base):
    """One model call, recorded."""

    trace_id: str
    subagent: str
    provenance: Provenance
    ts: datetime
    model: str
    system_prompt_sha: str
    input: TraceInput
    tools_offered: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    output: TraceOutput = Field(default_factory=TraceOutput)
    usage: Usage = Field(default_factory=Usage)
    latency_ms: LatencyMs = Field(default_factory=LatencyMs)
    status: TraceStatus = "ok"
    #: Populated only when status == "error"; a failed call is still recorded so
    #: one flaky response cannot silently shrink an eval's denominator.
    error: str | None = None

    @property
    def input_sha(self) -> str:
        return compute_input_sha(
            system_prompt_sha=self.system_prompt_sha,
            messages=self.input.messages,
            context_chunks=self.input.context_chunks,
            tools_offered=self.tools_offered,
        )

    @property
    def key(self) -> tuple[str, str, str]:
        """The replay store key: ``(subagent, model, input_sha)``."""
        return (self.subagent, self.model, self.input_sha)

    def to_jsonl_line(self) -> str:
        """Canonical single-line JSON. Byte-stable across processes."""
        return self.model_dump_json(by_alias=True) + "\n"

    @classmethod
    def from_jsonl_line(cls, line: str) -> "Trace":
        return cls.model_validate_json(line)


# --------------------------------------------------------------------------
# JSONL I/O
# --------------------------------------------------------------------------


def read_jsonl(path: str | Path) -> list[Trace]:
    """Parse a trace file. Blank lines are skipped; bad lines name themselves."""
    path = Path(path)
    traces: list[Trace] = []
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                traces.append(Trace.from_jsonl_line(line))
            except Exception as exc:
                raise ValueError(f"{path}:{lineno}: not a valid trace: {exc}") from exc
    return traces


def iter_jsonl(path: str | Path) -> Iterator[Trace]:
    """Streaming variant of :func:`read_jsonl` for large corpora."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield Trace.from_jsonl_line(line)
            except Exception as exc:
                raise ValueError(f"{path}:{lineno}: not a valid trace: {exc}") from exc


def write_jsonl(path: str | Path, traces: Iterable[Trace]) -> Path:
    """Write a trace file, replacing it. Round-trips byte-stably."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for trace in traces:
            handle.write(trace.to_jsonl_line())
    return path


def append_jsonl(path: str | Path, trace: Trace) -> Path:
    """Append one trace. This is the record-on-live write path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(trace.to_jsonl_line())
    return path
