# Milestones

This tracks build progress. It was reconstructed on 2026-08-14 from `M<n>`
tags scattered across code comments/docstrings (`ledger.py`,
`agents/orchestrator.py`, `config.py`, `tools/server.py`, tests) — keep it
updated going forward instead of letting status live only in comments.

## M1 — Base engine (single-agent report)

**Status: done.**

Core analysis engine: load portfolio, compute snapshot/metrics, generate a
dated report. Superseded in practice by the M2 orchestrator, but the
underlying engine (`portfolio/loader.py`, `portfolio/metrics.py`,
`portfolio/policy.py`) is what every later milestone builds on.

- `uv run mybroker report` — works
- `uv run mybroker status` — deterministic, no LLM

## M2 — Orchestrator: subagent graph, provenance gate, adversarial review

**Status: done.**

- `agents/orchestrator.py` — delegates to a roster of specialist subagents
  (`agents/definitions.py`) and synthesises findings
- Two gates before any recommendation reaches the report:
  1. **devils-advocate** — prompted adversarial review (not code-enforced)
  2. **`log_recommendation`** — code-enforced: every numeric claim in the
     evidence is checked against the run's actual tool-call log
     (`security/validator.py`) before acceptance into the ledger
- Regression suite exists (`tests/test_log_recommendation_tool.py`,
  `tests/test_validator.py`) built from a real bug caught in the first live
  M2 run

## M3 — Graph theory & risk

**Status: done — but wasn't actually reachable until 2026-08-14.**

- `graphs/correlation.py`, `graphs/clusters.py`, `graphs/overlap.py`
- `portfolio/risk.py`
- Tools: `compute_correlation_graph`, `compute_risk_metrics`,
  `compute_overlap` (`tools/server.py`)
- `MIN_CORRELATION_OVERLAP_DAYS` config for pairwise-overlap correlation
  trust (`config.py`)

**Two real bugs found and fixed while building M5/chat, both silent because
nothing had ever called these tools end-to-end:**
1. `compute_correlation_graph`, `compute_risk_metrics`, `compute_overlap`
   were fully implemented but **never added to `ALL_TOOLS`** — the
   orchestrator, subagents, and now chat had no way to actually invoke them,
   despite `risk-officer`'s own prompt correctly describing them as
   available. Fixed: added to `ALL_TOOLS`.
2. `compute_risk_metrics` referenced `MIN_CORRELATION_OVERLAP_DAYS` without
   importing it — a `NameError` waiting on the first insufficient-overlap
   warning, caught by `ruff` (`F821`) once the tool became reachable. Fixed:
   added the import.
3. `risk-officer`'s and `mf-analyst`'s prompts were rewritten to actually
   call the tools they now have access to (`compute_risk_metrics`,
   `compute_correlation_graph`, `compute_overlap`) instead of asserting the
   metrics were uncomputable.

## M4 — Streamlit dashboard

**Status: done** (2026-08-14).

Scoped from two candidates (dashboard, mutual-fund support) — see
correction below on why MF support dropped out.

- `ui/dashboard.py` — read-only visual view of the same engine `mybroker
  status` uses (`portfolio/loader.py` → `portfolio/metrics.py` →
  `portfolio/policy.py`). No LLM calls, no network.
- Sections: headline stat tiles (value, P&L, core/satellite, HHI), sector
  allocation + position-weight bar charts, a diverging P&L bar chart,
  the policy-breach table (severity icon + label), and a report viewer
  that renders `reports/*.md` in-page.
- Charts built per the `dataviz` skill: sequential single-hue (blue) for
  magnitude bars, the validated blue↔red diverging pair for P&L (not
  green/red — that failed the CVD gate outright at ΔE 4.1 deutan, run via
  `scripts/validate_palette.js`), fixed status colors + icon/label pairing
  for breach severity, hover tooltips on every chart, and a "show as table"
  twin under each one.
