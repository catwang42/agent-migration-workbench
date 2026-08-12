"""Gemini on Vertex AI, via the ``google-genai`` SDK.

One call in, one canonical :class:`~amw.traces.schema.Trace` out. Three things
this adapter is careful about:

* **Zero-credential import.** ``google.genai`` is imported inside
  :meth:`GeminiAdapter._ensure_client`, never at module scope, so
  ``import amw.adapters`` works on a laptop with no SDK configured and no ADC
  (CLAUDE.md ground rule 4).
* **Determinism.** ``temperature`` defaults to 0 when the request leaves it
  unset, so an ablation rung re-run gives the same answer for the same input.
* **Honest recording.** A call that fails its retry budget becomes a
  ``status:"error"`` trace, not an exception that kills the batch and not a
  fabricated response (ground rule 1). Structured output that the model
  returned as unparseable text is recorded as *text with no json*, so the
  schema-adherence metric downstream counts a real failure instead of a
  silently repaired one.

This adapter never records itself — ``amw.adapters.resolve`` wraps it in
:class:`~amw.adapters.RecordingAdapter`, which is the only write path and has
no off switch (ground rule 5).

Prompt mapping (the Claude adapters must mirror this so a shadow comparison
compares prompts, not encodings)::

    context_chunks -> one user Content, one Part per chunk   (omitted if empty)
    messages       -> one user Content, one Part per message

No glue text, labels, or separators are invented: whatever the prompt pack
wants the model to see, the prompt pack must say.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Iterable, Sequence

from amw.adapters.base import (
    AdapterError,
    ModelAdapter,
    ModelRequest,
    ToolSpec,
    build_trace,
    error_trace,
    run_with_retries,
)
from amw.config import ConfigError, ModelsConfig, load_all
from amw.traces.schema import LatencyMs, ToolCall, Trace, TraceOutput, Usage

__all__ = ["GeminiAdapter", "MissingEnvError", "DEFAULT_TEMPERATURE", "ACCESS_PATH"]

#: Access path key in config/models.yaml -> provider model ID.
ACCESS_PATH = "vertex"

#: Requests that do not pin a temperature run at 0. Reproducibility is the
#: point: the ablation ladder compares prompts, not sampling luck.
DEFAULT_TEMPERATURE = 0.0


class MissingEnvError(ConfigError):
    """A live call was attempted without the env vars Vertex needs.

    A :class:`~amw.config.ConfigError` on purpose: ``cli.py`` already turns
    those into a one-line message instead of a traceback.
    """


def _require_env(name: str, value: str | None) -> str:
    if value:
        return value
    raise MissingEnvError(
        f"{name} is not set, so no live Gemini call can be made. Set it in .env "
        f"(see .env.example) or run with --mode replay, which needs no "
        f"credentials at all."
    )


def _monotonic_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _import_genai() -> tuple[Any, Any]:
    """``(genai, types)``, imported on first live use only.

    Kept out of module scope so ``import amw.adapters`` — and therefore the
    entire replay path — works with google-genai absent or unconfigured
    (ground rule 4). A missing SDK is reported as a config problem, which
    ``cli.py`` prints as one line instead of a traceback.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:  # pragma: no cover - requirements install it
        raise MissingEnvError(
            f"the google-genai SDK is not importable ({exc}), so no live Gemini "
            "call can be made. `pip install -r requirements.txt`, or run with "
            "--mode replay, which needs no SDK at all."
        ) from exc
    return genai, types


