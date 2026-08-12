"""Adapter selection — the one place modes are resolved.

Callers never construct an adapter and never branch on provider. They ask for a
logical model key from ``config/models.yaml`` and a mode, and get something
that satisfies :class:`~amw.adapters.base.ModelAdapter`::

    from amw.adapters import resolve, AdapterRouter

    adapter = resolve("gemini-flash", mode="hybrid")
    trace = adapter.complete(request)

    router = AdapterRouter(mode="hybrid")     # many models, one store/client
    traces = router.complete_many(requests)

Two project rules are enforced structurally here rather than by convention.

**Modes live in one place (CLAUDE.md conventions).** ``live | replay | hybrid``
is decided by :func:`resolve` and nowhere else. Per-callsite mode checks are
how a "replay" run ends up making one real call from some forgotten branch.

**Record-on-live has no off switch (ground rule 5).** Every live adapter is
wrapped in :class:`RecordingAdapter` on the way out of :func:`resolve`, so
recording is a property of *how adapters are obtained*, not a flag an adapter
could forget to honour or a caller could pass ``False`` to. There is no
parameter to disable it, here or anywhere downstream.

The mode table
--------------

Hybrid is the workshop default: Gemini live (that is the thing under test),
Claude replayed (the baseline was recorded once; re-running it burns money to
re-measure a constant, and drifts the comparison).

===========  ==============================  ============================
mode         provider ``google``              provider ``anthropic``
===========  ==============================  ============================
``replay``   ReplayAdapter                   ReplayAdapter
``hybrid``   Recording(GeminiAdapter)        ReplayAdapter
``live``     Recording(GeminiAdapter)        per ``$CLAUDE_PATH``:
                                             ``vertex`` / ``anthropic`` ->
                                             Recording(Claude…Adapter);
                                             ``replay`` -> ReplayAdapter
===========  ==============================  ============================

Note the provider lookup: hybrid cannot be decided from the mode alone, only
from the mode *and* the requested model's ``provider`` in models.yaml.
"""

from __future__ import annotations

import inspect
import os
from datetime import datetime
from typing import Iterable

from amw.adapters.base import (
    AdapterError,
    ModelAdapter,
    ModelRequest,
    ToolSpec,
    build_trace,
    error_trace,
)
from amw.adapters.gemini import GeminiAdapter
from amw.adapters.replay import ReplayAdapter
from amw.config import ConfigError, ModelsConfig, load_all
from amw.traces.schema import Trace
from amw.traces.store import ReplayMissError, ReplayStore

__all__ = [
    "MODES",
    "CLAUDE_PATHS",
    "DEFAULT_MODE",
    "AdapterError",
    "AdapterRouter",
    "GeminiAdapter",
    "ModelAdapter",
    "ModelRequest",
    "RecordingAdapter",
    "ReplayAdapter",
    "ReplayMissError",
    "ToolSpec",
    "build_trace",
    "error_trace",
    "merge_windows",
    "resolve",
]


def merge_windows(
    windows: Iterable[tuple[datetime, datetime] | None],
) -> tuple[datetime, datetime] | None:
    """Union of ``(earliest, latest)`` spans, ignoring None. None if all None."""
    present = [w for w in windows if w is not None]
    if not present:
        return None
    return (min(w[0] for w in present), max(w[1] for w in present))

MODES: tuple[str, ...] = ("live", "replay", "hybrid")
CLAUDE_PATHS: tuple[str, ...] = ("vertex", "anthropic", "replay")

#: The workshop default: Gemini live, Claude replayed.
DEFAULT_MODE = "hybrid"

#: Module + preferred class name for each Claude access path. Imported lazily
#: so the replay path never needs the `anthropic` SDK installed.
_CLAUDE_ADAPTERS: dict[str, tuple[str, str]] = {
    "vertex": ("amw.adapters.claude_vertex", "ClaudeVertexAdapter"),
    "anthropic": ("amw.adapters.claude_anthropic", "ClaudeAnthropicAdapter"),
}


# --------------------------------------------------------------------------
# record-on-live
# --------------------------------------------------------------------------


