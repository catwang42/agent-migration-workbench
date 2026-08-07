"""Cross-lane check: Gemini and Claude must encode a request the same way.

The shadow comparison sets one number against another and calls the difference
a model difference. That inference is only valid if the two adapters put the
*same prompt* on the wire. If Gemini groups retrieved chunks into one turn and
Claude splits them across three, the comparison is partly measuring encodings,
and no amount of care downstream can separate that back out.

Both adapter lanes were built independently and each flagged this as needing
arbitration, so the agreed mapping is asserted here rather than left to two
matching comments that can drift apart:

    context_chunks -> one user turn, one text unit per chunk (omitted if empty)
    messages       -> one user turn, one text unit per message

"Text unit" is a ``types.Part`` on Gemini and a ``{"type": "text"}`` block on
Claude — the same grouping in each provider's own vocabulary. No glue text,
labels, or separators are added by either side: whatever the model should see,
the prompt pack has to say.

The Gemini side uses the real ``google.genai.types``, not a stub, so a change
in the SDK's Content shape fails here rather than in front of a customer.
"""

from __future__ import annotations

import pytest

from amw.adapters.base import ModelRequest
from amw.adapters.claude_vertex import ClaudeVertexAdapter
from amw.adapters.gemini import GeminiAdapter
from amw.config import load_all

types = pytest.importorskip("google.genai.types")


@pytest.fixture(scope="module")
def models():
    return load_all().models


def gemini_turns(request: ModelRequest) -> list[list[str]]:
    """Gemini's encoding, flattened to ``[[text, ...], ...]`` per user turn."""
    contents = GeminiAdapter._build_contents(request, types)
    turns = []
    for content in contents:
        assert content.role == "user"
        turns.append([part.text for part in content.parts])
    return turns


def claude_turns(models, request: ModelRequest) -> list[list[str]]:
    """Claude's encoding, flattened the same way."""
    adapter = ClaudeVertexAdapter(models, client=object())
    messages = adapter._request_kwargs(request)["messages"]
    turns = []
    for message in messages:
        assert message["role"] == "user"
        assert all(block["type"] == "text" for block in message["content"])
        turns.append([block["text"] for block in message["content"]])
    return turns


CASES = {
    "single message": ModelRequest(
        subagent="query_rewriter", model="claude-sonnet", system_prompt="S", messages=["one"]
    ),
    "several messages": ModelRequest(
        subagent="query_rewriter",
        model="claude-sonnet",
        system_prompt="S",
        messages=["one", "two", "three"],
    ),
    "chunks and messages": ModelRequest(
        subagent="chunk_summarizer",
        model="claude-sonnet",
        system_prompt="S",
        messages=["summarize"],
        context_chunks=["chunk A", "chunk B"],
    ),
    # Whitespace is content: a prompt pack that indents an example is making a
    # formatting claim to the model, and neither adapter may normalise it away.
    "whitespace preserved": ModelRequest(
        subagent="feature_extractor",
        model="claude-sonnet",
        system_prompt="S",
        messages=["  padded  ", "trailing\n", "inner\tTAB"],
        context_chunks=["\nleading newline"],
    ),
}


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
def test_both_adapters_group_the_prompt_identically(models, case):
    request = CASES[case]
    assert gemini_turns(request) == claude_turns(models, request)


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
def test_encoding_adds_no_text_of_its_own(models, case):
    """Every text unit is verbatim from the request, and none is dropped."""
    request = CASES[case]
    expected = list(request.context_chunks) + list(request.messages)
    for encode in (gemini_turns(request), claude_turns(models, request)):
        assert [text for turn in encode for text in turn] == expected


def test_empty_context_chunks_produce_no_empty_turn(models):
    request = CASES["single message"]
    assert gemini_turns(request) == [["one"]]
    assert claude_turns(models, request) == [["one"]]


def test_system_prompt_is_out_of_band_on_both_paths(models):
    """Neither adapter smuggles the system prompt in as a user turn."""
    request = CASES["chunks and messages"]
    for encode in (gemini_turns(request), claude_turns(models, request)):
        assert all(request.system_prompt not in text for turn in encode for text in turn)
