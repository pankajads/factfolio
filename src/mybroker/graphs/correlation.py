"""Mantegna minimum spanning tree: the correlation "risk skeleton".

Mantegna (1999) turns a correlation matrix into a metric distance —
`d(i,j) = sqrt(2(1 - rho(i,j)))` — and takes the minimum spanning tree over
it. The MST keeps only the (n-1) edges that connect every holding at the
LOWEST total distance, i.e. the strongest, least redundant correlation
backbone. It answers "what is this portfolio's real risk structure",
independent of the sector labels in tickers.yaml — two holdings the sector
map calls unrelated can still land next to each other in the tree if their
prices actually move together (and vice versa).

Correlations are computed PAIRWISE, not over one shared date range across all
symbols. A shared-range approach would shrink every correlation to the
window of the shortest-lived symbol (TMCV/TMPV, both post-demerger since Oct
2025) — throwing away most of the portfolio's usable history to accommodate
its two newest members. Pairwise overlap uses whatever history each PAIR
actually shares, and only excludes a pair when that shared window is too
short to trust (MIN_CORRELATION_OVERLAP_DAYS).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import networkx as nx
import numpy as np

from mybroker.config import MIN_CORRELATION_OVERLAP_DAYS


def daily_returns(series: list[dict]) -> dict[str, float]:
    """{date: simple daily return} from a sorted-by-date close-price series."""
    rows = sorted(series, key=lambda r: r["date"])
    out: dict[str, float] = {}
    for prev, curr in zip(rows, rows[1:], strict=False):
        if prev["close"]:
            out[curr["date"]] = (curr["close"] - prev["close"]) / prev["close"]
    return out


@dataclass
class PairCorrelation:
    a: str
    b: str
    correlation: float
    distance: float          # Mantegna distance: sqrt(2(1 - rho))
    n_overlap_days: int


def mantegna_distance(rho: float) -> float:
    # Clamp for float noise (a correlation of exactly ±1 can round to
    # 1.0000000000000002 and make (1-rho) slightly negative under sqrt).
    rho = max(-1.0, min(1.0, rho))
    return math.sqrt(2 * (1 - rho))


def pairwise_correlations(
    returns_by_symbol: dict[str, dict[str, float]],
    *,
    min_overlap: int = MIN_CORRELATION_OVERLAP_DAYS,
) -> tuple[list[PairCorrelation], list[str]]:
    """Every symbol pair with enough shared history to correlate.

    Returns (correlations, excluded_pairs_summary) — the second element names
    pairs skipped for insufficient overlap, so that absence is visible rather
    than silent.
    """
    symbols = sorted(returns_by_symbol)
    pairs: list[PairCorrelation] = []
    excluded: list[str] = []

    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            common = sorted(set(returns_by_symbol[a]) & set(returns_by_symbol[b]))
            if len(common) < min_overlap:
                excluded.append(f"{a}-{b}: only {len(common)} overlapping days (need {min_overlap})")
                continue
            xa = np.array([returns_by_symbol[a][d] for d in common])
            xb = np.array([returns_by_symbol[b][d] for d in common])
            if xa.std() == 0 or xb.std() == 0:
                excluded.append(f"{a}-{b}: zero-variance return series over the overlap window")
                continue
            rho = float(np.corrcoef(xa, xb)[0, 1])
            if math.isnan(rho):
                excluded.append(f"{a}-{b}: correlation computed to NaN")
                continue
            pairs.append(PairCorrelation(a, b, rho, mantegna_distance(rho), len(common)))

    return pairs, excluded


def correlation_graph(pairs: list[PairCorrelation], symbols: list[str]) -> nx.Graph:
    """Complete-ish weighted graph, edge weight = correlation coefficient.

    Feeds community detection and centrality — both want "high weight =
    strongly linked", which correlation already is. Negative-correlation
    edges are dropped rather than clipped to zero: Louvain modularity assumes
    non-negative weights, and a genuinely inverse relationship (e.g. a hedge)
    is not a community link, so omitting it is more honest than flattening it
    to "no relationship".
    """
    g = nx.Graph()
    g.add_nodes_from(symbols)
    for p in pairs:
        if p.correlation > 0:
            g.add_edge(p.a, p.b, weight=p.correlation, correlation=p.correlation,
                       distance=p.distance, n_overlap_days=p.n_overlap_days)
    return g


def mantegna_mst(pairs: list[PairCorrelation], symbols: list[str]) -> nx.Graph:
    """The minimum spanning tree(s) over Mantegna distance.

    Uses ALL pairs (positive or negative correlation) since distance is
    well-defined either way — an inverse relationship is still real
    structure, just far apart in the tree rather than close. If the
    resulting graph is disconnected (some symbol shares no correlatable
    pair with anything), `minimum_spanning_tree` returns a spanning FOREST:
    one tree per connected component, with isolated symbols left as
    zero-degree nodes rather than forced into a false connection.
    """
    g = nx.Graph()
    g.add_nodes_from(symbols)
    for p in pairs:
        g.add_edge(p.a, p.b, weight=p.distance, correlation=p.correlation,
                   n_overlap_days=p.n_overlap_days)
    if g.number_of_edges() == 0:
        return g
    return nx.minimum_spanning_tree(g, weight="weight")
