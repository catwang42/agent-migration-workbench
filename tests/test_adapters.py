"""Adapter tests — all offline, all credential-free.

That is not incidental: CLAUDE.md ground rule 4 says the whole workbench must
run in replay mode with zero credentials, so this file asserts it. The Gemini
adapter is exercised against a stub client but the *real* ``google.genai``
types, so a wrong config field fails here rather than in front of a customer.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from amw.adapters import (
    DEFAULT_MODE,
    MODES,
    AdapterRouter,
    RecordingAdapter,
    resolve,
)
from amw.adapters.base import ModelAdapter, ModelRequest, ToolSpec, build_trace
from amw.adapters.gemini import DEFAULT_TEMPERATURE, GeminiAdapter, MissingEnvError
from amw.adapters.replay import ReplayAdapter
from amw.config import ConfigError, load_all
from amw.traces.schema import ToolCall, TraceOutput, Usage
from amw.traces.store import ReplayMissError, ReplayStore

REPO_ROOT = Path(__file__).resolve().parent.parent

GEMINI = "gemini-flash"
CLAUDE = "claude-sonnet"

PLAN_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
}


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    """Prove the zero-credential rule rather than inheriting a dev shell."""
    for var in (
        "PROJECT_ID",
        "REGION",
        "CLAUDE_PATH",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(scope="module")
def models():
    return load_all().models


@pytest.fixture
def store(tmp_path) -> ReplayStore:
    return ReplayStore(tmp_path / "replay")


def make_request(**overrides) -> ModelRequest:
    payload = {
        "subagent": "query_rewriter",
        "model": GEMINI,
        "system_prompt": "Rewrite the user query into search queries.",
        "messages": ["Find prior art on solid-state battery separators."],
        "context_chunks": ["US10123456B2 — ceramic-coated polyolefin separator."],
    }
    payload.update(overrides)
    return ModelRequest(**payload)


def record(store: ReplayStore, request: ModelRequest, **kwargs):
    """Put a trace for ``request`` into the corpus, the way a live run would."""
    kwargs.setdefault("output", TraceOutput(text="recorded answer"))
    trace = build_trace(request, **kwargs)
    return store.append(trace)


# -- google.genai stubs -----------------------------------------------------


def part(text=None, function_call=None, thought=None):
    return SimpleNamespace(text=text, function_call=function_call, thought=thought)


def chunk(*parts, usage=None):
    content = SimpleNamespace(parts=list(parts))
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=content)], usage_metadata=usage
    )


def usage_meta(prompt=0, candidates=0, cached=0, thoughts=0):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=candidates,
        cached_content_token_count=cached,
        thoughts_token_count=thoughts,
    )


class StubClient:
    """Stands in for ``genai.Client``; records the call, replays canned chunks."""

    def __init__(self, chunks=(), error: Exception | None = None):
        self._chunks = list(chunks)
        self._error = error
        self.calls: list[dict] = []
        self.models = SimpleNamespace(generate_content_stream=self._stream)

    def _stream(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self._error is not None:
            raise self._error
        return iter(self._chunks)


def gemini(models, client) -> GeminiAdapter:
    # sleep=lambda _: None: retry backoff must not cost the suite 3 seconds.
    return GeminiAdapter(models=models, client=client, sleep=lambda _: None)


# --------------------------------------------------------------------------
# ReplayAdapter
# --------------------------------------------------------------------------


def test_replay_adapter_returns_the_recorded_trace(store):
    request = make_request()
    recorded = record(store, request, usage=Usage(input_tokens=812, output_tokens=196))

    trace = ReplayAdapter(store).complete(request)

    assert trace.trace_id == recorded.trace_id
    assert trace.output.text == "recorded answer"
    assert trace.usage.input_tokens == 812
    assert trace.key == request.replay_key


def test_replay_miss_raises_instead_of_fabricating(store):
    record(store, make_request())
    # Same subagent and model, different prompt -> different input_sha.
    other = make_request(messages=["Something nobody ever recorded."])

    with pytest.raises(ReplayMissError) as excinfo:
        ReplayAdapter(store).complete(other)

    assert other.input_sha in str(excinfo.value)


def test_replay_miss_is_not_downgraded_to_an_error_trace(store):
    """An error trace means the model failed. A miss means it was never asked."""
    with pytest.raises(ReplayMissError):
        ReplayAdapter(store).complete(make_request())


def test_replay_adapter_hands_out_copies(store):
    request = make_request()
    record(store, request)
    adapter = ReplayAdapter(store)

    first = adapter.complete(request)
    first.output.text = "mutated by a careless caller"

    assert adapter.complete(request).output.text == "recorded answer"


def test_replay_adapter_reports_its_recording_window(store):
    """Ground rule 1: replayed numbers must be labelled with their date."""
    stamp = datetime(2026, 8, 3, 10, 22, 41, tzinfo=timezone.utc)
    record(store, make_request(), ts=stamp)

    assert ReplayAdapter(store).recording_window() == (stamp, stamp)


def test_replay_adapter_distinguishes_ablation_rungs(store):
    """A0..A4 differ only in system prompt; they must not collide on one entry."""
    a0 = make_request(system_prompt="rung A0")
    a1 = make_request(system_prompt="rung A1")
    record(store, a0, output=TraceOutput(text="A0 answer"))
    record(store, a1, output=TraceOutput(text="A1 answer"))
    adapter = ReplayAdapter(store)

    assert adapter.complete(a0).output.text == "A0 answer"
    assert adapter.complete(a1).output.text == "A1 answer"


# --------------------------------------------------------------------------
# mode resolution
# --------------------------------------------------------------------------


def test_replay_mode_resolves_every_model_without_credentials(models, store):
    for key in models.models:
        adapter = resolve(key, "replay", models=models, store=store)
        assert isinstance(adapter, ReplayAdapter)
        assert adapter.mode == "replay"


def test_hybrid_runs_gemini_live_and_replays_claude(models, store):
    gem = resolve(GEMINI, "hybrid", models=models, store=store)
    claude = resolve(CLAUDE, "hybrid", models=models, store=store)

    assert isinstance(gem, RecordingAdapter)
    assert isinstance(gem.inner, GeminiAdapter)
    assert gem.mode == "live"
    assert isinstance(claude, ReplayAdapter)


def test_hybrid_is_the_default_mode(models, store):
    assert DEFAULT_MODE == "hybrid"
    assert isinstance(resolve(CLAUDE, models=models, store=store), ReplayAdapter)


def test_hybrid_ignores_claude_path_and_still_replays(models, store):
    """Hybrid replays the baseline by definition; CLAUDE_PATH cannot override it."""
    adapter = resolve(
        CLAUDE, "hybrid", models=models, store=store, claude_path="vertex"
    )
    assert isinstance(adapter, ReplayAdapter)


def test_live_mode_honours_claude_path_replay(models, store):
    adapter = resolve(CLAUDE, "live", models=models, store=store, claude_path="replay")
    assert isinstance(adapter, ReplayAdapter)


def test_live_mode_reads_claude_path_from_env(models, store, monkeypatch):
    monkeypatch.setenv("CLAUDE_PATH", "replay")
    assert isinstance(resolve(CLAUDE, "live", models=models, store=store), ReplayAdapter)


def test_unknown_claude_path_is_a_config_error(models, store):
    with pytest.raises(ConfigError, match="CLAUDE_PATH"):
        resolve(CLAUDE, "live", models=models, store=store, claude_path="bedrock")


def test_unknown_mode_is_a_config_error(models, store):
    with pytest.raises(ConfigError, match="unknown mode"):
        resolve(GEMINI, "dry-run", models=models, store=store)


def test_unknown_model_key_is_a_config_error(models, store):
    with pytest.raises(ConfigError, match="unknown model key"):
        resolve("gemini-nano", "replay", models=models, store=store)


def test_resolution_never_touches_the_network_at_construction(models, store):
    """Resolving a live adapter must not build a Vertex client or need env vars."""
    adapter = resolve(GEMINI, "live", models=models, store=store)
    assert adapter.inner._client is None  # type: ignore[attr-defined]


def test_modes_tuple_matches_the_documented_set():
    assert set(MODES) == {"live", "replay", "hybrid"}


# --------------------------------------------------------------------------
# record-on-live
# --------------------------------------------------------------------------


class FakeLive(ModelAdapter):
    name = "fake"
    mode = "live"

    def __init__(self, trace_kwargs=None, raises: Exception | None = None):
        self.trace_kwargs = trace_kwargs or {"output": TraceOutput(text="fresh")}
        self.raises = raises
        self.calls = 0

    def complete(self, request: ModelRequest):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return build_trace(request, **self.trace_kwargs)


def test_record_on_live_makes_the_call_replayable(store):
    """The point of recording: today's live call is tomorrow's offline demo."""
    request = make_request()
    live = RecordingAdapter(FakeLive(), store=store)

    fresh = live.complete(request)
    replayed = ReplayAdapter(ReplayStore(store.root)).complete(request)

    assert replayed.to_jsonl_line() == fresh.to_jsonl_line()


def test_recording_appends_to_the_subagent_file(store):
    request = make_request(subagent="chunk_summarizer")
    RecordingAdapter(FakeLive(), store=store).complete(request)

    assert (store.root / "chunk_summarizer.jsonl").is_file()


def test_recording_captures_error_traces_too(store):
    """A dropped failure shrinks the denominator and flatters the failing model."""
    request = make_request()
    inner = FakeLive(
        trace_kwargs={
            "output": TraceOutput(),
            "status": "error",
            "error": "504 DEADLINE_EXCEEDED",
        }
    )

    trace = RecordingAdapter(inner, store=store).complete(request)

    assert trace.status == "error"
    assert ReplayStore(store.root).get(*request.replay_key).status == "error"


def test_recording_survives_an_adapter_that_raises(store):
    request = make_request()
    live = RecordingAdapter(FakeLive(raises=RuntimeError("boom")), store=store)

    with pytest.raises(RuntimeError):
        live.complete(request)

    recorded = ReplayStore(store.root).get(*request.replay_key)
    assert recorded.status == "error"
    assert "boom" in (recorded.error or "")


def test_recording_skips_calls_that_never_left_the_process(store, models):
    """A missing PROJECT_ID is not a model failure and must not pollute the corpus."""
    request = make_request()
    adapter = RecordingAdapter(GeminiAdapter(models=models), store=store)

    with pytest.raises(MissingEnvError):
        adapter.complete(request)

    assert not (store.root / f"{request.subagent}.jsonl").exists()


def test_recording_wrapper_has_no_off_switch():
    """Ground rule 5 is structural: nothing here accepts a 'record=False'."""
    import inspect as _inspect

    params = set(_inspect.signature(RecordingAdapter.__init__).parameters)
    assert params == {"self", "inner", "store"}
    assert not any(
        "record" in p for p in _inspect.signature(resolve).parameters
    )


def test_recording_wrapper_reports_the_inner_identity(store):
    live = RecordingAdapter(FakeLive(), store=store)
    assert (live.name, live.mode) == ("fake", "live")


# --------------------------------------------------------------------------
# GeminiAdapter — request translation
# --------------------------------------------------------------------------


def test_gemini_defaults_to_temperature_zero(models):
    client = StubClient([chunk(part(text="hi"))])
    gemini(models, client).complete(make_request())

    assert client.calls[0]["config"].temperature == DEFAULT_TEMPERATURE == 0.0


def test_gemini_honours_an_explicit_temperature(models):
    client = StubClient([chunk(part(text="hi"))])
    gemini(models, client).complete(make_request(temperature=0.7))

    assert client.calls[0]["config"].temperature == 0.7


def test_gemini_sends_the_system_prompt_as_system_instruction(models):
    client = StubClient([chunk(part(text="hi"))])
    request = make_request(system_prompt="You rewrite queries.")
    gemini(models, client).complete(request)

    instruction = client.calls[0]["config"].system_instruction
    assert "You rewrite queries." in str(instruction)


def test_gemini_resolves_the_model_id_from_config(models):
    client = StubClient([chunk(part(text="hi"))])
    gemini(models, client).complete(make_request())

    assert client.calls[0]["model"] == models.spec(GEMINI).id_for("vertex")


def test_gemini_sends_context_chunks_before_messages(models):
    client = StubClient([chunk(part(text="hi"))])
    gemini(models, client).complete(
        make_request(context_chunks=["chunk one", "chunk two"], messages=["ask"])
    )

    contents = client.calls[0]["contents"]
    assert [p.text for p in contents[0].parts] == ["chunk one", "chunk two"]
    assert [p.text for p in contents[1].parts] == ["ask"]


def test_gemini_omits_an_empty_context_block(models):
    client = StubClient([chunk(part(text="hi"))])
    gemini(models, client).complete(make_request(context_chunks=[]))

    assert len(client.calls[0]["contents"]) == 1


def test_gemini_declares_tools(models):
    client = StubClient([chunk(part(text="hi"))])
    tool = ToolSpec(
        name="emit_query_plan", description="Emit a plan.", parameters=PLAN_SCHEMA
    )
    gemini(models, client).complete(make_request(tools=[tool]))

    declared = client.calls[0]["config"].tools[0].function_declarations[0]
    assert declared.name == "emit_query_plan"
    assert declared.parameters is not None


def test_gemini_sends_a_strict_response_schema(models):
    client = StubClient([chunk(part(text="{}"))])
    gemini(models, client).complete(make_request(response_schema=PLAN_SCHEMA))

    config = client.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is not None


def test_gemini_rejects_tools_and_response_schema_together(models):
    client = StubClient([chunk(part(text="hi"))])
    request = make_request(
        tools=[ToolSpec(name="emit", parameters=PLAN_SCHEMA)],
        response_schema=PLAN_SCHEMA,
    )
    with pytest.raises(ConfigError, match="one structured-output mechanism"):
        gemini(models, client).complete(request)


def test_gemini_refuses_a_claude_model_key(models):
    with pytest.raises(ConfigError, match="not 'google'"):
        gemini(models, StubClient()).model_id(CLAUDE)


# --------------------------------------------------------------------------
# GeminiAdapter — response assembly
# --------------------------------------------------------------------------


def test_gemini_assembles_streamed_text(models):
    client = StubClient([chunk(part(text="Hello, ")), chunk(part(text="world."))])
    trace = gemini(models, client).complete(make_request())

    assert trace.status == "ok"
    assert trace.output.text == "Hello, world."
    assert trace.output.json_ is None


def test_gemini_skips_thinking_parts(models):
    client = StubClient(
        [chunk(part(text="scratch", thought=True), part(text="answer"))]
    )
    trace = gemini(models, client).complete(make_request())

    assert trace.output.text == "answer"


def test_gemini_captures_usage_and_folds_in_thinking_tokens(models):
    client = StubClient(
        [
            chunk(
                part(text="hi"),
                usage=usage_meta(prompt=812, candidates=196, cached=64, thoughts=30),
            )
        ]
    )
    trace = gemini(models, client).complete(make_request())

    assert trace.usage.input_tokens == 812
    assert trace.usage.output_tokens == 226  # candidates + thoughts (both billed out)
    assert trace.usage.cached_tokens == 64


def test_gemini_captures_ttft_and_total_latency(models):
    client = StubClient([chunk(part(text="a")), chunk(part(text="b"))])
    trace = gemini(models, client).complete(make_request())

    assert trace.latency_ms.ttft is not None
    assert trace.latency_ms.total is not None
    assert trace.latency_ms.ttft <= trace.latency_ms.total


def test_gemini_records_a_tool_call_as_structured_output(models):
    args = {"queries": ["ceramic separator prior art"]}
    call = SimpleNamespace(name="emit_query_plan", args=args)
    client = StubClient([chunk(part(function_call=call))])

    trace = gemini(models, client).complete(
        make_request(tools=[ToolSpec(name="emit_query_plan", parameters=PLAN_SCHEMA)])
    )

    assert trace.tool_calls == [ToolCall(name="emit_query_plan", args=args)]
    assert trace.output.json_ == args


def test_gemini_keeps_every_tool_call_when_there_are_several(models):
    calls = [
        SimpleNamespace(name="emit", args={"i": 1}),
        SimpleNamespace(name="emit", args={"i": 2}),
    ]
    client = StubClient([chunk(*(part(function_call=c) for c in calls))])

    trace = gemini(models, client).complete(
        make_request(tools=[ToolSpec(name="emit", parameters=PLAN_SCHEMA)])
    )

    assert len(trace.tool_calls) == 2
    assert trace.output.json_ == [
        {"name": "emit", "args": {"i": 1}},
        {"name": "emit", "args": {"i": 2}},
    ]


def test_gemini_parses_schema_constrained_json(models):
    payload = {"queries": ["a", "b"]}
    client = StubClient([chunk(part(text=json.dumps(payload)))])

    trace = gemini(models, client).complete(make_request(response_schema=PLAN_SCHEMA))

    assert trace.output.json_ == payload
    assert trace.output.text is None


@pytest.mark.parametrize("body", ["Sure! Here you go: {oops", "42", ""])
def test_gemini_records_non_object_structured_output_as_text(models, body):
    """Valid-JSON scalars are as much a schema violation as broken syntax."""
    client = StubClient([chunk(part(text=body))])

    trace = gemini(models, client).complete(make_request(response_schema=PLAN_SCHEMA))

    assert trace.output.json_ is None
    assert trace.output.text == (body or None)


def test_gemini_records_unparseable_structured_output_as_text(models):
    """Schema violations are a quality signal; repairing them would hide it."""
    client = StubClient([chunk(part(text="Sure! Here you go: {oops"))])

    trace = gemini(models, client).complete(make_request(response_schema=PLAN_SCHEMA))

    assert trace.output.json_ is None
    assert trace.output.text == "Sure! Here you go: {oops"
    assert trace.status == "ok"  # the API call succeeded; the model did not comply


# --------------------------------------------------------------------------
# GeminiAdapter — failure handling
# --------------------------------------------------------------------------


def test_gemini_retries_twice_then_records_an_error_trace(models):
    client = StubClient(error=RuntimeError("504 DEADLINE_EXCEEDED"))

    trace = gemini(models, client).complete(make_request())

    assert len(client.calls) == 3  # one attempt + two retries
    assert trace.status == "error"
    assert "DEADLINE_EXCEEDED" in (trace.error or "")
    assert trace.output.text is None
    assert trace.latency_ms.total is not None


def test_gemini_error_does_not_raise_and_is_recorded(store, models):
    """One flaky call must not kill an eval run — but it must be recorded."""
    request = make_request()
    adapter = RecordingAdapter(
        gemini(models, StubClient(error=RuntimeError("boom"))), store=store
    )

    trace = adapter.complete(request)

    assert trace.status == "error"
    assert ReplayStore(store.root).get(*request.replay_key).status == "error"


def test_gemini_recovers_on_a_retry(models):
    class Flaky(StubClient):
        def _stream(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError("transient")
            return iter([chunk(part(text="second time lucky"))])

    client = Flaky()
    trace = gemini(models, client).complete(make_request())

    assert trace.status == "ok"
    assert trace.output.text == "second time lucky"


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------


def test_live_gemini_without_project_id_names_the_missing_var(models):
    adapter = GeminiAdapter(models=models, sleep=lambda _: None)

    with pytest.raises(MissingEnvError, match="PROJECT_ID"):
        adapter.complete(make_request())


def test_live_gemini_without_region_names_the_missing_var(models, monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "some-project")
    adapter = GeminiAdapter(models=models, sleep=lambda _: None)

    with pytest.raises(MissingEnvError, match="REGION"):
        adapter.complete(make_request())


def test_importing_adapters_never_imports_the_google_sdk():
    """The replay path must work with google-genai absent or unconfigured."""
    code = (
        "import sys; import amw.adapters; "
        "print(any(m == 'google.genai' or m.startswith('google.genai.') "
        "for m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False", result.stdout


# --------------------------------------------------------------------------
# AdapterRouter
# --------------------------------------------------------------------------


def test_router_replays_claude_and_records_gemini(models, store):
    router = AdapterRouter("hybrid", models=models, store=store)

    assert isinstance(router.for_model(CLAUDE), ReplayAdapter)
    assert isinstance(router.for_model(GEMINI), RecordingAdapter)


def test_router_reuses_one_adapter_per_model(models, store):
    router = AdapterRouter("replay", models=models, store=store)
    assert router.for_model(GEMINI) is router.for_model(GEMINI)


def test_router_completes_through_the_replay_store(store, models):
    request = make_request()
    record(store, request)

    router = AdapterRouter("replay", models=models, store=store)

    assert router.complete(request).output.text == "recorded answer"


def test_router_describes_which_models_are_live(models, store):
    summary = AdapterRouter("hybrid", models=models, store=store).describe()

    assert summary[GEMINI] == "live via gemini"
    assert summary[CLAUDE] == "replay via replay"


def test_router_reports_the_recording_window(store, models):
    stamp = datetime(2026, 8, 3, 10, 22, 41, tzinfo=timezone.utc)
    record(store, make_request(), ts=stamp)

    router = AdapterRouter("replay", models=models, store=store)
    assert router.recording_window() == (stamp, stamp)


def test_golden_fixture_corpus_is_replayable(tmp_path):
    """The checked-in corpus must load and serve through the adapter unchanged."""
    from amw.traces.schema import read_jsonl

    fixture = REPO_ROOT / "tests" / "fixtures" / "traces" / "sample_traces.jsonl"
    store = ReplayStore(tmp_path / "replay")
    for trace in read_jsonl(fixture):
        store.append(trace)

    adapter = ReplayAdapter(ReplayStore(tmp_path / "replay"))
    for trace in read_jsonl(fixture):
        served = adapter.store.get_trace(trace)
        assert served.to_jsonl_line() == trace.to_jsonl_line()
    assert adapter.recording_window() is not None
