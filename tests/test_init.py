"""`factfolio init` — first-run setup.

Regression coverage for a real bug caught while building this: the starter
policy template's YAML block contains literal `{months: ...}` mappings,
which `str.format()` interprets as a placeholder and crashes on. Fixed by
switching to `.replace()`; these tests lock that in.
"""

from __future__ import annotations

import pytest

from mybroker.portfolio.policy import Policy
from mybroker.ui.cli import cmd_init


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    """Every path cmd_init touches, redirected under tmp_path — never the
    real project's memory/investment_policy.md."""
    memory_dir = tmp_path / "memory"
    inbox_dir = tmp_path / "holdings_inbox"
    policy_file = memory_dir / "investment_policy.md"

    monkeypatch.setattr("mybroker.config.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("mybroker.config.MEMORY_DIR", memory_dir)
    monkeypatch.setattr("mybroker.config.THESES_DIR", memory_dir / "theses")
    monkeypatch.setattr("mybroker.config.REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr("mybroker.config.LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr("mybroker.config.CACHE_DIR", tmp_path / ".cache")
    monkeypatch.setattr("mybroker.config.HOLDINGS_INBOX_DIR", inbox_dir)
    monkeypatch.setattr("mybroker.config.HOLDINGS_EQUITY", tmp_path / "holdings.csv")
    monkeypatch.setattr("mybroker.config.POLICY_FILE", policy_file)
    yield policy_file


def test_writes_a_parseable_policy_file(isolated_project):
    policy_file = isolated_project
    assert cmd_init(None) == 0
    assert policy_file.exists()

    policy = Policy.load(policy_file)
    assert policy.target_cagr_pct == 12.0
    assert policy.core_min_pct == 60.0
    assert policy.max_position_pct == 8.0
    assert policy.speculative_symbols == []


def test_does_not_overwrite_an_existing_policy(isolated_project):
    policy_file = isolated_project
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text("MY CUSTOM POLICY — do not touch")

    cmd_init(None)

    assert policy_file.read_text() == "MY CUSTOM POLICY — do not touch"


def test_stamps_todays_date_in_the_changelog(isolated_project):
    from datetime import date

    cmd_init(None)
    assert date.today().isoformat() in isolated_project.read_text()

    # And no stray template placeholder left behind:
    assert "__TODAY__" not in isolated_project.read_text()


def test_idempotent_across_repeated_runs(isolated_project):
    assert cmd_init(None) == 0
    assert cmd_init(None) == 0
    assert cmd_init(None) == 0
    # Still parses fine after N re-runs.
    Policy.load(isolated_project)
