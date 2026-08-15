# FactFolio

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)
![Local-first](https://img.shields.io/badge/Runs-100%25%20Local-brightgreen.svg)
![Cost](https://img.shields.io/badge/Cost-Free%20%26%20Open%20Source-informational.svg)
[![PyPI](https://img.shields.io/pypi/v/factfolio.svg)](https://pypi.org/project/factfolio/)
[![CI](https://github.com/pankajads/factfolio/actions/workflows/ci.yml/badge.svg)](https://github.com/pankajads/factfolio/actions/workflows/ci.yml)

### Every number, traced. No black box, no guesswork, no fees.

Your broker app tells you what you own. Your advisor charges you to tell you
what to do about it. FactFolio is neither — it's a free, open-source,
locally-run multi-agent advisory system for Indian equity and mutual fund
portfolios that shows its work: BUY / SELL / HOLD / TRIM calls, checked
against an explicit investment policy, with **every single number traceable
back to the exact tool call that produced it.**

It never sends your holdings anywhere. It never predicts the future. And it
never asks you to trust a number it can't show you the receipt for.

```bash
uvx factfolio init
uvx factfolio report             # → reports/, a full multi-agent portfolio review
```

No clone, no virtualenv to manage — [`uv`](https://docs.astral.sh/uv/)
downloads and runs it. Prefer a single file with no Python at all? Grab a
standalone executable for your OS from the
[latest release](../../releases/latest) instead — see [Installing](#installing)
below for both paths, including what each one still needs (a `claude`
login, either way — see [Authentication](#authentication)).

---

## Why FactFolio

**🔍 Fully auditable, by design.** Every recommendation the system produces
is checked, before you ever see it, by a code-level gate that rejects any
claim citing a number not present in that run's actual tool-call log. Not
"the model promises to double-check itself" — an independent validator that
reads the audit trail directly. If a number's in the report, it's in the log.

**🧮 The LLM never computes a number.** Weights, returns, tax, correlations,
risk — all deterministic Python, same result every time, regardless of what
any model feels like saying that day. Agents only reason and call tools;
they never do arithmetic.

**🤖 Seven specialist agents, not one generalist.** A `market-analyst`,
`portfolio-auditor`, `stock-researcher`, `mf-analyst`, `tax-strategist`, and
`risk-officer` each analyse your portfolio from their own angle — then a
`devils-advocate` agent adversarially reviews every finding before it's
allowed into the report.

**🔒 Your portfolio never leaves your machine.** Holdings, values, and
quantities stay local, always — market data providers see ticker symbols
only, nothing more. No account to create, no cloud upload, no third party
ever sees what you own or what it's worth.

**🇮🇳 Built for Indian portfolios specifically.** Zerodha CSV import out of
the box, STCG/LTCG tax-impact modelling under Indian rules, core-satellite
and concentration (HHI) checks, and correlation/overlap analysis across
equity *and* mutual funds together.

**📈 No forecasts, no predictions, ever.** No tool in this system outputs a
price target or a buy/sell prediction. It hands you evidence — analyst
consensus, moving-average trend, screener ratios, correlation graphs — and
lets a policy and a panel of agents reason about it in the open. What you
do with it is still your call, on purpose.

**💸 Free. Forever. Yours to run.** MIT-licensed, no subscription, no
freemium tier, no "upgrade to see the real recommendation." Clone it, run
it on your own machine, read every line that touches your money.

---

## Installing

Three ways to get it, in order of how much you want on your machine:

**1. `uvx` / `pip` — no clone, no dependency management (recommended).**
```bash
uvx factfolio init          # runs it straight from PyPI, nothing installed persistently
# or: pip install factfolio && factfolio init
```
Reads and writes wherever you run it from — `cd` into a folder for this
portfolio first, the same way you'd use `git` or `terraform`.

**2. Standalone executable — no Python at all.** Download `factfolio` (or
`factfolio.exe` on Windows) for your OS from the
[latest release](../../releases/latest), make it executable, run it:
```bash
chmod +x factfolio-macos-arm64 && ./factfolio-macos-arm64 init
```
Single file, every dependency baked in (~130MB) — nothing else to install.
Still needs the `claude` CLI separately for `report`/`chat` — see
[Authentication](#authentication); no packaging choice removes that.

**3. Clone + dev setup — for contributing or reading/customising the code.**
```bash
uv venv --python 3.12
uv sync --extra dev
uv run factfolio init      # creates memory/, a starter policy file, prints next steps
```
> **Note:** always use `uv run …` here. The `python3` on your PATH is a
> different interpreter without these dependencies. The CLI command is
> `factfolio` (the underlying Python package is `mybroker` for historical
> reasons — both `uv run factfolio ...` and `uv run mybroker ...` run the
> identical CLI).

Whichever you pick, the day-to-day commands below are identical — swap
`uv run factfolio` for `uvx factfolio`, `factfolio` (pip-installed), or
`./factfolio-<platform>` (the executable) as needed.

Full walkthrough — adding your holdings, mapping tickers, customising the
policy — in [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md). Cutting a release
yourself (versioning, PyPI, the executables above): [`docs/RELEASING.md`](docs/RELEASING.md).

### Authentication

`factfolio report` and `factfolio chat` (terminal and the dashboard's Chat
tab) call the Claude Agent SDK, which shells out to the `claude` CLI and
lets *it* resolve credentials — this project never sets an API key itself.
That means:

- **Default: your local `claude login` session.** If you're already logged
  in (`claude` in a terminal, Pro/Max or Console), no setup is needed —
  every LLM-calling command here just works.
- **Override: `export ANTHROPIC_API_KEY=...`.** Set it and it takes
  precedence automatically — useful for a different billing account, CI, or
  a machine with no interactive login.

Every command that calls the LLM prints which one is active
(`auth: local claude login session` / `auth: ANTHROPIC_API_KEY (env var
override)`) so it's never ambiguous which credential a run used.
`factfolio status`/`validate`/`cron`/`estimate-dates` and the dashboard's
Overview tab need neither — they're pure deterministic Python.

## Usage

```bash
uv run validate-tickers        # must pass before any agent run
uv run pytest                  # verify the maths
uv run factfolio status        # deterministic snapshot — no LLM, instant
uv run factfolio report        # full multi-agent review → reports/
uv run factfolio dashboard     # Streamlit dashboard, incl. a chat tab
uv run factfolio chat          # terminal Q&A REPL, one agent
uv run factfolio cron          # grade past recommendations — no LLM
uv run factfolio estimate-dates  # tentative purchase-date estimation — no LLM
```

### Holdings input

Drop the standard Zerodha `holdings.csv` (and optionally `holdings_mf.csv`)
at the project root as before, **or** drop any broker export — csv, xls,
xlsx or pdf, equity or mutual fund, any filename — into `holdings_inbox/`.
Each file is sniffed and classified automatically; `factfolio status` /
`report` / `dashboard` merge everything found there with the root files.

### Unattended grading (`factfolio cron`)

`factfolio cron` grades recommendations past their review date against a
live price and writes the outcome back to the ledger — pure Python, no LLM
call, safe on a schedule. Wire it into cron or launchd, e.g.:

```cron
0 9 * * *  cd /path/to/factfolio && uv run factfolio cron >> logs/cron.out 2>&1
```

### Purchase dates

No purchase dates come from the broker exports, so every tax figure
defaults to the conservative "assumed short-term" case. `factfolio
estimate-dates` makes a **tentative, clearly-labelled estimate** instead:
for each holding it searches that symbol's own price history, backward
from today, for the most recent close near its avg_cost, and saves the
result to `memory/estimated_purchase_dates.{json,md}`. `compute_tax_impact`
uses a confident estimate as a fallback when a sale doesn't supply an
explicit purchase date — always flagged `purchase_date_source: "estimated"`
in the response, never presented as verified. **This is not a substitute
for your actual contract notes** — see the note on every estimate for why.

### Extra evidence tools

`get_analyst_consensus` (analyst price targets/rating, 50/200-DMA trend
position, via yfinance) and `get_screener_ratios` (bank Gross/Net NPA %,
shareholding pattern, a second independent read on P/E/ROE/ROCE, via a
best-effort screener.in scrape — no official API exists) give the agent
more *evidence* to reason with. Neither is a predictor: no tool in this
system outputs a buy/sell verdict or a price forecast — see [Why
FactFolio](#why-factfolio) above.

## Privacy

`holdings.csv`, `holdings_inbox/`, `memory/`, `reports/`, and `logs/` are
gitignored and never leave the machine. Market data providers (yfinance,
screener.in) receive **ticker symbols only** — never quantities or values.
screener.in has no official API; `get_screener_ratios` scrapes its public
company pages (permitted by their `robots.txt`) and is rate-limited and
cached like every other provider.

## Documentation

- [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md) — setup, adding your holdings,
  day-to-day usage, troubleshooting. **Start here.**
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — system diagram, data flow,
  the multi-agent orchestration, the security/provenance model
- [`docs/MILESTONES.md`](docs/MILESTONES.md) — build history and feature status

## Contributing

FactFolio is free and open source (MIT — see [`LICENSE`](LICENSE)) for
anyone in India to use, study, or build on. Issues and pull requests are
welcome — `uv run pytest` and `uv run ruff check src/ tests/` should both
pass clean before opening one; every PR runs the same checks in CI
(`.github/workflows/ci.yml`) and needs a green build before it can merge.
If FactFolio is useful to you, a ⭐ on the repo helps other investors find it.

## Professional services

FactFolio does deterministic analysis and gives you the evidence — it
deliberately doesn't forecast, and it isn't a substitute for a professional
who can go deeper than a general-purpose tool can. If you want hands-on
deep analysis, market-trend research, or data-driven forecasting for your
specific portfolio, you can reach out to **Pankaj Negi** for professional
services: [linkedin.com/in/pankajads](https://www.linkedin.com/in/pankajads).

## Disclaimer

FactFolio is an educational and personal-use tool, not investment advice.
It is not a substitute for a SEBI-registered investment adviser. Nothing it
outputs — a recommendation, a tax figure, an estimated purchase date, a
piece of "evidence" — is a guarantee of accuracy or of future performance,
and every figure should be independently verified before you act on it.
Provided **as-is, with no warranty**, per the [MIT license](LICENSE).
Market data is sourced from third parties (yfinance, screener.in) that this
project does not control and cannot vouch for.
