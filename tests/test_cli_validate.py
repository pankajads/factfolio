"""`factfolio validate`'s CLI wrapper (ui/cli.py's cmd_validate) — the
interactive offer to AI-resolve unmapped full-name holdings right there,
instead of sending the user back to `factfolio init` for a second command.

tickers_validate.main() itself (the deterministic gate) is covered by
test_tickers_validate.py; this file covers only the orchestration cli.py
layers on top of it — deciding whether to offer the resolver, honouring
the user's answer, and re-running the gate afterward. Everything below
that — validate_main() and _suggest_ticker_matches() — is monkeypatched
out, since exercising the real AI resolver needs a live `claude` session.
"""

from __future__ import annotations

from mybroker import tickers_validate
from mybroker.portfolio import importers
from mybroker.ui import cli


class _FakeStdin:
    """sys.stdin's real isatty is a C-level bound method that can't be
    monkeypatched in place — swap the whole object instead."""

    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


def _patch(
    monkeypatch, *, validate_results, unmapped, is_tty, answer=None,
    unresolvable=(),
):
    results = iter(validate_results)
    monkeypatch.setattr(tickers_validate, "main", lambda: next(results))
    monkeypatch.setattr(cli.ticker_seeding, "collect_unmapped_holdings", lambda: unmapped)
    monkeypatch.setattr(importers, "find_unresolvable_files", lambda: list(unresolvable))
    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin(is_tty))
    if answer is not None:
        monkeypatch.setattr(cli, "input", lambda *_a, **_kw: answer, raising=False)
    calls = {"resolved": False}
    monkeypatch.setattr(
        cli, "_suggest_ticker_matches", lambda: calls.__setitem__("resolved", True)
    )
    return calls


def test_clean_pass_never_prompts(monkeypatch, capsys):
    """validate_main() already returning 0 — nothing to offer, no prompt."""
    calls = _patch(monkeypatch, validate_results=[0], unmapped=[], is_tty=True)

    assert cli.cmd_validate(None) == 0
    assert not calls["resolved"]
    assert "Resolve" not in capsys.readouterr().out


def test_failure_unrelated_to_unmapped_holdings_is_not_offered_resolution(monkeypatch, capsys):
    """validate_main() failed for some other reason (no holdings at all, a
    real yfinance resolution failure, ...) — collect_unmapped_holdings()
    comes back empty, so there's nothing the resolver could fix. Must not
    prompt or loop."""
    calls = _patch(monkeypatch, validate_results=[1], unmapped=[], is_tty=True)

    assert cli.cmd_validate(None) == 1
    assert not calls["resolved"]


def test_non_interactive_never_prompts(monkeypatch, capsys):
    """A scripted/piped run (CI, cron, a frozen build with no console) must
    get exactly today's behaviour — fail, say what's missing, never block
    on input() that will never come."""
    calls = _patch(
        monkeypatch, validate_results=[1],
        unmapped=[{"name": "AXIS BANK LIMITED"}], is_tty=False,
    )

    assert cli.cmd_validate(None) == 1
    assert not calls["resolved"]
    assert "Resolve" not in capsys.readouterr().out


def test_declining_the_offer_leaves_the_original_failure_standing(monkeypatch, capsys):
    calls = _patch(
        monkeypatch, validate_results=[1],
        unmapped=[{"name": "AXIS BANK LIMITED"}], is_tty=True, answer="n",
    )

    assert cli.cmd_validate(None) == 1
    assert not calls["resolved"]


def test_accepting_the_offer_resolves_and_re_validates_once(monkeypatch, capsys):
    """The actual fix for the reported bug: accepting the offer runs the
    same resolver `init` uses, then re-runs the deterministic gate once
    more automatically — one `factfolio validate` invocation, not a second
    trip through `factfolio init`."""
    calls = _patch(
        monkeypatch, validate_results=[1, 0],
        unmapped=[{"name": "AXIS BANK LIMITED"}], is_tty=True, answer="y",
    )

    assert cli.cmd_validate(None) == 0
    assert calls["resolved"]


def test_blank_answer_defaults_to_yes(monkeypatch, capsys):
    calls = _patch(
        monkeypatch, validate_results=[1, 0],
        unmapped=[{"name": "AXIS BANK LIMITED"}], is_tty=True, answer="",
    )

    assert cli.cmd_validate(None) == 0
    assert calls["resolved"]


# ── AI-assisted column/schema resolution — a holdings_inbox file whose
# header was recognised but required columns weren't. Same interactive-
# only, deterministic-by-default discipline as the ticker-resolution offer
# above; see _offer_schema_resolution's own docstring in cli.py.

