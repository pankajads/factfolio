"""Pure-Python, deterministic drafting of tickers.yaml entries from
whatever holdings are already on disk — no network, no LLM.

Shared by two callers that both need it:
    `factfolio init`     (ui/cli.py's cmd_init)      — first-run setup
    `factfolio validate` (tickers_validate.py)       — self-heals a missing
                                                        or incomplete
                                                        tickers.yaml so the
                                                        gate never silently
                                                        probes the bundled
                                                        illustrative
                                                        defaults instead of
                                                        your real holdings

Genuine short-symbol sources only (Zerodha's `Instrument`, a generic
`Symbol` column) — see discover_equity_symbols_for_drafting's own docstring
for why a full-company-name column (a demat PDF, Sharekhan's "Scrip Name")
can't be drafted safely here. Those stay on the "not in tickers.yaml"
warning path, or `factfolio init`'s AI-assisted resolver.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml


def draft_ticker_entry(symbol: str) -> str:
    """One new tickers.yaml entry, clearly marked as unverified — see
    seed_draft_ticker_entries. `candidates` is a guess `factfolio
    validate` will empirically confirm or reject, never silently trusted;
    sector/tier/bucket stay honestly unclassified (`Unknown`/`unknown`)
    rather than guessed, since nothing in a holdings file says what sector
    a stock is in."""
    return (
        f"  {symbol}:\n"
        f"    name: {symbol}  # TODO: replace with the real company name\n"
        f"    candidates: [{symbol}.NS, {symbol}.BO]  # DRAFT — unverified guess\n"
        f"    sector: Unknown  # TODO\n"
        f"    tier: unknown  # TODO: large | mid | small\n"
        f"    bucket: satellite  # TODO: core | satellite\n"
        f"    notes: >\n"
        f"      DRAFT — auto-seeded from your holdings. Verify the candidates\n"
        f"      resolve (`factfolio validate`) and fill in sector/tier/bucket\n"
        f"      before relying on this.\n"
    )


def insert_ticker_yaml_block(tickers_file: Path, entries_text: str) -> bool:
    """Insert `entries_text` (one or more already-formatted entries) into
    the `symbols:` section of `tickers_file`, right after the last real
    entry and before any blank/comment lines already leading into the
    next top-level key (`indices:`/`settings:` in the bundled file, EOF
    otherwise) — never between them. Returns False (and touches nothing)
    if there's no `symbols:` key to anchor on at all, e.g. a hand-edited
    file that's drifted too far from the template to guess an insertion
    point in safely.
    """
    text = tickers_file.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    try:
        symbols_idx = next(i for i, line in enumerate(lines) if line.startswith("symbols:"))
    except StopIteration:
        return False

    # `symbols: {}` — an explicit empty mapping, the bundled scaffold's
    # steady state before anything's been drafted — can't take indented
    # block children as written. Rewrite it to bare `symbols:` first so
    # the entries inserted below parse as its children rather than
    # dangling top-level lines.
    if lines[symbols_idx].strip() == "symbols: {}":
        lines[symbols_idx] = "symbols:\n"

    end = len(lines)
    for i in range(symbols_idx + 1, len(lines)):
        if re.match(r"^[A-Za-z_]", lines[i]):
            end = i
            break
    insert_at = end
    while insert_at > symbols_idx + 1 and (
        lines[insert_at - 1].strip() == "" or lines[insert_at - 1].lstrip().startswith("#")
    ):
        insert_at -= 1

    lines[insert_at:insert_at] = ["\n" + entries_text]
    tickers_file.write_text("".join(lines), encoding="utf-8")
    return True


def seed_draft_ticker_entries(tickers_file: Path) -> set[str]:
    """Append a DRAFT entry for every symbol found in your holdings that
    isn't mapped in `tickers_file` yet — genuine short-symbol sources only
    (Zerodha's `Instrument`, a generic `Symbol` column); a source with
    only a full company name (a demat holdings PDF, say) has no reliable
    symbol to derive, so those stay on the existing "not in tickers.yaml"
    warning path instead of being guessed at here.

    Never touches an existing entry, and re-running this is exactly how
    new holdings get picked up over time — safe to call on every `init`
    or `validate`, not just the first. Returns the symbols actually added.
    """
    from mybroker.config import HOLDINGS_EQUITY, HOLDINGS_INBOX_DIR
    from mybroker.logging_setup import get_logger
    from mybroker.portfolio.importers import (
        discover_equity_symbols_for_drafting,
        discover_inbox_files,
    )

    logger = get_logger(__name__)
    existing = set((yaml.safe_load(tickers_file.read_text()) or {}).get("symbols") or {})

    found: set[str] = set()
    if HOLDINGS_EQUITY.exists():
        found |= discover_equity_symbols_for_drafting(HOLDINGS_EQUITY)
    for file in discover_inbox_files(HOLDINGS_INBOX_DIR):
        found |= discover_equity_symbols_for_drafting(file)

    new_symbols = sorted(found - existing)
    if not new_symbols:
        return set()

    block = "".join(draft_ticker_entry(s) for s in new_symbols)
    if not insert_ticker_yaml_block(tickers_file, block):
        logger.warning(
            "ticker_seeding: found %d new symbol(s) (%s) but %s has no "
            "'symbols:' key to insert into — nothing drafted",
            len(new_symbols), ", ".join(new_symbols), tickers_file,
        )
        return set()

    logger.info(
        "ticker_seeding: drafted %d new symbol(s) into %s: %s",
        len(new_symbols), tickers_file, ", ".join(new_symbols),
    )
    return set(new_symbols)


def is_pristine_draft(tickers_file: Path, symbol: str) -> bool:
    """True if `symbol` exists in `tickers_file` but is still exactly the
    placeholder draft_ticker_entry wrote — `name:` literally equal to the
    ticker itself, never backfilled with a real company name.

    That's the one case where an existing entry is provably safe to update
    automatically: a name that's still the bare symbol proves no human has
    edited this entry yet, so there's no deliberate customization to
    clobber. Anything else about the entry (a real name, or any other
    field touched) means treating it as hands-off, same as always.
    """
    data = yaml.safe_load(tickers_file.read_text(encoding="utf-8")) or {}
    entry = (data.get("symbols") or {}).get(symbol)
    return bool(entry) and entry.get("name") == symbol


def backfill_draft_entry(
    tickers_file: Path, symbol: str, *, name: str, sector: str | None = None,
    alias: str | None = None,
) -> bool:
    """Update just the `name:` (and `sector:`, if given and not already
    set) fields on an EXISTING tickers.yaml entry in place — call only
    after is_pristine_draft() confirms it's safe. Never touches
    candidates/tier/bucket/notes; those still need a human's own judgment
    regardless of where the name came from.

    `alias`, if given, gets inserted as a new `aliases:` line right after
    `name:` — recording the RAW source text this backfill was resolved
    from, verbatim. See config.py's resolve_symbol_by_name for why this
    matters: a source file's own PDF-wrapping ("VODAFON E IDEA LIMITED")
    or a broker's abbreviations routinely make the raw text normalize to
    something the clean `name:` field this call just wrote never will —
    without recording the exact raw text somewhere, this same holding
    gets re-flagged as "still unmapped" the very next time its source
    file is re-scanned, even though it was already correctly resolved
    moments ago. A pristine draft (this function's only precondition)
    never already has an aliases line, so this is always a fresh insert,
    never a merge.

    Returns False (touching nothing) if the entry can't be found at all.
    """
    text = tickers_file.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    key_pattern = re.compile(rf"^  {re.escape(symbol)}:\s*$")
    try:
        key_idx = next(i for i, line in enumerate(lines) if key_pattern.match(line))
    except StopIteration:
        return False

    end = len(lines)
    for i in range(key_idx + 1, len(lines)):
        # The next symbol key ("  OTHER:") or a new top-level section
        # ("indices:") both end this entry's block.
        if re.match(r"^  \S.*:\s*$", lines[i]) or re.match(r"^[A-Za-z_]", lines[i]):
            end = i
            break

    changed = False
    new_lines = list(lines[: key_idx + 1])
    for i in range(key_idx + 1, end):
        line = lines[i]
        if re.match(r"^    name:\s", line):
            new_lines.append(f"    name: {name}\n")
            changed = True
            if alias:
                alias_yaml = yaml.safe_dump([alias], default_flow_style=True).strip()
                new_lines.append(f"    aliases: {alias_yaml}  # raw source text this was resolved from\n")
            continue
        if sector and re.match(r"^    sector:\s", line):
            new_lines.append(f"    sector: {sector}\n")
            continue
        new_lines.append(line)
    new_lines.extend(lines[end:])

    if not changed:
        return False

    tickers_file.write_text("".join(new_lines), encoding="utf-8")
    return True


def add_alias_to_entry(tickers_file: Path, symbol: str, alias: str) -> bool:
    """Record `alias` (raw source text) as another recognised name for an
    EXISTING entry, merging into whatever `aliases:` list it already has
    rather than overwriting it — the case backfill_draft_entry doesn't
    cover: an entry that's already fully resolved (a real `name:`, no
    longer a pristine draft) but whose raw source text still doesn't
    match that name closely enough for resolve_symbol_by_name's fuzzy
    match (a PDF line-wrap — "TATA STEEL LIMITED" vs a clean "Tata Steel"
    — or a broker's own abbreviation).

    Without this, a human confirming "yes, this really is the same
    holding" at the interactive accept/edit/skip prompt (cli.py's
    _write_reviewed_symbol) had nothing durable to write when the
    suggested symbol already existed — the confirmation was thrown away
    every single run, so the exact same holding came back as "never
    mapped" on the very next `factfolio validate`, with no way out of the
    loop at all.

    A no-op (returns True, touches nothing) if `alias` is already
    recorded. Returns False (touching nothing) if `symbol` isn't in
    tickers_file at all.
    """
    text = tickers_file.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    key_pattern = re.compile(rf"^  {re.escape(symbol)}:\s*$")
    try:
        key_idx = next(i for i, line in enumerate(lines) if key_pattern.match(line))
    except StopIteration:
        return False

    end = len(lines)
    for i in range(key_idx + 1, len(lines)):
        if re.match(r"^  \S.*:\s*$", lines[i]) or re.match(r"^[A-Za-z_]", lines[i]):
            end = i
            break

    alias_idx = None
    for i in range(key_idx + 1, end):
        if re.match(r"^    aliases:\s", lines[i]):
            alias_idx = i
            break

    if alias_idx is not None:
        existing = yaml.safe_load(lines[alias_idx].split(":", 1)[1].split("#")[0]) or []
        if alias in existing:
            return True
        existing.append(alias)
        alias_yaml = yaml.safe_dump(existing, default_flow_style=True).strip()
        lines[alias_idx] = f"    aliases: {alias_yaml}  # raw source text this was resolved from\n"
        tickers_file.write_text("".join(lines), encoding="utf-8")
        return True

    name_idx = None
    for i in range(key_idx + 1, end):
        if re.match(r"^    name:\s", lines[i]):
            name_idx = i
            break
    if name_idx is None:
        return False

    alias_yaml = yaml.safe_dump([alias], default_flow_style=True).strip()
    lines.insert(name_idx + 1, f"    aliases: {alias_yaml}  # raw source text this was resolved from\n")
    tickers_file.write_text("".join(lines), encoding="utf-8")
    return True


def holdings_present() -> bool:
    """True if there's anything to draft tickers.yaml entries from — the
    legacy holdings.csv, or any broker export dropped into
    holdings_inbox/. Doesn't guarantee any of it actually parses to a
    genuine short-symbol column (see discover_equity_symbols_for_drafting)
    — just that there's *something* there, for a "no holdings at all yet"
    message versus a "holdings exist, tickers.yaml just isn't built" one.
    """
    from mybroker.config import HOLDINGS_EQUITY, HOLDINGS_INBOX_DIR
    from mybroker.portfolio.importers import discover_inbox_files

    return HOLDINGS_EQUITY.exists() or bool(discover_inbox_files(HOLDINGS_INBOX_DIR))


def collect_unmapped_holdings() -> list[dict]:
    """Every full-name holding (name + quantity + avg_cost) across
    holdings.csv and holdings_inbox/ that discover_equity_symbols_for_
    drafting can't auto-draft a symbol for — a demat statement's "Scrip
    Name" column, say — and that isn't already resolvable against the
    current tickers.yaml.

    Shared by both callers that need to know this count, not just draft
    what they can: `factfolio init`'s AI-assisted resolver (cli.py's
    _suggest_ticker_matches) actually resolves them; `factfolio validate`
    (tickers_validate.py) doesn't — and deliberately never will, since
    validate is documented as pure deterministic Python with no LLM call —
    but it must still SAY so out loud instead of reporting "all tickers
    resolved" while these sit completely unmapped. Before this existed,
    that gap was silent: validate only ever round-tripped tickers.yaml's
    own recorded symbols against yfinance, never checked whether every
    holding actually made it into tickers.yaml in the first place — so a
    portfolio with unmapped full-name holdings still got a clean "✓ All
    tickers resolved", and the only way to discover the missing ones was to
    separately think to re-run `init`.
    """
    from mybroker.config import HOLDINGS_EQUITY, HOLDINGS_INBOX_DIR
    from mybroker.portfolio.importers import discover_inbox_files, discover_unmapped_full_names

    sources = []
    if HOLDINGS_EQUITY.exists():
        sources.append(HOLDINGS_EQUITY)
    sources.extend(discover_inbox_files(HOLDINGS_INBOX_DIR))

    holdings: list[dict] = []
    seen: set[str] = set()
    for file in sources:
        for h in discover_unmapped_full_names(file):
            if h["name"] not in seen:
                seen.add(h["name"])
                holdings.append(h)
    return holdings
