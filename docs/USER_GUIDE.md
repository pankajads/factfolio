# User Guide

Everything from "I just cloned this" to "I run this every week." For *why*
it's built this way, see [`ARCHITECTURE.md`](ARCHITECTURE.md); for what's
shipped vs. not, see [`MILESTONES.md`](MILESTONES.md).

## 1. Prerequisites

- **Python 3.12+** and [**uv**](https://docs.astral.sh/uv/) (the package
  manager this project uses — `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **A Claude login or API key**, for the two commands that use the LLM
  (`report`, `chat`) — see [§4](#4-authentication). Everything else
  (`status`, `validate`, `cron`, `estimate-dates`) needs neither.
- **A holdings export** from your broker (Zerodha Kite works out of the
  box; anything else works via `holdings_inbox/`, see [§3](#3-add-your-holdings))

## 2. Install

```bash
git clone <your-fork-url> factfolio
cd factfolio
uv venv --python 3.12
uv sync --extra dev
uv run factfolio init
cd factfolio
```

`factfolio init` does six things, safe to re-run any time:

1. Creates a dedicated `factfolio/` project folder under wherever you ran
   it (yes, this means `factfolio/factfolio/` right after a fresh clone —
   the outer one is the code, the inner one is *your* data: `memory/`,
   `reports/`, `logs/`, `holdings_inbox/`, `.cache/`). Re-running `init`
   from inside that folder refreshes it in place rather than nesting
   another copy; if a folder named `factfolio` already exists somewhere but
   isn't one of its own projects, it asks before touching it (or, run
   non-interactively, creates `factfolio-2` instead of guessing).
2. Writes a **starter `memory/investment_policy.md`** if you don't already
   have one, clearly marked DRAFT. In an interactive terminal, it first
   asks 4 quick questions — target CAGR, risk appetite (conservative/
   moderate/aggressive), horizon in years, monthly investable capital —
   and fills the file in from your answers instead of generic placeholders
   (risk appetite also picks sensible starting position/sector caps for
   the rest). Piped/scripted/CI runs skip the questions and get the plain
   generic template. Either way, it's plain text — as your goals, risk
   appetite, or target return change, just edit it again; nothing here is
   fixed at init time, and an existing policy file is never overwritten or
   re-prompted for.
3. Seeds your own `tickers.yaml` at the project root, copied from the
   bundled defaults — yours to add symbols to from then on (see
   [§3](#3-add-your-holdings)). Never overwritten on a re-run either.
4. Scans `holdings.csv`/`holdings_inbox/` for symbols you hold that
   aren't mapped yet and adds a DRAFT entry for each — see
   [§3](#3-add-your-holdings) for exactly what qualifies and what a
   drafted entry looks like. Re-running `init` after adding a new holding
   picks up just the new symbol, never re-touching an existing entry.
5. For holdings that only have a full company name (no genuine symbol to
   draft from), asks an agent to research and resolve each one — see
   [§3](#3-add-your-holdings) for what it can and can't safely decide on
   its own, and what it falls back to without a `claude` login.
6. Prints exactly where it landed and what to do next.

Every command below this point — and the rest of this guide — assumes
you've `cd`ed into that `factfolio/` project folder. That checklist is
really the rest of this section:

## 3. Add your holdings

Two ways to get your portfolio into the tool — pick whichever matches what
your broker gives you:

**Zerodha Kite (native format):** Console → Portfolio → Holdings → Export,
save as `holdings.csv` at the project root. Expected columns: `Instrument,
Qty., Avg. cost, LTP, Invested, Cur. val, P&L, Net chg., Day chg.` — that's
exactly what Kite's own export produces, nothing to edit.

**Anything else — any broker, any format:** drop the file into
`holdings_inbox/`, unmodified. Supported formats: **csv, xls, xlsx, pdf, txt**
(an `.xls`/`.xlsx` that's actually an HTML table saved with that extension —
common with some banks/brokers — is also handled automatically). It's
sniffed automatically — header row located by keyword, classified as
equity or mutual-fund, parsed regardless of the exact column names your
broker used (`Avg Rate`/`Average Price`/`Buy Price` all mean the same
thing as `Avg. cost`, for example). A file it can't confidently classify
raises an error naming the file, rather than silently doing nothing.
Mutual-fund statements go in the same folder; `holdings_mf.csv` at the
root also still works if you'd rather hand-build one.

**Password-protected PDFs** (a CAMS/KFintech mutual-fund CAS statement, or
an NSDL/CDSL depository CAS) work too: put the password in a sidecar file
named `<the pdf's filename>.password` right next to it in `holdings_inbox/`
(e.g. `Statement.pdf.password`, plain text, just the password — gitignored
along with everything else in that folder), or set `FACTFOLIO_PDF_PASSWORD`
if every encrypted file shares one. Neither convention is obvious up
front: CAMS/KFintech CAS statements use your PAN in **UPPERCASE**;
NSDL/CDSL CAS statements use PAN+date-of-birth (`DDMMYYYY`) — the exact
format is always in the email that sent you the statement.

Both sources merge automatically — you can have `holdings.csv` at the root
*and* other files in `holdings_inbox/` at the same time; there's no need to
consolidate everything into one file. The same stock held across multiple
files/accounts is combined into one position for analysis — see the ticker
mapping section below for exactly how.

**Every holding needs a ticker mapping.** `factfolio init` seeded your own
`tickers.yaml` at the project root, copied from the bundled defaults —
it's yours from that point on: edits persist across runs and upgrades, on
every install method including the standalone executable, unlike editing
the package's own bundled copy directly.

Every time you run `init` (the first time and every re-run after), it also
scans `holdings.csv` and everything in `holdings_inbox/` and adds a
**DRAFT** entry for any symbol it finds that isn't mapped yet — but only
from a source with a genuine short trading-symbol column (Zerodha's
`Instrument`, a generic `Symbol` column); a source with only a full
company name (a demat holdings PDF, say) has no reliable symbol to derive,
so those still just warn instead of being guessed at. A drafted entry
looks like this — `candidates` is an unverified guess `factfolio validate`
will empirically confirm or reject, and `sector`/`tier`/`bucket` stay
honestly `Unknown`/`unknown` rather than guessed, since nothing in a
holdings file says what sector a stock is in:

```yaml
NEWSTOCK:
  name: NEWSTOCK  # TODO: replace with the real company name
  candidates: [NEWSTOCK.NS, NEWSTOCK.BO]  # DRAFT — unverified guess
  sector: Unknown  # TODO
  tier: unknown  # TODO: large | mid | small
  bucket: satellite  # TODO: core | satellite
```

Review and fill in every drafted entry before relying on it — that's the
whole point of marking it DRAFT rather than pretending it's done.

A source with only a full company name (no genuine symbol column) still
can't be safely auto-*drafted* — real accounts confirmed why: the same
company-name search that resolves `AXIS BANK LIMITED` correctly can also
return a London GDR or a Brazilian BDR alongside it, and returns nothing
at all for a company that's since been renamed (Zomato → Eternal is the
concrete example baked into this project's own history). Guessing wrong
here means silently wrong sector/policy numbers, so a bare guess is never
auto-written. `init` still helps a lot here, though — with a `claude`
login available, it hands each unmapped name to a small agent
(`agents/ticker_resolver.py`) that searches, reasons about likely
duplicates across your other holdings (the same stock recorded twice
under a slightly different name, say), and rates its own confidence. Only
"high confidence, no duplicate flag" resolutions — each one checked in
code against the actual search result it's grounded in, not just asked
for — get written straight to `tickers.yaml`, sector included:

```
Asking an agent to resolve 2 unmapped holding name(s)…
  ✓ 'AXIS BANK LIMITED' → AXISBANK.NS — added to tickers.yaml; still review tier/bucket
  ? 'NTPC LTD' — looks like the same holding as 'NTPC LIMITED': same NTPC.NS match,
    identical quantity, legal-suffix-only name difference — likely recorded twice.
```

No `claude` login, no network, or anything the agent can't parse cleanly
falls back to a single plain yfinance-search suggestion per name instead —
no reasoning, never auto-written, just a likely ticker to verify yourself:

```
Looking up possible matches for 1 unmapped holding name(s) via yfinance…
  ? 'AXIS BANK LIMITED' — possible match AXISBANK.NS — verify, then add it to tickers.yaml yourself
```

Either way, `tier`/`bucket` are never filled in automatically — no
external source can tell you which bucket a stock belongs in for your own
strategy, so that stays your call regardless of how the symbol itself got
resolved.

```yaml
symbols:
  YOURSYMBOL:
    name: Full Company Name Ltd
    candidates: [YOURSYMBOL.NS, YOURSYMBOL.BO]   # yfinance tickers to try, in order
    sector: Banking                               # used for sector concentration limits
    tier: large                                   # large | mid | small
    bucket: core                                  # core | satellite
    notes: >
      Anything unusual — a recent rename, a demerger, thin analyst coverage.
```

This is deliberate friction, not an oversight: the system never guesses a
ticker (a naive `f"{symbol}.NS"` silently breaks on renames and demergers —
see `tickers.yaml`'s own header comment for real examples), so every symbol
must be mapped explicitly before it can be analysed.

**Holding the same stock across multiple brokers/demat accounts, or from a
source with only a company name and no short symbol (a bank/DP holdings
PDF, say)?** Two things happen automatically once you've added the symbol
once:

- A row whose "symbol" column is actually a full company name (`HDFC BANK
  LTD.`) matches your `tickers.yaml` entry by its `name:` field — case,
  punctuation, and `Ltd`/`Ltd.`/`Limited` all normalized away — so you
  don't need a separate entry per spelling variant a statement happens to
  use. This only ever resolves when exactly one entry matches; an
  unrecognised or ambiguous name still stays unmapped and warns, same as
  any other unmapped symbol — it's never guessed.
- Every lot of the same resolved symbol — split across accounts, brokers,
  or recorded separately by a DP after a corporate action — is summed into
  one combined position before concentration and position-size limits are
  checked, so your real total exposure to a stock is what gets measured,
  not whatever fraction of it happens to sit in one file.

A rename or demerger that genuinely produces two *different* entities
(Tata Motors' 2024 split into TMCV/TMPV, say) is deliberately **not**
auto-merged even if the names look related — that call needs a human, not
a heuristic.

Then resolve everything:

```bash
uv run factfolio validate
```

This must pass — 0 unresolved symbols — before any other command will give
you complete numbers. It writes `.cache/resolved_tickers.json` so this
doesn't get re-checked on every run.

## 4. Authentication

`factfolio report` and `factfolio chat` need Claude. Nothing else does.

- **Default — local login.** Run `claude login` once (Pro/Max subscription
  or Anthropic Console) and every LLM command here just works — this
  project never sets its own API key, it lets the `claude` CLI resolve
  credentials itself.
- **Override — `export ANTHROPIC_API_KEY=...`.** Takes precedence
  automatically if set. Useful for a different billing account, a CI box,
  or a machine with no interactive login.

Every LLM-calling command prints which one is active, so it's never
ambiguous. Full detail in the [README](../README.md#authentication).

## 5. Customise your policy

Open `memory/investment_policy.md`. The fenced ` ```yaml ` block is what
the code actually reads; the prose around it is where you explain *why*,
so the two stay in sync as your thinking changes. At minimum, set:

| Field | What it controls |
|---|---|
| `target_cagr_pct` | Your return target — shows up in report framing, not enforced |
| `core_min_pct` / `core_max_pct` | Index-fund/large-cap floor — the biggest lever on breach severity |
| `max_position_pct` / `max_satellite_position_pct` | Single-position caps, core vs. satellite |
| `max_sector_pct` | Single-sector cap |
| `speculative_symbols` | Names you've flagged as binary-outcome bets — sized to `speculative_cap_pct` regardless of sector |
| `monthly_capital` | New money available per month — drives every "close this gap without selling" calculation |

Every recommendation is measured against this file, not against general
market wisdom — change a number here and the agent's behaviour changes
with it on the next run.

## 6. Day-to-day usage

```bash
uv run factfolio status           # instant snapshot — weights, breaches, HHI. No LLM.
uv run factfolio report           # full multi-agent review → reports/YYYY-MM-DD.md + a table in your terminal
uv run factfolio chat             # terminal Q&A, one agent, cheaper/faster than report
uv run factfolio mcp              # run as an MCP server for other tools/agents
uv run factfolio estimate-dates   # tentative purchase-date estimation for tax calc
uv run factfolio cron             # grade past recommendations against real prices
```

**`status`** is the one to run constantly — it's instant, free, and always
current. Good for "did anything change since yesterday."

**`report`** is the deep pass: a 7-agent team (market regime, portfolio
audit, per-stock research, tax costing, risk, and a mandatory adversarial
review of every draft) producing a dated Markdown report in `reports/`, plus
a table right in your terminal — symbol, action, conviction, rationale, key
evidence — for every recommendation that survived the gate, with a live
status line while it runs so a multi-minute wait doesn't look like a hang.
Takes a few minutes and costs real tokens — see
[`ARCHITECTURE.md`](ARCHITECTURE.md#multi-agent-orchestration-factfolio-report)
for what each agent actually does. Every BUY/SELL/TRIM/WATCH it finalises
gets logged to `memory/ledger.jsonl` for later grading (see §8).

**`chat`** is for a quick question ("what's my automobile exposure?", "how
did my BEL trim do?") without spinning up the full team — one agent,
direct tool access, same evidence-only discipline, but it can't log a
formal recommendation (that only happens through `report`, where the
adversarial-review step is mandatory).

**`mcp`** runs the same engine as a standalone
[MCP](https://modelcontextprotocol.io) server over stdio, for external
clients — the VS Code Claude extension, Claude Desktop, another agent — to
call directly: `portfolio_status`, `validate_tickers`,
`run_portfolio_review`. Structured output instead of a formatted table,
otherwise identical.

### Reading a report

Every claim in a report cites the tool and field it came from — e.g. "TMCV
is 14.27% of the portfolio, per `get_portfolio_snapshot`." That citation
isn't decoration: `log_recommendation` checks it against the real tool-call
log for that run before the recommendation is allowed to exist at all (see
[`ARCHITECTURE.md`](ARCHITECTURE.md#data-flow-from-holdings-file-to-recommendation)).
If a number in a report doesn't trace to a tool, something has gone wrong —
that's not supposed to be possible, and is worth filing an issue over.

Every report ends with **"Where this analysis is weak"** — data gaps, tools
that don't exist yet, anything a subagent flagged as unavailable. Read that
section before acting on the rest; it's there specifically so the report
doesn't overclaim.

## 7. Tax prep

No purchase dates come from a standard holdings export, so tax figures
default to the conservative "assumed short-term" case (higher tax) unless
you tell it otherwise. Two ways to fix that:

- **Give it real dates.** If you pass `purchase_date` explicitly wherever
  the tool asks for it, that always wins.
- **Let it estimate.** `factfolio estimate-dates` searches each holding's
  own price history for the most recent close near its avg_cost and saves
  a tentative date to `memory/estimated_purchase_dates.{json,md}`. Every
  use of an estimate is flagged `purchase_date_source: "estimated"` and
  never presented as verified — **confirm against your actual contract
  notes before filing anything.**

## 8. Automation

`factfolio cron` grades recommendations whose review date has passed
against a live price, no LLM involved, and writes the outcome back to
`memory/ledger.jsonl`. Wire it into a real cron job or launchd:

```cron
0 9 * * *  cd /path/to/factfolio && uv run factfolio cron >> logs/cron.out 2>&1
```

`report` is not meant to be run unattended on a schedule by default — it
costs real LLM tokens per run and produces recommendations meant to be
read, not auto-executed. Automate the free/deterministic parts (`cron`);
run `report` when you actually want a fresh review.

## 9. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `No investment policy at memory/investment_policy.md` | Run `factfolio init` |
| `No equity holdings found` | Add `holdings.csv` at the root, or any export to `holdings_inbox/` — see §3 |
| `Symbol 'X' is not in tickers.yaml` | Add an entry for it — see §3, then re-run `factfolio validate` |
| `factfolio validate` reports unresolved symbols | The symbol's real ticker isn't in its `candidates` list, or it's been renamed/delisted — check the company's current NSE/BSE symbol and add it as a candidate |
| `report`/`chat` hang or fail with an auth error | Run `claude login`, or check `ANTHROPIC_API_KEY` is valid — see §4 |
| A number in `holdings_inbox/` didn't parse | The importer couldn't find a header row it recognised — check the file has "Instrument"/"Qty" (equity) or "Folio"/"Scheme Name" (mutual fund) somewhere in it; PDF import in particular is less proven than csv/xls, see `MILESTONES.md` |

## 10. What this tool is not

It does not place trades, does not connect to your broker account beyond
reading an export file you provide, and does not predict prices — every
"evidence" tool (analyst consensus, trend position, screener.in ratios)
reports a fact, never a forecast. See the [README](../README.md#disclaimer)
for the full disclaimer. You decide and execute; this gives you the
numbers to decide with.
