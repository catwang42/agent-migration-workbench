"""Timestamps a reader can parse at a glance, without losing the machine one.

Every timestamp this project records is UTC ISO-8601, because that is the only
sane thing to store. But `2026-08-09T16:07:15+00:00` in the middle of a sentence
is a wall of digits that a workshop audience reads past, and the one question it
actually has to answer — *how old is this recording?* — is the one it answers
worst, because the reader is sitting in Singapore and has to subtract eight hours
in their head while the presenter keeps talking.

So the site renders both: the local wall-clock reading first, in words, and the
ISO original beneath it in small muted type. The ISO string is never dropped —
it is the provenance, and ground rule 2 says provenance is printed, not
paraphrased. What changes is only which of the two the eye lands on first.

Singapore is UTC+08:00 year-round: no daylight saving, no historical shifts
inside any window this project will ever record. That makes the conversion a
fixed offset rather than a timezone database lookup, which is why this module
has no dependency on ``zoneinfo`` and cannot fail on a machine with no tzdata
installed — a real failure mode in slim containers, and one that would take the
site build down for a cosmetic feature.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = [
    "SGT",
    "SGT_LABEL",
    "to_sgt_text",
    "sgt_line",
    "sgt_window",
]

#: Singapore Standard Time. Fixed offset, no DST, no historical transitions.
SGT = timezone(timedelta(hours=8), "SGT")

SGT_LABEL = "SGT"

_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _parse(value: str) -> datetime:
    """An ISO-8601 timestamp, as an aware datetime.

    A naive timestamp is read as UTC rather than as local time: everything this
    project writes is UTC, and guessing the host's timezone would silently shift
    a recording window by however many hours the build machine happens to be
    from Greenwich.
    """
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_sgt_text(value: str) -> str:
    """``"2026-08-09T16:07:15+00:00"`` -> ``"10 Aug 2026, 12:07 AM (SGT)"``.

    Built by hand rather than with ``strftime`` because the two directives this
    format needs to drop leading zeros — ``%-d`` and ``%-I`` — are glibc
    extensions. They are not in the C standard, they are spelled ``%#d`` on
    Windows, and they raise on musl. A four-line lookup is not worth a
    platform-dependent build.
    """
    local = _parse(value).astimezone(SGT)
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return (
        f"{local.day} {_MONTHS[local.month - 1]} {local.year}, "
        f"{hour}:{local.minute:02d} {meridiem} ({SGT_LABEL})"
    )


def sgt_line(value: str, *, prefix: str = "") -> str:
    """One timestamp as two lines of Markdown: readable, then ISO underneath.

    The ISO original is wrapped in ``.amw-provenance`` rather than deleted. A
    reader reconciling this page against ``artifacts/`` needs the exact string
    that is in the JSON, and "roughly midnight Singapore time" will not match a
    ``recorded_from`` field.
    """
    head = f"{prefix}{to_sgt_text(value)}" if prefix else to_sgt_text(value)
    return f"{head}\n{{ .amw-provenance }}\n\nUTC: `{value}`\n{{ .amw-provenance }}"


def sgt_window(start: str, end: str, *, prefix: str = "Recorded ") -> str:
    """A recording *window* — the shape almost every timestamp here takes.

    Returns a two-line Markdown block: the human reading, then the ISO pair in
    muted small type. Both endpoints are converted; a window with one converted
    end and one raw end is worse than either alone.
    """
    return (
        f"{prefix}{to_sgt_text(start)} → {to_sgt_text(end)}\n\n"
        f"UTC: `{start}` → `{end}`\n{{ .amw-provenance }}"
    )
