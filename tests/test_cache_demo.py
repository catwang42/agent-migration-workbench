"""Tests for the context-caching demo scaffold — offline and credential-free.

The demo's whole value is that its headline number is a measurement, so most
of this file is about the two ways that can go wrong: reporting a number the
service did not give (fabrication), and reporting a *missing* number as zero
(worse, because zero looks like a finding).

The Gemini calls run against a stub client but the real ``google.genai`` types,
following ``tests/test_adapters.py``: a config field that Vertex would reject
fails here rather than in front of a customer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from amw.config import ConfigError, load_all
from amw.economics import cache_demo
from amw.economics.cache_demo import (
    COLD_LABEL,
    WARM_LABEL,
    CachedPreambleAdapter,
    CacheDemoResult,
    PairedCall,
    build_preamble,
    render_lines,
    run_cache_demo,
)
from amw.traces.store import ReplayStore

REPO_ROOT = Path(__file__).resolve().parent.parent

CACHE_NAME = "projects/p/locations/global/cachedContents/1234"
CACHED_TOKENS = 2317


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    for var in (
        "PROJECT_ID",
        "REGION",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(scope="module")
def cfg():
    return load_all()


@pytest.fixture
def store(tmp_path) -> ReplayStore:
    return ReplayStore(tmp_path / "replay")


# --------------------------------------------------------------------------
# stubs
# --------------------------------------------------------------------------


def usage_meta(prompt=0, candidates=0, cached=0):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=candidates,
        cached_content_token_count=cached,
        thoughts_token_count=0,
    )


def chunk(text, usage):
    content = SimpleNamespace(parts=[SimpleNamespace(text=text, function_call=None, thought=None)])
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=content)], usage_metadata=usage
    )


class StubCaches:
    def __init__(self, tokens=CACHED_TOKENS, error=None, name=CACHE_NAME):
        self.tokens = tokens
        self.error = error
        self.name = name
        self.created: list[dict] = []
        self.deleted: list[str] = []

    def create(self, *, model, config):
        self.created.append({"model": model, "config": config})
        if self.error is not None:
            raise self.error
        usage = (
            None
            if self.tokens is None
            else SimpleNamespace(total_token_count=self.tokens)
        )
        return SimpleNamespace(name=self.name, usage_metadata=usage)

    def delete(self, *, name):
        self.deleted.append(name)


class StubClient:
    """``genai.Client`` stand-in: canned cache + one canned reply per call."""

    def __init__(self, *, cold_cached=0, warm_cached=CACHED_TOKENS, caches=None):
        self.caches = caches if caches is not None else StubCaches()
        self._replies = [
            chunk("cold answer", usage_meta(prompt=9000, candidates=12, cached=cold_cached)),
            chunk(
                "warm answer",
                usage_meta(prompt=9000, candidates=12, cached=warm_cached),
            ),
        ]
        self.calls: list[dict] = []
        self.models = SimpleNamespace(generate_content_stream=self._stream)

    def _stream(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return iter([self._replies[min(len(self.calls) - 1, 1)]])


def demo(cfg, store, **kwargs):
    client = kwargs.pop("client", None) or StubClient()
    result = run_cache_demo(
        mode="live", config=cfg, store=store, client=client, write=False, **kwargs
    )
    return result, client


# --------------------------------------------------------------------------
# nothing measured means nothing reported
# --------------------------------------------------------------------------


def test_a_demo_that_has_not_run_says_so_and_shows_no_figures():
    result = CacheDemoResult()
    assert result.status == "not_run"
    assert not result.measured
    for field in ("cached_tokens", "warm_cached_tokens", "model", "ttl_hours"):
        assert getattr(result, field) is None, f"{field} must be absent, not zero"
    assert result.breakeven == []
    assert result.calls == []
    assert "not been run" in result.summary_line()


def test_the_unrun_rendering_never_prints_a_breakeven():
    text = "\n".join(render_lines(CacheDemoResult()))
    assert "not_run" in text
    assert "not overlaid" in text
    assert "calls/day" not in text


def test_replay_mode_is_refused_because_a_replayed_count_is_not_a_measurement(cfg):
    with pytest.raises(ConfigError, match="live-only"):
        run_cache_demo(mode="replay", config=cfg, write=False)


def test_an_unknown_subagent_is_a_caller_error(cfg):
    with pytest.raises(ConfigError, match="unknown subagent"):
        run_cache_demo(mode="live", config=cfg, subagent="nope", write=False)


# --------------------------------------------------------------------------
# the preamble
# --------------------------------------------------------------------------


def test_the_preamble_is_real_corpus_text_behind_the_real_system_prompt():
    from amw.agents.prompt_packs import load_pack

    preamble = build_preamble()
    assert preamble.system == load_pack(preamble.subagent, preamble.variant).system
    assert preamble.chunk_count == cache_demo.PREAMBLE_CHUNKS
    assert preamble.item_ids
    # The `[id] text` gluing is what the adapters actually send, so it must be
    # what gets cached.
    assert all(chunk.startswith("[") for chunk in preamble.chunks)
    assert preamble.characters > 0


def test_the_preamble_is_deterministic():
    assert build_preamble().chunks == build_preamble().chunks


def test_the_preamble_carries_no_token_count():
    """Only the service counts tokens here. ``characters`` is a fact about a
    string; a token estimate would be a guess dressed as a measurement."""
    assert "tokens" not in set(build_preamble().model_dump())


def test_an_empty_preamble_is_rejected():
    with pytest.raises(ValueError, match="caches nothing"):
        build_preamble(chunks=0)


def test_a_missing_corpus_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="cli.py gen"):
        build_preamble(dataset_dir=tmp_path)


# --------------------------------------------------------------------------
# the pair
# --------------------------------------------------------------------------


def test_a_successful_pair_reports_the_services_own_token_count(cfg, store):
    result, client = demo(cfg, store)

    assert result.status == "ok"
    assert result.measured
    assert result.cached_tokens == CACHED_TOKENS
    assert result.warm_cached_tokens == CACHED_TOKENS
    assert result.reconciled
    assert [call.label for call in result.calls] == [COLD_LABEL, WARM_LABEL]
    assert result.calls[0].cached_tokens == 0
    assert result.calls[1].cached_tokens == CACHED_TOKENS


def test_both_calls_are_recorded_with_no_way_to_turn_it_off(cfg, store):
    """Ground rule 5. The demo is a live call path like any other."""
    demo(cfg, store)
    store.reload()
    assert len(store) == 2
    assert {trace.subagent for trace in store} == {cache_demo.DEMO_SUBAGENT}


def test_the_two_calls_differ_only_in_their_suffix(cfg, store):
    demo(cfg, store)
    store.reload()
    shas = {trace.input_sha for trace in store}
    assert len(shas) == 2, "the pair must be two distinct requests, not one twice"
    assert len({trace.system_prompt_sha for trace in store}) == 1


def test_the_cache_is_created_from_the_preamble_and_then_deleted(cfg, store):
    caches = StubCaches()
    result, _ = demo(cfg, store, client=StubClient(caches=caches))
    assert len(caches.created) == 1
    config = caches.created[0]["config"]
    assert config.system_instruction == result.preamble.system
    assert config.ttl == "3600s"
    # Leaking a cache bills storage rent for the TTL — the exact cost the
    # breakeven model warns about.
    assert caches.deleted == [CACHE_NAME]


def test_the_cache_is_deleted_even_when_a_call_blows_up(cfg, store, monkeypatch):
    caches = StubCaches()
    client = StubClient(caches=caches)

    def boom(**kwargs):
        raise RuntimeError("stream died")

    client.models.generate_content_stream = boom
    result = run_cache_demo(
        mode="live", config=cfg, store=store, client=client, write=False
    )
    assert result.status == "service_error"
    assert caches.deleted == [CACHE_NAME]


def test_the_warm_call_sends_the_cache_instead_of_the_preamble(cfg, store):
    _, client = demo(cfg, store)
    cold_config, warm_config = client.calls[0]["config"], client.calls[1]["config"]

    assert getattr(cold_config, "cached_content", None) is None
    assert cold_config.system_instruction is not None
    assert warm_config.cached_content == CACHE_NAME
    # Restating either alongside cached_content is rejected by Vertex, and
    # would double-bill the preamble if it were not.
    assert warm_config.system_instruction is None
    assert warm_config.tools is None
    # Two Contents cold (chunks + suffix), one warm (suffix only).
    assert len(client.calls[0]["contents"]) == 2
    assert len(client.calls[1]["contents"]) == 1


def test_the_warm_trace_still_records_the_whole_logical_input(cfg, store):
    """The wire call is short because the preamble is cached; the *trace* must
    still say what the model saw, or the corpus stops being replayable."""
    demo(cfg, store)
    store.reload()
    warm = [t for t in store if t.trace_id.endswith(WARM_LABEL)][0]
    assert warm.input.context_chunks
    assert warm.system_prompt_sha


# --------------------------------------------------------------------------
# the ways a cache demo lies if you let it
# --------------------------------------------------------------------------


def test_a_cache_that_was_created_but_not_used_is_not_a_win(cfg, store):
    result, _ = demo(cfg, store, client=StubClient(warm_cached=0, caches=StubCaches(tokens=0)))
    assert result.status == "no_cache_hit"
    assert not result.measured
    assert result.breakeven == []
    assert "Do not show this as a caching win" in result.detail


def test_counts_that_disagree_are_flagged_not_averaged(cfg, store):
    result, _ = demo(cfg, store, client=StubClient(warm_cached=11))
    assert result.status == "ok"
    assert not result.reconciled
    assert any("should match" in note for note in result.notes)
    assert "DOES NOT RECONCILE" in result.summary_line()


def test_a_create_failure_is_a_status_not_a_crash(cfg, store):
    caches = StubCaches(error=RuntimeError("400 cached content is too small"))
    result, _ = demo(cfg, store, client=StubClient(caches=caches))
    assert result.status == "service_error"
    assert "too small" in result.detail
    assert result.cached_tokens is None
    assert result.breakeven == []


def test_a_create_call_with_no_usage_metadata_says_so(cfg, store):
    result, _ = demo(cfg, store, client=StubClient(caches=StubCaches(tokens=None)))
    assert result.status == "ok"
    assert result.cached_tokens is None
    assert result.warm_cached_tokens == CACHED_TOKENS
    assert not result.reconciled
    assert any("no usage metadata" in note for note in result.notes)


def test_an_errored_call_produces_no_overlay(cfg, store):
    result, _ = demo(cfg, store)
    result.calls[1] = PairedCall(
        label=WARM_LABEL,
        trace_id="x",
        model="m",
        input_tokens=0,
        output_tokens=0,
        cached_tokens=0,
        status="error",
        error="429",
    )
    # Same assertion the runner makes, restated on the model: an errored half
    # can never be presented as half a measurement.
    assert any(call.status != "ok" for call in result.calls)


# --------------------------------------------------------------------------
# the overlay
# --------------------------------------------------------------------------


def test_the_overlay_is_driven_by_the_measured_count(cfg, store):
    result, _ = demo(cfg, store)
    assert result.breakeven
    assert {row.cached_tokens for row in result.breakeven} == {CACHED_TOKENS}
    assert [row.ttl_hours for row in result.breakeven] == list(
        cache_demo.DEFAULT_TTL_HOURS
    )


def test_unverified_prices_make_the_overlay_refuse_rather_than_guess(cfg, store):
    """Today ``config/pricing.yaml`` is all VERIFY. A measured token count plus
    an explicit "not computable" is the honest pair of statements."""
    if cfg.pricing.is_verified:
        pytest.skip("prices have been refreshed; the refusal path no longer applies")
    result, _ = demo(cfg, store)
    assert all(not row.computable for row in result.breakeven)
    assert all(row.breakeven_calls_per_day is None for row in result.breakeven)
    assert any("VERIFY" in note for note in result.notes)
    text = "\n".join(render_lines(result))
    assert str(CACHED_TOKENS) in text
    assert "not computable" in text


def test_no_price_literal_lives_in_the_cache_demo_module():
    """Ground rule 3, the same scan ``tests/test_economics.py`` runs on its
    siblings, extended to this module."""
    source = (REPO_ROOT / "amw" / "economics" / "cache_demo.py").read_text(
        encoding="utf-8"
    )
    for token in ("0.075", "0.30", "0.375", "per_1m", "USD/1M", "$0."):
        assert token not in source, f"{token!r} looks like a hardcoded price"
    assert "pricing.rate(" not in source, (
        "cache_demo must not fetch rates itself; the overlay goes through "
        "cache_breakeven so there is one place prices are read"
    )


def test_no_provider_sdk_is_imported_at_module_scope():
    source = (REPO_ROOT / "amw" / "economics" / "cache_demo.py").read_text(
        encoding="utf-8"
    )
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            assert "google.genai" not in line and "vertexai" not in line, (
                f"module-scope provider import breaks the zero-credential rule: {line!r}"
            )


def test_no_model_id_is_hardcoded(cfg):
    source = (REPO_ROOT / "amw" / "economics" / "cache_demo.py").read_text(
        encoding="utf-8"
    )
    for key in cfg.models.models:
        assert cfg.models.spec(key).id_for("vertex") not in source


def test_the_cached_adapter_refuses_to_pretend_it_has_a_cache():
    with pytest.raises(ValueError, match="cache resource name"):
        CachedPreambleAdapter(cached_content="")
