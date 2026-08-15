"""Screener.in provider — parsing logic tested against a synthetic page
matching the real structure (verified against real HDFCBANK/BEL pages while
building this), no network required. Network-dependent tests are `live`
(skipped by default), matching test_data.py's convention.
"""

from __future__ import annotations

import pytest
import requests
from bs4 import BeautifulSoup

from mybroker.data.cache import Cache
from mybroker.data.screener_provider import (
    ExtraRatios,
    ScreenerProvider,
    _extract_rows,
    _latest_nonblank,
    _parse_number,
    _slugify,
)

# A trimmed synthetic page reproducing the real sections' structure (top-
# ratios list, quarters/ratios/shareholding tables) — real screener.in HTML
# is far larger, but this is the shape every selector in the module depends on.
_SYNTHETIC_PAGE = """
<html><body>
<ul id="top-ratios">
  <li class="flex flex-space-between"><span class="name">Market Cap</span>
    <span class="nowrap value">₹ <span class="number">11,17,181</span> Cr.</span></li>
  <li class="flex flex-space-between"><span class="name">High / Low</span>
    <span class="nowrap value">₹ <span class="number">1,020</span> / <span class="number">722</span></span></li>
  <li class="flex flex-space-between"><span class="name">Stock P/E</span>
    <span class="nowrap value"><span class="number">14.8</span></span></li>
  <li class="flex flex-space-between"><span class="name">Book Value</span>
    <span class="nowrap value">₹ <span class="number">375</span></span></li>
  <li class="flex flex-space-between"><span class="name">Dividend Yield</span>
    <span class="nowrap value"><span class="number">1.79</span> %</span></li>
  <li class="flex flex-space-between"><span class="name">ROCE</span>
    <span class="nowrap value"><span class="number">6.92</span> %</span></li>
  <li class="flex flex-space-between"><span class="name">ROE</span>
    <span class="nowrap value"><span class="number">14.0</span> %</span></li>
</ul>
<section id="quarters">
  <table>
    <thead><tr><th></th><th>Mar 2025</th><th>Jun 2025</th></tr></thead>
    <tbody>
      <tr><td class="text">Gross NPA %</td><td>1.24%</td><td>1.15%</td></tr>
      <tr><td class="text">Net NPA %</td><td>0.42%</td><td></td></tr>
      <tr><td class="text">Financing Margin %</td><td>15%</td><td>16%</td></tr>
    </tbody>
  </table>
</section>
<section id="ratios">
  <table>
    <thead><tr><th></th><th>Mar 2024</th><th>Mar 2025</th></tr></thead>
    <tbody>
      <tr><td class="text">ROE %</td><td>17%</td><td>14%</td></tr>
    </tbody>
  </table>
</section>
<section id="shareholding">
  <table>
    <thead><tr><th></th><th>Mar 2025</th><th>Jun 2025</th></tr></thead>
    <tbody>
      <tr><td class="text">Promoters +</td><td>51.14%</td><td>51.14%</td></tr>
      <tr><td class="text">FIIs +</td><td>44.05%</td><td>41.82%</td></tr>
    </tbody>
  </table>
</section>
</body></html>
"""


class TestSlugify:
    def test_strips_plus_percent_and_lowercases(self):
        assert _slugify("ROE %") == "roe"
        assert _slugify("FIIs +") == "fiis"

    def test_slashes_become_underscores(self):
        assert _slugify("Stock P/E") == "stock_p_e"
        assert _slugify("High / Low") == "high_low"

    def test_collapses_repeated_separators(self):
        assert _slugify("Net  Profit") == "net_profit"


class TestParseNumber:
    def test_plain_number(self):
        assert _parse_number("14.8") == 14.8

    def test_strips_currency_commas_and_percent(self):
        assert _parse_number("₹ 11,17,181") == 1117181.0
        assert _parse_number("1.24%") == 1.24

    def test_strips_crore_suffix(self):
        assert _parse_number("11,17,181 Cr.") == 1117181.0

    def test_blank_and_dash_are_none(self):
        assert _parse_number("") is None
        assert _parse_number("-") is None
        assert _parse_number("—") is None

    def test_none_input_is_none(self):
        assert _parse_number(None) is None

    def test_non_numeric_returns_original_string(self):
        assert _parse_number("strong_buy") == "strong_buy"


class TestExtractRowsAndLatestNonblank:
    def test_extracts_rows_and_period_headers(self):
        soup = BeautifulSoup(_SYNTHETIC_PAGE, "html.parser")
        table = soup.find("section", id="quarters").find("table")
        rows, periods = _extract_rows(table)
        assert periods == ["Mar 2025", "Jun 2025"]
        assert rows["gross_npa"] == [1.24, 1.15]

    def test_latest_nonblank_prefers_rightmost_value(self):
        rows = {"gross_npa": [1.24, 1.15], "net_npa": [0.42, None]}
        latest, period = _latest_nonblank(rows, ["Mar 2025", "Jun 2025"])
        assert latest["gross_npa"] == 1.15   # rightmost
        assert latest["net_npa"] == 0.42     # falls back — rightmost was blank
        assert period == "Jun 2025"

    def test_row_thats_entirely_blank_reports_none(self):
        rows = {"x": [None, None]}
        latest, _ = _latest_nonblank(rows, ["a", "b"])
        assert latest["x"] is None


