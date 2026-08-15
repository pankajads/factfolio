"""The recommendation ledger: every validated call, dated, for later scoring.

Two representations of the same events, written together:

  `memory/decision_journal.md`  — human-readable, for you to read.
  `memory/ledger.jsonl`         — structured, for the scoring loop (M5) to
                                   read back and grade against actual outcomes.

Only `log_recommendation` (tools/server.py) writes here, and only after
`security.validator.verify_recommendation` has passed. A recommendation
cannot reach the ledger without a provenance-checked evidence trail.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta

from mybroker.config import LEDGER_FILE, MEMORY_DIR, ensure_dirs

LEDGER_JSONL = MEMORY_DIR / "ledger.jsonl"

# How long before a recommendation is due for outcome review, by conviction.
# Higher conviction gets a shorter fuse — it's a stronger claim, so it should
# be checked sooner.
REVIEW_HORIZON_DAYS = {"high": 30, "medium": 60, "low": 90}


@dataclass
class LedgerEntry:
    rec_id: str
    run_id: str
    logged_at: str
    symbol: str
    action: str
    conviction: str
    rationale: str
    evidence: list[dict] = field(default_factory=list)
    tax_impact: dict | None = None
    risk_if_wrong: str = ""
    invalidation_trigger: str = ""
    price_at_recommendation: float | None = None
    review_after: str = ""
    outcome: dict | None = None  # filled in later by the scoring loop (M5)


def _make_rec_id(symbol: str, action: str) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{symbol}-{action}".upper()


def _price_from_evidence(evidence: list[dict]) -> float | None:
    """Best-effort: pull a price-shaped value out of the evidence list, so
    later outcome scoring has an entry price without re-parsing prose."""
    for item in evidence:
        field_name = str(item.get("field", "")).lower()
        if any(k in field_name for k in ("price", "ltp", "quote", "close")):
            try:
                return float(item["value"])
            except (KeyError, TypeError, ValueError):
                continue
    return None


def append_recommendation(rec: dict, *, run_id: str) -> LedgerEntry:
    """Append one validated recommendation. Caller must have already checked
    it with `security.validator.verify_recommendation` — this function does
    not re-validate, it only records."""
    ensure_dirs()

    symbol = str(rec["symbol"]).upper()
    action = str(rec["action"]).upper()
    conviction = str(rec["conviction"]).lower()
    horizon = REVIEW_HORIZON_DAYS.get(conviction, 60)

    entry = LedgerEntry(
        rec_id=_make_rec_id(symbol, action),
        run_id=run_id,
        logged_at=datetime.now(UTC).isoformat(),
        symbol=symbol,
        action=action,
        conviction=conviction,
        rationale=str(rec["rationale"]),
        evidence=rec.get("evidence") or [],
        tax_impact=rec.get("tax_impact"),
        risk_if_wrong=str(rec.get("risk_if_wrong", "")),
        invalidation_trigger=str(rec.get("invalidation_trigger", "")),
        price_at_recommendation=_price_from_evidence(rec.get("evidence") or []),
        review_after=(date.today() + timedelta(days=horizon)).isoformat(),
    )

    with LEDGER_JSONL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(entry), default=str) + "\n")

    _append_markdown(entry)
    return entry


def _append_markdown(e: LedgerEntry) -> None:
    price = f"₹{e.price_at_recommendation:,.2f}" if e.price_at_recommendation else "—"
    lines = [
        f"\n## {e.logged_at[:10]} — {e.action} {e.symbol}  `{e.rec_id}`",
        "",
        f"**Conviction:** {e.conviction}  ·  **Price at call:** {price}  ·  "
        f"**Review after:** {e.review_after}",
        "",
        f"{e.rationale}",
        "",
    ]
    if e.tax_impact:
        lines.append(f"**Tax impact:** {json.dumps(e.tax_impact)}")
    if e.risk_if_wrong:
        lines.append(f"**Risk if wrong:** {e.risk_if_wrong}")
    if e.invalidation_trigger:
        lines.append(f"**Invalidation trigger:** {e.invalidation_trigger}")
    if e.evidence:
        cites = "; ".join(
            f"{ev.get('tool')}.{ev.get('field')}={ev.get('value')}" for ev in e.evidence
        )
        lines.append(f"**Evidence:** {cites}")

    with LEDGER_FILE.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def load_ledger() -> list[LedgerEntry]:
    """Read back every logged recommendation. Used by the M5 scoring loop."""
    if not LEDGER_JSONL.exists():
        return []
    out = []
    for line in LEDGER_JSONL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(LedgerEntry(**data))
    return out


def due_for_review(today: date | None = None) -> list[LedgerEntry]:
    """Recommendations whose review_after date has passed and have no
    recorded outcome yet — the M5 scoring loop's work queue."""
    today = today or date.today()
    return [
        e for e in load_ledger()
        if e.outcome is None and date.fromisoformat(e.review_after) <= today
    ]


def record_outcome(rec_id: str, outcome: dict) -> LedgerEntry:
    """Write a graded outcome back to one ledger entry (M5's scoring loop).

    The ledger is small (one line per recommendation ever made), so a full
    read-modify-write of ledger.jsonl is simpler and safer than an in-place
    patch, and it leaves `append_recommendation`'s append-only contract for
    NEW entries untouched — this is the one place that rewrites the file.
    """
    entries = load_ledger()
    match = next((e for e in entries if e.rec_id == rec_id), None)
    if match is None:
        raise KeyError(f"No ledger entry with rec_id={rec_id!r}.")

    match.outcome = outcome

    with LEDGER_JSONL.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(asdict(e), default=str) + "\n")

    _append_outcome_markdown(match)
    return match


def _append_outcome_markdown(e: LedgerEntry) -> None:
    o = e.outcome or {}
    verdict = o.get("verdict")
    verdict_str = f" — **{verdict.replace('_', ' ')}**" if verdict else ""
    price_then, price_now = o.get("price_at_recommendation"), o.get("price_at_grading")
    move = (
        f"₹{price_then:,.2f} → ₹{price_now:,.2f} ({o.get('return_pct'):+.2f}% over "
        f"{o.get('days_since_recommendation')} days)"
        if price_then is not None and price_now is not None
        else "not gradeable — see `memory/ledger.jsonl`"
    )
    line = f"\n**Outcome ({o.get('graded_at', '')[:10]}) for `{e.rec_id}`{verdict_str}:** {move}\n"
    with LEDGER_FILE.open("a", encoding="utf-8") as fh:
        fh.write(line)