class RecordingAdapter(ModelAdapter):
    """Wraps a live adapter so every call lands in ``artifacts/replay/``.

    Deliberately not optional and deliberately not a flag: :func:`resolve`
    applies it to every live adapter, so an adapter cannot forget to record and
    a caller has no way to ask for an unrecorded call (ground rule 5).

    Recording is what turns today's live run into tomorrow's offline demo, and
    error traces are recorded too — a dropped failure silently shrinks an
    eval's denominator and flatters whichever model failed.
    """

    def __init__(self, inner: ModelAdapter, store: ReplayStore | None = None) -> None:
        self.inner = inner
        self.store = store if store is not None else ReplayStore()

    @property
    def name(self) -> str:  # type: ignore[override]
        return self.inner.name

    @property
    def mode(self) -> str:  # type: ignore[override]
        return self.inner.mode

    def complete(self, request: ModelRequest) -> Trace:
        try:
            trace = self.inner.complete(request)
        except ConfigError:
            # Bad model key, missing PROJECT_ID, unreadable config: the request
            # never left the process. Recording it would put a trace that looks
            # like a model failure into the corpus and shrink a later eval's
            # denominator for a reason that has nothing to do with the model.
            raise
        except Exception as exc:
            # A well-behaved adapter returns an error trace instead of raising
            # (see base.ModelAdapter.complete). If one raises anyway, assume the
            # call did go out and record it — under-recording is the worse
            # failure — then let the exception continue on its way.
            self._record(error_trace(request, exc))
            raise
        return self._record(trace)

    def _record(self, trace: Trace) -> Trace:
        self.store.append(trace)
        return trace

    def recording_window(
        self, subagent: str | None = None
    ) -> tuple[datetime, datetime] | None:
        return self.store.recording_window(subagent)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RecordingAdapter inner={self.inner!r} root={self.store.root}>"


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


def _check_mode(mode: str) -> str:
    if mode not in MODES:
        raise ConfigError(f"unknown mode {mode!r}; expected one of {list(MODES)}")
    return mode


def _resolve_claude_path(claude_path: str | None) -> str:
    path = claude_path or os.environ.get("CLAUDE_PATH") or "vertex"
    if path not in CLAUDE_PATHS:
        raise ConfigError(
            f"CLAUDE_PATH={path!r} is not one of {list(CLAUDE_PATHS)} "
            "(see .env.example)"
        )
    return path


def _load_claude_adapter(path: str, models: ModelsConfig) -> ModelAdapter:
    """Import and construct the Claude adapter for an access path.

    Lazy on purpose: the ``anthropic`` SDK must never be needed to run in
    replay mode. Tolerant on purpose too — this lane and the Claude lane land
    independently, so the class is located by name with a fallback scan, and
    the constructor is probed for a ``models`` keyword rather than assumed.
    """
    module_name, class_name = _CLAUDE_ADAPTERS[path]
    try:
        module = __import__(module_name, fromlist=["*"])
    except ImportError as exc:
        raise ConfigError(
            f"CLAUDE_PATH={path!r} needs {module_name}, which could not be "
            f"imported ({exc}). Use --mode replay|hybrid, or set "
            f"CLAUDE_PATH=replay, to run without a Claude backend."
        ) from exc

    cls = getattr(module, class_name, None)
    if cls is None:
        candidates = [
            obj
            for obj in vars(module).values()
            if inspect.isclass(obj)
            and issubclass(obj, ModelAdapter)
            and obj is not ModelAdapter
            and obj.__module__ == module_name
        ]
        if len(candidates) != 1:
            raise ConfigError(
                f"{module_name} does not define {class_name} and does not "
                f"contain exactly one ModelAdapter subclass (found "
                f"{[c.__name__ for c in candidates]})."
            )
        cls = candidates[0]

    try:
        return cls(models=models)
    except TypeError:
        return cls()