class TestParseFullPage:
    """ScreenerProvider._parse against the synthetic page — the integration
    of every selector above."""

    def test_top_ratios(self, tmp_path):
        soup = BeautifulSoup(_SYNTHETIC_PAGE, "html.parser")
        p = ScreenerProvider(cache=Cache(tmp_path / "c.db"))
        result = p._parse("HDFCBANK", soup)
        assert result.pe == 14.8
        assert result.book_value == 375.0
        assert result.roe_pct == 14.0
        assert result.roce_pct == 6.92
        assert result.market_cap_cr == 1117181.0
        assert result.fifty_two_week_high == 1020.0
        assert result.fifty_two_week_low == 722.0

    def test_quarterly_bank_extras(self, tmp_path):
        soup = BeautifulSoup(_SYNTHETIC_PAGE, "html.parser")
        p = ScreenerProvider(cache=Cache(tmp_path / "c.db"))
        result = p._parse("HDFCBANK", soup)
        assert result.quarterly_extras["gross_npa"] == 1.15
        assert result.quarterly_extras["net_npa"] == 0.42  # blank latest -> fallback
        assert result.quarterly_extras["financing_margin"] == 16.0

    def test_shareholding(self, tmp_path):
        soup = BeautifulSoup(_SYNTHETIC_PAGE, "html.parser")
        p = ScreenerProvider(cache=Cache(tmp_path / "c.db"))
        result = p._parse("HDFCBANK", soup)
        assert result.shareholding_pct["promoters"] == 51.14
        assert result.shareholding_pct["fiis"] == 41.82

    def test_annual_ratios(self, tmp_path):
        soup = BeautifulSoup(_SYNTHETIC_PAGE, "html.parser")
        p = ScreenerProvider(cache=Cache(tmp_path / "c.db"))
        result = p._parse("HDFCBANK", soup)
        assert result.annual_extras["roe"] == 14.0


class TestGetRatiosErrorHandling:
    """Network/parse failures degrade to a warning-bearing DataResult, never
    an exception — same discipline as yfinance_provider.py."""

    def test_network_failure_returns_warning_not_raise(self, tmp_path):
        class FailingSession:
            headers: dict = {}

            def get(self, *a, **kw):
                raise requests.RequestException("boom")

        p = ScreenerProvider(cache=Cache(tmp_path / "c.db"), session=FailingSession())
        r = p.get_ratios("BEL")
        assert not r.ok
        assert any("boom" in w for w in r.warnings)

    def test_unparseable_page_returns_warning_not_raise(self, tmp_path):
        class Resp:
            text = "<html><body>nothing recognisable here</body></html>"

            def raise_for_status(self):
                pass

        class OddSession:
            headers: dict = {}

            def get(self, *a, **kw):
                return Resp()

        p = ScreenerProvider(cache=Cache(tmp_path / "c.db"), session=OddSession())
        r = p.get_ratios("BEL")
        # A page with none of the expected sections still parses to an
        # (empty) ExtraRatios rather than raising — every field is Optional.
        assert r.ok
        assert isinstance(r.data, ExtraRatios)
        assert r.data.pe is None

    def test_successful_fetch_is_cached(self, tmp_path):
        class Resp:
            text = _SYNTHETIC_PAGE

            def raise_for_status(self):
                pass

        calls = {"n": 0}

        class CountingSession:
            headers: dict = {}

            def get(self, *a, **kw):
                calls["n"] += 1
                return Resp()

        p = ScreenerProvider(cache=Cache(tmp_path / "c.db"), session=CountingSession())
        p.get_ratios("BEL")
        p.get_ratios("BEL")
        assert calls["n"] == 1  # second call served from cache

    def test_disclaimer_present_in_every_result(self, tmp_path):
        class Resp:
            text = _SYNTHETIC_PAGE

            def raise_for_status(self):
                pass

        class Session:
            headers: dict = {}

            def get(self, *a, **kw):
                return Resp()

        p = ScreenerProvider(cache=Cache(tmp_path / "c.db"), session=Session())
        r = p.get_ratios("BEL")
        assert any("screener.in" in w.lower() for w in r.warnings)


@pytest.mark.live
class TestLive:
    def test_real_fetch_against_hdfcbank(self):
        p = ScreenerProvider()
        r = p.get_ratios("HDFCBANK")
        assert r.ok
        assert r.data.pe is not None
        assert "gross_npa" in r.data.quarterly_extras
