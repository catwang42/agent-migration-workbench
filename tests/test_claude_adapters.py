"""Offline tests for the two Claude adapters (T04).

Everything here runs with no credentials and no network: a fake client is
injected through the adapters' ``client=`` constructor argument, so the real
``anthropic`` SDK is never constructed and ``_build_client`` is never reached
except in the tests that deliberately assert its error messages.

What is pinned down:

* prompt pass-through is byte-exact (the Claude prompt IS the baseline);
* ``temperature`` / ``top_p`` / ``top_k`` / ``thinking`` never reach the wire;
* model IDs come from config/models.yaml, per access path;
* usage, TTFT and total latency mapping;
* structured output and tool-call capture;
* retry x2 then a ``status:"error"`` trace instead of an exception;
* the resulting Trace round-trips byte-stably through JSONL.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from amw.adapters.base import ModelRequest, ToolSpec
from amw.adapters.claude_anthropic import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    ClaudeAnthropicAdapter,
)
from amw.adapters.claude_vertex import ClaudeVertexAdapter
from amw.adapters.base import AdapterError
from amw.config import ConfigError, load_all
from amw.traces.schema import Trace

REPO_ROOT = Path(__file__).resolve().parent.parent

CLAUDE_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT",
    "REGION",
    # ClaudeVertexAdapter reads this one *first* (Claude runs in `global` while
    # Gemini runs in us-central1). Leaving it out meant these tests only saw a
    # missing region because nothing in the process happened to set it — an
    # ambient CLAUDE_REGION, exported or loaded from .env, silently turned the
    # "reports both missing" assertions into "reports one missing".
    "CLAUDE_REGION",
    "CLOUD_ML_REGION",
)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_block(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=args)


def thinking_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking=text)


def fake_message(
    blocks,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=list(blocks),
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
        ),
    )


DEFAULT_EVENTS = (
    SimpleNamespace(type="message_start"),
    SimpleNamespace(type="content_block_start"),
    SimpleNamespace(type="content_block_delta"),
    SimpleNamespace(type="message_stop"),
)


class FakeStream:
    def __init__(self, events, message):
        self._events = list(events)
        self._message = message
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.closed = True
        return False

    def __iter__(self):
        return iter(self._events)

    def get_final_message(self):
        return self._message


class FakeMessages:
    """Records every kwargs dict and replays a scripted list of outcomes.

    An outcome is either a message object (returned) or an Exception instance
    (raised, as the real SDK would on an API failure). The final outcome repeats
    if the adapter calls more times than there are outcomes.
    """

    def __init__(self, outcomes, events=DEFAULT_EVENTS):
        self._outcomes = list(outcomes)
        self._events = events
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        outcome = (
            self._outcomes.pop(0) if len(self._outcomes) > 1 else self._outcomes[0]
        )
        if isinstance(outcome, BaseException):
            raise outcome
        return FakeStream(self._events, outcome)


class FakeClient:
    def __init__(self, outcomes, events=DEFAULT_EVENTS):
        self.messages = FakeMessages(outcomes, events)


class RecordingSleep:
    def __init__(self):
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def models():
    """The real config/models.yaml, so model-ID drift shows up as a failure."""
    return load_all().models


SYSTEM_PROMPT = (
    "<role>\n  You rewrite patent search queries.\n</role>\n"
    "<rules>\n  - Keep CPC codes verbatim.\n  -   Preserve   odd   spacing.\n"
    "</rules>\n"
)


def make_request(**overrides) -> ModelRequest:
    kwargs = dict(
        subagent="query_rewriter",
        model="claude-sonnet",
        system_prompt=SYSTEM_PROMPT,
        messages=["<query>fuel cell membrane</query>"],
        item_id="qr-000123",
    )
    kwargs.update(overrides)
    return ModelRequest(**kwargs)


def vertex(models, client, **kwargs) -> ClaudeVertexAdapter:
    return ClaudeVertexAdapter(
        models, client=client, project_id="p", region="r", **kwargs
    )


def direct(models, client, **kwargs) -> ClaudeAnthropicAdapter:
    return ClaudeAnthropicAdapter(models, client=client, api_key="k", **kwargs)


# --------------------------------------------------------------------------
# zero-credential import (ground rule 4)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relpath",
    ["amw/adapters/claude_vertex.py", "amw/adapters/claude_anthropic.py"],
)
def test_sdk_is_not_imported_at_module_scope(relpath):
    """`import amw.adapters.claude_*` must not need the SDK, creds, or network."""
    tree = ast.parse((REPO_ROOT / relpath).read_text(encoding="utf-8"))
    offenders = []
    for node in tree.body:  # module scope only
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "anthropic"]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "anthropic":
                offenders.append(node.module)
    assert offenders == [], f"{relpath} imports the SDK at module scope: {offenders}"


def test_construction_needs_no_credentials(models, monkeypatch):
    for var in CLAUDE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    client = FakeClient([fake_message([text_block("ok")])])
    assert vertex(models, client).name == "claude_vertex"
    assert direct(models, client).name == "claude_anthropic"


def test_adapter_identity(models):
    client = FakeClient([fake_message([text_block("ok")])])
    for adapter, name in (
        (vertex(models, client), "claude_vertex"),
        (direct(models, client), "claude_anthropic"),
    ):
        assert adapter.name == name
        assert adapter.mode == "live"


# --------------------------------------------------------------------------
# request translation
# --------------------------------------------------------------------------


def test_system_prompt_and_messages_pass_through_verbatim(models):
    client = FakeClient([fake_message([text_block("ok")])])
    request = make_request(messages=["  first  ", "second\n"])
    vertex(models, client).complete(request)

    sent = client.messages.calls[0]
    assert sent["system"] == SYSTEM_PROMPT  # byte-for-byte, no normalisation
    # One user turn, one text block per message — mirroring Gemini's one
    # Content / one Part per message. See test_adapter_parity.py.
    assert sent["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "  first  "},
                {"type": "text", "text": "second\n"},
            ],
        },
    ]


def test_context_chunks_precede_turns_with_no_added_wording(models):
    client = FakeClient([fake_message([text_block("ok")])])
    request = make_request(messages=["Q"], context_chunks=["chunk A", "chunk B"])
    vertex(models, client).complete(request)

    assert client.messages.calls[0]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "chunk A"},
                {"type": "text", "text": "chunk B"},
            ],
        },
        {"role": "user", "content": [{"type": "text", "text": "Q"}]},
    ]


def test_sampling_params_are_never_forwarded(models):
    """Current-generation Claude models reject temperature/top_p/top_k with 400."""
    client = FakeClient([fake_message([text_block("ok")])])
    vertex(models, client).complete(make_request(temperature=0.0))

    sent = client.messages.calls[0]
    for forbidden in ("temperature", "top_p", "top_k", "thinking"):
        assert forbidden not in sent


def test_model_id_resolves_per_access_path(models):
    request = make_request()
    expected_vertex = models.spec("claude-sonnet").id_for("vertex")
    expected_direct = models.spec("claude-sonnet").id_for("anthropic")

    vc = FakeClient([fake_message([text_block("ok")])])
    vertex(models, vc).complete(request)
    assert vc.messages.calls[0]["model"] == expected_vertex
    # No Bedrock-style vendor prefix on Vertex.
    assert not vc.messages.calls[0]["model"].startswith("anthropic.")

    dc = FakeClient([fake_message([text_block("ok")])])
    direct(models, dc).complete(request)
    assert dc.messages.calls[0]["model"] == expected_direct


def test_unknown_model_key_raises_rather_than_recording_an_error_trace(models):
    client = FakeClient([fake_message([text_block("ok")])])
    with pytest.raises(ConfigError):
        vertex(models, client).complete(make_request(model="claude-nope"))
    assert client.messages.calls == []


def test_max_tokens_default_and_override(models):
    client = FakeClient([fake_message([text_block("ok")])])
    adapter = vertex(models, client)
    adapter.complete(make_request())
    assert client.messages.calls[0]["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS

    adapter.complete(make_request(max_output_tokens=321))
    assert client.messages.calls[1]["max_tokens"] == 321


def test_tools_are_translated_to_input_schema(models):
    client = FakeClient([fake_message([text_block("ok")])])
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    request = make_request(
        tools=[ToolSpec(name="emit_query_plan", description="d", parameters=schema)]
    )
    vertex(models, client).complete(request)

    assert client.messages.calls[0]["tools"] == [
        {"name": "emit_query_plan", "description": "d", "input_schema": schema}
    ]


def test_openapi_nullable_becomes_a_json_schema_type_union(models):
    """`nullable` is OpenAPI, not JSON Schema. Claude ignores unknown keywords,
    so an untranslated schema leaves the model no way to say "not stated" — it
    writes the *string* "null", which scores as invalid JSON and as fabrication.
    That cost 4 of 10 Feature Extractor items on the 2026-08-09 phase-2 run.
    """
    from amw.agents.schemas import json_schema

    client = FakeClient([fake_message([text_block("ok")])])
    request = make_request(
        tools=[
            ToolSpec(
                name="emit_features",
                description="d",
                parameters=json_schema("feature_extractor"),
            )
        ]
    )
    vertex(models, client).complete(request)

    sent = client.messages.calls[0]["tools"][0]["input_schema"]
    assert "nullable" not in json.dumps(sent)
    props = sent["properties"]
    assert props["independent_claim_count"]["type"] == ["integer", "null"]
    assert props["title"]["type"] == ["string", "null"]
    # a non-nullable field is left exactly as it was
    assert props["cpc_codes"] == {"items": {"type": "string"}, "type": "array"}
    # descriptions and every other keyword survive the rewrite
    assert props["filing_date"]["description"] == "YYYY-MM-DD if stated."


def test_nullable_translation_leaves_a_plain_schema_untouched(models):
    client = FakeClient([fake_message([text_block("ok")])])
    schema = {"type": "object", "properties": {"q": {"type": "string"}}}
    request = make_request(tools=[ToolSpec(name="t", description="d", parameters=schema)])
    vertex(models, client).complete(request)
    assert client.messages.calls[0]["tools"][0]["input_schema"] == schema


def test_no_tools_key_when_no_tools_offered(models):
    client = FakeClient([fake_message([text_block("ok")])])
    vertex(models, client).complete(make_request())
    assert "tools" not in client.messages.calls[0]
    assert "output_config" not in client.messages.calls[0]


# --------------------------------------------------------------------------
# response translation
# --------------------------------------------------------------------------


def test_usage_and_latency_are_captured(models):
    client = FakeClient(
        [
            fake_message(
                [text_block("hello")],
                input_tokens=812,
                output_tokens=196,
                cache_read=64,
                cache_creation=128,
            )
        ]
    )
    trace = vertex(models, client).complete(make_request())

    assert trace.status == "ok"
    assert trace.usage.input_tokens == 812
    assert trace.usage.output_tokens == 196
    assert trace.usage.cached_tokens == 64  # cache_read_input_tokens
    assert trace.latency_ms.ttft is not None
    assert trace.latency_ms.total is not None
    assert trace.latency_ms.total >= trace.latency_ms.ttft


def test_ttft_is_none_when_no_content_event_arrives(models):
    client = FakeClient(
        [fake_message([text_block("hi")])],
        events=(SimpleNamespace(type="message_start"),),
    )
    trace = vertex(models, client).complete(make_request())
    assert trace.latency_ms.ttft is None
    assert trace.latency_ms.total is not None


def test_plain_text_response(models):
    client = FakeClient([fake_message([thinking_block("hmm"), text_block("answer")])])
    trace = vertex(models, client).complete(make_request())
    assert trace.output.text == "answer"  # thinking blocks are not part of output
    assert trace.output.json_ is None
    assert trace.tool_calls == []


def test_structured_output_uses_output_config_and_parses_json(models):
    payload = {"terms": ["fuel cell"], "cpc": ["H01M8/00"]}
    client = FakeClient([fake_message([text_block(json.dumps(payload))])])
    schema = {"type": "object", "properties": {"terms": {"type": "array"}}}
    trace = vertex(models, client).complete(make_request(response_schema=schema))

    sent = client.messages.calls[0]
    assert sent["output_config"] == {
        "format": {"type": "json_schema", "schema": schema}
    }
    # No synthetic tool injected: tools_offered / input_sha stay faithful.
    assert "tools" not in sent
    assert trace.tools_offered == []
    assert trace.output.json_ == payload


def test_unparseable_structured_output_is_not_fabricated(models):
    client = FakeClient([fake_message([text_block("not json at all")])])
    trace = vertex(models, client).complete(
        make_request(response_schema={"type": "object"})
    )
    assert trace.output.json_ is None
    assert trace.output.text == "not json at all"
    assert trace.status == "ok"


def test_tool_calls_are_captured_and_mirrored_into_output_json(models):
    args = {"terms": ["membrane"], "top_k": 5}
    client = FakeClient([fake_message([tool_block("emit_query_plan", args)])])
    request = make_request(tools=[ToolSpec(name="emit_query_plan")])
    trace = vertex(models, client).complete(request)

    assert [(c.name, c.args) for c in trace.tool_calls] == [("emit_query_plan", args)]
    assert trace.output.json_ == args
    assert trace.tools_offered == ["emit_query_plan"]


def test_multiple_tool_calls_do_not_populate_output_json(models):
    client = FakeClient(
        [fake_message([tool_block("a", {"x": 1}), tool_block("b", {"y": 2})])]
    )
    trace = vertex(models, client).complete(
        make_request(tools=[ToolSpec(name="a"), ToolSpec(name="b")])
    )
    assert len(trace.tool_calls) == 2
    assert trace.output.json_ is None


# --------------------------------------------------------------------------
# retry / error semantics
# --------------------------------------------------------------------------


def test_retries_twice_then_records_an_error_trace(models):
    boom = RuntimeError("503 upstream unavailable")
    client = FakeClient([boom])
    sleeper = RecordingSleep()
    trace = vertex(models, client, sleep=sleeper).complete(make_request())

    assert len(client.messages.calls) == 3  # one attempt plus two retries
    assert sleeper.delays == [1.0, 2.0]  # exponential backoff
    assert trace.status == "error"
    assert trace.error == "RuntimeError: 503 upstream unavailable"
    assert trace.output.text is None and trace.output.json_ is None
    assert trace.usage.input_tokens == 0
    assert trace.latency_ms.total is not None


def test_a_transient_failure_still_yields_an_ok_trace(models):
    client = FakeClient(
        [RuntimeError("flaky"), RuntimeError("flaky"), fake_message([text_block("ok")])]
    )
    trace = vertex(models, client, sleep=RecordingSleep()).complete(make_request())
    assert len(client.messages.calls) == 3
    assert trace.status == "ok"
    assert trace.output.text == "ok"


def test_complete_many_continues_past_a_failure(models):
    client = FakeClient([RuntimeError("down")])
    traces = vertex(models, client, sleep=RecordingSleep()).complete_many(
        [make_request(item_id="a"), make_request(item_id="b")]
    )
    assert [t.trace_id for t in traces] == ["a", "b"]
    assert [t.status for t in traces] == ["error", "error"]


# --------------------------------------------------------------------------
# trace shape / round trip
# --------------------------------------------------------------------------


def test_trace_round_trips_byte_stably(models):
    client = FakeClient(
        [
            fake_message(
                [tool_block("emit_query_plan", {"terms": ["a"]})],
                input_tokens=10,
                output_tokens=20,
                cache_read=5,
            )
        ]
    )
    trace = vertex(models, client).complete(
        make_request(
            context_chunks=["c1"], tools=[ToolSpec(name="emit_query_plan")]
        )
    )

    line = trace.to_jsonl_line()
    assert Trace.from_jsonl_line(line).to_jsonl_line() == line
    assert json.loads(line)["output"]["json"] == {"terms": ["a"]}  # aliased key


def test_trace_key_matches_the_request_replay_key(models):
    client = FakeClient([fake_message([text_block("ok")])])
    request = make_request(context_chunks=["c1"], tools=[ToolSpec(name="t")])
    trace = vertex(models, client).complete(request)
    assert trace.key == request.replay_key


def test_both_paths_produce_identical_traces_apart_from_transport(models):
    request = make_request(context_chunks=["c1"])
    message = fake_message([text_block("same")], input_tokens=7, output_tokens=8)

    vc = FakeClient([message])
    dc = FakeClient([message])
    v_trace = vertex(models, vc).complete(request)
    d_trace = direct(models, dc).complete(request)

    # Only the resolved provider model ID may differ between the two paths.
    v_sent = dict(vc.messages.calls[0])
    d_sent = dict(dc.messages.calls[0])
    v_sent.pop("model")
    d_sent.pop("model")
    assert v_sent == d_sent

    def normalise(trace):
        data = json.loads(trace.to_jsonl_line())
        data.pop("ts")
        data.pop("latency_ms")
        return data

    assert normalise(v_trace) == normalise(d_trace)


# --------------------------------------------------------------------------
# credential errors (no client injected, still no network)
# --------------------------------------------------------------------------


def test_vertex_reports_missing_project_and_region(models, monkeypatch):
    for var in CLAUDE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(AdapterError) as excinfo:
        ClaudeVertexAdapter(models)
    message = str(excinfo.value)
    assert "PROJECT_ID" in message and "REGION" in message
    assert "application-default login" in message


def test_vertex_reports_only_the_missing_one(models, monkeypatch):
    for var in CLAUDE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PROJECT_ID", "some-project")
    with pytest.raises(AdapterError) as excinfo:
        ClaudeVertexAdapter(models)
    assert "REGION" in str(excinfo.value)
    assert "PROJECT_ID" not in str(excinfo.value)


def test_direct_path_reports_missing_api_key(models, monkeypatch):
    for var in CLAUDE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(AdapterError) as excinfo:
        ClaudeAnthropicAdapter(models)
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)
