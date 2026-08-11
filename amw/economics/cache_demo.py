"""Context caching, measured rather than modelled: one shared-preamble pair.

``amw.economics.cache_breakeven`` answers "how many calls a day before caching
pays" from ``config/pricing.yaml``. It is a model, and it has one free
parameter a customer will immediately push on: **how many tokens actually get
cached**. Today that number is whatever the operator typed into
``--cache-preamble-tokens``.

This module replaces the typing with a measurement. It makes two real calls
that share a preamble, reads ``cached_content_token_count`` out of the second
one's usage metadata, and hands *that* number — the service's own count, not
an estimate, not a tokenizer approximation — to
:func:`~amw.economics.cache_breakeven.breakeven_curve`. The workshop version of
the claim goes from "if your preamble is about this big" to "your preamble is
this big, here is the count the API returned, here is where it breaks even".

Nothing here has been run. :data:`CacheDemoResult.status` starts at
``not_run``, every measured field is ``None``, and the breakeven overlay is
empty until there is a real token count to overlay. A cache demo that has not
happened reports that it has not happened; it does not report zero saved
tokens, which would read as a measurement of no benefit (ground rule 1).

The pair
--------

======  ========================================================================
cold    system preamble + shared chunks + a per-call suffix, sent inline, no
        cache. Establishes what the preamble costs at the fresh input rate and
        confirms ``cached_tokens == 0``.
warm    the identical preamble served from an explicit cache, plus a
        *different* suffix. ``cached_content_token_count`` on this call is the
        measurement everything else hangs off.
======  ========================================================================

The suffixes differ on purpose. Two identical calls would be a demo of response
caching, which is not what is being sold, and the second one's numbers would
prove nothing about a preamble shared across *different* requests — which is
the actual RAG shape.

Explicit, not implicit
----------------------

Gemini 2.5 also caches implicitly, and an implicit hit is cheaper to
demonstrate: send the same prefix twice and watch the count appear. It is the
wrong demo for this deck. Implicit caching is best-effort, gives no TTL to
reason about, and cannot be reconciled with a breakeven model whose whole
structure is *write cost + rent for a TTL window*. An explicit cache has a
create call whose cost is visible, a TTL the operator chose, and a token count
the service commits to — the three inputs
:func:`~amw.economics.cache_breakeven.cache_breakeven` needs. So this module
creates a cache, uses it, and deletes it.

How the two calls stay comparable
----------------------------------

Both go through :class:`~amw.adapters.gemini.GeminiAdapter` — the warm one via
:class:`CachedPreambleAdapter`, a subclass that changes exactly two things:
the wire config gains ``cached_content`` and drops the
``system_instruction``/``tools`` that now live in the cache, and the wire
contents drop the shared chunks for the same reason. The
:class:`~amw.adapters.base.ModelRequest` still carries the whole logical input,
so the recorded trace says what the model saw rather than what happened to go
over the wire on this particular billing arrangement. Everything else —
retries, streaming, usage mapping, the ``status:"error"`` path — is inherited,
because a demo that reimplements the adapter is a demo of the reimplementation.

Both calls are recorded to ``artifacts/replay/`` through
:meth:`~amw.traces.store.ReplayStore.append`, unconditionally and with no flag
to turn it off (ground rule 5).

Prices
------

Not one appears here. The overlay is
:func:`~amw.economics.cache_breakeven.breakeven_curve`, which fetches every
rate through :meth:`~amw.config.PricingConfig.rate` and refuses on a ``VERIFY``
placeholder. With today's unverified table a completed demo reports a real
token count and an explicitly *not computable* breakeven — which is the honest
pair of statements, and the one that gets fixed by running
``scripts/refresh_pricing.py``, not by editing this file.

Import is credential-free and SDK-free: every provider import sits inside a
function body (ground rule 4).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from amw.adapters.base import ModelRequest
from amw.adapters.gemini import GeminiAdapter
from amw.agents.prompt_packs import load_pack
from amw.agents.schemas import SUBAGENTS
from amw.config import AppConfig, ConfigError, load_all
from amw.datasets.schema import DatasetItem, read_items
from amw.economics.cache_breakeven import (
    DEFAULT_TTL_HOURS,
    CacheBreakeven,
    breakeven_curve,
)
from amw.economics.cost_model import CANDIDATE_ROLE
from amw.traces.schema import Trace
from amw.traces.store import ReplayStore

__all__ = [
    "CACHE_DEMO_VERSION",
    "DEMO_SUBAGENT",
    "DEMO_VARIANT",
    "PREAMBLE_CHUNKS",
    "COLD_LABEL",
    "WARM_LABEL",
    "CachedPreambleAdapter",
    "CacheDemoResult",
    "SharedPreamble",
    "PairedCall",
    "build_preamble",
    "cmd_cache_demo",
    "default_demo_path",
    "render_lines",
    "run_cache_demo",
]

#: Bumped when the artifact shape changes.
CACHE_DEMO_VERSION = "1"

#: The subagent whose preamble is demonstrated. Chunk Summarizer is the one
#: with a genuinely shared preamble in production — the same instruction and
#: the same document context across every chunk of a document — so it is the
#: one where the caching argument is real rather than contrived.
DEMO_SUBAGENT = "chunk_summarizer"

#: Prompt pack the preamble's system instruction comes from. The candidate
#: arm, because caching is being costed for the migration target.
DEMO_VARIANT = "gemini_naive"

#: How many corpus chunks are pooled into the shared preamble. Explicit caching
#: has a per-model minimum token count; too few chunks and the create call is
#: rejected by the service. Raise this rather than guessing at tokens — the
#: only token count this module trusts is the one the API returns.
PREAMBLE_CHUNKS = 24

COLD_LABEL = "cold"
WARM_LABEL = "warm"

#: The two suffixes. Deliberately different (see the module docstring) and
#: deliberately trivial: the demo is about the preamble's token accounting, and
#: a suffix long enough to be interesting would blur the reading.
COLD_SUFFIX = "Summarise the first chunk above in one sentence."
WARM_SUFFIX = "Summarise the last chunk above in one sentence."

REPO_ROOT = Path(__file__).resolve().parents[2]


def default_demo_path() -> Path:
    return REPO_ROOT / "artifacts" / "results" / "cache_demo.json"


def default_dataset_dir() -> Path:
    return REPO_ROOT / "datasets"


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# the adapter
# --------------------------------------------------------------------------


class CachedPreambleAdapter(GeminiAdapter):
    """:class:`~amw.adapters.gemini.GeminiAdapter`, serving one cached preamble.

    Two overrides, both narrow, both for the same reason: whatever is already
    inside the cache must not be sent again. Re-sending it would bill the
    preamble at the fresh rate *and* pay cache rent, which is the worst of both
    and would show up as a cached-token count that does not match the
    request — the exact failure this demo exists to rule out.

    :param cached_content: the resource name from ``client.caches.create``.
    """

    name = "gemini-cached"

    def __init__(self, *, cached_content: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not cached_content:
            raise ValueError(
                "CachedPreambleAdapter needs a cache resource name; without one "
                "it is just GeminiAdapter with a misleading name"
            )
        self.cached_content = cached_content

    def _build_config(self, request: ModelRequest, types: Any) -> Any:
        # Vertex rejects system_instruction and tools alongside cached_content:
        # they were fixed when the cache was created and cannot be restated.
        # Building the base config first keeps temperature/max_output_tokens
        # handling in one place instead of forking it.
        config = super()._build_config(request, types)
        config.system_instruction = None
        config.tools = None
        config.cached_content = self.cached_content
        return config

    @staticmethod
    def _build_contents(request: ModelRequest, types: Any) -> list[Any]:
        # Shared chunks are in the cache; only the per-call suffix goes over
        # the wire. The ModelRequest still carries the chunks, so the recorded
        # trace describes the logical call.
        return [
            types.Content(
                role="user",
                parts=[types.Part(text=message) for message in request.messages],
            )
        ]


# --------------------------------------------------------------------------
# what gets cached
# --------------------------------------------------------------------------


class SharedPreamble(_Base):
    """The text both calls share, plus how it was assembled.

    No token count lives here. The size of this thing in tokens is a fact about
    the tokenizer, and the only tokenizer whose answer is billable is the
    service's — so the count arrives later, on :class:`CacheDemoResult`, from
    the create call's own usage metadata.
    """

    subagent: str
    variant: str
    system: str
    chunks: list[str] = Field(default_factory=list)
    item_ids: list[str] = Field(default_factory=list)
    #: Characters, which is a fact about the string and not a claim about
    #: billing. Present so a reader can tell "the preamble was too small" from
    #: "the preamble was fine and the service refused for another reason".
    characters: int = 0

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


def build_preamble(
    *,
    config: AppConfig | None = None,
    subagent: str = DEMO_SUBAGENT,
    variant: str = DEMO_VARIANT,
    dataset_dir: str | Path | None = None,
    chunks: int = PREAMBLE_CHUNKS,
) -> SharedPreamble:
    """Pool real corpus chunks behind the real system prompt.

    Real on both counts, deliberately. A lorem-ipsum preamble would cache and
    would produce a perfectly valid token count, but the number a customer
    cares about is *their* preamble's, and the closest honest stand-in is the
    corpus this workbench already generated for them. Items are taken in
    ``item_id`` order so the same corpus always yields the same preamble and
    two runs of this demo are comparable.
    """
    cfg = config or load_all()
    del cfg  # the preamble needs the corpus and the pack, not the config
    if chunks <= 0:
        raise ValueError("chunks must be positive; an empty preamble caches nothing")

    directory = Path(dataset_dir) if dataset_dir else default_dataset_dir()
    path = directory / f"{subagent}.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"no dataset at {path}. The cache demo shares the corpus the rest of "
            f"the workbench measures; run `python cli.py gen` first."
        )

    pack = load_pack(subagent, variant)
    text: list[str] = []
    used: list[str] = []
    for item in sorted(read_items(path), key=lambda i: i.item_id):
        chunk_texts = _chunk_texts(item)
        if not chunk_texts:
            continue
        used.append(item.item_id)
        text.extend(chunk_texts)
        if len(text) >= chunks:
            break
    text = text[:chunks]

    return SharedPreamble(
        subagent=subagent,
        variant=variant,
        system=pack.system,
        chunks=text,
        item_ids=used,
        characters=len(pack.system) + sum(len(chunk) for chunk in text),
    )


def _chunk_texts(item: DatasetItem) -> list[str]:
    """The item's chunks in exactly the form the adapters send them.

    :meth:`~amw.datasets.schema.DatasetInput.context_chunks` and not the raw
    ``chunk.text``: the ``[id] text`` gluing is part of what the model sees on
    a real call, so caching the ungluded version would measure a preamble that
    never gets sent.
    """
    return item.input.context_chunks()


# --------------------------------------------------------------------------
# the result
# --------------------------------------------------------------------------

CacheDemoStatus = Literal["not_run", "ok", "no_cache_hit", "refused", "service_error"]


class PairedCall(_Base):
    """One half of the pair, as the service reported it."""

    label: str
    trace_id: str
    model: str
    #: Straight off ``Trace.usage``. ``cached_tokens`` on the warm call is the
    #: whole point of the exercise.
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    latency_total_ms: int | None = None
    status: str = "ok"
    error: str | None = None

    @classmethod
    def from_trace(cls, label: str, trace: Trace) -> "PairedCall":
        return cls(
            label=label,
            trace_id=trace.trace_id,
            model=trace.model,
            input_tokens=trace.usage.input_tokens,
            output_tokens=trace.usage.output_tokens,
            cached_tokens=trace.usage.cached_tokens,
            latency_total_ms=trace.latency_ms.total,
            status=trace.status,
            error=trace.error,
        )


class CacheDemoResult(_Base):
    """``artifacts/results/cache_demo.json`` — the pair, or why there isn't one.

    ``status`` defaults to ``not_run`` and every measured field to ``None``.
    There is no state of this object in which a number appears that the service
    did not return.
    """

    cache_demo_version: str = CACHE_DEMO_VERSION
    status: CacheDemoStatus = "not_run"
    detail: str = "the cache demo has not been run"
    mode: str = "live"
    model: str | None = None
    ttl_hours: float | None = None
    preamble: SharedPreamble | None = None

    #: The create call's own count of what it cached. This is the number the
    #: breakeven overlay is driven by; ``None`` until the service returns it.
    cached_tokens: int | None = None
    #: The warm call's ``cached_content_token_count``. Should equal
    #: ``cached_tokens``; :attr:`reconciled` says whether it did.
    warm_cached_tokens: int | None = None
    calls: list[PairedCall] = Field(default_factory=list)

    #: The overlay, at the measured token count, across the TTL ladder. Empty
    #: while nothing is measured — never a curve over an assumed size.
    breakeven: list[CacheBreakeven] = Field(default_factory=list)

    run_at: str | None = None
    notes: list[str] = Field(default_factory=list)

    @property
    def measured(self) -> bool:
        return self.status == "ok" and self.cached_tokens is not None

    @property
    def reconciled(self) -> bool:
        """Did the warm call bill the number of tokens the cache says it holds?

        The one check that makes the demo evidence rather than theatre. A cache
        can be created successfully and then not be used — wrong model, expired
        TTL, a config field silently dropped — and the call still returns a
        perfectly good answer. Only the two counts agreeing shows the discount
        was actually earned.
        """
        return (
            self.cached_tokens is not None
            and self.warm_cached_tokens is not None
            and self.cached_tokens == self.warm_cached_tokens
        )

    def summary_line(self) -> str:
        if not self.measured:
            return f"context-caching demo: {self.status} — {self.detail}"
        return (
            f"context-caching demo: {self.cached_tokens} tokens cached and "
            f"{self.warm_cached_tokens} billed as cached on the warm call "
            f"({'reconciled' if self.reconciled else 'DOES NOT RECONCILE'})"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------


def _request(
    preamble: SharedPreamble, model_key: str, suffix: str, item_id: str
) -> ModelRequest:
    """The logical call: whole preamble, one suffix. Same shape for both halves.

    Identical between cold and warm except for the suffix, which is what makes
    the two traces comparable at all. Whether the preamble travels inline or
    comes out of a cache is a wire detail the adapter handles.
    """
    return ModelRequest(
        subagent=preamble.subagent,
        model=model_key,
        system_prompt=preamble.system,
        messages=[suffix],
        context_chunks=list(preamble.chunks),
        item_id=item_id,
    )


def _ttl_seconds(ttl_hours: float) -> str:
    return f"{int(round(ttl_hours * 3600))}s"


def run_cache_demo(
    *,
    mode: str,
    config: AppConfig | None = None,
    customer: str | None = None,
    subagent: str = DEMO_SUBAGENT,
    variant: str = DEMO_VARIANT,
    dataset_dir: str | Path | None = None,
    chunks: int = PREAMBLE_CHUNKS,
    ttl_hours: float = 1.0,
    ttl_ladder: tuple[float, ...] = DEFAULT_TTL_HOURS,
    model_role: str = CANDIDATE_ROLE,
    out_path: str | Path | None = None,
    write: bool = True,
    store: ReplayStore | None = None,
    client: Any = None,
) -> CacheDemoResult:
    """Create a cache, make the pair, read the counts, overlay the breakeven.

    Live only. There is no replay path and there must not be one: a replayed
    "cached token count" is a number copied out of an old file and presented as
    this run's measurement, and the demo's entire claim is that the number came
    from the service just now.

    Service failures become a ``service_error`` status rather than an
    exception — the stretch demo must not be able to take down a workshop — but
    a caller error (replay mode, no corpus) still raises.

    :param client: a pre-built ``genai.Client`` (tests inject a stub).
    """
    if mode == "replay":
        raise ConfigError(
            "the context-caching demo is live-only: its whole claim is a "
            "cached-token count the service returned during the demo. Replaying "
            "one would be quoting an old measurement as a new one. Use "
            "--mode live, or read the modelled breakeven from `cli.py scorecard`, "
            "which needs no calls at all."
        )
    if subagent not in SUBAGENTS:
        raise ConfigError(
            f"unknown subagent {subagent!r}; expected one of {list(SUBAGENTS)}"
        )
    cfg = config or load_all(customer=customer)
    model_key = cfg.models.for_role(model_role)[0]

    preamble = build_preamble(
        config=cfg,
        subagent=subagent,
        variant=variant,
        dataset_dir=dataset_dir,
        chunks=chunks,
    )
    result = CacheDemoResult(
        mode=mode,
        model=model_key,
        ttl_hours=ttl_hours,
        preamble=preamble,
        run_at=_now(),
    )

    replay = store if store is not None else ReplayStore()
    cold_adapter = GeminiAdapter(models=cfg.models, client=client)

    try:
        with _explicit_cache(
            cold_adapter, preamble, model_key, ttl_hours=ttl_hours
        ) as cache:
            result.cached_tokens = cache.tokens
            if cache.tokens is None:
                result.notes.append(
                    "the create call returned no usage metadata, so the cache's "
                    "own token count is unavailable; the warm call's count is "
                    "reported alone and nothing is reconciled against it."
                )
            cold = cold_adapter.complete(
                _request(preamble, model_key, COLD_SUFFIX, f"cache-demo-{COLD_LABEL}")
            )
            replay.append(cold)
            result.calls.append(PairedCall.from_trace(COLD_LABEL, cold))

            warm_adapter = CachedPreambleAdapter(
                cached_content=cache.name,
                models=cfg.models,
                client=cold_adapter._ensure_client(),
            )
            warm = warm_adapter.complete(
                _request(preamble, model_key, WARM_SUFFIX, f"cache-demo-{WARM_LABEL}")
            )
            replay.append(warm)
            result.calls.append(PairedCall.from_trace(WARM_LABEL, warm))
            result.warm_cached_tokens = warm.usage.cached_tokens
    except Exception as exc:  # noqa: BLE001 - a stretch demo reports, never crashes
        result.status = "service_error"
        result.detail = (
            f"{type(exc).__name__}: {exc}. No cached-token count was measured, so "
            f"no breakeven is overlaid; `cli.py scorecard --cache-preamble-tokens` "
            f"still gives the modelled table."
        )
        return _write(result, out_path, write=write)

    failed = [call for call in result.calls if call.status != "ok"]
    if failed:
        result.status = "service_error"
        result.detail = (
            f"the {failed[0].label} call was recorded as an error: {failed[0].error}. "
            f"The trace is in artifacts/replay/ either way, but there is no "
            f"usable pair and nothing is overlaid."
        )
        return _write(result, out_path, write=write)

    measured = result.cached_tokens or result.warm_cached_tokens
    if not measured:
        result.status = "no_cache_hit"
        result.detail = (
            "both calls succeeded but the warm one billed 0 cached tokens: the "
            "cache was created and then not used. Do not show this as a caching "
            "win — check that the cache and the call name the same model and "
            "that the TTL had not expired."
        )
        return _write(result, out_path, write=write)

    if not result.reconciled:
        result.notes.append(
            f"the cache reports {result.cached_tokens} tokens and the warm call "
            f"billed {result.warm_cached_tokens} as cached. They should match; "
            f"the breakeven below is driven by the cache's own count."
        )

    result.status = "ok"
    result.detail = (
        f"a {measured}-token preamble was cached and served to the warm call. "
        f"Both calls are recorded in artifacts/replay/."
    )
    # The overlay. Every rate inside comes from config/pricing.yaml and refuses
    # on VERIFY, so with today's table this is a list of explicit refusals with
    # a real token count attached — which is the honest state, not a gap.
    result.breakeven = breakeven_curve(
        cfg, cached_tokens=measured, ttl_hours=ttl_ladder, model_key=model_key
    )
    if result.breakeven and not result.breakeven[0].computable:
        result.notes.append(
            "the token count is measured but the breakeven is not computable: "
            "config/pricing.yaml still reads VERIFY. Run "
            "scripts/refresh_pricing.py and re-render — the measurement does not "
            "need repeating."
        )
    return _write(result, out_path, write=write)


class _Cache:
    """The created cache: its resource name and the tokens the service counted."""

    def __init__(self, name: str, tokens: int | None) -> None:
        self.name = name
        self.tokens = tokens


def _cache_tokens(cache: Any) -> int | None:
    """The create call's token count, or ``None`` if it did not report one.

    Read through ``getattr`` because this is the field the whole demo rests on
    and an SDK rename should degrade to "unavailable" — which
    :func:`run_cache_demo` says out loud — rather than to a plausible zero.
    """
    usage = getattr(cache, "usage_metadata", None)
    if usage is None:
        return None
    total = getattr(usage, "total_token_count", None)
    return int(total) if total else None


def _explicit_cache(
    adapter: GeminiAdapter,
    preamble: SharedPreamble,
    model_key: str,
    *,
    ttl_hours: float,
) -> "_CacheContext":
    return _CacheContext(adapter, preamble, model_key, ttl_hours=ttl_hours)


class _CacheContext:
    """Create the cache, yield it, delete it — even when the pair fails.

    Deleting matters for a reason the breakeven model makes explicit: a live
    cache bills rent per token-hour whether or not anything reads it. A demo
    that leaks caches quietly bills the customer's project for the storage side
    of the very trade-off it was arguing about.
    """

    def __init__(
        self,
        adapter: GeminiAdapter,
        preamble: SharedPreamble,
        model_key: str,
        *,
        ttl_hours: float,
    ) -> None:
        self.adapter = adapter
        self.preamble = preamble
        self.model_key = model_key
        self.ttl_hours = ttl_hours
        self._client: Any = None
        self._name: str | None = None

    def __enter__(self) -> _Cache:
        from google.genai import types  # lazy: ground rule 4

        self._client = self.adapter._ensure_client()
        model_id = self.adapter.model_id(self.model_key)
        cache = self._client.caches.create(
            model=model_id,
            config=types.CreateCachedContentConfig(
                system_instruction=self.preamble.system,
                contents=[
                    types.Content(
                        role="user",
                        parts=[types.Part(text=chunk) for chunk in self.preamble.chunks],
                    )
                ],
                ttl=_ttl_seconds(self.ttl_hours),
                display_name=f"amw-cache-demo-{self.preamble.subagent}",
            ),
        )
        self._name = getattr(cache, "name", None)
        if not self._name:
            raise ConfigError(
                "caches.create returned no resource name; there is nothing to "
                "attach the warm call to."
            )
        return _Cache(self._name, _cache_tokens(cache))

    def __exit__(self, *exc: Any) -> Literal[False]:
        if self._client is not None and self._name:
            try:
                self._client.caches.delete(name=self._name)
            except Exception:  # noqa: BLE001 - cleanup must not mask the real error
                # The TTL will collect it. Said here rather than swallowed
                # silently so an operator watching the console knows to check.
                print(
                    f"warning: could not delete cache {self._name}; it will "
                    f"expire on its own TTL but is billing rent until then",
                    file=sys.stderr,
                )
        return False


def _write(
    result: CacheDemoResult, out_path: str | Path | None, *, write: bool
) -> CacheDemoResult:
    if not write:
        return result
    path = Path(out_path) if out_path else default_demo_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return result


def render_lines(result: CacheDemoResult) -> list[str]:
    """Console rendering. Prints refusals in full and invents no figures."""
    lines = ["=== context-caching demo ===", f"  {result.summary_line()}"]
    if result.preamble is not None:
        lines.append(
            f"  preamble    : {result.preamble.subagent}/{result.preamble.variant}, "
            f"{result.preamble.chunk_count} chunk(s) from "
            f"{len(result.preamble.item_ids)} item(s), "
            f"{result.preamble.characters} characters"
        )
    if result.model:
        lines.append(f"  model       : {result.model}")
    for call in result.calls:
        lines.append(
            f"  {call.label:<11}: input {call.input_tokens}, cached "
            f"{call.cached_tokens}, output {call.output_tokens}"
            + (f", {call.latency_total_ms} ms" if call.latency_total_ms else "")
            + ("" if call.status == "ok" else f"  [{call.status}: {call.error}]")
        )
    if not result.measured:
        lines.append(f"  detail      : {result.detail}")
        lines.append("  breakeven   : not overlaid — nothing was measured")
        return lines

    lines.append("")
    lines.append(f"  breakeven at {result.cached_tokens} measured cached tokens:")
    for row in result.breakeven:
        if row.breakeven_calls_per_day is None:
            lines.append(f"    ttl {row.ttl_hours:>5.1f}h : {row.state}")
        else:
            lines.append(
                f"    ttl {row.ttl_hours:>5.1f}h : "
                f"{row.breakeven_calls_per_day:,.0f} calls/day to break even"
            )
    if result.breakeven:
        lines.append("")
        for assumption in result.breakeven[0].assumptions:
            lines.append(f"    assumes: {assumption}")
    return lines


def cmd_cache_demo(args: Any, cfg: Any) -> int:
    """``python cli.py cache-demo --mode live``.

    Exit codes: 0 when a cached-token count was measured; 3 when the attempt is
    recorded but nothing was measured (no hit, service error) — non-zero so a
    script cannot mistake a failed demo for a successful one; 2 for a caller
    error, including ``--mode replay``.
    """
    try:
        result = run_cache_demo(
            mode=args.mode,
            config=cfg,
            subagent=getattr(args, "subagent", None) or DEMO_SUBAGENT,
            variant=getattr(args, "variant", None) or DEMO_VARIANT,
            dataset_dir=getattr(args, "dataset_dir", None),
            chunks=getattr(args, "chunks", None) or PREAMBLE_CHUNKS,
            ttl_hours=getattr(args, "ttl_hours", None) or 1.0,
            out_path=getattr(args, "out", None),
        )
    except (ConfigError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for line in render_lines(result):
        print(line)
    for note in result.notes:
        print(f"note: {note}", file=sys.stderr)
    return 0 if result.measured else 3
