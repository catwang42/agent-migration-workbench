"""The replay store: recorded traces, served back instead of re-calling a model.

`artifacts/replay/<subagent>.jsonl`, keyed on ``(subagent, model, input_sha)``.

Two rules from CLAUDE.md land here:

* **Ground rule 4** — replay must work with zero credentials. This store is the
  whole reason `cli.py e2e --mode replay` can run offline.
* **Ground rule 1** — replay replays *previously recorded real calls* and says
  so on screen with the recording date. :meth:`ReplayStore.recording_window`
  supplies that date; nothing here ever synthesises a response. A missing key is
  a loud :class:`ReplayMissError`, never a plausible stand-in.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Iterator

from amw.traces.schema import Trace, append_jsonl, iter_jsonl

__all__ = ["ReplayMissError", "ReplayStore", "default_replay_dir"]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def default_replay_dir() -> Path:
    return REPO_ROOT / "artifacts" / "replay"


class ReplayMissError(LookupError):
    """No recorded trace for the requested key.

    Raised rather than returning a default: a fabricated response would flow
    straight into a customer-facing metric.
    """

    def __init__(self, subagent: str, model: str, input_sha: str, hint: str = "") -> None:
        self.subagent = subagent
        self.model = model
        self.input_sha = input_sha
        message = (
            f"replay miss for (subagent={subagent!r}, model={model!r}, "
            f"input_sha={input_sha!r})"
        )
        if hint:
            message += f" — {hint}"
        super().__init__(message)


class ReplayStore:
    """Read/append access to the recorded trace corpus."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_replay_dir()
        self._cache: dict[str, dict[tuple[str, str, str], Trace]] = {}

    # -- layout ----------------------------------------------------------

    def path_for(self, subagent: str) -> Path:
        if not _SAFE_NAME.match(subagent):
            raise ValueError(
                f"invalid subagent name {subagent!r}: expected [A-Za-z0-9_-]+"
            )
        return self.root / f"{subagent}.jsonl"

    def subagents(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.stem for p in self.root.glob("*.jsonl"))

    # -- indexing --------------------------------------------------------

    def _index(self, subagent: str) -> dict[tuple[str, str, str], Trace]:
        if subagent not in self._cache:
            entries: dict[tuple[str, str, str], Trace] = {}
            path = self.path_for(subagent)
            if path.is_file():
                # Later records supersede earlier ones: a re-recording of the
                # same call is a correction, not a duplicate.
                for trace in iter_jsonl(path):
                    entries[trace.key] = trace
            self._cache[subagent] = entries
        return self._cache[subagent]

    def reload(self) -> None:
        """Drop the in-memory index (call after out-of-band writes)."""
        self._cache.clear()

    # -- reads -----------------------------------------------------------

    def get(self, subagent: str, model: str, input_sha: str) -> Trace:
        """Return the recorded trace, or raise :class:`ReplayMissError`."""
        index = self._index(subagent)
        key = (subagent, model, input_sha)
        try:
            return index[key]
        except KeyError:
            raise ReplayMissError(
                subagent, model, input_sha, hint=self._miss_hint(index, model)
            ) from None

    def _miss_hint(
        self, index: dict[tuple[str, str, str], Trace], model: str
    ) -> str:
        if not index:
            return f"no traces recorded for this subagent yet ({self.root})"
        for_model = sum(1 for _, m, _ in index if m == model)
        if not for_model:
            models = sorted({m for _, m, _ in index})
            return f"no traces for this model; recorded models: {models}"
        return (
            f"{for_model} trace(s) recorded for this model, none with that "
            "input_sha (prompt, messages, context or tools differ). "
            "Re-record with --mode live|hybrid."
        )

    def get_trace(self, trace: Trace) -> Trace:
        """Look up the recording matching another trace's key."""
        return self.get(*trace.key)

    def has(self, subagent: str, model: str, input_sha: str) -> bool:
        return (subagent, model, input_sha) in self._index(subagent)

    def traces(self, subagent: str | None = None) -> list[Trace]:
        names = [subagent] if subagent is not None else self.subagents()
        return [t for name in names for t in self._index(name).values()]

    def __iter__(self) -> Iterator[Trace]:
        return iter(self.traces())

    def __len__(self) -> int:
        return sum(len(self._index(name)) for name in self.subagents())

    def recording_window(
        self, subagent: str | None = None
    ) -> tuple[datetime, datetime] | None:
        """``(earliest_ts, latest_ts)`` of the corpus, or None if empty.

        Replay-mode output must print this so nobody mistakes a replayed number
        for a fresh one (ground rule 1).
        """
        stamps = [t.ts for t in self.traces(subagent)]
        if not stamps:
            return None
        return (min(stamps), max(stamps))

    # -- writes ----------------------------------------------------------

    def append(self, trace: Trace) -> Trace:
        """Record a trace. This is the record-on-live path — it has no off switch."""
        path = self.path_for(trace.subagent)
        append_jsonl(path, trace)
        self._index(trace.subagent)[trace.key] = trace
        return trace
