"""M5 — outcome scoring loop.

Deterministic grading, no LLM involved: for every recommendation whose
`review_after` date has passed and has no outcome yet, fetch the current
price, compute the return since the recommendation, and write a graded
outcome back to the ledger via `ledger.record_outcome`. This is what closes
the loop the M2 provenance gate started — every recommendation traceable
when made, now every recommendation gradeable after the fact.

Grading is plain arithmetic, not a judgement call:

  - BUY:        "gained" if return >= 0 else "lost"
  - SELL/TRIM:  "avoided_decline" if return <= 0 else "missed_gain" — the
                position sold or reduced would have cost or made money had
                it been held instead.
  - WATCH:      no verdict — it was a deferred decision, not a position
                change, so there is nothing to score against. Price move is
                still reported.

A recommendation with no `price_at_recommendation` captured at log time, or
an unresolvable/unreachable symbol, is marked ungradeable with the reason —
never silently skipped or scored with a guessed number.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime

from mybroker.data.yfinance_provider import YFinanceProvider
from mybroker.ledger import LedgerEntry, due_for_review, record_outcome

_VERDICT_RULES: dict[str, Callable[[float], str]] = {
    "BUY": lambda r: "gained" if r >= 0 else "lost",
    "SELL": lambda r: "avoided_decline" if r <= 0 else "missed_gain",
    "TRIM": lambda r: "avoided_decline" if r <= 0 else "missed_gain",
}


@dataclass
class GradeResult:
    rec_id: str
    symbol: str
    action: str
    graded: bool
    outcome: dict | None = None
    reason: str | None = None  # why ungradeable, when graded is False
    warnings: list[str] = field(default_factory=list)


def grade_entry(entry: LedgerEntry, provider: YFinanceProvider) -> GradeResult:
    """Grade one ledger entry. Never raises — every failure mode returns a
    GradeResult with graded=False and a stated reason."""
    if entry.price_at_recommendation is None:
        return GradeResult(
            entry.rec_id, entry.symbol, entry.action, graded=False,
            reason="No price_at_recommendation was captured in the evidence "
                   "at log time — cannot compute a return.",
        )
    if entry.price_at_recommendation == 0:
        return GradeResult(
            entry.rec_id, entry.symbol, entry.action, graded=False,
            reason="price_at_recommendation was 0 — cannot compute a return.",
        )

    try:
        result = provider.get_quote(entry.symbol)
    except KeyError as exc:
        return GradeResult(
            entry.rec_id, entry.symbol, entry.action, graded=False,
            reason=f"Symbol unresolvable: {exc}",
        )

    if result.data is None:
        return GradeResult(
            entry.rec_id, entry.symbol, entry.action, graded=False,
            reason=f"No live quote available: {'; '.join(result.warnings) or 'unknown error'}",
            warnings=result.warnings,
        )

    price_now = result.data.price
    price_then = entry.price_at_recommendation
    return_pct = (price_now - price_then) / price_then * 100

    logged_at = datetime.fromisoformat(entry.logged_at)
    if logged_at.tzinfo is None:
        logged_at = logged_at.replace(tzinfo=UTC)
    days_held = (datetime.now(UTC) - logged_at).days

    rule = _VERDICT_RULES.get(entry.action)
    verdict = rule(return_pct) if rule else None

    outcome = {
        "graded_at": datetime.now(UTC).isoformat(),
        "price_at_recommendation": price_then,
        "price_at_grading": round(price_now, 4),
        "return_pct": round(return_pct, 2),
        "days_since_recommendation": days_held,
        "verdict": verdict,
        "quote_provenance": asdict(result.provenance),
    }
    return GradeResult(
        entry.rec_id, entry.symbol, entry.action, graded=True,
        outcome=outcome, warnings=result.warnings,
    )


def grade_due_recommendations(
    today: date | None = None, provider: YFinanceProvider | None = None
) -> list[GradeResult]:
    """The M5 job: grade everything `due_for_review()` returns and persist
    outcomes for the ones that graded successfully. Pure Python plus one
    price fetch per entry — no LLM call, no agent session, safe to run from
    an unattended cron job."""
    provider = provider or YFinanceProvider()
    results: list[GradeResult] = []
    for entry in due_for_review(today):
        result = grade_entry(entry, provider)
        if result.graded and result.outcome is not None:
            record_outcome(result.rec_id, result.outcome)
        results.append(result)
    return results