- Wired into the CLI: `mybroker dashboard` (`ui/cli.py: cmd_dashboard`)
  shells out to `streamlit run` on the module, matching the `cmd_validate`
  subprocess pattern.
- Verified via `streamlit.testing.v1.AppTest` (no exceptions, 8 metrics /
  4 tables render) plus a live headless `streamlit run` smoke test.

**Correction to the original scoping:** mutual-fund support was floated
as the other half of M4, but turned out to already be fully implemented —
`portfolio/loader.py: load_mutual_funds()`, `MFPosition`, and
`metrics.py`'s folding of `mf_value`/`core_pct` are all real code with a
passing test (`test_absent_mf_file_is_not_an_error`). The 2026-08-14
report's `has_mutual_funds: false` was because no `holdings_mf.csv` *data
file* exists yet, not a missing feature. No M4 engineering work was needed
there — the dashboard now also visualizes `mf_value` correctly the moment
that file is added.

## M5 — Outcome scoring loop

**Status: done** (2026-08-14).

- `scoring.py` — `grade_entry()` / `grade_due_recommendations()`: pure
  Python, one live quote per due recommendation, no LLM call. Verdict rules
  are plain arithmetic, not judgement:
  - `BUY`: `gained` if return ≥ 0 else `lost`
  - `SELL`/`TRIM`: `avoided_decline` if return ≤ 0 else `missed_gain`
  - `WATCH`: no verdict (a deferred decision, not a position change) — price
    move still reported
  - Anything without a captured `price_at_recommendation`, a zero price, or
    an unresolvable/unreachable symbol is `graded=False` with a stated
    `reason` — never silently skipped or guessed.
- `ledger.py: record_outcome()` — the write-back half; full read-modify-write
  of `ledger.jsonl` (small file, one line per recommendation ever made),
  keeping `append_recommendation`'s append-only contract for new entries
  untouched. Appends a one-line outcome note to `decision_journal.md` too.
- MCP tool `review_recommendation_outcomes` — grades what's due, then
  returns newly-graded, ungradeable-and-why, full graded history, and a
  verdict tally. Available to chat (`agents/chat.py`) so "how did my past
  calls do" is answerable directly; excluded from the orchestrator's
  mandatory workflow (nothing requires it there yet).
- `mybroker cron` (`ui/cli.py: cmd_cron`) — the unattended entry point:
  runs `grade_due_recommendations()`, logs a JSON line per run to
  `logs/cron.jsonl`, prints a terminal-friendly summary, exit code reflects
  whether the *run* errored (not whether anything was due). Idempotent —
  a graded entry is never re-graded. See README for a crontab example.
- Tests: `tests/test_scoring.py`, 14 cases — verdict rules, ungradeable
  paths (missing/zero price, unresolvable symbol, no quote), and a full
  ledger round-trip (`due_for_review` → grade → persisted → excluded).

## Chat & cron interfaces

**Status: done** (2026-08-14). `ui/cli.py`'s docstring promise — "Every
interface (CLI, dashboard, chat, cron) calls the same engine" — is now true
for all four.