def _unresolvable_item(**overrides):
    grid = [["Scheme Name", "Val"], ["Fund A", "10000"]]
    item = {
        "path": type("P", (), {"name": "weird.csv"})(),
        "grid": grid,
        "grid_excerpt": grid,
        "error": "could not find column(s) ['current_value', 'invested']",
    }
    item.update(overrides)
    return item


def test_schema_offer_runs_before_the_ticker_gate_when_files_are_unresolvable(
    monkeypatch, capsys,
):
    calls = _patch(
        monkeypatch, validate_results=[0], unmapped=[], is_tty=True,
        unresolvable=[_unresolvable_item()], answer="n",
    )

    cli.cmd_validate(None)

    out = capsys.readouterr().out
    assert "weird.csv" in out
    assert not calls["resolved"]  # declining doesn't touch ticker resolution


def test_schema_offer_skipped_when_non_interactive(monkeypatch, capsys):
    _patch(
        monkeypatch, validate_results=[0], unmapped=[], is_tty=False,
        unresolvable=[_unresolvable_item()],
    )

    cli.cmd_validate(None)

    assert "weird.csv" not in capsys.readouterr().out


def test_schema_offer_declined_writes_nothing(monkeypatch, capsys):
    saved = {"called": False}
    monkeypatch.setattr(
        importers, "save_column_map",
        lambda *a, **kw: saved.__setitem__("called", True),
    )
    _patch(
        monkeypatch, validate_results=[0], unmapped=[], is_tty=True,
        unresolvable=[_unresolvable_item()], answer="n",
    )

    cli.cmd_validate(None)

    assert not saved["called"]


def test_schema_offer_accepted_and_valid_saves_the_mapping(monkeypatch, capsys):
    from mybroker.agents import schema_resolver as schema_resolver_module

    async def _fake_resolve(grid_excerpt):
        return schema_resolver_module.ResolvedSchema(
            header_row=0, kind="mf",
            columns={"scheme_name": 0, "invested": 1, "current_value": 1},
            confidence="high", reasoning="the only two columns available",
        )

    monkeypatch.setattr(schema_resolver_module, "resolve_schema", _fake_resolve)
    monkeypatch.setattr(importers, "validate_schema", lambda *a, **kw: (True, []))
    saved = {}
    monkeypatch.setattr(
        importers, "save_column_map",
        lambda header, **kw: saved.update(header=header, **kw),
    )
    _patch(
        monkeypatch, validate_results=[0], unmapped=[], is_tty=True,
        unresolvable=[_unresolvable_item()], answer="y",
    )

    cli.cmd_validate(None)

    assert saved["kind"] == "mf"
    assert saved["columns"]["scheme_name"] == 0
    assert saved["source"] == "weird.csv"
    out = capsys.readouterr().out
    assert "mapped as" in out


def test_schema_offer_accepted_but_invalid_does_not_save(monkeypatch, capsys):
    """The actual grounding gate: an agent's proposed mapping that fails
    validate_schema must never reach the cache, no matter how confident
    it claims to be."""
    from mybroker.agents import schema_resolver as schema_resolver_module

    async def _fake_resolve(grid_excerpt):
        return schema_resolver_module.ResolvedSchema(
            header_row=0, kind="mf",
            columns={"scheme_name": 1, "invested": 1, "current_value": 1},
            confidence="high", reasoning="looks right to me",
        )

    monkeypatch.setattr(schema_resolver_module, "resolve_schema", _fake_resolve)
    monkeypatch.setattr(
        importers, "validate_schema",
        lambda *a, **kw: (False, ["scheme_name: column 1 looks purely numeric"]),
    )
    saved = {"called": False}
    monkeypatch.setattr(
        importers, "save_column_map",
        lambda *a, **kw: saved.__setitem__("called", True),
    )
    _patch(
        monkeypatch, validate_results=[0], unmapped=[], is_tty=True,
        unresolvable=[_unresolvable_item()], answer="y",
    )

    cli.cmd_validate(None)

    assert not saved["called"]
    out = capsys.readouterr().out
    assert "didn't check out" in out
    assert "looks purely numeric" in out


def test_schema_offer_handles_agent_failure_gracefully(monkeypatch, capsys):
    from mybroker.agents import schema_resolver as schema_resolver_module

    async def _boom(grid_excerpt):
        raise RuntimeError("no `claude` login found")

    monkeypatch.setattr(schema_resolver_module, "resolve_schema", _boom)
    _patch(
        monkeypatch, validate_results=[0], unmapped=[], is_tty=True,
        unresolvable=[_unresolvable_item()], answer="y",
    )

    # Must not crash cmd_validate — the file just stays unparseable.
    assert cli.cmd_validate(None) == 0
    assert "Couldn't resolve it" in capsys.readouterr().out


