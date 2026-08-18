"""`factfolio init` — first-run setup.

Covers:
1. The folder-nesting behaviour: `init` creates a dedicated `factfolio/`
   project folder under wherever it's run from (like `git clone` or
   `create-react-app`), rather than scattering runtime dirs into the
   current directory — see cli._resolve_init_target's docstring for the
   full decision tree.
2. tickers.yaml being seeded into that project folder from the bundled
   defaults (never read in place — see config.py's PyInstaller-onefile
   note on why).
3. The 4-question policy interview (interactive terminals, brand-new
   policy file only) and its fallback to the generic template otherwise.
   The interview's f-string template embeds literal `{months: ...}` YAML
   mappings — a regression test locks in that they're escaped correctly
   rather than misparsed as format placeholders (the old `.replace()`
   implementation had exactly this bug once already).
"""

from __future__ import annotations

import pytest

from mybroker.portfolio.policy import Policy
from mybroker.ui.cli import (
    _looks_initialized,
    _render_policy_template,
    _resolve_init_target,
    _run_policy_interview,
    cmd_init,
)


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    """Run every test from inside an empty tmp_path — never the real
    project's own directory."""
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _policy_file(project_dir):
    return project_dir / "memory" / "investment_policy.md"


def test_nests_a_factfolio_folder_under_cwd(isolated_project):
    assert cmd_init(None) == 0

    project_dir = isolated_project / "factfolio"
    assert project_dir.is_dir()
    assert _policy_file(project_dir).exists()
    # Nothing written directly into the cwd itself.
    assert not (isolated_project / "memory").exists()


def test_writes_a_parseable_policy_file(isolated_project):
    assert cmd_init(None) == 0
    policy_file = _policy_file(isolated_project / "factfolio")

    policy = Policy.load(policy_file)
    assert policy.target_cagr_pct == 12.0
    assert policy.core_min_pct == 60.0
    assert policy.max_position_pct == 8.0
    assert policy.speculative_symbols == []


def test_does_not_overwrite_an_existing_policy(isolated_project):
    project_dir = isolated_project / "factfolio"
    policy_file = _policy_file(project_dir)
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text("MY CUSTOM POLICY — do not touch")

    cmd_init(None)

    assert policy_file.read_text() == "MY CUSTOM POLICY — do not touch"


def test_stamps_todays_date_in_the_changelog(isolated_project):
    from datetime import date

    cmd_init(None)
    text = _policy_file(isolated_project / "factfolio").read_text()
    assert date.today().isoformat() in text
    assert "__TODAY__" not in text  # no stray template placeholder left behind


def test_idempotent_across_repeated_runs(isolated_project):
    assert cmd_init(None) == 0
    assert cmd_init(None) == 0
    assert cmd_init(None) == 0

    project_dir = isolated_project / "factfolio"
    Policy.load(_policy_file(project_dir))  # still parses fine after N re-runs
    # And still only one project folder, not factfolio-2/-3/...
    assert sorted(p.name for p in isolated_project.iterdir()) == ["factfolio"]


def test_rerun_from_inside_the_project_stays_there(isolated_project, monkeypatch):
    """Once you've `cd`ed into the project factfolio/ printed, re-running
    init from inside it must refresh in place — not nest a second copy."""
    cmd_init(None)
    project_dir = isolated_project / "factfolio"
    monkeypatch.chdir(project_dir)

    cmd_init(None)

    assert not (project_dir / "factfolio").exists()


def test_looks_initialized_requires_a_real_policy_file(tmp_path):
    assert not _looks_initialized(tmp_path)
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "investment_policy.md").write_text("x")
    assert _looks_initialized(tmp_path)


def test_resolve_target_reuses_an_existing_factfolio_project(tmp_path):
    project_dir = tmp_path / "factfolio"
    (project_dir / "memory").mkdir(parents=True)
    (project_dir / "memory" / "investment_policy.md").write_text("x")

    assert _resolve_init_target(tmp_path) == project_dir


def test_resolve_target_copies_instead_of_crashing_on_a_stray_file(tmp_path):
    """Regression: a plain FILE named 'factfolio' (e.g. left over from an
    old download) previously crashed with NotADirectoryError when 'init'
    tried to mkdir memory/ inside it. There's no in-place option that
    doesn't mean deleting the user's file, so always make way instead."""
    stray_file = tmp_path / "factfolio"
    stray_file.write_text("not a project, not even a folder")

    target = _resolve_init_target(tmp_path)

    assert target == tmp_path / "factfolio-2"
    assert stray_file.is_file()
    assert stray_file.read_text() == "not a project, not even a folder"  # untouched


def test_init_end_to_end_with_a_stray_file_in_the_way(isolated_project):
    """cmd_init itself must not crash in this scenario either."""
    (isolated_project / "factfolio").write_text("stray file, not a folder")

    assert cmd_init(None) == 0

    project_dir = isolated_project / "factfolio-2"
    assert _policy_file(project_dir).exists()


