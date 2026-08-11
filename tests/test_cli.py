"""CLI wiring: argument surface and the region-disagreement warnings.

The warnings are the part with logic in it, and the thing they guard is a
provenance bug rather than a crash: a run whose numbers come from one region
while the report footer names another. That fails silently and looks fine, so
it gets tests.
"""

from __future__ import annotations

import pytest

import cli
from amw.config import load_all


@pytest.fixture(scope="module")
def cfg():
    return load_all()


def warnings(cfg, mode="live", **env):
    import os

    saved = {k: os.environ.get(k) for k in ("REGION", "CLAUDE_REGION")}
    try:
        for key in saved:
            os.environ.pop(key, None)
        for key, value in env.items():
            if value is not None:
                os.environ[key] = value
        return cli.region_warnings(cfg, mode)
    finally:
        for key, value in saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value


# -- region warnings --------------------------------------------------------


def test_replay_mode_never_warns(cfg):
    """Replay dials nothing anywhere; the corpus carries its own labels."""
    assert warnings(cfg, mode="replay", REGION="nowhere-1", CLAUDE_REGION="global") == []


def test_no_warning_when_env_matches_the_profile(cfg):
    assert warnings(cfg, REGION=cfg.customer.region) == []


def test_no_warning_when_region_is_unset(cfg):
    """Unset REGION is caught by the adapters' own credential errors, not here."""
    assert warnings(cfg) == []


def test_env_region_disagreeing_with_profile_warns(cfg):
    [warning] = warnings(cfg, REGION="europe-west4")
    assert "europe-west4" in warning
    assert cfg.customer.region in warning


def test_split_claude_and_gemini_regions_warn_about_the_latency_gate(cfg):
    found = warnings(cfg, REGION=cfg.customer.region, CLAUDE_REGION="global")
    assert len(found) == 1
    assert "latency_p95" in found[0]
    assert "global" in found[0] and cfg.customer.region in found[0]


def test_claude_region_matching_region_is_not_a_split(cfg):
    assert warnings(cfg, REGION=cfg.customer.region, CLAUDE_REGION=cfg.customer.region) == []


def test_both_disagreements_are_reported_together(cfg):
    found = warnings(cfg, REGION="europe-west4", CLAUDE_REGION="global")
    assert len(found) == 2


# -- argument surface -------------------------------------------------------


def test_every_command_takes_mode_and_customer():
    parser = cli.build_parser()
    for command in cli.COMMANDS:
        args = parser.parse_args([command])
        assert args.mode == "replay", "replay is the zero-credential default"
        assert args.customer is None


def test_smoke_backend_and_claude_path_choices():
    parser = cli.build_parser()
    args = parser.parse_args(["smoke", "--backend", "claude", "--claude-path", "vertex"])
    assert (args.backend, args.claude_path) == ("claude", "vertex")
    assert parser.parse_args(["smoke"]).backend is None


@pytest.mark.parametrize("argv", [["smoke", "--backend", "bedrock"], ["gen", "--mode", "wat"]])
def test_bad_choices_are_rejected(argv):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(argv)


def test_unimplemented_command_reports_its_task_and_exits_nonzero(capsys):
    """It must never print something that could be read as "it ran".

    Written against the mechanism (``COMMANDS`` minus ``HANDLERS``) rather than
    against one command name. The previous version asserted on ``scorecard``,
    which meant the test had to be edited the day T12 landed — and a test you
    rewrite to make a feature pass has stopped guarding anything. This version
    covers whatever is still unbuilt, and skips itself once nothing is.
    """
    unimplemented = [name for name in cli.COMMANDS if name not in cli.HANDLERS]
    if not unimplemented:
        pytest.skip("every subcommand is wired up; the guard has nothing to guard")
    for name in unimplemented:
        assert cli.main([name, "--mode", "replay"]) == 3
        err = capsys.readouterr().err
        assert cli.COMMANDS[name][1] in err
        assert "Nothing was run." in err
