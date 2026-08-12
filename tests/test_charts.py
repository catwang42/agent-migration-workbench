"""The site's generated charts, and the one property that matters: they are
built from the artifacts, not drawn.

A chart is the easiest place on a customer-facing site to fabricate a result —
it looks like a measurement, nobody reads its axis against a JSON file, and a
hand-typed bar height survives every review. So these tests do not check that
the SVG looks nice. They check that each builder reads the artifact it names,
that its bar values are the artifact's values, that it refuses rather than
invents when the artifact is missing a rung, and that it never mixes the two
splits — the cross-split comparison ``amw/reporting/ladder.py`` exists to
forbid.

They also pin the two things that would silently break the build: a chart that
carries a Markdown link (a partial is included from pages at two directory
depths, so a relative link is wrong on one of them), and an SVG that keeps its
intrinsic width (which stops it scaling to the content column and drops the
labels below the projector floor).
"""

from __future__ import annotations

import json
import re

import pytest

from amw.reporting import charts


@pytest.fixture(scope="module")
def repo():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def built(repo):
    """Every chart, built once. Slow enough that per-test rebuilds are wasteful."""
    return {c.slug: c for c in charts.build_all(repo)}


def _judge(record):
    """The judge block hanging off a ladder record, or None if the rung never ran."""
    arm = getattr(record, "arm", None)
    return getattr(arm, "judge", None) if arm is not None else None


def _svg_numbers(svg: str) -> str:
    """The SVG's text nodes, concatenated — what a reader actually sees."""
    return " ".join(re.findall(r"<text[^>]*>([^<]*)</text>", svg))


def test_every_builder_produces_a_chart(built):
    assert set(built) == {"two-judges", "naive-swap", "fe-ladder"}


@pytest.mark.parametrize("slug", ["two-judges", "naive-swap", "fe-ladder"])
def test_chart_names_the_artifact_it_came_from(built, repo, slug):
    """Ground rule 1: a figure without a named, existing source is not shippable."""
    chart = built[slug]
    assert chart.source.startswith("artifacts/")
    assert (repo / chart.source).exists()
    assert chart.source in chart.caption or chart.caption  # caption is non-empty


@pytest.mark.parametrize("slug", ["two-judges", "naive-swap", "fe-ladder"])
def test_caption_carries_no_markdown_link(built, slug):
    """Partials are included from `index.md` and from `modules/*.md`.

    Any relative link inside one is therefore wrong on one of the two pages, and
    `mkdocs build --strict` turns that into a failed build. The including page
    supplies the link instead.
    """
    assert "](" not in built[slug].caption


@pytest.mark.parametrize("slug", ["two-judges", "naive-swap", "fe-ladder"])
def test_svg_scales_to_the_column(built, slug):
    """No intrinsic width/height on the root element, and a viewBox to scale by.

    A fixed-width SVG renders small on a projector, and shrinking a chart is the
    same readability failure as shrinking a table.
    """
    svg = built[slug].svg
    root = svg[: svg.index(">") + 1]
    assert "viewBox=" in root
    assert 'width="' not in root
    assert 'height="' not in root
    assert 'class="amw-chart__svg"' in root


@pytest.mark.parametrize("slug", ["two-judges", "naive-swap", "fe-ladder"])
def test_svg_is_deterministic(built, repo, slug):
    """Rebuilding must not produce a diff.

    matplotlib stamps the build date into <metadata>; left in, every site rebuild
    would show three changed files whether or not a number moved, and a real
    change would be invisible in the noise.
    """
    again = {c.slug: c for c in charts.build_all(repo)}
    assert again[slug].svg == built[slug].svg


@pytest.mark.parametrize("slug", ["two-judges", "naive-swap", "fe-ladder"])
def test_alt_text_describes_the_finding(built, slug):
    """A chart with no alt text is a number that some readers cannot read at all."""
    assert len(built[slug].alt) > 80


def test_two_judges_plots_the_recorded_pass_rates(built, repo):
    """Bar values come out of crosscheck.json, not out of the prose."""
    data = json.loads((repo / charts.CROSSCHECK_SOURCE).read_text())
    text = _svg_numbers(built["two-judges"].svg)
    for entry in data["subagents"]:
        for key in ("gated_pass_rate", "check_pass_rate"):
            assert f"{entry['overall'][key]:.3f}" in text


def test_naive_swap_plots_the_two_findings_module_04_draws_out(built, repo):
    data = json.loads((repo / charts.PHASE2_SOURCE).read_text())
    arms = {(a["subagent"], a["variant"]): a for a in data["arms"]}
    text = _svg_numbers(built["naive-swap"].svg)
    for subagent, metric, _label in charts.NAIVE_SWAP_METRICS:
        for variant in ("claude_baseline", "gemini_naive"):
            point = arms[(subagent, variant)]["metrics"][metric]["point"]
            assert f"{point:.3f}" in text


def test_fe_ladder_is_one_split_only(built, repo):
    """Never a core-28 bar beside a full-70 bar.

    `amw/reporting/ladder.py::MIXED_SPLIT_NOTE` forbids the comparison in the
    tables; a chart that mixed the splits would smuggle it back in as a picture.
    """
    from amw.reporting.ladder import ladder_rows
    from amw.tuning.ablate import AblationResult

    chart = built["fe-ladder"]
    result = AblationResult.model_validate_json(
        (repo / charts.FE_LADDER_SOURCE).read_text()
    )
    off_split = [
        judge
        for judge in (_judge(r) for r in ladder_rows(result.rungs))
        if judge is not None
        and judge.estimate is not None
        and judge.split != charts.FE_LADDER_SPLIT
    ]
    # The artifact really does hold rungs on the other split — otherwise this
    # test would pass for the wrong reason.
    assert off_split, "expected at least one off-split rung in the artifact"
    text = _svg_numbers(chart.svg)
    on_split_points = {
        f"{_judge(r).estimate.point:.3f}"
        for r in ladder_rows(result.rungs)
        if _judge(r) is not None
        and _judge(r).estimate is not None
        and _judge(r).split == charts.FE_LADDER_SPLIT
    }
    for judge in off_split:
        printed = f"{judge.estimate.point:.3f}"
        if printed in on_split_points:
            continue  # the two splits happen to agree to three places
        assert printed not in text


def test_fe_ladder_shows_the_rungs_that_went_backwards(built):
    """No silent drops: the ladder includes the two rungs below the incumbent.

    A ladder chart that only rises is a sales chart. `A0` and `A1-A3` both went
    down on Feature Extractor and both stay on the picture.
    """
    text = _svg_numbers(built["fe-ladder"].svg)
    assert "A0" in text
    assert "A1-A3" in text
    assert "A4-optimizer" in text


def test_fe_ladder_plots_the_recorded_scores(built, repo):
    from amw.reporting.ladder import ladder_rows
    from amw.tuning.ablate import AblationResult

    result = AblationResult.model_validate_json(
        (repo / charts.FE_LADDER_SOURCE).read_text()
    )
    text = _svg_numbers(built["fe-ladder"].svg)
    seen = 0
    for record in ladder_rows(result.rungs):
        judge = _judge(record)
        if judge is None or judge.estimate is None:
            continue
        if judge.split != charts.FE_LADDER_SPLIT:
            continue
        assert f"{judge.estimate.point:.3f}" in text
        seen += 1
    assert seen >= 5, "expected the whole core-28 ladder on the chart"


def test_a_missing_artifact_refuses_rather_than_drawing_nothing(tmp_path):
    """An empty chart on a customer site is worse than a failed build."""
    with pytest.raises(SystemExit):
        charts.fe_ladder(tmp_path)
