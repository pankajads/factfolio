"""Bipartite stock ↔ mutual-fund-scheme overlap.

True look-through exposure needs TWO things this system does not have yet:

  1. `holdings_mf.csv` — which schemes are held and how much (the loader
     already supports this file; it's just never been supplied).
  2. A scheme-holdings data source — which underlying stocks each scheme
     holds, and at what weight. AMFI's NAV file (the only MF data source
     built so far) gives price, not portfolio composition; scheme
     holdings disclosure is a separate feed this system does not fetch.

`bipartite_overlap` below is the real computation, ready for both to exist.
Until then, the tool wrapper in tools/server.py reports the gap honestly
rather than pretending overlap was checked.
"""

from __future__ import annotations

import networkx as nx


def bipartite_overlap(
    stock_weights_pct: dict[str, float],
    scheme_holdings_pct: dict[str, dict[str, float]],
) -> dict:
    """Collapse scheme holdings onto direct-equity positions.

    `scheme_holdings_pct` is {scheme_name: {stock_symbol: pct_of_scheme}}.
    Returns each stock's TRUE exposure — direct weight plus its share of
    every scheme's allocation to it, scaled by that scheme's share of the
    total mutual-fund book — alongside the bipartite graph itself so a
    caller can inspect scheme-to-scheme co-ownership (two schemes holding
    the same stock are indirectly linked, exactly like two portfolio
    holdings correlating).
    """
    g = nx.Graph()
    for stock in stock_weights_pct:
        g.add_node(stock, bipartite=0, kind="stock")
    for scheme in scheme_holdings_pct:
        g.add_node(scheme, bipartite=1, kind="scheme")
        for stock, pct in scheme_holdings_pct[scheme].items():
            g.add_edge(scheme, stock, weight=pct)

    total_mf_weight = sum(scheme_holdings_pct.get(s, {}).get("__scheme_weight_pct__", 0) for s in scheme_holdings_pct)

    true_exposure: dict[str, float] = dict(stock_weights_pct)
    for scheme, holdings in scheme_holdings_pct.items():
        scheme_weight_pct = holdings.get("__scheme_weight_pct__", 0.0)
        for stock, pct_of_scheme in holdings.items():
            if stock == "__scheme_weight_pct__":
                continue
            contribution = scheme_weight_pct * (pct_of_scheme / 100.0)
            true_exposure[stock] = true_exposure.get(stock, 0.0) + contribution

    return {
        "true_exposure_pct": {k: round(v, 2) for k, v in true_exposure.items()},
        "total_mf_weight_pct": round(total_mf_weight, 2),
        "graph": g,
    }
