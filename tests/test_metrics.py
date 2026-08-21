"""portfolio/metrics.py's snapshot() — deterministic arithmetic over a
loaded Portfolio. Covers the mutual-fund side specifically: a real-world
bug report where MF holdings' data was fully present and correctly valued
(equity_value/mf_value/core_pct all correct), but every MF row in the
"Top positions" table displayed as an unrecognisable raw code
("14050001.05 2066") instead of the fund's name — indistinguishable from
missing data at a glance, even though nothing was actually missing.
"""

from __future__ import annotations

import pytest

from mybroker.portfolio.loader import MFPosition, Portfolio
from mybroker.portfolio.metrics import snapshot


def _mf(scheme_name, amfi_code, current_value, invested=None, folio=""):
    return MFPosition(
        scheme_name=scheme_name,
        amfi_code=amfi_code,
        units=10.0,
        avg_nav=10.0,
        current_nav=current_value / 10.0,
        invested=invested if invested is not None else current_value * 0.9,
        current_value=current_value,
        folio=folio,
    )


def test_mf_position_key_is_the_scheme_name_not_amfi_code():
    """The actual regression: a fund's amfi_code is an opaque registry
    number nobody recognises on sight — and for at least one real broker
    export, isn't even reliably a genuine AMFI code (its own internal
    "Scheme Code" gets picked up by the same loose matching). The scheme
    name is what a human looking at a positions table actually needs to
    see, regardless of what amfi_code holds."""
    portfolio = Portfolio(mutual_funds=[
        _mf("UTI Large Cap Fund - Growth", "14050001.05 2066", 130940.90),
    ])

    snap = snapshot(portfolio)

    assert snap.positions[0].key == "UTI Large Cap Fund - Growth"
    assert snap.positions[0].label == "UTI Large Cap Fund - Growth"


def test_mf_position_key_is_the_scheme_name_even_with_no_amfi_code():
    """Not just "prefer scheme_name when amfi_code looks bad" — scheme_name
    is the key unconditionally, matching a fund with no amfi_code at all
    (the `or m.scheme_name` fallback this replaces already handled that
    case; this test just locks in it still works)."""
    portfolio = Portfolio(mutual_funds=[
        _mf("Some Fund - Growth", "", 50000.0),
    ])

    snap = snapshot(portfolio)

    assert snap.positions[0].key == "Some Fund - Growth"


def test_mf_holdings_are_not_silently_missing_from_the_snapshot():
    """The reported symptom, precisely: with real invested/current values
    present, MF holdings must show up in totals, core_pct, the sector
    breakdown, and the ranked positions list — not just be countable in
    aggregate while invisible everywhere a human actually looks."""
    portfolio = Portfolio(mutual_funds=[
        _mf("UTI Large Cap Fund - Growth", "14050001.05 2066", 130940.90, invested=121993.90),
        _mf("Kotak Large Cap Fund - Growth", "14050237.00 2066", 97726.75, invested=85995.70),
    ])

    snap = snapshot(portfolio)

    assert snap.mf_value == pytest.approx(228667.65)
    assert snap.core_pct == pytest.approx(100.0)  # MF is always core
    mf_sector = next(s for s in snap.sectors if s.key == "Mutual Funds")
    assert mf_sector.weight_pct == pytest.approx(100.0)
    assert {p.label for p in snap.positions} == {
        "UTI Large Cap Fund - Growth", "Kotak Large Cap Fund - Growth",
    }


def test_folio_disambiguates_the_same_scheme_held_under_two_folios():
    """The real-world case this covers: a direct-plan investor with no
    AMFI code at all (a bank/broker sold them the fund directly) whose
    CAS statement lists the SAME scheme twice under two different
    folios — a genuinely separate second lot, not a duplicate row. Without
    folio, both would render as identical, indistinguishable labels in
    the positions table."""
    portfolio = Portfolio(mutual_funds=[
        _mf("NIPPON INDIA SMALL CAP FUND - GROWTH PLAN GROWTH OPTION", "",
            50160.53, folio="499352098905"),
        _mf("NIPPON INDIA SMALL CAP FUND - GROWTH PLAN GROWTH OPTION", "",
            151049.44, folio="499352099031"),
    ])

    snap = snapshot(portfolio)

    labels = {p.label for p in snap.positions}
    assert labels == {
        "NIPPON INDIA SMALL CAP FUND - GROWTH PLAN GROWTH OPTION (Folio 499352098905)",
        "NIPPON INDIA SMALL CAP FUND - GROWTH PLAN GROWTH OPTION (Folio 499352099031)",
    }
    # Values must still be attributable to the right lot, not swapped or
    # merged.
    by_label = {p.label: p.value for p in snap.positions}
    assert by_label[
        "NIPPON INDIA SMALL CAP FUND - GROWTH PLAN GROWTH OPTION (Folio 499352098905)"
    ] == pytest.approx(50160.53)
    assert by_label[
        "NIPPON INDIA SMALL CAP FUND - GROWTH PLAN GROWTH OPTION (Folio 499352099031)"
    ] == pytest.approx(151049.44)


def test_folio_disambiguation_only_applies_on_an_actual_collision():
    """The common case — each scheme name appears once — must stay exactly
    as clean as a bare scheme name; folio should never clutter a label
    that was never actually ambiguous."""
    portfolio = Portfolio(mutual_funds=[
        _mf("UTI Large Cap Fund - Growth", "", 130940.90, folio="588354645193"),
    ])

    snap = snapshot(portfolio)

    assert snap.positions[0].label == "UTI Large Cap Fund - Growth"


def test_folio_disambiguation_skipped_when_folio_is_missing():
    """A collision with no folio to disambiguate with can't be fixed by
    this mechanism — must not crash or produce a broken "(Folio )" label,
    just fall back to the bare (still-ambiguous) name."""
    portfolio = Portfolio(mutual_funds=[
        _mf("Some Fund - Growth", "", 10000.0, folio=""),
        _mf("Some Fund - Growth", "", 20000.0, folio=""),
    ])

    snap = snapshot(portfolio)

    assert all(p.label == "Some Fund - Growth" for p in snap.positions)
