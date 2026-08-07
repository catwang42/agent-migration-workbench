"""Claude via the direct Anthropic API (``CLAUDE_PATH=anthropic``).

This module also holds :class:`_ClaudeMessagesAdapter`, the shared Messages-API
implementation that :mod:`amw.adapters.claude_vertex` subclasses.

Why the shared base lives here rather than in a third module: T04's file
footprint is exactly ``claude_vertex.py`` + ``claude_anthropic.py`` + their
test. Duplicating ~150 lines of request translation across the two files is the
worse option — the two paths must produce *identical* traces for the same
request, and a fix applied to one copy and not the other is precisely the kind
of silent drift that would invalidate the Claude baseline the whole migration is
measured against. The two paths differ only in how the client object is built
and which ``ids:`` key in config/models.yaml resolves the model, so subclassing
is honest here. If a later task wants a neutral ``_claude_common.py``, moving
the base class is a pure cut-and-paste.

Ground rules honoured (CLAUDE.md):
* #1 no fabricated results — a failed call yields a ``status:"error"`` trace
  with empty output; nothing is ever synthesized.
* #4 zero credentials to import — the ``anthropic`` SDK is imported inside the
  constructor, never at module import time.
* Retry x2 with backoff, then record an error trace and let the batch continue.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from amw.adapters.base import (
    AdapterError,
    ModelAdapter,
    ModelRequest,
    build_trace,
    error_trace,
    run_with_retries,
)
from amw.config import ConfigError, ModelsConfig
from amw.traces.schema import LatencyMs, ToolCall, Trace, TraceOutput, Usage

__all__ = [
    "ClaudeAnthropicAdapter",
    "MissingCredentialsError",
    "DEFAULT_MAX_OUTPUT_TOKENS",
]


class MissingCredentialsError(ConfigError, AdapterError):
    """A live Claude call was attempted without the credentials it needs.

    Inherits both deliberately. It is a *setup* problem, so it is a
    :class:`~amw.config.ConfigError` — which is what ``resolve()`` documents,
    what ``cli.py`` prints as one friendly line instead of a traceback, and
    what the Gemini lane already raises for the same situation
    (:class:`amw.adapters.gemini.MissingEnvError`). It stays an
    :class:`~amw.adapters.base.AdapterError` too, so ``except AdapterError``
    around a call site still catches it. Without the first base a missing
    ``PROJECT_ID`` would escape the CLI's handler as a bare ``RuntimeError``
    traceback; without the second, the two providers would need different
    handling for the identical failure.
    """


#: The Messages API *requires* ``max_tokens``; there is no provider default to
#: inherit. Requests that do not set ``max_output_tokens`` get this. Chosen to
#: comfortably cover the three Act 1 subagents (rewritten queries, chunk
#: summaries, extracted feature JSON) without truncating a long summary.
DEFAULT_MAX_OUTPUT_TOKENS = 4096

#: Stream event types that mean "the model has started producing output". The
#: first one of these is time-to-first-token.
_FIRST_CONTENT_EVENTS = frozenset({"content_block_start", "content_block_delta"})


def _ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


class _ClaudeMessagesAdapter(ModelAdapter):
    """Shared Anthropic Messages API implementation for both Claude paths.

    Subclasses supply :attr:`ACCESS_PATH` (the ``ids:`` key in
    config/models.yaml) and :meth:`_build_client`.
    """

    #: Key under ``models.<key>.ids`` in config/models.yaml.
    ACCESS_PATH: str = ""

    mode = "live"

    def __init__(
        self,
        models: ModelsConfig,
        *,
        client: Any | None = None,
        default_max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        :param models: the loaded ``config/models.yaml`` (``AppConfig.models``).
            Model IDs are resolved through it; none are hardcoded here.
        :param client: an already-built Anthropic client. Injecting one skips
            credential resolution entirely, which is how the offline tests run.
        :param default_max_output_tokens: used when a request does not set
            ``max_output_tokens``. ``max_tokens`` is mandatory on this API.
        :param sleep: retry backoff sleep, injectable so tests do not wait.
        """
        self.models = models
        self.default_max_output_tokens = default_max_output_tokens
        self._sleep = sleep
        self._client = client if client is not None else self._build_client()

    # -- transport ---------------------------------------------------------

    def _build_client(self) -> Any:  # pragma: no cover - subclass responsibility
        raise NotImplementedError

    # -- request translation ----------------------------------------------

    def _resolve_model_id(self, request: ModelRequest) -> str:
        """Logical key -> provider model ID. Raises ConfigError on a bad key.

        A bad model key is a programmer/config error, so it is resolved *before*
        the retry loop and allowed to propagate (base.ModelAdapter.complete
        reserves exceptions for exactly this).
        """
        return self.models.spec(request.model).id_for(self.ACCESS_PATH)

    def _request_kwargs(self, request: ModelRequest) -> dict[str, Any]:
        """Build the Messages API kwargs for one request.

        PROMPT PASS-THROUGH IS LOAD-BEARING. The Claude system prompt and user
        turns are the *baseline being measured*: they go on the wire byte for
        byte. Do not reformat, strip, normalize whitespace, wrap in tags, inject
        scaffolding, or "improve" anything here. The prompts are XML-style and
        stay that way. Any mutation silently changes what the migration is
        compared against, which is a measurement-validity bug, not a style
        choice.
        """
        # This mapping mirrors amw.adapters.gemini._build_contents exactly, and
        # must keep mirroring it:
        #
        #     context_chunks -> one user turn, one text block per chunk (omitted
        #                       if empty)
        #     messages       -> one user turn, one text block per message
        #
        # Gemini emits one Content with N Parts; Claude emits one message with N
        # text blocks. Same grouping, same order, no glue text, labels or
        # separators invented on either side. If the two encodings diverge the
        # shadow comparison measures encodings rather than prompts, so
        # test_adapter_parity.py asserts the correspondence.
        def _turn(texts: list[str]) -> dict[str, Any]:
            return {
                "role": "user",
                "content": [{"type": "text", "text": text} for text in texts],
            }

        messages: list[dict[str, Any]] = []
        if request.context_chunks:
            messages.append(_turn(list(request.context_chunks)))
        messages.append(_turn(list(request.messages)))

        kwargs: dict[str, Any] = {
            "model": self._resolve_model_id(request),
            # Required by the Messages API; there is no provider-side default.
            "max_tokens": request.max_output_tokens or self.default_max_output_tokens,
            "system": request.system_prompt,  # verbatim, see docstring
            "messages": messages,
        }

        # DELIBERATELY ABSENT: temperature / top_p / top_k. Current-generation
        # Claude models (Opus 5, Sonnet 5, Opus 4.8/4.7, Fable 5) reject all
        # three with a 400. ModelRequest.temperature exists for Gemini's
        # determinism setting; forwarding it here would fail every call.
        #
        # Also deliberately absent: `thinking`. The baseline is a plain call;
        # enabling extended thinking would change the thing being measured.
        # (On these models the only accepted form is {"type": "adaptive"} —
        # budget_tokens is a 400.)

        if request.tools:
            kwargs["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters
                    or {"type": "object", "properties": {}},
                }
                for tool in request.tools
            ]

        if request.response_schema is not None:
            # Structured output via output_config.format rather than a forced
            # synthetic tool. Reason: a forced tool would have to be added to
            # `tools`, but Trace.tools_offered (and therefore input_sha and the
            # replay key) is derived from request.tools by build_trace. An
            # injected tool would either be missing from the trace — making the
            # record disagree with the call, a provenance-integrity problem —
            # or change the replay key. output_config leaves `tools` alone, so
            # a request may carry real tools and a response schema at once.
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": request.response_schema}
            }

        return kwargs

    # -- response translation ---------------------------------------------

    @staticmethod
    def _parse_message(
        message: Any, *, expect_json: bool
    ) -> tuple[TraceOutput, list[ToolCall]]:
        """Provider message -> (TraceOutput, tool calls). Never invents data."""
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in getattr(message, "content", None) or []:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        name=getattr(block, "name", ""),
                        args=dict(getattr(block, "input", None) or {}),
                    )
                )
            # thinking / redacted_thinking blocks are intentionally dropped:
            # they are not part of the answer being scored.

        text = "".join(text_parts)
        parsed: Any | None = None

        if expect_json and text:
            try:
                candidate = json.loads(text)
            except (ValueError, TypeError):
                # Do not fabricate structure. Keep the raw text; downstream eval
                # can score it as a schema miss.
                candidate = None
            if isinstance(candidate, (dict, list)):
                parsed = candidate

        if parsed is None and len(tool_calls) == 1:
            # Convention from the canonical trace example (traces/schema.py
            # docstring): when a single tool call carries the structured
            # payload, output.json mirrors its arguments so scoring code has one
            # place to look. Copied, not synthesized.
            parsed = dict(tool_calls[0].args)

        return TraceOutput(text=text or None, json_=parsed), tool_calls

    @staticmethod
    def _parse_usage(message: Any) -> Usage:
        usage = getattr(message, "usage", None)
        if usage is None:
            return Usage()
        return Usage(
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            # Cache *reads* are the discounted tokens that matter for the cost
            # model. cache_creation_input_tokens has no slot in the canonical
            # Usage schema (extra="forbid"), so it is not recorded.
            cached_tokens=int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        )

    # -- the call ----------------------------------------------------------

    def _attempt(self, kwargs: dict[str, Any]) -> tuple[Any, int | None, int]:
        """One streamed call. Returns (final message, ttft_ms, total_ms).

        Streaming serves two purposes: it is the only way to observe
        time-to-first-token, and it avoids request timeouts on long outputs.
        """
        started = time.perf_counter()
        ttft_ms: int | None = None
        with self._client.messages.stream(**kwargs) as stream:
            for event in stream:
                event_type = getattr(event, "type", None)
                if ttft_ms is None and event_type in _FIRST_CONTENT_EVENTS:
                    ttft_ms = _ms_since(started)
            message = stream.get_final_message()
        return message, ttft_ms, _ms_since(started)

    def complete(self, request: ModelRequest) -> Trace:
        kwargs = self._request_kwargs(request)  # may raise ConfigError: intended
        started = time.perf_counter()
        try:
            message, ttft_ms, total_ms = run_with_retries(
                lambda: self._attempt(kwargs), sleep=self._sleep
            )
        except Exception as exc:  # retry budget spent
            # Never raise for an ordinary API failure: record it and let the
            # batch carry on (CLAUDE.md conventions).
            return error_trace(request, exc, total_ms=_ms_since(started))

        output, tool_calls = self._parse_message(
            message, expect_json=request.response_schema is not None
        )
        return build_trace(
            request,
            output=output,
            usage=self._parse_usage(message),
            latency_ms=LatencyMs(ttft=ttft_ms, total=total_ms),
            tool_calls=tool_calls,
        )


