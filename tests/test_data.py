"""Data layer: cache behaviour and ticker-resolution safety.

Network-dependent tests are marked `live` and skipped by default:
    uv run pytest -m live
"""

import time

import pytest

from mybroker.data.base import AnalystConsensus, DataResult, Fundamentals, Provenance
from mybroker.data.cache import Cache
from mybroker.data.yfinance_provider import YFinanceProvider


@pytest.fixture
def cache(tmp_path):
    return Cache(tmp_path / "test.db")


class TestCache:
    def test_roundtrip(self, cache):
        cache.put("quote", "BEL.NS", {"price": 405.15})
        hit = cache.get("quote", "BEL.NS")
        assert hit is not None
        payload, age = hit
        assert payload["price"] == 405.15
        assert age < 5

    def test_miss_returns_none(self, cache):
        assert cache.get("quote", "NOTHING") is None

    def test_expired_entry_is_a_miss(self, cache, monkeypatch):
        monkeypatch.setitem(__import__(
            "mybroker.data.cache", fromlist=["TTL"]).TTL, "quote", 0)
        cache.put("quote", "BEL.NS", {"price": 1})
        time.sleep(0.01)
        assert cache.get("quote", "BEL.NS") is None

    def test_stale_read_still_returns_expired_data(self, cache, monkeypatch):
        """Serving honestly-labelled stale data beats failing the run."""
        monkeypatch.setitem(__import__(
            "mybroker.data.cache", fromlist=["TTL"]).TTL, "quote", 0)
        cache.put("quote", "BEL.NS", {"price": 405.15})
        time.sleep(0.01)
        assert cache.get("quote", "BEL.NS") is None       # normal read misses
        stale = cache.get_stale("quote", "BEL.NS")        # stale read hits
        assert stale is not None and stale[0]["price"] == 405.15

    def test_kinds_do_not_collide(self, cache):
        cache.put("quote", "X", {"v": 1})
        cache.put("fundamentals", "X", {"v": 2})
        assert cache.get("quote", "X")[0]["v"] == 1
        assert cache.get("fundamentals", "X")[0]["v"] == 2

    def test_clear_by_kind(self, cache):
        cache.put("quote", "A", {})
        cache.put("history", "B", {})
        cache.clear("quote")
        assert cache.get("quote", "A") is None
        assert cache.get("history", "B") is not None

    def test_corrupt_payload_is_a_miss_not_a_crash(self, cache):
        import sqlite3
        with sqlite3.connect(cache.path) as c:
            c.execute(
                "INSERT INTO cache VALUES (?,?,?,?)",
                ("quote:BAD", "quote", "{not json", time.time()),
            )
        assert cache.get("quote", "BAD") is None


class TestTickerResolution:
    """The safety property: never guess a ticker."""

    def test_unmapped_symbol_raises_rather_than_guessing(self):
        p = YFinanceProvider()
        with pytest.raises(KeyError, match="not in tickers.yaml"):
            p.resolve("DEFINITELY_NOT_A_REAL_SYMBOL")

    def test_error_message_warns_against_ns_suffix_guess(self):
        p = YFinanceProvider()
        with pytest.raises(KeyError) as exc:
            p.resolve("NOPE")
        assert ".NS" in str(exc.value)

    @pytest.mark.parametrize("symbol", ["SAMPLERENAME", "SAMPLESPLIT-A", "SAMPLESPLIT-B"])
    def test_renamed_and_demerged_symbols_resolve(self, symbol):
        """The bundled tickers.yaml's own rename/demerger examples — these
        are exactly the shape of symbol a naive .NS guess gets wrong.
        Real-world regression: this test previously hardcoded the
        maintainer's actual portfolio symbols (ETERNAL/HEXT/TMCV/TMPV),
        which only ever worked locally by masking through a stale
        .cache/resolved_tickers.json — it silently broke in CI the moment
        the bundled file's example data changed. Pointing this at the
        bundled file's own illustrative entries instead means it can never
        depend on anyone's personal tickers.yaml again."""
        assert YFinanceProvider().resolve(symbol).endswith((".NS", ".BO"))


class TestProvenance:
    def test_every_result_carries_source_and_timestamp(self):
        r = DataResult(data=1, provenance=Provenance.now("test"))
        assert r.provenance.source == "test"
        assert r.provenance.as_of
        assert r.to_dict()["provenance"]["source"] == "test"

    def test_missing_fields_are_reported_not_zeroed(self):
        f = Fundamentals(symbol="X", pe_ratio=20.0)
        missing = f.missing_fields()
        assert "roe" in missing
        assert "pe_ratio" not in missing


class TestAnalystConsensus:
    """Evidence, not a signal — the properties describe, they never verdict."""

    def test_target_upside_pct(self):
        a = AnalystConsensus(symbol="X", current_price=100.0, target_mean_price=120.0)
        assert a.target_upside_pct == pytest.approx(20.0)

    def test_target_upside_pct_none_without_both_prices(self):
        assert AnalystConsensus(symbol="X", current_price=100.0).target_upside_pct is None
        assert AnalystConsensus(symbol="X", target_mean_price=100.0).target_upside_pct is None

    def test_trend_position_above_both(self):
        a = AnalystConsensus(
            symbol="X", current_price=110, fifty_day_average=100, two_hundred_day_average=90
        )
        assert a.trend_position == "above both 50-DMA and 200-DMA"

    def test_trend_position_below_both(self):
        a = AnalystConsensus(
            symbol="X", current_price=80, fifty_day_average=100, two_hundred_day_average=110
        )
        assert a.trend_position == "below both 50-DMA and 200-DMA"

    def test_trend_position_mixed(self):
        a = AnalystConsensus(
            symbol="X", current_price=105, fifty_day_average=100, two_hundred_day_average=110
        )
        assert a.trend_position == "mixed — above one DMA, below the other"

    def test_trend_position_none_when_data_missing(self):
        assert AnalystConsensus(symbol="X", current_price=100).trend_position is None

    def test_missing_fields_reports_uncovered_stock(self):
        """A small-cap with zero analyst coverage — every target field None,
        reported as missing, not defaulted to a number."""
        a = AnalystConsensus(symbol="X", current_price=100.0)
        missing = a.missing_fields()
        assert "target_mean_price" in missing
        assert "current_price" not in missing


@pytest.mark.live
class TestLive:
    def test_quote_matches_holdings_csv_ltp(self):
        """Cross-check the provider against the broker's own LTP."""
        r = YFinanceProvider().get_quote("HDFCBANK")
        assert r.ok
        assert r.data.price == pytest.approx(729.0, rel=0.15)

    def test_short_history_produces_a_warning(self):
        r = YFinanceProvider().get_history("TMCV", days=250)
        assert r.ok
        assert any("Short history" in w for w in r.warnings)

    def test_analyst_consensus_covered_large_cap(self):
        r = YFinanceProvider().get_analyst_consensus("BEL")
        assert r.ok
        assert r.data.number_of_analysts is not None
        assert r.data.recommendation_key is not None

    def test_analyst_consensus_uncovered_small_cap_warns_not_fails(self):
        """GUJTHEM has no analyst coverage at all — absence, not an error."""
        r = YFinanceProvider().get_analyst_consensus("GUJTHEM")
        assert r.ok  # the fetch itself succeeds — DMA data still comes back
        assert r.data.number_of_analysts is None
        assert any("no analyst coverage" in w for w in r.warnings)