- **Chat** (`agents/chat.py`, `mybroker chat`): a terminal REPL, deliberately
  NOT the M2 orchestrator — one agent, direct tool access, no subagent
  roster, no mandatory devils-advocate pass, `MODEL_WORKER` (cheaper) instead
  of `MODEL_ORCHESTRATOR`. Uses `ClaudeSDKClient` for real multi-turn state
  (vs. the orchestrator's single-shot `query()`). `log_recommendation` is
  deliberately excluded from its tool list — chat answers questions, it does
  not create ledger-tracked recommendations; the system prompt tells the
  user to run `mybroker report` for that. Same `audit_and_guard` /
  `capture_tool_result` hooks as the orchestrator, under a `chat-`-prefixed
  `run_id` so its calls are distinguishable in `logs/tool_calls.jsonl`.
- **Chat, embedded** (`ui/dashboard.py`, Chat tab): the same engine
  (`agents/chat.build_options`), inside the dashboard. Streamlit reruns the
  whole script on every interaction, which doesn't suit a persistent
  `ClaudeSDKClient` — worked around with a background thread running its own
  asyncio event loop, held in `st.session_state` across reruns; each turn is
  dispatched onto it via `run_coroutine_threadsafe`. The Overview tab stays
  fully LLM-free; only the Chat tab makes live calls.
- **Cron**: see M5 above — the cron job's entire job *is* M5's grading loop.

## Universal holdings importer + `holdings_inbox/`

**Status: done** (2026-08-14).

- `config.py: HOLDINGS_INBOX_DIR` — drop any broker export here: csv, xls,
  xlsx, or pdf, equity or mutual fund, any filename. Gitignored like the
  other holdings files.
- `portfolio/importers.py` — the general-purpose parser `load_equity`/
  `load_mutual_funds` don't attempt (those stay strict, on purpose — they're
  what the golden-value tests pin). For inbox files: read into a plain grid
  regardless of format (`_read_csv_grid`/`_read_excel_grid`/`_read_pdf_grid`
  via pdfplumber), scan for a header row by keyword (not exact name) and
  classify it equity vs. mutual-fund, slice to data rows (stop at blank or a
  "Total" row), map columns by keyword-containment, and build the same
  `EquityPosition`/`MFPosition` dataclasses used everywhere else. A file or
  row that can't be confidently classified raises, naming the file — never
  silently parsed as empty.
- `portfolio/loader.py: load_portfolio()` — now merges the legacy
  `holdings.csv`/`holdings_mf.csv` pair with everything `discover_inbox_files()`
  finds. `include_inbox=False` reproduces the old root-files-only behaviour
  exactly (what the golden-value tests use, since they're pinned to
  `holdings.csv` alone and predate any MF data existing).
- Proven against a real file: the user's actual Sharekhan MF statement
  (`holdings_inbox/holding_mf.xls` — PII header rows, Indian-format number
  grouping, a trailing Total row) parses to 7 fund lots whose invested/
  current totals reconcile exactly against an independent pandas re-read of
  the same file (`tests/test_portfolio.py: TestInboxImport`).
