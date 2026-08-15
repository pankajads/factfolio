# User Guide

Everything from "I just cloned this" to "I run this every week." For *why*
it's built this way, see [`ARCHITECTURE.md`](ARCHITECTURE.md); for what's
shipped vs. not, see [`MILESTONES.md`](MILESTONES.md).

## 1. Prerequisites

- **Python 3.12+** and [**uv**](https://docs.astral.sh/uv/) (the package
  manager this project uses — `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **A Claude login or API key**, for the two commands that use the LLM
  (`report`, `chat`) — see [§4](#4-authentication). Everything else
  (`status`, `validate`, `cron`, `estimate-dates`, the dashboard's Overview
  tab) needs neither.
- **A holdings export** from your broker (Zerodha Kite works out of the
  box; anything else works via `holdings_inbox/`, see [§3](#3-add-your-holdings))

## 2. Install

```bash
git clone <your-fork-url> factfolio
cd factfolio
uv venv --python 3.12
uv sync --extra dev
uv run factfolio init
```

`factfolio init` does three things, safe to re-run any time:

1. Creates the runtime folders (`memory/`, `reports/`, `logs/`,
   `holdings_inbox/`, `.cache/`) if they don't exist.
2. Writes a **starter `memory/investment_policy.md`** if you don't already
   have one — generic placeholder numbers, clearly marked DRAFT. It will
   never overwrite a policy file you've already customised.
3. Prints a checklist of what's still missing before the tool is useful for
   *your* portfolio.

That checklist is really the rest of this section:

## 3. Add your holdings

Two ways to get your portfolio into the tool — pick whichever matches what
your broker gives you:

**Zerodha Kite (native format):** Console → Portfolio → Holdings → Export,
save as `holdings.csv` at the project root. Expected columns: `Instrument,
Qty., Avg. cost, LTP, Invested, Cur. val, P&L, Net chg., Day chg.` — that's
exactly what Kite's own export produces, nothing to edit.

**Anything else — any broker, any format:** drop the file into
`holdings_inbox/`, unmodified. Supported formats: **csv, xls, xlsx, pdf**.
It's sniffed automatically — header row located by keyword, classified as
equity or mutual-fund, parsed regardless of the exact column names your
broker used. A file it can't confidently classify raises an error naming
the file, rather than silently doing nothing. Mutual-fund statements go in
the same folder; `holdings_mf.csv` at the root also still works if you'd
rather hand-build one.

Both sources merge automatically — you can have `holdings.csv` at the root
*and* other files in `holdings_inbox/` at the same time; there's no need to
consolidate everything into one file.

**Every holding needs a ticker mapping.** Open
`src/mybroker/data/tickers.yaml` and add an entry for each symbol you hold
that isn't already there (the file ships with the maintainer's own
portfolio's symbols as examples — yours will be different):

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

Then resolve everything:

```bash
uv run factfolio validate
```

This must pass — 0 unresolved symbols — before any other command will give
you complete numbers. It writes `.cache/resolved_tickers.json` so this
doesn't get re-checked on every run.

## 4. Authentication

`factfolio report` and `factfolio chat` (terminal and the dashboard's Chat
tab) need Claude. Nothing else does.

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
uv run factfolio report           # full multi-agent review → reports/YYYY-MM-DD.md
uv run factfolio dashboard        # Streamlit UI — charts + a chat tab
uv run factfolio chat             # terminal Q&A, one agent, cheaper/faster than report
uv run factfolio estimate-dates   # tentative purchase-date estimation for tax calc
uv run factfolio cron             # grade past recommendations against real prices
```

**`status`** is the one to run constantly — it's instant, free, and always
current. Good for "did anything change since yesterday."

**`report`** is the deep pass: a 7-agent team (market regime, portfolio
audit, per-stock research, tax costing, risk, and a mandatory adversarial
review of every draft) producing a dated Markdown report in `reports/`.
Takes a few minutes and costs real tokens — see
[`ARCHITECTURE.md`](ARCHITECTURE.md#multi-agent-orchestration-factfolio-report)
for what each agent actually does. Every BUY/SELL/TRIM/WATCH it finalises
gets logged to `memory/ledger.jsonl` for later grading (see §8).

**`chat`** is for a quick question ("what's my automobile exposure?", "how
did my BEL trim do?") without spinning up the full team — one agent,
direct tool access, same evidence-only discipline, but it can't log a
formal recommendation (that only happens through `report`, where the
adversarial-review step is mandatory).

**`dashboard`** gives you the same numbers as `status` as charts, plus a
Chat tab using the same engine as the terminal `chat` command.

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
| Dashboard chat tab errors | Same auth requirement as `chat` — the Overview tab works without any LLM setup, only the Chat tab needs it |

## 10. What this tool is not

It does not place trades, does not connect to your broker account beyond
reading an export file you provide, and does not predict prices — every
"evidence" tool (analyst consensus, trend position, screener.in ratios)
reports a fact, never a forecast. See the [README](../README.md#disclaimer)
for the full disclaimer. You decide and execute; this gives you the
numbers to decide with.