class ClaudeAnthropicAdapter(_ClaudeMessagesAdapter):
    """Claude over the direct Anthropic API (``CLAUDE_PATH=anthropic``).

    Auth is an Anthropic API key (``ANTHROPIC_API_KEY``). The Vertex path uses
    GCP ADC instead and needs no key — see
    :class:`amw.adapters.claude_vertex.ClaudeVertexAdapter`.
    """

    name = "claude_anthropic"
    ACCESS_PATH = "anthropic"

    def __init__(
        self,
        models: ModelsConfig,
        *,
        api_key: str | None = None,
        client: Any | None = None,
        default_max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """:param api_key: defaults to ``$ANTHROPIC_API_KEY``."""
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or None
        super().__init__(
            models,
            client=client,
            default_max_output_tokens=default_max_output_tokens,
            sleep=sleep,
        )

    def _build_client(self) -> Any:
        if not self.api_key:
            raise MissingCredentialsError(
                "ClaudeAnthropicAdapter needs an Anthropic API key: set "
                "ANTHROPIC_API_KEY in .env or pass api_key=... . To use Claude "
                "on Vertex with GCP Application Default Credentials instead "
                "(no API key), set CLAUDE_PATH=vertex."
            )
        # Imported here, not at module scope: importing this module must work
        # with no SDK extras, no credentials and no network (ground rule 4).
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise AdapterError(
                "the `anthropic` package is required for CLAUDE_PATH=anthropic; "
                "install it with `pip install -r requirements.txt`"
            ) from exc
        return Anthropic(api_key=self.api_key)
