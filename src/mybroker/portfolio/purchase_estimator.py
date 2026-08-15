"""Tentative purchase-date estimation from price history + avg_cost.

No purchase dates exist anywhere in the loaded holdings — every tax figure
downstream gets stamped "purchase date unknown — assumed SHORT term (higher
tax)" as a result (`tax.py: compute_sale`). This module makes a best-effort,
clearly-labelled ESTIMATE instead of leaving that a blank assumption: for
each equity position, search its own price history, backward from today,
for the most recent close within tolerance of avg_cost, and treat that as a
tentative buy date.

This is fundamentally an approximation, and every result says so:

  - `avg_cost` is a single blended average that may span several real lots
    bought on different dates — matching one date to it is a simplification,
    not a reconstruction of the actual purchase history.
  - A range-bound stock can trade at the same price on many dates. Searching
    MOST-RECENT-FIRST and stopping at the first match is the conservative
    choice: a more recent date means a shorter holding period, which is the
    same "assume the higher tax, not the lower one" bias `tax.py` already
    applies to a fully-unknown purchase date. This estimator narrows that
    assumption with evidence; it does not flip it to the aggressive
    direction.
  - Corporate actions (demergers, bonuses, splits) can make historical
    prices incomparable to today's avg_cost even for a position that long
    predates them — TMCV/TMPV (the Tata Motors CV/PV demerger) are the named
    example elsewhere in this codebase. A position whose available history
    never comes within tolerance is reported UNESTIMATED, never force-fit to
    the nearest-available price regardless of distance.

Never presented as fact. Every consumer of a `DateEstimate` must carry
`note`/`confident` forward, not just `estimated_date` — see
`tools/server.py: compute_tax_impact` for the pattern.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime

from mybroker.config import MEMORY_DIR, ensure_dirs

ESTIMATES_JSON = MEMORY_DIR / "estimated_purchase_dates.json"
ESTIMATES_MD = MEMORY_DIR / "estimated_purchase_dates.md"

# Tolerance bands tried tightest-first — stop at the first band that finds
# ANY match, so the reported tolerance is always the tightest that worked.
_TOLERANCE_BANDS_PCT = (1.5, 3.0, 5.0, 8.0)


@dataclass
class DateEstimate:
    symbol: str
    avg_cost: float
    estimated_date: str | None       # ISO date, or None if no confident match
    matched_price: float | None
    tolerance_pct: float | None      # the band that actually matched
    holding_days_from_today: int | None
    method: str
    confident: bool
    note: str


def estimate_purchase_date(
    symbol: str,
    avg_cost: float,
    price_history: list[dict],
    *,
    today: date | None = None,
) -> DateEstimate:
    """`price_history`: [{"date": "YYYY-MM-DD", "close": float}, ...], any
    order — sorted internally, most-recent-first, before searching."""
    today = today or date.today()
    method = "most recent historical close within tolerance of avg_cost"

    if not price_history or avg_cost is None or avg_cost <= 0:
        return DateEstimate(
            symbol=symbol, avg_cost=avg_cost, estimated_date=None,
            matched_price=None, tolerance_pct=None,
            holding_days_from_today=None, method=method, confident=False,
            note="No price history or invalid avg_cost — cannot estimate.",
        )

    ordered = sorted(
        (r for r in price_history if r.get("close") is not None),
        key=lambda r: r["date"], reverse=True,
    )
    if not ordered:
        return DateEstimate(
            symbol=symbol, avg_cost=avg_cost, estimated_date=None,
            matched_price=None, tolerance_pct=None,
            holding_days_from_today=None, method=method, confident=False,
            note="Price history had no usable close values — cannot estimate.",
        )

    for tolerance in _TOLERANCE_BANDS_PCT:
        band = avg_cost * tolerance / 100
        for row in ordered:
            close = float(row["close"])
            if abs(close - avg_cost) <= band:
                d = datetime.fromisoformat(row["date"]).date()
                return DateEstimate(
                    symbol=symbol, avg_cost=round(avg_cost, 4),
                    estimated_date=d.isoformat(), matched_price=round(close, 4),
                    tolerance_pct=tolerance,
                    holding_days_from_today=(today - d).days,
                    method=method, confident=True,
                    note=(
                        f"Closing price ₹{close:,.2f} on {d.isoformat()} was "
                        f"within {tolerance:.1f}% of avg_cost ₹{avg_cost:,.2f} "
                        f"— the most recent such match, searched backward from "
                        f"{today.isoformat()}. TENTATIVE: avg_cost blends every "
                        f"real lot into one number, so this is an approximate "
                        f"reference, not a verified buy date. Confirm against "
                        f"the actual contract note before relying on it for a "
                        f"real tax filing."
                    ),
                )

    oldest = ordered[-1]["date"]
    return DateEstimate(
        symbol=symbol, avg_cost=round(avg_cost, 4), estimated_date=None,
        matched_price=None, tolerance_pct=None,
        holding_days_from_today=None, method=method, confident=False,
        note=(
            f"No close within {_TOLERANCE_BANDS_PCT[-1]:.0f}% of avg_cost "
            f"₹{avg_cost:,.2f} in the available history (back to {oldest}). "
            f"Likely predates available history, or a corporate action "
            f"(demerger/split/bonus) makes historical prices incomparable to "
            f"today's avg_cost. Falls back to unknown/assumed-short-term."
        ),
    )


def save_estimates(estimates: list[DateEstimate]) -> None:
    """Persist to memory/ in both forms: JSON for code to read back
    (compute_tax_impact's lookup), Markdown for a human to skim."""
    ensure_dirs()
    generated_at = datetime.now(UTC).isoformat()

    payload = {
        "generated_at": generated_at,
        "method": (
            "Searches each symbol's own price history, most-recent-first, "
            "for the closest close to avg_cost. TENTATIVE — not from contract "
            "notes. See docs/MILESTONES.md and each entry's own note."
        ),
        "estimates": {e.symbol: asdict(e) for e in estimates},
    }
    ESTIMATES_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        "# Estimated purchase dates",
        "",
        f"_Generated {generated_at[:10]} — **tentative, not from contract "
        f"notes.** Each estimate is the most recent historical close within "
        f"tolerance of avg_cost, searched backward from today. Confirm "
        f"against real contract notes before filing taxes on these dates._",
        "",
        "| Symbol | Estimated date | Matched price | Tolerance | Holding days | Confident |",
        "|---|---|---|---|---|---|",
    ]
    for e in sorted(estimates, key=lambda x: x.symbol):
        lines.append(
            f"| {e.symbol} | {e.estimated_date or '—'} | "
            f"{f'₹{e.matched_price:,.2f}' if e.matched_price else '—'} | "
            f"{f'{e.tolerance_pct:.1f}%' if e.tolerance_pct else '—'} | "
            f"{e.holding_days_from_today if e.holding_days_from_today is not None else '—'} | "
            f"{'yes' if e.confident else 'no'} |"
        )
    lines.append("")
    for e in sorted(estimates, key=lambda x: x.symbol):
        lines.append(f"**{e.symbol}:** {e.note}\n")

    ESTIMATES_MD.write_text("\n".join(lines), encoding="utf-8")


def load_estimates() -> dict[str, DateEstimate]:
    """Read back the saved estimates, keyed by symbol. Empty dict if the
    file doesn't exist yet — callers must treat that as "no estimate", not
    an error."""
    if not ESTIMATES_JSON.exists():
        return {}
    try:
        payload = json.loads(ESTIMATES_JSON.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {
        symbol: DateEstimate(**data)
        for symbol, data in (payload.get("estimates") or {}).items()
    }
