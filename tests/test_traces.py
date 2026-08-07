"""T02 — canonical trace schema + replay store.

Two things must hold, and everything else in the workbench depends on them:

* a recorded corpus round-trips **byte-stably**, so replay serves back exactly
  what was recorded and a schema change shows up as a diff;
* a replay miss is **loud**, so an offline run can never substitute an invented
  response for a call that was never made.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from amw.traces import (
    LatencyMs,
    ReplayMissError,
    ReplayStore,
    ToolCall,
    Trace,
    TraceInput,
    TraceOutput,
    Usage,
    append_jsonl,
    compute_input_sha,
    read_jsonl,
    sha256_text,
    write_jsonl,
)

FIXTURE = Path(__file__).parent / "fixtures" / "traces" / "sample_traces.jsonl"


@pytest.fixture(scope="module")
def fixture_traces() -> list[Trace]:
    return read_jsonl(FIXTURE)


def make_trace(**overrides) -> Trace:
    base = dict(
        trace_id="t-1",
        subagent="query_rewriter",
        provenance="synthetic",
        ts=datetime(2026, 8, 3, 10, 22, 41, tzinfo=timezone.utc),
        model="gemini-flash",
        system_prompt_sha=sha256_text("system prompt A"),
        input=TraceInput(messages=["hello"], context_chunks=["chunk"]),
        tools_offered=["emit_query_plan"],
        output=TraceOutput(text="hi"),
    )
    base.update(overrides)
    return Trace(**base)


# --------------------------------------------------------------------------
# schema shape
# --------------------------------------------------------------------------


def test_fixture_parses(fixture_traces: list[Trace]) -> None:
    assert len(fixture_traces) == 3
    assert [t.subagent for t in fixture_traces] == [
        "query_rewriter",
        "chunk_summarizer",
        "feature_extractor",
    ]


def test_wire_shape_matches_master_plan(fixture_traces: list[Trace]) -> None:
    # docs/master_plan.md §5.9 is the contract Act 2's converters will target.
    record = json.loads(fixture_traces[0].to_jsonl_line())
    assert set(record) == {
        "trace_id",
        "subagent",
        "provenance",
        "ts",
        "model",
        "system_prompt_sha",
        "input",
        "tools_offered",
        "tool_calls",
        "output",
        "usage",
        "latency_ms",
        "status",
        "error",
    }
    assert set(record["input"]) == {"messages", "context_chunks"}
    # the wire name is `json`, not the Python-side `json_`
    assert set(record["output"]) == {"text", "json"}
    assert set(record["usage"]) == {"input_tokens", "output_tokens", "cached_tokens"}
    assert set(record["latency_ms"]) == {"ttft", "total"}
    assert record["ts"] == "2026-08-03T10:22:41Z"


def test_output_json_alias_round_trips() -> None:
    trace = make_trace(output=TraceOutput(json={"a": 1}))
    assert '"json":{"a":1}' in trace.to_jsonl_line()
    assert Trace.from_jsonl_line(trace.to_jsonl_line()).output.json_ == {"a": 1}


def test_error_trace_is_recorded_not_dropped(fixture_traces: list[Trace]) -> None:
    # A failed live call becomes a status:"error" trace so a flaky response
    # cannot silently shrink an eval's denominator (CLAUDE.md conventions).
    failed = fixture_traces[2]
    assert failed.status == "error"
    assert "DEADLINE_EXCEEDED" in failed.error
    assert failed.usage.output_tokens == 0


def test_unknown_field_is_rejected() -> None:
    line = fixture_traces_line = FIXTURE.read_text(encoding="utf-8").splitlines()[0]
    record = json.loads(line)
    record["temperature"] = 0.7
    with pytest.raises(Exception, match="temperature"):
        Trace.from_jsonl_line(json.dumps(record))
    assert fixture_traces_line  # unmodified original still parses
    Trace.from_jsonl_line(line)


def test_bad_line_names_file_and_line_number(tmp_path: Path) -> None:
    path = tmp_path / "broken.jsonl"
    good = FIXTURE.read_text(encoding="utf-8").splitlines()[0]
    path.write_text(f"{good}\n{{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"broken\.jsonl:2"):
        read_jsonl(path)


# --------------------------------------------------------------------------
# byte-stable round trip
# --------------------------------------------------------------------------


def test_fixture_round_trips_byte_stable(tmp_path: Path) -> None:
    original = FIXTURE.read_bytes()
    out = write_jsonl(tmp_path / "out.jsonl", read_jsonl(FIXTURE))
    assert out.read_bytes() == original


def test_round_trip_is_idempotent(tmp_path: Path) -> None:
    first = write_jsonl(tmp_path / "a.jsonl", read_jsonl(FIXTURE)).read_bytes()
    second = write_jsonl(tmp_path / "b.jsonl", read_jsonl(tmp_path / "a.jsonl"))
    assert second.read_bytes() == first


def test_serialization_is_stable_across_identical_objects() -> None:
    assert make_trace().to_jsonl_line() == make_trace().to_jsonl_line()


def test_unicode_is_preserved_not_escaped(fixture_traces: list[Trace]) -> None:
    line = fixture_traces[0].to_jsonl_line()
    assert "—" in line and "\\u2014" not in line


def test_blank_lines_are_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "padded.jsonl"
    path.write_text(
        "\n" + FIXTURE.read_text(encoding="utf-8") + "\n\n", encoding="utf-8"
    )
    assert len(read_jsonl(path)) == 3


def test_append_jsonl_grows_the_file(tmp_path: Path) -> None:
    path = tmp_path / "grow.jsonl"
    append_jsonl(path, make_trace(trace_id="t-1"))
    append_jsonl(path, make_trace(trace_id="t-2"))
    assert [t.trace_id for t in read_jsonl(path)] == ["t-1", "t-2"]


# --------------------------------------------------------------------------
# the replay key
# --------------------------------------------------------------------------


def test_key_is_subagent_model_input_sha() -> None:
    trace = make_trace()
    assert trace.key == ("query_rewriter", "gemini-flash", trace.input_sha)


def test_input_sha_is_deterministic() -> None:
    assert make_trace().input_sha == make_trace().input_sha


def test_same_input_different_system_prompt_gives_different_key() -> None:
    # This is the ablation ladder's correctness requirement: A0..A4 send the
    # SAME dataset item with different system prompts. If the key ignored the
    # prompt, every rung would collide onto one cache entry and replay would
    # serve the wrong rung's output.
    a = make_trace(system_prompt_sha=sha256_text("system prompt A"))
    b = make_trace(system_prompt_sha=sha256_text("system prompt B"))
    assert a.input_sha != b.input_sha


def test_different_tools_offered_gives_different_key() -> None:
    # The A2 rung's entire change is offering a strict-schema tool.
    a = make_trace(tools_offered=[])
    b = make_trace(tools_offered=["emit_query_plan"])
    assert a.input_sha != b.input_sha


def test_tool_order_does_not_change_the_key() -> None:
    a = make_trace(tools_offered=["a", "b"])
    b = make_trace(tools_offered=["b", "a"])
    assert a.input_sha == b.input_sha


def test_messages_and_context_affect_the_key() -> None:
    base = make_trace()
    assert base.input_sha != make_trace(
        input=TraceInput(messages=["different"], context_chunks=["chunk"])
    ).input_sha
    assert base.input_sha != make_trace(
        input=TraceInput(messages=["hello"], context_chunks=["other chunk"])
    ).input_sha


def test_compute_input_sha_matches_the_trace_property() -> None:
    # Adapters compute the key BEFORE calling, to decide hit vs. miss.
    trace = make_trace()
    assert (
        compute_input_sha(
            system_prompt_sha=trace.system_prompt_sha,
            messages=trace.input.messages,
            context_chunks=trace.input.context_chunks,
            tools_offered=trace.tools_offered,
        )
        == trace.input_sha
    )


def test_output_does_not_affect_the_key() -> None:
    a = make_trace(output=TraceOutput(text="one"))
    b = make_trace(
        output=TraceOutput(text="two"),
        usage=Usage(input_tokens=9),
        latency_ms=LatencyMs(total=5),
        tool_calls=[ToolCall(name="x")],
    )
    assert a.input_sha == b.input_sha


# --------------------------------------------------------------------------
# ReplayStore
# --------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, fixture_traces: list[Trace]) -> ReplayStore:
    st = ReplayStore(tmp_path / "replay")
    for trace in fixture_traces:
        st.append(trace)
    return st


def test_append_then_get_is_a_hit(store: ReplayStore, fixture_traces) -> None:
    for original in fixture_traces:
        assert store.get(*original.key).to_jsonl_line() == original.to_jsonl_line()


def test_hit_survives_a_reload_from_disk(store: ReplayStore, fixture_traces) -> None:
    # The corpus, not the process, is the source of truth.
    fresh = ReplayStore(store.root)
    assert fresh.get(*fixture_traces[0].key).trace_id == "fixture-qr-000001"
    assert len(fresh) == 3
    assert fresh.subagents() == [
        "chunk_summarizer",
        "feature_extractor",
        "query_rewriter",
    ]


def test_append_writes_one_file_per_subagent(store: ReplayStore) -> None:
    assert (store.root / "query_rewriter.jsonl").is_file()
    assert store.path_for("chunk_summarizer").read_text().count("\n") == 1


def test_miss_raises_and_names_the_key(store: ReplayStore) -> None:
    with pytest.raises(ReplayMissError) as exc:
        store.get("query_rewriter", "gemini-flash", "deadbeefdeadbeef")
    message = str(exc.value)
    assert "query_rewriter" in message
    assert "gemini-flash" in message
    assert "deadbeefdeadbeef" in message
    assert exc.value.input_sha == "deadbeefdeadbeef"


def test_miss_on_empty_store_says_so(tmp_path: Path) -> None:
    empty = ReplayStore(tmp_path / "replay")
    with pytest.raises(ReplayMissError, match="no traces recorded"):
        empty.get("query_rewriter", "gemini-flash", "abc")


def test_miss_on_unknown_model_lists_recorded_models(store: ReplayStore) -> None:
    with pytest.raises(ReplayMissError, match=r"recorded models: \['claude-sonnet'\]"):
        store.get("query_rewriter", "claude-opus", "abc")


def test_miss_on_unknown_input_says_to_re_record(
    store: ReplayStore, fixture_traces
) -> None:
    subagent, model, _ = fixture_traces[0].key
    with pytest.raises(ReplayMissError, match="Re-record with --mode live"):
        store.get(subagent, model, "0000000000000000")


def test_has_and_get_trace(store: ReplayStore, fixture_traces) -> None:
    original = fixture_traces[1]
    assert store.has(*original.key)
    assert not store.has("chunk_summarizer", "gemini-pro", original.input_sha)
    assert store.get_trace(original).trace_id == original.trace_id


def test_re_recording_supersedes_the_earlier_trace(
    store: ReplayStore, fixture_traces
) -> None:
    original = fixture_traces[0]
    corrected = original.model_copy(update={"trace_id": "fixture-qr-000001-rerun"})
    store.append(corrected)

    assert store.get(*original.key).trace_id == "fixture-qr-000001-rerun"
    # both lines are still on disk: the corpus is append-only evidence
    assert len(read_jsonl(store.path_for("query_rewriter"))) == 2
    assert ReplayStore(store.root).get(*original.key).trace_id.endswith("-rerun")


def test_recording_window_reports_the_corpus_dates(store: ReplayStore) -> None:
    # Ground rule 1: replay must label itself on screen with the recording date.
    window = store.recording_window()
    assert window == (
        datetime(2026, 8, 3, 10, 22, 41, tzinfo=timezone.utc),
        datetime(2026, 8, 3, 10, 25, 55, tzinfo=timezone.utc),
    )
    assert store.recording_window("chunk_summarizer")[0].minute == 24


def test_recording_window_is_none_when_empty(tmp_path: Path) -> None:
    assert ReplayStore(tmp_path / "replay").recording_window() is None


def test_traces_can_be_filtered_by_subagent(store: ReplayStore) -> None:
    assert len(store.traces("query_rewriter")) == 1
    assert len(list(store)) == 3


def test_subagent_name_cannot_escape_the_store_dir(store: ReplayStore) -> None:
    with pytest.raises(ValueError, match="invalid subagent name"):
        store.path_for("../../etc/passwd")


def test_reload_picks_up_out_of_band_writes(store: ReplayStore) -> None:
    extra = make_trace(trace_id="t-out-of-band", subagent="query_rewriter")
    append_jsonl(store.path_for("query_rewriter"), extra)
    with pytest.raises(ReplayMissError):
        store.get(*extra.key)
    store.reload()
    assert store.get(*extra.key).trace_id == "t-out-of-band"
