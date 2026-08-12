"""Site charts, drawn from the artifacts at build time.

Every chart on the public site is generated here, by reading the same JSON the
tables read. Nothing is typed in. That is not a style preference — a chart is
the easiest artefact on a site to fake, because nobody diffs a picture against a
number, and a bar drawn one pixel too tall is a fabricated result that no test
would catch (ground rule 1). Reading the artifact makes the picture and the
table wrong or right *together*, and makes the freeze rebuild refresh both.

Three deliberate choices about the output:

**SVG with live text, not paths.** ``svg.fonttype = "none"`` keeps labels as
``<text>`` nodes, so they inherit Inter from the page, stay selectable and
searchable, and — the reason that matters here — can be recoloured by CSS. The
site has a light scheme and a dark one; a chart with baked-in grey labels is
illegible in one of them.

**Transparent background, no chrome.** No frame, no gridlines competing with the
bars, no title inside the image. The page supplies the heading and the caption;
duplicating them into the figure just makes two things that can disagree.

**Confidence intervals are drawn wherever they exist.** A bar without its
interval invites exactly the point-estimate reading that ``config/gates.yaml``
spends six gates refusing to make. Where the artifact carries ``lo``/``hi``,
they are drawn as whiskers, and the caption says so.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (backend must be set first)

__all__ = [
    "Chart",
    "INDIGO",
    "CYAN",
    "build_all",
    "crosscheck_judges",
    "fe_ladder",
    "naive_swap",
]

# The site palette, from site_src/stylesheets/extra.css. Both readable on the
# navy canvas and on white, which is what a transparent chart in a two-scheme
# theme requires.
INDIGO = "#4f6df5"
CYAN = "#22d3ee"
#: Placeholder ink. Never seen: `.amw-chart svg text` overrides it per scheme.
INK = "#64748b"

#: Point size of the largest label. The SVG is emitted with no intrinsic width
#: and scaled to the content column by CSS, so on a normal desktop reading width
#: these land around 20px — comfortably over the 16px projector floor. Anything
#: set smaller than 13 here drops under that floor at mobile widths.
_BASE_FONT = 16.0
_SMALL_FONT = 14.0


@dataclass(frozen=True)
class Chart:
    """One generated figure, with the provenance that has to travel with it."""

    slug: str
    #: Inline SVG markup. Inlined rather than <img>-linked so the page's CSS can
    #: recolour the labels for the active colour scheme.
    svg: str
    alt: str
    caption: str
    #: Repository-relative path of the artifact the numbers were read from.
    source: str


def _style() -> None:
    # `Inter` is a webfont the *browser* has and this machine does not, so
    # matplotlib logs a findfont miss for every text node it lays out — dozens of
    # identical warnings per chart, in the middle of the site build's output.
    # The miss is harmless: `svg.fonttype = "none"` writes the family list into
    # the SVG unresolved, so the browser picks Inter and matplotlib only ever
    # needed a font to measure with. Silencing the logger keeps a real warning
    # from being buried in noise.
    import logging

    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

    plt.rcParams.update(
        {
            "svg.fonttype": "none",
            "font.family": "Inter, system-ui, sans-serif",
            "font.size": _BASE_FONT,
            "text.color": INK,
            "axes.labelcolor": INK,
            "xtick.color": INK,
            "ytick.color": INK,
            "axes.edgecolor": INK,
            "figure.facecolor": "none",
            "axes.facecolor": "none",
            "savefig.facecolor": "none",
            "savefig.transparent": True,
        }
    )


_XML_DECL = re.compile(r"<\?xml[^>]*\?>\s*|<!DOCTYPE[^>]*>\s*", re.I)
_METADATA = re.compile(r"<metadata>.*?</metadata>\s*", re.S)
#: matplotlib writes an intrinsic size in points on the root element.
_ROOT_SIZE = re.compile(r'(<svg\b[^>]*?)\s+width="[^"]*"\s+height="[^"]*"')


def _to_svg(fig) -> str:
    """The figure as inline-ready, responsive SVG.

    Three things are removed on the way out:

    * The XML declaration and DOCTYPE — legal in a standalone file, illegal in
      the middle of an HTML body.
    * matplotlib's RDF metadata block, which carries a build date. Left in, every
      rebuild would diff even when no number moved, and a chart that always looks
      changed is a chart nobody checks.
    * The intrinsic ``width``/``height`` in points. Without them the ``viewBox``
      alone drives layout, so CSS can scale the figure to the content column —
      which is also what keeps the labels above the 16px floor on a projector
      and stops the chart overflowing on a phone.

    And matplotlib's per-run element ids are renumbered, for the same reason the
    metadata goes: see :func:`_stabilise_ids`.
    """
    from io import StringIO

    buffer = StringIO()
    fig.savefig(buffer, format="svg", bbox_inches="tight", transparent=True)
    plt.close(fig)
    svg = buffer.getvalue()
    svg = _XML_DECL.sub("", svg)
    svg = _METADATA.sub("", svg)
    svg = _ROOT_SIZE.sub(r'\1 class="amw-chart__svg"', svg, count=1)
    svg = _stabilise_ids(svg)
    return svg.strip()


#: matplotlib's generated element ids: a letter or two, then ten hex digits
#: (``p46fc860f06`` for a clip path, ``m3b7d1c0e91`` for a marker definition).
_MPL_ID = re.compile(r"\b([A-Za-z]{1,2}[0-9a-f]{10})\b")


def _stabilise_ids(svg: str) -> str:
    """Renumber matplotlib's element ids so the same data yields the same bytes.

    matplotlib derives clip-path and marker ids from the object's memory address,
    so an identical figure gets different ids on every run. Left alone, each site
    rebuild would show all three chart partials as modified whether or not a
    single number had moved — and a chart that always looks changed is a chart
    nobody reads the diff of. That matters here specifically because the charts
    are regenerated at the content freeze: the freeze diff has to show which
    figures actually moved.

    Ids are renumbered in order of first appearance, so the mapping depends only
    on the document, and every reference (``url(#id)``, ``xlink:href="#id"``) is
    rewritten with the definition.
    """
    mapping: dict[str, str] = {}
    for match in _MPL_ID.finditer(svg):
        mapping.setdefault(match.group(1), f"amw{len(mapping):02d}")
    return _MPL_ID.sub(lambda m: mapping[m.group(1)], svg)


def _read(repo: Path, relative: str) -> dict:
    path = repo / relative
    if not path.is_file():
        raise SystemExit(
            f"charts: artifact is missing: {relative}\n"
            "  Charts are generated from artifacts, never drawn by hand. Run the "
            "pipeline that produces this file rather than publishing a chart "
            "without it."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _pretty(subagent: str) -> str:
    return subagent.replace("_", " ").title()


def _hide_spines(ax, keep: Sequence[str] = ()) -> None:
    for name, spine in ax.spines.items():
        spine.set_visible(name in keep)


# --------------------------------------------------------------------------- #
# 1. The two judges, side by side                                              #
# --------------------------------------------------------------------------- #

CROSSCHECK_SOURCE = "artifacts/results/crosscheck.json"


def crosscheck_judges(repo: Path) -> Chart:
    """Paired bars: what each vendor's judge scored, per subagent.

    The quantity plotted is ``*_pass_rate`` — the share of rubric criteria the
    judge marked passed across every arm it scored. It is the only quantity the
    cross-check records for *both* judges, so it is the only honest paired bar.
    The gated judge's mean score is not plotted opposite it: those are two
    different measurements and putting them in one pair would imply a comparison
    that was never run.
    """
    data = _read(repo, CROSSCHECK_SOURCE)
    subagents = data["subagents"]
    labels = [_pretty(s["subagent"]) for s in subagents]
    gated = [s["overall"]["gated_pass_rate"] for s in subagents]
    check = [s["overall"]["check_pass_rate"] for s in subagents]

    _style()
    fig, ax = plt.subplots(figsize=(8.6, 3.9))
    positions = range(len(labels))
    width = 0.36
    left = [p - width / 2 for p in positions]
    right = [p + width / 2 for p in positions]

    ax.bar(left, gated, width, label=data["gated_judge"]["judge_model"], color=INDIGO)
    ax.bar(right, check, width, label=data["check_judge"]["judge_model"], color=CYAN)

    for xs, values in ((left, gated), (right, check)):
        for x, value in zip(xs, values):
            ax.text(x, value + 0.012, f"{value:.3f}", ha="center", va="bottom", fontsize=_SMALL_FONT)

    ax.set_xticks(list(positions))
    ax.set_xticklabels(labels)
    ax.set_ylim(0, 1.06)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.set_ylabel("Rubric criteria passed")
    ax.legend(frameon=False, loc="lower right", fontsize=_SMALL_FONT)
    ax.tick_params(length=0)
    _hide_spines(ax, keep=("bottom",))

    return Chart(
        slug="two-judges",
        svg=_to_svg(fig),
        alt=(
            "Grouped bar chart. For each of the three subagents, two bars: the share "
            "of rubric criteria passed by the gated Gemini 2.5 Pro judge and by the "
            "Claude Sonnet 5 cross-check judge. The two bars are close in every pair "
            "and the three subagents fall in the same order under both judges."
        ),
        # No Markdown link here: a partial is included from pages at two
        # different directory depths, so any relative path is wrong on one of
        # them and `mkdocs build --strict` fails the build. The including page
        # supplies the link; the caption supplies the pointer in words.
        caption="Two judges, same ranking of the three subagents. Full numbers in module 06.",
        source=CROSSCHECK_SOURCE,
    )


# --------------------------------------------------------------------------- #
# 2. What the naive swap cost                                                  #
# --------------------------------------------------------------------------- #

PHASE2_SOURCE = "artifacts/results/phase2_n70_widened.json"

#: The two findings module 04 draws out, as (subagent, metric, plain-English name).
#: Named explicitly rather than plotted wholesale: the page makes an argument
#: about these two, and a chart of all fourteen metrics makes no argument at all.
NAIVE_SWAP_METRICS: tuple[tuple[str, str, str], ...] = (
    ("query_rewriter", "exact_match_intent", "Query Rewriter\nintent exactly right"),
    ("chunk_summarizer", "citation_coverage", "Chunk Summarizer\ncitations kept"),
)


def naive_swap(repo: Path) -> Chart:
    """Before and after, for the two metrics the naive swap moved most."""
    data = _read(repo, PHASE2_SOURCE)
    arms = {(a["subagent"], a["variant"]): a for a in data["arms"]}

    labels: list[str] = []
    before: list[float] = []
    after: list[float] = []
    before_ci: list[tuple[float, float]] = []
    after_ci: list[tuple[float, float]] = []

    for subagent, metric, label in NAIVE_SWAP_METRICS:
        incumbent = arms[(subagent, "claude_baseline")]["metrics"][metric]
        candidate = arms[(subagent, "gemini_naive")]["metrics"][metric]
        labels.append(label)
        before.append(incumbent["point"])
        after.append(candidate["point"])
        before_ci.append((incumbent["estimate"]["lo"], incumbent["estimate"]["hi"]))
        after_ci.append((candidate["estimate"]["lo"], candidate["estimate"]["hi"]))

    def _yerr(points: list[float], bounds: list[tuple[float, float]]):
        return [
            [p - lo for p, (lo, _) in zip(points, bounds)],
            [hi - p for p, (_, hi) in zip(points, bounds)],
        ]

    _style()
    fig, ax = plt.subplots(figsize=(8.6, 4.0))
    positions = range(len(labels))
    width = 0.34
    left = [p - width / 2 for p in positions]
    right = [p + width / 2 for p in positions]
    bar_kw = dict(error_kw={"ecolor": INK, "elinewidth": 1.4, "capsize": 5})

    ax.bar(
        left, before, width, yerr=_yerr(before, before_ci),
        label="Claude, current prompt", color=INDIGO, **bar_kw,
    )
    ax.bar(
        right, after, width, yerr=_yerr(after, after_ci),
        label="Gemini, same prompt bytes", color=CYAN, **bar_kw,
    )

    for xs, values, bounds in ((left, before, before_ci), (right, after, after_ci)):
        for x, value, (_, hi) in zip(xs, values, bounds):
            ax.text(x, hi + 0.02, f"{value:.3f}", ha="center", va="bottom", fontsize=_SMALL_FONT)

    ax.set_xticks(list(positions))
    ax.set_xticklabels(labels)
    ax.set_ylim(0, 1.16)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.set_ylabel("Score")
    ax.legend(frameon=False, loc="lower left", fontsize=_SMALL_FONT)
    ax.tick_params(length=0)
    _hide_spines(ax, keep=("bottom",))

    return Chart(
        slug="naive-swap",
        svg=_to_svg(fig),
        alt=(
            "Grouped bar chart with error bars. Query Rewriter intent accuracy falls "
            "from the Claude baseline to the naive Gemini swap; Chunk Summarizer "
            "citation coverage falls from a perfect score. Whiskers show the 95% "
            "confidence range."
        ),
        caption=(
            "The two metrics the naive swap moved most, n=70, whiskers are the 95% "
            "confidence range."
        ),
        source=PHASE2_SOURCE,
    )


# --------------------------------------------------------------------------- #
# 3. The Feature Extractor ladder                                              #
# --------------------------------------------------------------------------- #

FE_LADDER_SOURCE = "artifacts/results/ablation_feature_extractor.json"

#: Plain-English gloss per rung. The rung id stays on the axis beside it — the
#: gloss is an aid to a first-time reader, not a rename.
RUNG_PLAIN: dict[str, str] = {
    "baseline": "original prompt, on Claude",
    "A0": "same prompt bytes, on Gemini",
    "A1-A3": "generic rewrite",
    "A0-schema": "output mode changed only",
    "A4-novelty-tool": "targeted fix",
    "A4-novelty-schema": "targeted fix + output mode",
    "A4-optimizer": "optimizer-written instruction",
}

#: The ladder is plotted on ONE split. Mixing n=28 and n=70 bars into a single
#: axis is precisely the cross-split comparison amw/reporting/ladder.py's
#: MIXED_SPLIT_NOTE exists to forbid, and a chart cannot carry that warning the
#: way a table cell can.
FE_LADDER_SPLIT = "core"


def fe_ladder(repo: Path) -> Chart:
    """The Feature Extractor ladder as horizontal bars, one split only.

    Every rung measured on the split is drawn, in ladder order — including the
    two rungs that went *backwards*. Selecting the flattering subset would turn
    the one chart most likely to be screenshotted into a sales chart, which is
    the failure mode ``NO_WINNER_NOTE`` names.
    """
    from amw.reporting.ladder import ladder_rows
    from amw.tuning.ablate import AblationResult

    result = AblationResult.model_validate(_read(repo, FE_LADDER_SOURCE))

    rungs: list[tuple[str, float, float, float]] = []
    n_items: int | None = None
    for record in ladder_rows(result.rungs):
        arm = getattr(record, "arm", None)
        judge = getattr(arm, "judge", None) if arm is not None else None
        if judge is None or judge.split != FE_LADDER_SPLIT or judge.estimate is None:
            continue
        rungs.append(
            (record.rung, judge.estimate.point, judge.estimate.lo, judge.estimate.hi)
        )
        n_items = judge.items_scored
    if not rungs:
        raise SystemExit(
            f"charts: no rung in {FE_LADDER_SOURCE} is scored on split "
            f"{FE_LADDER_SPLIT!r}. Refusing to draw an empty ladder."
        )

    # The first rung on the ladder is the incumbent, by construction.
    incumbent = rungs[0][1]

    # Drawn top-to-bottom in ladder order, which means reversing for matplotlib's
    # bottom-up y axis.
    rungs = list(reversed(rungs))
    labels = [f"{rung}\n{RUNG_PLAIN.get(rung, '')}".rstrip() for rung, *_ in rungs]
    points = [r[1] for r in rungs]
    lows = [r[2] for r in rungs]
    highs = [r[3] for r in rungs]

    _style()
    fig, ax = plt.subplots(figsize=(9.0, 5.2))
    positions = list(range(len(rungs)))
    xerr = [
        [p - lo for p, lo in zip(points, lows)],
        [hi - p for p, hi in zip(points, highs)],
    ]
    colours = [CYAN if p >= incumbent else INDIGO for p in points]
    ax.barh(
        positions, points, height=0.62, color=colours,
        xerr=xerr, error_kw={"ecolor": INK, "elinewidth": 1.4, "capsize": 5},
    )
    ax.axvline(incumbent, color=INK, linestyle="--", linewidth=1.3)
    ax.text(
        incumbent, len(rungs) - 0.28,
        f"  incumbent {incumbent:.3f}", va="bottom", ha="left", fontsize=_SMALL_FONT,
    )

    for y, (_, point, _, hi) in zip(positions, rungs):
        ax.text(hi + 0.008, y, f"{point:.3f}", va="center", ha="left", fontsize=_SMALL_FONT)

    ax.set_yticks(positions)
    ax.set_yticklabels(labels, fontsize=_SMALL_FONT)
    ax.set_xlim(0.70, 1.04)
    ax.set_xticks([0.75, 0.80, 0.85, 0.90, 0.95, 1.0])
    ax.set_xlabel("Judged score")
    ax.tick_params(length=0)
    _hide_spines(ax)

    return Chart(
        slug="fe-ladder",
        svg=_to_svg(fig),
        alt=(
            "Horizontal bar chart of the Feature Extractor ablation ladder. Bars run "
            "from the original Claude prompt at the top through the naive swap and "
            "the generic rewrite, which both fall below it, to the targeted fixes and "
            "the optimizer-written instruction, which rise above it. A dashed line "
            "marks the incumbent. Whiskers show the 95% confidence range."
        ),
        caption=(
            f"Every Feature Extractor rung scored on the {n_items}-item core split, "
            "in ladder order — including the two that went backwards. Whiskers are "
            "the 95% confidence range; the dashed line is the incumbent. Rungs "
            "scored on the full 70 are a different instrument and are not on this "
            "chart."
        ),
        source=FE_LADDER_SOURCE,
    )


# --------------------------------------------------------------------------- #

#: Every chart the site publishes, in the order build_site.py generates them.
BUILDERS = (crosscheck_judges, naive_swap, fe_ladder)


def build_all(repo: Path) -> list[Chart]:
    """Generate every site chart from the artifacts under ``repo``."""
    return [builder(repo) for builder in BUILDERS]