- New deps: `xlrd`, `openpyxl` (xls/xlsx), `pdfplumber` (pdf).
- **Known limitation, stated rather than hidden:** equity-file column
  matching is generalized from Zerodha's own header vocabulary (qty, avg
  cost, invested, cur val, …) plus common aliases, but has only been proven
  against Zerodha's CSV so far — no non-Zerodha equity export (xls/pdf) has
  been tested. Symbol enrichment also assumes the extracted symbol string
  already matches a `tickers.yaml` key (as Zerodha's does); a broker that
  exports company names instead of trading symbols will parse but warn
  "not in tickers.yaml" for every row rather than fuzzy-matching a name.

**Follow-up (2026-08-14, same day):** the user moved the root-level
`holdings.csv` itself into `holdings_inbox/` — no code change was needed
for `mybroker status`/`report`/`dashboard`/`chat` (that's exactly what the
inbox-merge logic above was built for), but three test fixtures had
hardcoded the root path directly. Fixed with `_real_equity_path()`
(`tests/test_portfolio.py`), which resolves root-or-inbox so the golden
tests don't care which location holds the real file.

## Tentative purchase-date estimation

**Status: done** (2026-08-14).

No purchase dates exist anywhere in the loaded holdings, so every tax
figure defaulted to "unknown — assumed SHORT term (higher tax)". This adds
a best-effort, clearly-labelled ESTIMATE rather than leaving that blank:

- `portfolio/purchase_estimator.py: estimate_purchase_date()` — for one
  symbol, searches its own price history **backward from today** for the
  most recent close within tolerance of `avg_cost` (tightest tolerance
  band first: 1.5% → 3% → 5% → 8%). Deliberately most-recent-match, not
  oldest-match: a more recent estimated date means a shorter holding
  period, which is the same "assume the higher tax, not the lower one"
  conservative bias `tax.py` already applies to a fully-unknown purchase
  date — this narrows that assumption with evidence, it does not flip it
  to the aggressive direction. No match within any tolerance band →
  `confident=False`, falls back to the old unknown/short-term behaviour,
  never force-fit.
- `mybroker estimate-dates` (`ui/cli.py: cmd_estimate_dates`) — the
  network-touching half: one `get_history` call per equity position (no
  LLM), writes `memory/estimated_purchase_dates.{json,md}`. Run against
  the real portfolio: 13/14 positions got a confident estimate; TMCV
  (post-demerger, ~191 days of history) correctly got none — its avg_cost
  predates the available series, and the estimator said so rather than
  guessing.
- `tools/server.py: compute_tax_impact` — when a sale omits
  `purchase_date`, looks up a confident estimate as a fallback and flags
  `purchase_date_source: "estimated"` (vs `"explicit"`/`"unknown"`) plus an
  appended assumption naming it unverified. An explicit `purchase_date`
  always wins over an estimate.
- Every output — CLI line, JSON, Markdown, the tax-tool assumption — carries
  the same caveat: **tentative, not from contract notes, confirm before
  filing.** This is a reference for judgement, not a record.
- Tests: `tests/test_purchase_estimator.py`, 20 cases — matching logic
  (recency preference, tolerance widening, no-match fallback), the
  save/load round-trip, and the `compute_tax_impact` wiring (explicit
  beats estimate; estimate used and flagged; no estimate falls back
  correctly).

## Public-data evidence tools (analyst consensus + screener.in)

**Status: done** (2026-08-14). Deliberately scoped as **more evidence, not
a predictor** — this system's design rule ("the LLM never computes a
number", "a position being down is not a sell signal") stays intact. No
tool here outputs a buy/sell verdict or a price forecast.

- **`get_analyst_consensus`** (`data/base.py: AnalystConsensus`,
  `yfinance_provider.py`) — analyst mean/high/low/median price targets,
  consensus rating, analyst count, and `trend_position` (price vs. its own
  50-DMA/200-DMA — descriptive, not a forecast). Small/micro caps often
  have zero coverage; reported as an absence (`number_of_analysts: null`
  + a warning), never treated as bearish.
- **`get_screener_ratios`** (`data/screener_provider.py`) — screener.in has
  no official API, so this is a best-effort HTML scrape (checked
  `robots.txt`: `/company/<SYMBOL>/...` is permitted, `/user/*` is not and
  is never touched). Fetches the **standalone** page specifically — bank
  NPA/financing-margin rows are populated there and blank on consolidated
  (confirmed against real HDFCBANK pages while building this). Returns:
  a second independent read on P/E/ROE/ROCE/book value (cross-check against
  `get_fundamentals`, don't auto-resolve disagreement), whatever
  sector-specific rows screener's own template shows (Gross/Net NPA %,
  "Financing Margin %" — screener's own P&L-margin label, kept under that
  name rather than mis-labelled NIM), and shareholding pattern
  (promoter/FII/DII/government %). A parse failure (site structure changed)
  degrades to a warning, never a crash or a guessed number.
- Both wired into `stock-researcher`'s tool list and prompt
  (`agents/definitions.py`), plus a `## Judgement` rule in
  `orchestrator.py`: consensus/ratios are evidence to cite and weigh, never
  a verdict to defer to.
- New deps: `beautifulsoup4` (already transitive via `pdfplumber`, now
  explicit since `screener_provider.py` imports it directly).
- Tests: `tests/test_screener_provider.py` (20 cases — parsing against a
  synthetic page matching the real structure, network-failure and parse-
  failure degradation, caching) + `AnalystConsensus` property tests in
  `test_data.py`, plus `@pytest.mark.live` integration tests for both
  (skipped by default, run with `pytest -m live`).

## M6 — Dashboard removed; CLI-only, plus an MCP server

**Status: done** (2026-08-16). The M4 Streamlit dashboard is gone —
`ui/dashboard.py` deleted, `streamlit`/`plotly` dropped as dependencies,
`cmd_dashboard` removed from `ui/cli.py`. Cause: real users hit a chain of
frozen-build-specific Streamlit bugs in production (wrong dev-mode
detection under PyInstaller's temp-extraction path, a wildcard network
bind exposing the dashboard to the whole LAN, and a filesystem-watcher
CPU-starvation bug pinning the process near 100% until it stopped
responding) — each one root-caused and fixed in turn, but the pattern made
clear that a GUI toolkit was the wrong shape for what's fundamentally a
terminal tool, not that Streamlit itself is unusable. factfolio is CLI-only
now, on principle, not just because the bugs got fixed.

- **`ui/cli.py` no-args behaviour** (`cmd_welcome`): first-run setup if
  needed, an instant `status` snapshot if there's already a portfolio to
  show, always the numbered next-step menu — plain terminal output,
  identical on every platform, no window that can vanish or fail to load.
- **Rich-rendered output**: `status` and the post-`report` recommendations
  view (symbol / action / conviction / rationale / key evidence, straight
  from `ledger.py: recommendations_for_run()`) are now proper tables via
  `rich`, not a wall of markdown to search through for the actual decision.
- **Live progress**: `run_review()` (`agents/orchestrator.py`) takes an
  optional `on_event` callback, fired once per tool call / subagent
  dispatch / text chunk; `factfolio report`'s status line and `factfolio
  chat`'s "thinking…" indicator are both driven by it, so a multi-minute
  or multi-second wait reads as progress, not a hang. `on_event` is
  optional specifically so nothing else (tests, the MCP server) has to
  care about it.
- **`factfolio mcp`** (`mcp_server.py`, new): a standalone MCP server over
  stdio — distinct from `tools/server.py`, which stays in-process/internal
  to the orchestrator's own agent runs — exposing `portfolio_status`,
  `validate_tickers`, and `run_portfolio_review` to external clients (VS
  Code's Claude extension, Claude Desktop, another agent). Verified with a
  real `mcp` Python client over stdio: handshake, `list_tools`, and a live
  `portfolio_status` call against real holdings, not just read against the
  source.
- `packaging/factfolio.spec` simplified accordingly — no more Streamlit
  static-asset collection, the single most fragile part of the old build.

## M7 — Agent-assisted ticker resolution

**Status: done.**

`factfolio init`'s plain yfinance-search suggestion (a single fuzzy lookup
per unmapped full-name holding, `config.suggest_ticker_for_name`) has a
real ceiling: it can't reason about ambiguity, so it either guesses or
punts every case identically. A demat holdings PDF's "Scrip Name" column
often has exactly the cases where that ceiling matters — the same holding
recorded twice under slightly different names, or a corporate action
(Tata Motors' 2024 demerger) that makes "the obvious answer" genuinely
wrong.

`agents/ticker_resolver.py` — one narrow, single-tool agent turn, not the
full M2 orchestrator:

- The agent gets exactly one tool, `search_ticker_by_name` (a live
  yfinance company-name search filtered to real NSE/BSE equity listings),
  and every unmapped holding's name **and quantity** in one batch, so it
  can reason about cross-row duplicates itself rather than resolving each
  name in isolation.
- **Code-level validation, not just a prompted rule**: a claimed symbol
  must appear in that exact name's own recorded search results or it's
  rejected regardless of stated confidence — the same "a claim must trace
  to a real tool call" discipline `tools/server.py`'s provenance validator
  already enforces for report recommendations, reimplemented here directly
  since this task's evidence shape doesn't fit that validator's schema.
- Only "high confidence, validated, not flagged as a duplicate, symbol not
  already in `tickers.yaml`" resolutions get auto-written — with a real
  sector from the same evidence, not a bare guess. A code-level dedupe
  guard also catches a same-symbol collision the agent's own prompted
  duplicate-detection missed (verified in practice — see below).
  Everything else prints the agent's own reasoning for a human to decide.
- Falls back to the plain single-search suggestion (no reasoning, never
  auto-written) if the agent path fails for any reason — no `claude`
  login, network, a malformed response — since this stays convenience,
  never a gate that could block `init`.

Verified against a real (sanitized) demat statement, live: correctly
identified `NTPC LIMITED`/`NTPC LTD` as the same holding recorded twice
(matching quantity, legal-suffix-only name difference) and reasoned
correctly about the Tata Motors demerger — high confidence for the entity
that legally retained the "Tata Motors Limited" name post-split, medium/
flagged for the genuinely ambiguous sibling row, rather than guessing
either way. Separately, the code-level dedupe guard caught a same-symbol
collision (`TATA POWER CO LTD` / `TATA POWER CO. LTD.`, both resolving to
`TATAPOWER.NS`) that the agent itself had reasoned were *not* duplicates
(different quantities, plausible separate lots) — both readings can be
right at once (same symbol, still two real lots), which is exactly why
the tickers.yaml-entry decision and the lot-merging decision
(`loader._merge_same_symbol_lots`) are kept as two separate steps rather
than one.

## M8 — New-idea screening (core-satellite candidates outside current holdings)

**Status: not started — scoped, deliberately deferred.**

Everything to this point analyses stocks/funds you *already hold* —
`tickers.yaml` only ever contains your own portfolio, and there's no
broader universe to draw from. This milestone is a genuinely different
capability: surfacing new candidates you don't currently hold, not just
auditing what's there.

Explicitly scoped down from "screen all of NSE" (~2,000 listed stocks) to
keep it tractable and consistent with the project's own "no forecasts, no
predictions" stance:

- **Bounded per sector, not universe-wide**: shortlist the top 10
  candidates per sector by fundamentals (the existing `get_screener_ratios`/
  `get_fundamentals`-style evidence, not a new prediction model) — never
  more than 10 tracked per sector at a time.
- **Portfolio construction rule**: no more than 3–4 actual holdings per
  sector at once, drawn from that shortlist.
- **Periodic review, not continuous**: re-run the screen monthly or
  quarterly and rotate positions out if a holding no longer clears the
  bar, rather than reacting to daily price moves.
- Needs a real, bounded universe source first (e.g. NSE's own equity
  list per sector) — nothing in the codebase currently maintains one
  outside of what a user has personally added to `tickers.yaml`.

Not building this now because it's a different scale of problem (screening
a universe vs. reasoning about your own evidence) and deserves its own
design pass, not an addition bolted onto the holdings-ingestion work this
round was actually about.

## Remaining gaps

- **`compute_overlap` is still honestly blocked.** MF holdings are now real
  data (see above), but true look-through overlap additionally needs each
  scheme's underlying holdings composition, which no provider fetches yet —
  the tool says so explicitly rather than approximating.
- **PDF equity/MF import has zero test coverage.** `_read_pdf_grid` (via
  pdfplumber) is implemented and shares all the same header-sniffing/
  classification code the tested xls path uses, but no test — real file or
  synthetic — exercises it yet, and no real PDF broker statement has been
  run through it. Treat it as unverified until one is.
- **Purchase-date estimates are approximations, not lot reconstruction.**
  `avg_cost` blends every real lot into one number; the estimator matches
  one date to it, which is fundamentally a simplification for a position
  bought in multiple tranches. Treat every estimated date, and therefore
  every tax figure computed from one, as a reference for judgement — verify
  against the actual contract notes before filing anything.
- **`get_screener_ratios` will break silently if screener.in changes its
  page structure.** No official API exists, so there is no upstream
  contract to depend on; a parse failure degrades to a warning today
  (tested), but a *structure* change that still parses successfully into
  wrong values would not currently be caught — there is no cross-check
  beyond eyeballing disagreement with `get_fundamentals`.
- **This doc's own upkeep** — keep this file, not code comments, as the
  source of truth for milestone status.