class GeminiAdapter(ModelAdapter):
    """Live Gemini calls against Vertex AI.

    :param models: model registry; defaults to ``load_all().models`` on first
        use. Model IDs are never literals here — they come from
        ``config/models.yaml`` via ``spec(key).id_for("vertex")``.
    :param project: overrides ``$PROJECT_ID``.
    :param location: overrides ``$REGION``.
    :param client: a pre-built ``genai.Client`` (tests inject a stub; nothing
        else should need this).
    """

    name = "gemini"
    mode = "live"

    def __init__(
        self,
        *,
        models: ModelsConfig | None = None,
        project: str | None = None,
        location: str | None = None,
        client: Any | None = None,
        sleep=time.sleep,
        thinking_budget: int | None = None,
    ) -> None:
        self._models = models
        self._project = project
        self._location = location
        self._client = client
        self._sleep = sleep
        #: Cap on thinking tokens, or ``None`` for the model's default.
        #:
        #: ``None`` is the default and is what every eval, ladder and shadow
        #: run uses — setting a budget would change measurements that are
        #: already recorded. It exists for the capped-thinking probe, which
        #: needs to show that the knob is reachable: the 2026-08-12 cost audit
        #: found the candidate billed for 14-61% more output tokens than it
        #: returned characters, at the model's *default* budget, and a reader
        #: is entitled to ask whether that is adjustable.
        self._thinking_budget = thinking_budget

    # -- lazy wiring ------------------------------------------------------

    @property
    def models(self) -> ModelsConfig:
        if self._models is None:
            self._models = load_all().models
        return self._models

    @property
    def project(self) -> str:
        return _require_env("PROJECT_ID", self._project or os.environ.get("PROJECT_ID"))

    @property
    def location(self) -> str:
        return _require_env("REGION", self._location or os.environ.get("REGION"))

    def _ensure_client(self) -> Any:
        """Build the Vertex client on first live call, not at import time."""
        if self._client is None:
            genai, _ = _import_genai()
            self._client = genai.Client(
                vertexai=True, project=self.project, location=self.location
            )
        return self._client

    def model_id(self, model_key: str) -> str:
        spec = self.models.spec(model_key)
        if spec.provider != "google":
            raise ConfigError(
                f"GeminiAdapter cannot serve model {model_key!r}: provider is "
                f"{spec.provider!r}, not 'google'. Mode resolution in "
                f"amw/adapters/__init__.py picks the adapter — do not construct "
                f"one directly."
            )
        return spec.id_for(ACCESS_PATH)

    # -- request translation ----------------------------------------------

    @staticmethod
    def _build_contents(request: ModelRequest, types: Any) -> list[Any]:
        contents: list[Any] = []
        if request.context_chunks:
            contents.append(
                types.Content(
                    role="user",
                    parts=[types.Part(text=chunk) for chunk in request.context_chunks],
                )
            )
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part(text=message) for message in request.messages],
            )
        )
        return contents

    @staticmethod
    def _build_tools(tools: Sequence[ToolSpec], types: Any) -> list[Any]:
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=tool.name,
                        description=tool.description,
                        parameters=tool.parameters or None,
                    )
                    for tool in tools
                ]
            )
        ]

    def _build_config(self, request: ModelRequest, types: Any) -> Any:
        if request.tools and request.response_schema is not None:
            # Gemini rejects response_mime_type=application/json alongside
            # function declarations. Failing here names the real problem
            # instead of surfacing an opaque 400 mid-eval.
            raise ConfigError(
                f"request for subagent {request.subagent!r} sets both tools and "
                "response_schema; Gemini accepts one structured-output mechanism "
                "per call. Pick a tool declaration or a response schema."
            )

        kwargs: dict[str, Any] = {
            "system_instruction": request.system_prompt or None,
            "temperature": (
                DEFAULT_TEMPERATURE
                if request.temperature is None
                else request.temperature
            ),
        }
        if request.max_output_tokens is not None:
            kwargs["max_output_tokens"] = request.max_output_tokens
        if request.tools:
            kwargs["tools"] = self._build_tools(request.tools, types)
        if request.response_schema is not None:
            kwargs["response_mime_type"] = "application/json"
            kwargs["response_schema"] = request.response_schema
        if self._thinking_budget is not None:
            # include_thoughts stays False: the probe is about what the budget
            # does to the bill and the clock, and streaming thought text back
            # would put reasoning traces into a recorded corpus that gets shown
            # to a customer.
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=self._thinking_budget, include_thoughts=False
            )
        return types.GenerateContentConfig(**kwargs)

    # -- response assembly -------------------------------------------------

    @staticmethod
    def _parts(chunk: Any) -> Iterable[Any]:
        candidates = getattr(chunk, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            for part in (getattr(content, "parts", None) or []):
                yield part

    @staticmethod
    def _usage(raw: Any) -> Usage:
        """Map Vertex usage onto the canonical shape.

        Two conventions worth knowing downstream: Gemini's
        ``prompt_token_count`` *includes* cached tokens, and thinking tokens
        are billed as output, so they are folded into ``output_tokens``.
        """
        if raw is None:
            return Usage()

        def count(field: str) -> int:
            return int(getattr(raw, field, None) or 0)

        return Usage(
            input_tokens=count("prompt_token_count"),
            output_tokens=count("candidates_token_count")
            + count("thoughts_token_count"),
            cached_tokens=count("cached_content_token_count"),
        )

    def _consume_stream(self, stream: Iterable[Any], started: float) -> dict[str, Any]:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage_raw: Any = None
        ttft_ms: int | None = None

        for chunk in stream:
            if ttft_ms is None:
                ttft_ms = _monotonic_ms(started)
            for part in self._parts(chunk):
                if getattr(part, "thought", None):
                    continue  # thinking traces are not the answer
                text = getattr(part, "text", None)
                if text:
                    text_parts.append(text)
                call = getattr(part, "function_call", None)
                if call is not None and getattr(call, "name", None):
                    tool_calls.append(
                        ToolCall(name=call.name, args=dict(getattr(call, "args", None) or {}))
                    )
            chunk_usage = getattr(chunk, "usage_metadata", None)
            if chunk_usage is not None:
                usage_raw = chunk_usage

        return {
            "text": "".join(text_parts),
            "tool_calls": tool_calls,
            "usage": self._usage(usage_raw),
            "ttft_ms": ttft_ms,
        }

    @staticmethod
    def _build_output(
        request: ModelRequest, text: str, tool_calls: Sequence[ToolCall]
    ) -> TraceOutput:
        if tool_calls:
            # A tool call *is* the structured output. One call: its args are the
            # payload (this is the shape in tests/fixtures/traces). Several:
            # keep them all, named, rather than silently dropping any.
            if len(tool_calls) == 1:
                payload: Any = tool_calls[0].args
            else:
                payload = [{"name": c.name, "args": c.args} for c in tool_calls]
            return TraceOutput(text=text or None, json=payload)

        if request.response_schema is not None:
            try:
                # ValueError covers both a JSONDecodeError and pydantic
                # rejecting a valid-but-unusable payload (a bare scalar).
                return TraceOutput(json=json.loads(text))
            except (ValueError, TypeError):
                # A schema was demanded and the model did not honour it. Record
                # exactly that: text present, json absent. Repairing it here
                # would hide a real quality signal.
                return TraceOutput(text=text or None)

        return TraceOutput(text=text or None)

    # -- the contract -----------------------------------------------------

    def complete(self, request: ModelRequest) -> Trace:
        # Everything that can fail on the request itself fails before the
        # client is built, so a malformed request reports itself instead of
        # first demanding credentials it does not need.
        _, types = _import_genai()
        model_id = self.model_id(request.model)
        config = self._build_config(request, types)
        contents = self._build_contents(request, types)
        client = self._ensure_client()

        # Latency is measured per attempt, not across the retry budget: a p95
        # gate should read the latency of the call that produced the answer,
        # not that call plus the backoff of two dead ones.
        first_started = time.monotonic()
        attempt: dict[str, float] = {"started": first_started}

        def call() -> dict[str, Any]:
            attempt["started"] = time.monotonic()
            stream = client.models.generate_content_stream(
                model=model_id, contents=contents, config=config
            )
            return self._consume_stream(stream, attempt["started"])

        try:
            result = run_with_retries(call, sleep=self._sleep)
        except Exception as exc:  # noqa: BLE001 - budget spent; record, don't crash
            return error_trace(
                request,
                AdapterError(f"gemini call failed after retries: {exc}"),
                total_ms=_monotonic_ms(first_started),
            )

        return build_trace(
            request,
            output=self._build_output(request, result["text"], result["tool_calls"]),
            usage=result["usage"],
            latency_ms=LatencyMs(
                ttft=result["ttft_ms"], total=_monotonic_ms(attempt["started"])
            ),
            tool_calls=result["tool_calls"],
        )
