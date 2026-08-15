"""Resolve every portfolio symbol to a working yfinance ticker.

This is a GATE. Nothing downstream — no metrics, no agent run — should proceed
until this exits 0. yfinance answers an unknown ticker with an empty frame
rather than an error, so without this check a rename or demerger silently
produces missing data that looks like a code bug three layers later.

Writes the resolved map to .cache/resolved_tickers.json so runtime never
re-probes.

Exposed two ways — both run the identical check:
    factfolio validate           # the CLI subcommand
    uv run validate-tickers      # standalone console script (pyproject.toml)

Lives inside the package (not scripts/) specifically so both of those work
however factfolio was installed — a loose script in scripts/ only exists in
a git checkout, not in a wheel installed from PyPI or a frozen build.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import yfinance as yf

from mybroker.config import RESOLVED_TICKERS, ensure_dirs, load_tickers

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
OK, FAIL, WARN = f"{GREEN}✓{RESET}", f"{RED}✗{RESET}", f"{YELLOW}!{RESET}"


def probe(ticker: str, period: str = "1y") -> tuple[bool, int, float | None]:
    """Try one ticker. Returns (usable, n_rows, last_close)."""
    try:
        df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    except Exception:
        return False, 0, None
    if df is None or df.empty or "Close" not in df.columns:
        return False, 0, None
    closes = df["Close"].dropna()
    if closes.empty:
        return False, 0, None
    return True, len(closes), float(closes.iloc[-1])


def resolve_group(group: dict, kind: str, min_history: int) -> tuple[dict, list[str]]:
    """Resolve every entry in a group, trying candidates in order."""
    resolved: dict[str, dict] = {}
    failures: list[str] = []

    for key, meta in group.items():
        candidates = meta.get("candidates", [])
        name = meta.get("name", key)
        winner = None
        best_short = None  # best candidate that resolved but has thin history

        # Prefer the first candidate with ENOUGH history. A candidate that
        # merely returns rows is not good enough — Yahoo serves 1-row stubs for
        # some delisted/renamed index symbols, and accepting one silently
        # poisons every downstream calculation.
        for cand in candidates:
            usable, rows, last = probe(cand)
            if usable and rows >= min_history:
                winner = (cand, rows, last)
                break
            if usable and (best_short is None or rows > best_short[1]):
                best_short = (cand, rows, last)
            time.sleep(0.25)  # yfinance rate-limits bursts aggressively

        if winner is None and best_short is not None:
            winner = best_short  # resolved, but will be flagged below

        if winner is None:
            print(f"  {FAIL} {key:<14} {name}")
            print(f"       {DIM}tried: {', '.join(candidates) or '(none)'}{RESET}")
            failures.append(key)
            continue

        ticker, rows, last = winner
        short = rows < min_history
        mark = WARN if short else OK
        flag = f"  {YELLOW}INSUFFICIENT_HISTORY{RESET}" if short else ""
        alt = f"  {DIM}(fallback — not {candidates[0]}){RESET}" if ticker != candidates[0] else ""

        proxy = f"  {DIM}(proxy){RESET}" if meta.get("is_proxy") else ""
        price = f"₹{last:,.2f}" if last is not None else "—"
        print(f"  {mark} {key:<14} → {ticker:<18} {rows:>4}d  {price:>12}{flag}{alt}{proxy}")

        resolved[key] = {
            "ticker": ticker,
            "name": name,
            "history_days": rows,
            "last_close": last,
            "sufficient_history": not short,
            "is_proxy": bool(meta.get("is_proxy", False)),
            "sector": meta.get("sector"),
            "tier": meta.get("tier"),
            "bucket": meta.get("bucket"),
            "kind": kind,
        }
        time.sleep(0.25)

    return resolved, failures


def main() -> int:
    ensure_dirs()
    cfg = load_tickers()
    min_history = cfg.get("settings", {}).get("min_history_days", 60)

    print(f"\n{DIM}Resolving tickers via yfinance "
          f"(min history {min_history}d for correlation maths){RESET}\n")

    print("Holdings")
    sym_resolved, sym_failures = resolve_group(cfg["symbols"], "equity", min_history)

    print("\nIndices")
    idx_resolved, idx_failures = resolve_group(cfg["indices"], "index", min_history)

    payload = {
        "resolved_at": datetime.now(UTC).isoformat(),
        "symbols": sym_resolved,
        "indices": idx_resolved,
    }
    RESOLVED_TICKERS.write_text(json.dumps(payload, indent=2))

    total = len(cfg["symbols"]) + len(cfg["indices"])
    failures = sym_failures + idx_failures
    short = [k for k, v in {**sym_resolved, **idx_resolved}.items()
             if not v["sufficient_history"]]

    print(f"\n{DIM}{'─' * 68}{RESET}")
    print(f"Resolved {total - len(failures)}/{total}   →  {RESOLVED_TICKERS.name}")

    if short:
        print(f"\n{WARN} Short history (excluded from correlation maths):")
        for k in short:
            v = {**sym_resolved, **idx_resolved}[k]
            print(f"    {k} — {v['history_days']}d")

    if failures:
        print(f"\n{FAIL} UNRESOLVED: {', '.join(failures)}")
        print(f"  {DIM}Add working candidates to src/mybroker/data/tickers.yaml{RESET}\n")
        return 1

    print(f"\n{OK} All tickers resolved.\n")
    return 0