def test_schema_offer_no_table_found_does_not_save(monkeypatch, capsys):
    """The agent honestly reporting it found no holdings table at all
    (header_row/kind both null) — not an error, just nothing to save."""
    from mybroker.agents import schema_resolver as schema_resolver_module

    async def _fake_resolve(grid_excerpt):
        return schema_resolver_module.ResolvedSchema(
            header_row=None, kind=None, columns={}, confidence="low",
            reasoning="none of these rows form a holdings table",
        )

    monkeypatch.setattr(schema_resolver_module, "resolve_schema", _fake_resolve)
    saved = {"called": False}
    monkeypatch.setattr(
        importers, "save_column_map",
        lambda *a, **kw: saved.__setitem__("called", True),
    )
    _patch(
        monkeypatch, validate_results=[0], unmapped=[], is_tty=True,
        unresolvable=[_unresolvable_item()], answer="y",
    )

    assert cli.cmd_validate(None) == 0
    assert not saved["called"]
    out = capsys.readouterr().out
    assert "couldn't find a holdings table" in out


# ── Mutual-fund summary — `validate` never touches MF tickers (there's
# nothing to yfinance-resolve for a fund), but a real user reported that
# saying NOTHING about them made it look like they'd been silently dropped.
# _print_mf_summary is the fix: one confirmation line, on every exit path.

def _portfolio_with_mf(*, current_values=(50000.0,)):
    from mybroker.portfolio.loader import MFPosition, Portfolio

    return Portfolio(mutual_funds=[
        MFPosition(
            scheme_name=f"Fund {i}", amfi_code="", units=10.0, avg_nav=10.0,
            current_nav=v / 10.0, invested=v * 0.9, current_value=v,
        )
        for i, v in enumerate(current_values)
    ])


def test_mf_summary_prints_when_holdings_exist(monkeypatch, capsys):
    from mybroker.portfolio import loader

    monkeypatch.setattr(
        loader, "load_portfolio", lambda: _portfolio_with_mf(current_values=(50000.0, 25000.0))
    )
    _patch(monkeypatch, validate_results=[0], unmapped=[], is_tty=True)

    cli.cmd_validate(None)

    out = capsys.readouterr().out
    assert "Mutual funds" in out
    assert "2 holdings found" in out
    assert "75,000" in out
    assert "factfolio status" in out


def test_mf_summary_silent_when_no_mf_holdings(monkeypatch, capsys):
    from mybroker.portfolio import loader

    monkeypatch.setattr(loader, "load_portfolio", lambda: _portfolio_with_mf(current_values=()))
    _patch(monkeypatch, validate_results=[0], unmapped=[], is_tty=True)

    cli.cmd_validate(None)

    assert "Mutual funds" not in capsys.readouterr().out


def test_mf_summary_never_crashes_validate_on_load_failure(monkeypatch, capsys):
    from mybroker.portfolio import loader

    def _boom():
        raise FileNotFoundError("no holdings")

    monkeypatch.setattr(loader, "load_portfolio", _boom)
    _patch(monkeypatch, validate_results=[0], unmapped=[], is_tty=True)

    assert cli.cmd_validate(None) == 0
    assert "Mutual funds" not in capsys.readouterr().out


def test_mf_summary_prints_singular_for_one_holding(monkeypatch, capsys):
    from mybroker.portfolio import loader

    monkeypatch.setattr(loader, "load_portfolio", lambda: _portfolio_with_mf())
    _patch(monkeypatch, validate_results=[0], unmapped=[], is_tty=True)

    cli.cmd_validate(None)

    out = capsys.readouterr().out
    assert "1 holding found" in out  # not "1 holdings found"


def test_mf_summary_prints_even_when_the_ticker_gate_fails(monkeypatch, capsys):
    """The actual reported bug: MF summary must appear regardless of
    whether equity ticker resolution succeeds — they're unrelated
    concerns, and a user with a broken equity mapping still has every
    right to see their MF holdings were read correctly."""
    from mybroker.portfolio import loader

    monkeypatch.setattr(loader, "load_portfolio", lambda: _portfolio_with_mf())
    _patch(
        monkeypatch, validate_results=[1], unmapped=[], is_tty=True,
    )

    cli.cmd_validate(None)

    assert "Mutual funds" in capsys.readouterr().out