def test_resolve_target_copies_instead_of_clobbering_an_unrelated_folder(
    tmp_path, monkeypatch
):
    """A pre-existing 'factfolio' folder that ISN'T one of our projects
    (no memory/investment_policy.md) must never be silently written into."""
    unrelated = tmp_path / "factfolio"
    unrelated.mkdir()
    (unrelated / "some_unrelated_file.txt").write_text("not ours")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)  # non-interactive

    target = _resolve_init_target(tmp_path)

    assert target == tmp_path / "factfolio-2"
    assert (unrelated / "some_unrelated_file.txt").read_text() == "not ours"  # untouched


# ── tickers.yaml seeding ─────────────────────────────────────────────────────

def test_seeds_tickers_yaml_from_bundled_defaults(isolated_project):
    from mybroker.config import DEFAULT_TICKERS_FILE

    assert cmd_init(None) == 0

    project_tickers = isolated_project / "factfolio" / "tickers.yaml"
    assert project_tickers.exists()
    assert project_tickers.read_text() == DEFAULT_TICKERS_FILE.read_text()


def test_does_not_overwrite_an_existing_tickers_yaml(isolated_project):
    project_dir = isolated_project / "factfolio"
    (project_dir / "memory").mkdir(parents=True)
    (project_dir / "memory" / "investment_policy.md").write_text("MY POLICY")
    (project_dir / "tickers.yaml").write_text("symbols: {CUSTOM: {}}")

    cmd_init(None)

    assert (project_dir / "tickers.yaml").read_text() == "symbols: {CUSTOM: {}}"


# ── Policy interview ─────────────────────────────────────────────────────────

def test_render_policy_template_generic_matches_old_defaults(tmp_path):
    """No answers → the same numbers the plain template always shipped."""
    path = tmp_path / "policy.md"
    path.write_text(_render_policy_template(None))

    policy = Policy.load(path)
    assert policy.target_cagr_pct == 12.0
    assert policy.core_min_pct == 60.0
    assert policy.max_position_pct == 8.0
    assert policy.speculative_symbols == []
    assert "__TODAY__" not in path.read_text()


def test_render_policy_template_from_interview_answers(tmp_path):
    answers = {
        "target_cagr_pct": 14.0, "risk": "aggressive", "horizon_years": 20.0,
        "monthly_capital": 15000.0, "core_min_pct": 40.0, "core_max_pct": 55.0,
        "max_position_pct": 10.0, "max_satellite_position_pct": 7.0,
        "max_sector_pct": 30.0, "speculative_cap_pct": 15.0,
        "min_positions": 6, "max_positions": 30, "max_annual_turnover_pct": 35.0,
    }
    path = tmp_path / "policy.md"
    text = _render_policy_template(answers)
    path.write_text(text)

    # Regression: the interview template is an f-string with literal
    # {months: ...} YAML mappings embedded — must render as literal braces,
    # not crash or get swallowed as a format placeholder.
    assert "{months: 6,  core_pct: 30.0}" in text

    policy = Policy.load(path)
    assert policy.target_cagr_pct == 14.0
    assert policy.core_min_pct == 40.0
    assert policy.core_max_pct == 55.0
    assert policy.max_position_pct == 10.0
    assert policy.monthly_capital == 15000.0
    assert policy.min_positions == 6
    assert "aggressive" in text


def test_interview_returns_answers_with_risk_preset(monkeypatch):
    answers_iter = iter(["13", "aggressive", "20", "15000"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: next(answers_iter))

    result = _run_policy_interview()

    assert result["target_cagr_pct"] == 13.0
    assert result["risk"] == "aggressive"
    assert result["horizon_years"] == 20.0
    assert result["monthly_capital"] == 15000.0
    assert result["core_min_pct"] == 40.0  # aggressive preset


def test_interview_blank_input_takes_every_default(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: "")

    result = _run_policy_interview()

    assert result["target_cagr_pct"] == 12.0
    assert result["risk"] == "moderate"
    assert result["horizon_years"] == 10.0
    assert result["monthly_capital"] == 0.0


def test_interview_falls_back_to_default_on_garbage_input(monkeypatch):
    answers_iter = iter(["not-a-number", "yolo-risk", "", ""])
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: next(answers_iter))

    result = _run_policy_interview()

    assert result["target_cagr_pct"] == 12.0  # invalid float -> default
    assert result["risk"] == "moderate"  # invalid choice -> default


def test_interview_returns_none_on_eof_instead_of_crashing(monkeypatch):
    def _raise_eof(*_a, **_kw):
        raise EOFError

    monkeypatch.setattr("builtins.input", _raise_eof)

    assert _run_policy_interview() is None


def test_init_runs_the_interview_when_interactive(isolated_project, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    answers_iter = iter(["9", "conservative", "5", "10000"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_kw: next(answers_iter))

    assert cmd_init(None) == 0

    policy = Policy.load(_policy_file(isolated_project / "factfolio"))
    assert policy.target_cagr_pct == 9.0
    assert policy.core_min_pct == 70.0  # conservative preset
    assert policy.monthly_capital == 10000.0


def test_init_skips_the_interview_when_not_interactive(isolated_project, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert cmd_init(None) == 0

    policy = Policy.load(_policy_file(isolated_project / "factfolio"))
    assert policy.target_cagr_pct == 12.0  # generic default, no interview ran