def resolve(
    model: str,
    mode: str = DEFAULT_MODE,
    *,
    models: ModelsConfig | None = None,
    store: ReplayStore | None = None,
    claude_path: str | None = None,
) -> ModelAdapter:
    """Return the adapter that should serve ``model`` in ``mode``.

    :param model: logical key from ``config/models.yaml`` (``gemini-flash``,
        ``claude-sonnet``), never a provider model ID.
    :param mode: ``live`` | ``replay`` | ``hybrid``. See the module table.
    :param models: model registry; defaults to ``load_all().models``.
    :param store: replay store for both reading and record-on-live; defaults to
        ``artifacts/replay/``.
    :param claude_path: overrides ``$CLAUDE_PATH``. Only consulted in ``live``
        mode — ``hybrid`` replays Claude by definition.
    :raises ~amw.config.ConfigError: unknown mode, unknown model key, or a
        Claude backend that cannot be imported.

    Every live adapter comes back wrapped in :class:`RecordingAdapter`. That is
    not configurable.
    """
    _check_mode(mode)
    registry = models if models is not None else load_all().models
    spec = registry.spec(model)  # raises ConfigError on an unknown key

    def replay() -> ModelAdapter:
        return ReplayAdapter(store=store)

    def recorded(inner: ModelAdapter) -> ModelAdapter:
        return RecordingAdapter(inner, store=store)

    if mode == "replay":
        return replay()

    if spec.provider == "google":
        # spec.region is None for every model that runs where $REGION points;
        # the adapter falls back to $REGION in that case. Only a model the
        # region does not serve carries an override, and it carries it in the
        # registry rather than at the callsite, so every path that resolves
        # this model gets the same region.
        return recorded(GeminiAdapter(models=registry, location=spec.region))

    if spec.provider == "anthropic":
        if mode == "hybrid":
            # The baseline was recorded once; replaying it keeps the comparison
            # stable and does not re-bill a constant.
            return replay()
        path = _resolve_claude_path(claude_path)
        if path == "replay":
            return replay()
        return recorded(_load_claude_adapter(path, registry))

    raise ConfigError(  # pragma: no cover - ModelSpec.provider is a Literal
        f"model {model!r} has provider {spec.provider!r}, which has no adapter"
    )


# --------------------------------------------------------------------------
# multi-model convenience
# --------------------------------------------------------------------------


class AdapterRouter:
    """One mode, many models: resolves per request and reuses adapters.

    Phase runners call both Claude and Gemini in the same loop. Constructing an
    adapter per call would rebuild the Vertex client every time and (worse)
    tempt each callsite into its own mode check; the router keeps a single
    :class:`~amw.traces.store.ReplayStore` and one adapter per model key.
    """

    def __init__(
        self,
        mode: str = DEFAULT_MODE,
        *,
        models: ModelsConfig | None = None,
        store: ReplayStore | None = None,
        claude_path: str | None = None,
    ) -> None:
        self.mode = _check_mode(mode)
        self.models = models if models is not None else load_all().models
        self.store = store if store is not None else ReplayStore()
        self.claude_path = claude_path
        self._adapters: dict[str, ModelAdapter] = {}

    def for_model(self, model: str) -> ModelAdapter:
        if model not in self._adapters:
            self._adapters[model] = resolve(
                model,
                self.mode,
                models=self.models,
                store=self.store,
                claude_path=self.claude_path,
            )
        return self._adapters[model]

    def complete(self, request: ModelRequest) -> Trace:
        return self.for_model(request.model).complete(request)

    def complete_many(self, requests: Iterable[ModelRequest]) -> list[Trace]:
        return [self.complete(request) for request in requests]

    def recording_window(
        self, subagent: str | None = None
    ) -> tuple[datetime, datetime] | None:
        """Recording span of the corpus, for the on-screen replay label."""
        return self.store.recording_window(subagent)

    def served_window(self) -> tuple[datetime, datetime] | None:
        """Span of the traces this router actually replayed, across models.

        In `hybrid` only some adapters replay, so this covers the replayed
        half and the live half is dated by the run itself.
        """
        return merge_windows(
            getattr(adapter, "served_window", lambda: None)()
            for adapter in self._adapters.values()
        )

    def describe(self) -> dict[str, str]:
        """``{model_key: "live via gemini" | "replay"}`` for report footers.

        Resolves every model in the registry, so a reader can see at a glance
        which numbers came from a fresh call and which were replayed
        (ground rule 1). A backend that cannot be constructed — no SDK, no
        API key — is reported as unavailable: this is a footer helper, and it
        must never be the thing that kills a report.
        """
        summary: dict[str, str] = {}
        for key in sorted(self.models.models):
            try:
                adapter = self.for_model(key)
            except Exception as exc:  # noqa: BLE001 - see docstring
                summary[key] = f"unavailable: {type(exc).__name__}: {exc}"
                continue
            summary[key] = f"{adapter.mode} via {adapter.name}"
        return summary
