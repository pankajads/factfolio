"""Screener.in scraper — supplementary Indian-equity metrics yfinance
doesn't carry: bank Gross/Net NPA %, "Financing Margin %" (screener's P&L-
margin proxy for lending businesses, not exactly bank-reported NIM — labelled
as screener's own term, never renamed to NIM), shareholding pattern
(promoter/FII/DII/government %), and a second, independently-sourced read on
the headline ratios (P/E, ROE, ROCE, book value) yfinance also reports — two
sources agreeing is itself evidence; disagreeing is worth surfacing, not
silently picking one.

No official API exists for screener.in — this is a best-effort HTML scrape.
Checked robots.txt (2026-08-14): `/company/<SYMBOL>/...` — the pages this
touches — is permitted; only `/user/*` (account pages) is disallowed, and
this provider never requests those. Rate-limited and cached like every other
provider in `data/`, and every response's `warnings` says "scraped, not an
official feed, treat as secondary" — never the primary citation for a claim
without saying where it came from.

Symbol resolution: screener.in's URL slug IS the NSE trading symbol (same as
Zerodha's own `Instrument` column) — confirmed against every symbol in this
portfolio, including the TMCV/TMPV demerger symbols. Unlike yfinance_
provider.py, no separate resolution map is needed.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from mybroker.data.base import DataResult, Provenance
from mybroker.data.cache import Cache

BASE_URL = "https://www.screener.in/company"
_USER_AGENT = "mybroker/1.0 (personal portfolio tool; contact via GitHub)"
_RATE_LIMIT_SLEEP = 1.0  # screener.in is a small site — be more conservative than yfinance
_TIMEOUT_S = 15
# Cache TTL for "screener_ratios" lives centrally in data/cache.py's TTL dict
# (24h), same mechanism every other provider uses — not duplicated here.

DISCLAIMER = (
    "Scraped from screener.in, not an official data feed. Screener has no "
    "public API; this parses the same HTML a browser renders and WILL break "
    "silently if their page structure changes — a missing/empty result is "
    "reported as a warning, never guessed. Treat every figure here as a "
    "secondary cross-check, not the primary citation, for anything material."
)


@dataclass
class ExtraRatios:
    symbol: str
    # Top-of-page headline ratios (yfinance also reports most of these —
    # useful as a second, independent source).
    market_cap_cr: float | None = None
    pe: float | None = None
    book_value: float | None = None
    dividend_yield_pct: float | None = None
    roce_pct: float | None = None
    roe_pct: float | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None
    # Sector-specific rows, however screener's own template labels them —
    # e.g. banks/NBFCs get "Gross NPA %", "Net NPA %", "Financing Margin %";
    # other sectors get "OPM %", "Debtor Days", etc. Key = screener's own
    # row label, snake_cased; value = the most recent non-blank period.
    quarterly_extras: dict[str, float | str | None] = field(default_factory=dict)
    annual_extras: dict[str, float | str | None] = field(default_factory=dict)
    shareholding_pct: dict[str, float | None] = field(default_factory=dict)
    period_labels: dict[str, str] = field(default_factory=dict)  # which period each *_extras value is from


def _slugify(label: str) -> str:
    s = re.sub(r"[+%]", "", label).strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def _parse_number(raw: str) -> float | str | None:
    """Best-effort numeric parse. Returns the original string when it isn't
    cleanly numeric (e.g. already-blank cells) rather than forcing a 0."""
    if raw is None:
        return None
    cleaned = raw.strip().replace(",", "").replace("₹", "").replace("%", "")
    cleaned = re.sub(r"\s*Cr\.?\s*$", "", cleaned).strip()  # "11,17,181 Cr." market-cap suffix
    if cleaned in ("", "-", "—"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return raw.strip() or None


class ScreenerProvider:
    name = "screener.in"

    def __init__(self, cache: Cache | None = None, session: requests.Session | None = None) -> None:
        self.cache = cache or Cache()
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", _USER_AGENT)

    def _fetch_html(self, symbol: str, *, consolidated: bool) -> str:
        path = "consolidated/" if consolidated else ""
        url = f"{BASE_URL}/{symbol}/{path}"
        resp = self.session.get(url, timeout=_TIMEOUT_S)
        resp.raise_for_status()
        return resp.text

    def get_ratios(self, symbol: str) -> DataResult:
        """Standalone financials, deliberately — bank NPA/financing-margin
        rows are populated on the standalone page and blank on consolidated
        for every bank checked while building this (HDFCBANK confirmed)."""
        symbol = symbol.upper()
        cache_key = symbol

        if hit := self.cache.get("screener_ratios", cache_key):
            payload, age = hit
            return DataResult(
                data=ExtraRatios(**payload),
                provenance=Provenance.now(
                    self.name, ticker=symbol, cached=True,
                    note=f"cache hit, {age / 3600:.0f}h old",
                ),
                warnings=[DISCLAIMER],
            )

        from bs4 import BeautifulSoup

        try:
            html = self._fetch_html(symbol, consolidated=False)
        except requests.RequestException as exc:
            return DataResult(
                data=None,
                provenance=Provenance.now(self.name, ticker=symbol, note="fetch failed"),
                warnings=[f"{symbol}: screener.in fetch failed ({exc})."],
            )

        try:
            soup = BeautifulSoup(html, "html.parser")
            ratios = self._parse(symbol, soup)
        except Exception as exc:  # noqa: BLE001 - scraper must degrade, never crash the caller
            return DataResult(
                data=None,
                provenance=Provenance.now(self.name, ticker=symbol, note="parse failed"),
                warnings=[
                    f"{symbol}: screener.in page structure did not match the "
                    f"expected shape ({exc}). The site likely changed; this "
                    f"provider needs updating, not the data trusted as-is."
                ],
            )

        self.cache.put("screener_ratios", cache_key, ratios.__dict__)
        time.sleep(_RATE_LIMIT_SLEEP)

        return DataResult(
            data=ratios,
            provenance=Provenance.now(self.name, ticker=symbol, cached=False),
            warnings=[DISCLAIMER],
        )

    # ── Parsing ──────────────────────────────────────────────────────────────
    def _parse(self, symbol: str, soup: Any) -> ExtraRatios:
        out = ExtraRatios(symbol=symbol)

        top = soup.find("ul", id="top-ratios")
        if top:
            flat: dict[str, float | str | None] = {}
            for li in top.find_all("li"):
                name_el, val_el = li.find("span", class_="name"), li.find("span", class_="value")
                if not (name_el and val_el):
                    continue
                flat[_slugify(name_el.get_text(strip=True))] = _parse_number(
                    val_el.get_text(" ", strip=True)
                )
            out.market_cap_cr = _as_float(flat.get("market_cap"))
            out.pe = _as_float(flat.get("stock_p_e"))
            out.book_value = _as_float(flat.get("book_value"))
            out.dividend_yield_pct = _as_float(flat.get("dividend_yield"))
            out.roce_pct = _as_float(flat.get("roce"))
            out.roe_pct = _as_float(flat.get("roe"))
            hl_span = next((li for li in top.find_all("li") if "High" in li.get_text()), None)
            if hl_span:
                nums = [n.get_text(strip=True) for n in hl_span.find_all("span", class_="number")]
                if len(nums) == 2:
                    out.fifty_two_week_high = _as_float(_parse_number(nums[0]))
                    out.fifty_two_week_low = _as_float(_parse_number(nums[1]))

        quarters = soup.find("section", id="quarters")
        if quarters and (table := quarters.find("table")):
            rows, periods = _extract_rows(table)
            out.quarterly_extras, out.period_labels["quarterly"] = _latest_nonblank(rows, periods)

        ratios_section = soup.find("section", id="ratios")
        if ratios_section and (table := ratios_section.find("table")):
            rows, periods = _extract_rows(table)
            out.annual_extras, out.period_labels["annual"] = _latest_nonblank(rows, periods)

        shareholding = soup.find("section", id="shareholding")
        if shareholding and (table := shareholding.find("table")):
            rows, periods = _extract_rows(table)
            latest, _ = _latest_nonblank(rows, periods)
            out.shareholding_pct = {k: _as_float(v) for k, v in latest.items()}

        return out


def _extract_rows(table: Any) -> tuple[dict[str, list[float | str | None]], list[str]]:
    thead = table.find("thead")
    periods = [th.get_text(strip=True) for th in thead.find_all("th")][1:] if thead else []

    rows: dict[str, list[float | str | None]] = {}
    tbody = table.find("tbody")
    if not tbody:
        return rows, periods
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        label = _slugify(cells[0].get_text(" ", strip=True))
        if not label:
            continue
        values = [_parse_number(c.get_text(strip=True)) for c in cells[1:]]
        rows[label] = values
    return rows, periods


def _latest_nonblank(
    rows: dict[str, list[float | str | None]], periods: list[str]
) -> tuple[dict[str, float | str | None], str]:
    """For each row, the most recent (rightmost) non-None value — screener
    lists periods oldest-to-newest left-to-right on every table checked."""
    out: dict[str, float | str | None] = {}
    latest_period = periods[-1] if periods else ""
    for label, values in rows.items():
        for i in range(len(values) - 1, -1, -1):
            if values[i] is not None:
                out[label] = values[i]
                break
        else:
            out[label] = None
    return out, latest_period


def _as_float(v: Any) -> float | None:
    return v if isinstance(v, int | float) else None
