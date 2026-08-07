"""The zero-credential adapter: serve a previously recorded real call.

This is what makes ``python cli.py e2e --mode replay`` run on a plane
(CLAUDE.md ground rule 4), and it is deliberately dumb: look up
``(subagent, model, input_sha)``, return what was recorded, or raise.

There is no fallback path, no canned response, no "close enough" match. A
missing recording is a loud :class:`~amw.traces.store.ReplayMissError`, because
the alternative — inventing a response — would flow straight into a
customer-facing metric (ground rule 1). Callers that want to survive a miss
must decide that explicitly; the adapter will not decide it for them.

Replayed output has to be labelled on screen with its recording date, so
:meth:`ReplayAdapter.recording_window` hands that date to the reporting layer.
"""

from __future__ import annotations

from datetime import datetime

from amw.adapters.base import ModelAdapter, ModelRequest
from amw.traces.schema import Trace
from amw.traces.store import ReplayStore

__all__ = ["ReplayAdapter"]


class ReplayAdapter(ModelAdapter):
    """Serve traces from ``artifacts/replay/``. Never calls a model."""

    name = "replay"
    mode = "replay"

    def __init__(self, store: ReplayStore | None = None) -> None:
        self.store = store if store is not None else ReplayStore()

    def complete(self, request: ModelRequest) -> Trace:
        """Return the recorded trace for this request.

        :raises ~amw.traces.store.ReplayMissError: nothing was recorded for
            this key. Not caught and not converted to a ``status:"error"``
            trace: an error trace means *the model failed*, and claiming that
            about a call that was never made would corrupt the eval.
        """
        recorded = self.store.get(*request.replay_key)
        # Deep copy: the store hands out its own index entries, and a caller
        # mutating one would quietly rewrite the corpus for the rest of the run.
        return recorded.model_copy(deep=True)

    def recording_window(
        self, subagent: str | None = None
    ) -> tuple[datetime, datetime] | None:
        """``(earliest, latest)`` recording timestamp, for the on-screen label."""
        return self.store.recording_window(subagent)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ReplayAdapter root={self.store.root}>"
