"""yfinance-backed provider for NSE equities and indices.

Two behaviours worth knowing about:

1. Tickers are resolved from the pre-validated map written by
   scripts/validate_tickers.py — never by appending ".NS" to a Zerodha symbol.
   That guess is what breaks on ETERNAL (renamed), HEXT (relisted) and
   TMCV/TMPV (demerged), and it breaks silently.

2. yfinance answers an unknown ticker with an EMPTY FRAME, not an exception.
   Every fetch here therefore checks for emptiness explicitly and returns a
   warning-bearing result rather than letting a NaN propagate into the maths.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

import yfinance as yf

from mybroker.config import RESOLVED_TICKERS, symbol_meta
from mybroker.data.base import (
    AnalystConsensus,
    DataResult,
    Fundamentals,
    MarketDataProvider,
    Provenance,
    Quote,
)
from mybroker.data.cache import Cache

_RATE_LIMIT_SLEEP = 0.25


def _clean(x: Any) -> float | None:
    """Coerce to float, mapping NaN/inf to None so they never reach the maths."""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v


class YFinanceProvider(MarketDataProvider):
    name = "yfinance"

    def __init__(self, cache: Cache | None = None) -> None:
        self.cache = cache or Cache()
        self._resolved: dict[str, dict] = {}
        if RESOLVED_TICKERS.exists():
            payload = json.loads(RESOLVED_TICKERS.read_text())
            self._resolved = {**payload.get("symbols", {}), **payload.get("indices", {})}

    # ── Ticker resolution ────────────────────────────────────────────────────
    def resolve(self, symbol: str) -> str:
        """Zerodha symbol → yfinance ticker.

        Prefers the empirically validated map. Falls back to the first
        candidate in tickers.yaml, and raises if the symbol is unmapped —
        deliberately never guessing f"{symbol}.NS".
        """
        if symbol in self._resolved:
            return self._resolved[symbol]["ticker"]

        meta = symbol_meta(symbol)  # raises KeyError with guidance if absent
        candidates = meta.get("candidates") or []
        if not candidates:
            raise KeyError(f"{symbol} has no candidate tickers in tickers.yaml")
        return candidates[0]

    def has_sufficient_history(self, symbol: str) -> bool:
        """False when the series is too short for correlation maths."""
        entry = self._resolved.get(symbol)
        return bool(entry["sufficient_history"]) if entry else True

    # ── Fetches ──────────────────────────────────────────────────────────────
    def get_quote(self, symbol: str) -> DataResult:
        ticker = self.resolve(symbol)

        if hit := self.cache.get("quote", ticker):
            payload, age = hit
            return DataResult(
                data=Quote(**payload),
                provenance=Provenance.now(
                    self.name, ticker=ticker, cached=True,
                    note=f"cache hit, {age / 60:.0f}m old",
                ),
            )

        try:
            df = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
        except Exception as exc:
            return self._stale_or_fail("quote", ticker, symbol, exc)

        if df is None or df.empty:
            return self._stale_or_fail(
                "quote", ticker, symbol,
                RuntimeError("yfinance returned an empty frame"),
            )

        closes = df["Close"].dropna()
        last = float(closes.iloc[-1])
        prev = float(closes.iloc[-2]) if len(closes) > 1 else None

        quote = Quote(
            symbol=symbol,
            price=last,
            previous_close=prev,
            day_change_pct=((last - prev) / prev * 100) if prev else None,
            day_high=_clean(df["High"].iloc[-1]),
            day_low=_clean(df["Low"].iloc[-1]),
            volume=int(v) if (v := _clean(df["Volume"].iloc[-1])) else None,
        )
        self.cache.put("quote", ticker, quote.__dict__)
        time.sleep(_RATE_LIMIT_SLEEP)

        return DataResult(
            data=quote,
            provenance=Provenance.now(self.name, ticker=ticker, cached=False),
        )

    def get_history(self, symbol: str, *, days: int = 250) -> DataResult:
        ticker = self.resolve(symbol)
        ident = f"{ticker}:{days}"

        if hit := self.cache.get("history", ident):
            payload, age = hit
            return DataResult(
                data=payload,
                provenance=Provenance.now(
                    self.name, ticker=ticker, cached=True,
                    note=f"cache hit, {age / 3600:.0f}h old",
                ),
                # Recomputed, not cached: a caveat that silently disappears on
                # the second run is worse than no caveat at all.
                warnings=self._history_warnings(symbol, payload, days),
            )

        period = f"{max(1, math.ceil(days / 250))}y"
        try:
            df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
        except Exception as exc:
            return self._stale_or_fail("history", ident, symbol, exc)

        if df is None or df.empty:
            return self._stale_or_fail(
                "history", ident, symbol,
                RuntimeError("yfinance returned an empty frame"),
            )

        closes = df["Close"].dropna().tail(days)
        series = [
            {"date": idx.strftime("%Y-%m-%d"), "close": float(val)}
            for idx, val in closes.items()
        ]

        self.cache.put("history", ident, series)
        time.sleep(_RATE_LIMIT_SLEEP)

        return DataResult(
            data=series,
            provenance=Provenance.now(self.name, ticker=ticker, cached=False),
            warnings=self._history_warnings(symbol, series, days),
        )

    @staticmethod
    def _history_warnings(symbol: str, series: list, days: int) -> list[str]:
        """Caveats derived from the series itself, so they survive caching."""
        if len(series) >= days:
            return []
        return [
            f"{symbol}: requested {days} days, got {len(series)}. "
            f"Short history — treat long-window statistics with caution."
        ]

    def get_fundamentals(self, symbol: str) -> DataResult:
        ticker = self.resolve(symbol)

        if hit := self.cache.get("fundamentals", ticker):
            payload, age = hit
            return DataResult(
                data=Fundamentals(**payload),
                provenance=Provenance.now(
                    self.name, ticker=ticker, cached=True,
                    note=f"cache hit, {age / 86400:.0f}d old",
                ),
            )

        try:
            info = yf.Ticker(ticker).info or {}
        except Exception as exc:
            return self._stale_or_fail("fundamentals", ticker, symbol, exc)

        # yfinance >= 1.x returns dividendYield ALREADY as a percentage
        # (BEL: 0.62 meaning 0.62%). A "<1 means fraction, so ×100" heuristic
        # turns 0.62% into 62%, which is what an earlier version did. Derive
        # from rate/price instead, which is unambiguous, and fall back to the
        # reported figure only when that is impossible.
        dy = _clean(info.get("dividendYield"))
        rate = _clean(info.get("dividendRate"))
        price = _clean(info.get("currentPrice")) or _clean(info.get("previousClose"))
        if rate is not None and price:
            dy = rate / price * 100
        elif dy is not None and dy > 25:
            # No sane Indian equity yields >25%; treat as unusable rather than
            # letting an absurd figure reach the report.
            dy = None

        f = Fundamentals(
            symbol=symbol,
            market_cap=_clean(info.get("marketCap")),
            pe_ratio=_clean(info.get("trailingPE")),
            pb_ratio=_clean(info.get("priceToBook")),
            roe=(r * 100 if (r := _clean(info.get("returnOnEquity"))) is not None else None),
            debt_to_equity=_clean(info.get("debtToEquity")),
            dividend_yield=dy,
            eps=_clean(info.get("trailingEps")),
            book_value=_clean(info.get("bookValue")),
            sector=info.get("sector"),
            industry=info.get("industry"),
            fifty_two_week_high=_clean(info.get("fiftyTwoWeekHigh")),
            fifty_two_week_low=_clean(info.get("fiftyTwoWeekLow")),
        )

        warnings: list[str] = []
        if missing := f.missing_fields():
            warnings.append(
                f"{symbol}: no data for {', '.join(missing)}. "
                f"Common for thinly-covered small caps and recent listings — "
                f"do not treat absence as a zero."
            )

        self.cache.put("fundamentals", ticker, f.__dict__)
        time.sleep(_RATE_LIMIT_SLEEP)

        return DataResult(
            data=f,
            provenance=Provenance.now(self.name, ticker=ticker, cached=False),
            warnings=warnings,
        )

    def get_analyst_consensus(self, symbol: str) -> DataResult:
        """Analyst price targets/rating plus 50/200-DMA trend position —
        EVIDENCE for the agent to reason with, not a signal. See
        AnalystConsensus's own docstring for what this deliberately does not
        claim to be."""
        ticker = self.resolve(symbol)

        if hit := self.cache.get("analyst_consensus", ticker):
            payload, age = hit
            cached_ac = AnalystConsensus(**payload)
            return DataResult(
                data=cached_ac,
                provenance=Provenance.now(
                    self.name, ticker=ticker, cached=True,
                    note=f"cache hit, {age / 3600:.0f}h old",
                ),
                # Recomputed from the cached payload, not itself cached — a
                # coverage warning that silently disappears on the second
                # call is worse than no warning at all (same rule
                # get_history's _history_warnings already follows).
                warnings=(
                    [f"{symbol}: no analyst coverage found — common for small/micro "
                     f"caps. Absence is not bearish or bullish, it's absence."]
                    if cached_ac.number_of_analysts is None else []
                ),
            )

        try:
            info = yf.Ticker(ticker).info or {}
        except Exception as exc:
            return self._stale_or_fail("analyst_consensus", ticker, symbol, exc)

        rec_key = info.get("recommendationKey")
        ac = AnalystConsensus(
            symbol=symbol,
            current_price=_clean(info.get("currentPrice")) or _clean(info.get("previousClose")),
            target_mean_price=_clean(info.get("targetMeanPrice")),
            target_high_price=_clean(info.get("targetHighPrice")),
            target_low_price=_clean(info.get("targetLowPrice")),
            target_median_price=_clean(info.get("targetMedianPrice")),
            number_of_analysts=(
                int(n) if (n := _clean(info.get("numberOfAnalystOpinions"))) is not None else None
            ),
            recommendation_key=rec_key if rec_key and rec_key != "none" else None,
            recommendation_mean=_clean(info.get("recommendationMean")),
            fifty_day_average=_clean(info.get("fiftyDayAverage")),
            two_hundred_day_average=_clean(info.get("twoHundredDayAverage")),
        )

        warnings: list[str] = []
        if ac.number_of_analysts is None:
            warnings.append(
                f"{symbol}: no analyst coverage found — common for small/micro "
                f"caps. Absence is not bearish or bullish, it's absence."
            )

        self.cache.put("analyst_consensus", ticker, ac.__dict__)
        time.sleep(_RATE_LIMIT_SLEEP)

        return DataResult(
            data=ac,
            provenance=Provenance.now(self.name, ticker=ticker, cached=False),
            warnings=warnings,
        )

    # ── Failure handling ─────────────────────────────────────────────────────
    def _stale_or_fail(
        self, kind: str, ident: str, symbol: str, exc: Exception
    ) -> DataResult:
        """Prefer an honestly-labelled stale value over failing the run."""
        if stale := self.cache.get_stale(kind, ident):
            payload, age = stale
            data = payload
            if kind == "quote":
                data = Quote(**payload)
            elif kind == "fundamentals":
                data = Fundamentals(**payload)
            elif kind == "analyst_consensus":
                data = AnalystConsensus(**payload)
            return DataResult(
                data=data,
                provenance=Provenance.now(
                    self.name, ticker=ident, cached=True,
                    note=f"STALE ({age / 3600:.1f}h old) — live fetch failed",
                ),
                warnings=[
                    f"{symbol}: live fetch failed ({exc}). Serving cached data "
                    f"{age / 3600:.1f}h old. Verify before acting on it."
                ],
            )

        return DataResult(
            data=None,
            provenance=Provenance.now(self.name, ticker=ident, note="fetch failed"),
            warnings=[f"{symbol}: no data available ({exc})."],
        )
