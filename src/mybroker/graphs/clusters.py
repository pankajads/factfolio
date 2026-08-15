"""Community detection, centrality, and a composite diversification score.

Built on top of `graphs/correlation.py`'s outputs:
  - Louvain communities + eigenvector centrality run on the CORRELATION graph
    (positive-correlation edges, weight = correlation) — the graph where
    "more weight" means "more connected", which is what both algorithms
    assume.
  - The diversification score additionally uses the Mantegna MST's mean edge
    distance, since the MST (not the full correlation graph) is the accepted
    measure of a portfolio's minimal-redundancy risk backbone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import networkx as nx
from networkx.algorithms.community import louvain_communities


def communities(graph: nx.Graph, *, seed: int = 42) -> list[list[str]]:
    """Louvain communities, largest first. A symbol with no positive-weight
    edge to anything forms its own singleton community — this is the honest
    outcome for e.g. a holding that moves independently of everything else,
    not a failure of the algorithm.

    `seed` is fixed (not random) so the same inputs always produce the same
    grouping — a diversification score that changes between two runs of an
    unchanged portfolio would undermine the "track weekly" use case.
    """
    if graph.number_of_nodes() == 0:
        return []
    comms = louvain_communities(graph, weight="weight", seed=seed)
    return sorted((sorted(c) for c in comms), key=len, reverse=True)


def eigenvector_centrality(graph: nx.Graph) -> dict[str, float]:
    """Which holdings sit most centrally in the correlation structure —
    i.e. most "systemically" linked to the rest of the book. A high-weight,
    high-centrality position is a concentration risk the sector labels alone
    would not show: even a well-diversified-by-sector book can have one
    holding that correlates broadly with everything else in it.

    Returns {} if the graph has no edges (centrality is undefined without
    any connections) rather than a misleading uniform score.
    """
    if graph.number_of_edges() == 0:
        return {}
    try:
        raw = nx.eigenvector_centrality_numpy(graph, weight="weight")
    except (nx.PowerIterationFailedConvergence, nx.AmbiguousSolution):  # pragma: no cover
        return {}
    return {k: round(float(v), 4) for k, v in raw.items()}


@dataclass
class DiversificationScore:
    """0-100 composite. Higher = more genuinely diversified.

    Four equally-weighted (25% each) sub-scores, each independently
    documented and independently inspectable — this is a derived statistic
    from real tool outputs, not a judgement call, but a composite score is
    only trustworthy if its construction is visible rather than asserted.

      community_score      — (n_communities - 1) / (n_holdings - 1) * 100.
                              1 community (everything moves together) = 0.
                              n_holdings communities (nothing correlates) = 100.
      concentration_score  — 100 - (largest community's %% of portfolio value).
                              One community holding the whole book = 0.
      hhi_score             — 100 * (1 - HHI/10000). Standard HHI ranges
                              0 (maximally diversified) to 10000 (single
                              holding); this rescales to the same 0-100
                              direction as the other three sub-scores.
      mst_distance_score    — 100 * mean(MST edge distance) / sqrt(2), capped
                              at 100. Mantegna distance is 0 for perfect
                              positive correlation and up to 2 for perfect
                              negative correlation; sqrt(2) is the distance
                              at zero correlation, used as the practical
                              equity-portfolio ceiling for this rescaling.
    """

    community_score: float
    concentration_score: float
    hhi_score: float
    mst_distance_score: float
    overall: float
    n_communities: int
    largest_community: list[str] = field(default_factory=list)
    largest_community_weight_pct: float = 0.0


def diversification_score(
    *,
    comms: list[list[str]],
    weights_pct: dict[str, float],
    mst: nx.Graph,
    hhi: float,
) -> DiversificationScore | None:
    """Combine community structure, HHI, and MST distance into one score.

    Returns None if there isn't enough graph structure to score at all
    (fewer than 2 holdings, or no communities computed) — a fabricated
    "100" or "0" for an un-scoreable portfolio would be worse than admitting
    the score doesn't apply yet.
    """
    n = len(weights_pct)
    if n < 2 or not comms:
        return None

    community_score = ((len(comms) - 1) / (n - 1)) * 100 if n > 1 else 0.0

    comm_weights = [sum(weights_pct.get(s, 0.0) for s in c) for c in comms]
    largest_idx = max(range(len(comms)), key=lambda i: comm_weights[i])
    largest_weight = comm_weights[largest_idx]
    concentration_score = max(0.0, 100 - largest_weight)

    hhi_score = max(0.0, min(100.0, 100 * (1 - hhi / 10000)))

    distances = [d for _, _, d in mst.edges(data="weight") if d is not None]
    if distances:
        mean_distance = sum(distances) / len(distances)
        mst_distance_score = max(0.0, min(100.0, 100 * mean_distance / (2 ** 0.5)))
    else:
        # No edges at all (e.g. every pair excluded for insufficient overlap)
        # means nothing is measurably connected — treat as maximally diverse
        # on this sub-score rather than penalising for a data gap.
        mst_distance_score = 100.0

    overall = (community_score + concentration_score + hhi_score + mst_distance_score) / 4

    return DiversificationScore(
        community_score=round(community_score, 1),
        concentration_score=round(concentration_score, 1),
        hhi_score=round(hhi_score, 1),
        mst_distance_score=round(mst_distance_score, 1),
        overall=round(overall, 1),
        n_communities=len(comms),
        largest_community=comms[largest_idx],
        largest_community_weight_pct=round(largest_weight, 2),
    )
