"""The adapter contract: one call in, one canonical :class:`Trace` out.

Every backend — Gemini, Claude-on-Vertex, Claude-direct, replay — implements
:class:`ModelAdapter`. Callers never branch on provider; they build a
:class:`ModelRequest`, call ``.complete()``, and get a ``Trace``.

This module is the seam between lanes, so it is deliberately small and stable:
request shape, trace construction, and the shared retry policy. Mode resolution
(``live | replay | hybrid``) and record-on-live wrapping live in
``amw/adapters/__init__.py`` — one place, never per-callsite (CLAUDE.md
conventions).
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from amw.traces.schema import (
    LatencyMs,
    Provenance,
    ToolCall,
    Trace,
    TraceInput,
    TraceOutput,
    Usage,
    compute_input_sha,
    sha256_text,
)

__all__ = [
    "ToolSpec",
    "ModelRequest",
    "ModelAdapter",
    "AdapterError",
    "build_trace",
    "error_trace",
    "new_trace_id",
    "run_with_retries",
    "RETRY_ATTEMPTS",
]

#: One initial attempt plus two retries (CLAUDE.md: "retry x2 with backoff").
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY_S = 1.0

T = TypeVar("T")


class AdapterError(RuntimeError):
    """A model call that failed after the retry budget was spent."""


def new_trace_id(subagent: str) -> str:
    return f"{subagent}-{uuid.uuid4().hex[:12]}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class ToolSpec(BaseModel):
    """A tool offered to the model.

    ``parameters`` is a JSON Schema / OpenAPI-3 subset object. Both providers
    accept that shape; each adapter translates it to its own wire format.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    parameters: dict[str, Any] = Field(default_factory=dict)


class ModelRequest(BaseModel):
    """Everything needed to make one call, provider-agnostic."""

    model_config = ConfigDict(extra="forbid")

    subagent: str
    #: Logical key from config/models.yaml (e.g. "gemini-flash"), never a
    #: provider model ID. Adapters resolve it through ModelsConfig.
    model: str
    system_prompt: str
    messages: list[str]
    context_chunks: list[str] = Field(default_factory=list)
    tools: list[ToolSpec] = Field(default_factory=list)
    #: Strict structured-output schema, when the prompt pack asks for one.
    response_schema: dict[str, Any] | None = None
    max_output_tokens: int | None = None
    #: Gemini runs at 0 for determinism. NOTE: current-generation Claude models
    #: reject temperature/top_p/top_k with a 400 — the Claude adapters must not
    #: forward this field.
    temperature: float | None = None
    provenance: Provenance = "synthetic"
    #: Item identity from the dataset; used for the trace_id when present.
    item_id: str | None = None

    @property
    def system_prompt_sha(self) -> str:
        return sha256_text(self.system_prompt)

    @property
    def tools_offered(self) -> list[str]:
        return sorted(tool.name for tool in self.tools)

    @property
    def input_sha(self) -> str:
        return compute_input_sha(
            system_prompt_sha=self.system_prompt_sha,
            messages=self.messages,
            context_chunks=self.context_chunks,
            tools_offered=self.tools_offered,
        )

    @property
    def replay_key(self) -> tuple[str, str, str]:
        """``(subagent, model, input_sha)`` — the ReplayStore key."""
        return (self.subagent, self.model, self.input_sha)

    def trace_id(self) -> str:
        return self.item_id or new_trace_id(self.subagent)


def build_trace(
    request: ModelRequest,
    *,
    output: TraceOutput,
    usage: Usage | None = None,
    latency_ms: LatencyMs | None = None,
    tool_calls: Sequence[ToolCall] = (),
    status: str = "ok",
    error: str | None = None,
    ts: datetime | None = None,
) -> Trace:
    """Assemble a canonical trace from a request plus a provider response.

    Adapters must go through this rather than constructing Trace directly, so
    the replay key is always derived the same way on the record and read paths.
    """
    return Trace(
        trace_id=request.trace_id(),
        subagent=request.subagent,
        provenance=request.provenance,
        ts=ts or utc_now(),
        model=request.model,
        system_prompt_sha=request.system_prompt_sha,
        input=TraceInput(
            messages=list(request.messages),
            context_chunks=list(request.context_chunks),
        ),
        tools_offered=request.tools_offered,
        tool_calls=list(tool_calls),
        output=output,
        usage=usage or Usage(),
        latency_ms=latency_ms or LatencyMs(),
        status=status,  # type: ignore[arg-type]
        error=error,
    )


def error_trace(
    request: ModelRequest, exc: BaseException, *, total_ms: int | None = None
) -> Trace:
    """Record a failed call instead of dropping it.

    A dropped failure silently shrinks an eval's denominator, which quietly
    flatters whichever model failed. Recording it keeps the batch honest.
    """
    return build_trace(
        request,
        output=TraceOutput(),
        latency_ms=LatencyMs(total=total_ms),
        status="error",
        error=f"{type(exc).__name__}: {exc}",
    )


def run_with_retries(
    call: Callable[[], T],
    *,
    attempts: int = RETRY_ATTEMPTS,
    base_delay: float = RETRY_BASE_DELAY_S,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``call``, retrying with exponential backoff. Re-raises the last error.

    Callers turn that final exception into a ``status:"error"`` trace and carry
    on with the batch — one flaky call must not kill an eval run.
    """
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return call()
        except retry_on as exc:  # noqa: PERF203 - retry loop
            last = exc
            if attempt == attempts - 1:
                break
            sleep(base_delay * (2**attempt))
    assert last is not None
    raise last


class ModelAdapter(ABC):
    """One backend. Implementations are constructed by ``amw.adapters.resolve``."""

    #: "gemini" | "claude_vertex" | "claude_anthropic" | "replay"
    name: str = "adapter"
    #: "live" or "replay". Live adapters get wrapped so every call is recorded.
    mode: str = "live"

    @abstractmethod
    def complete(self, request: ModelRequest) -> Trace:
        """Execute one call and return a canonical trace.

        Must not raise for an ordinary model/API failure: exhaust the retry
        budget, then return :func:`error_trace`. Reserve exceptions for
        programmer errors (bad config, unknown model key).
        """

    def complete_many(self, requests: Iterable[ModelRequest]) -> list[Trace]:
        return [self.complete(request) for request in requests]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name} mode={self.mode}>"
