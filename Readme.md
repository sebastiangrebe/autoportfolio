# autoportfolio

An autonomous portfolio management agent for [Claude Code](https://claude.com/claude-code). Set your strategy, go to sleep, wake up to a monitoring report. Inspired by [karpathy/autoresearch](https://github.com/karpathy/autoresearch) — but for personal investing instead of ML experiments.

autoportfolio manages real money decisions: it validates tickers, discovers new opportunities via web research, tracks holdings and P&L across sessions, and generates a visual dashboard. It runs interactively when you want to trade, or autonomously on a schedule to monitor your positions overnight.

## Overview

**Two modes, one state file:**

- **Interactive** (`/autoportfolio`) — Full session: analyze tickers, propose trades, get your approval, execute, update dashboard. You're in the loop.
- **Monitor** (`/autoportfolio monitor`) — Autonomous: check all holdings for sell signals, evaluate watchlist conditions, snapshot P&L, generate a report. No human needed. Schedule it with a cron trigger and wake up to results.

**Investment framework** — Barbell strategy pairing high-momentum growth with tax-efficient dividend income:

- **Growth side**: High-conviction momentum positions — price above key moving averages, strong RSI, positive sentiment.
- **Dividend side**: Stable, high-yield income. Non-US residents get automatic Irish-domiciled UCITS ETF substitutions to avoid the 30% US dividend withholding tax.
- **Strategy as config**: Your investment approach, tax residency, sector preferences, and income targets are persisted in the state file — not hardcoded. Fork the repo, change your strategy, and run.

**Everything is stateful** — budget, holdings, trade ledger, value snapshots, watchlist, strategy, and session logs all live in one JSON file. Every session picks up where the last one left off.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Claude Code                                                 │
│                                                              │
│  SKILL.md (the brain)                                        │
│    ├── Interactive mode                                      │
│    │     ├── Loads strategy + state from JSON                │
│    │     ├── Validates user tickers + discovers new picks    │
│    │     ├── Applies sell/buy reasoning with budget rules    │
│    │     ├── Presents plan → approval gate → executes        │
│    │     └── Logs session to sessions.jsonl                  │
│    │                                                         │
│    └── Monitor mode (autonomous / scheduled)                 │
│          ├── Fetches live data for all holdings               │
│          ├── Checks sell signals + watchlist conditions       │
│          ├── Snapshots portfolio value                        │
│          ├── Generates report + updates dashboard            │
│          └── Logs session to sessions.jsonl                  │
│                                                              │
│  bin/ (added to PATH while plugin is enabled)                │
│    ├── fetch_data.py          (live prices + ticker search)  │
│    ├── execute_trade.py       (trades, imports, watchlist)   │
│    └── generate_dashboard.py  (HTML dashboard builder)       │
│                                                              │
│  data/                                                       │
│    ├── portfolio_state.json   (strategy, holdings, ledger)   │
│    ├── sessions.jsonl         (session history log)          │
│    └── dashboard.html         (visual portfolio overview)    │
└──────────────────────────────────────────────────────────────┘
```

The Python scripts are intentionally simple — they fetch data, manage JSON state, and render HTML. All financial reasoning, web research, tax logic, and user interaction happens inside Claude Code via the SKILL.md instructions. The same architecture as [autoresearch](https://github.com/karpathy/autoresearch): a human-written instruction file (SKILL.md) guides an AI agent through a repeatable loop, with state persisted between runs.

### File locations

| File | Path | Purpose |
|------|------|---------|
| SKILL.md | `plugin/skills/autoportfolio/SKILL.md` | Skill definition and LLM instructions |
| fetch_data.py | `plugin/bin/fetch_data.py` | Fetch live technicals + ticker search |
| execute_trade.py | `plugin/bin/execute_trade.py` | Execute trades, manage budget, snapshot values |
| generate_dashboard.py | `plugin/bin/generate_dashboard.py` | Build HTML dashboard |
| Portfolio state | `data/portfolio_state.json` | Budget, holdings, ledger, value history (gitignored) |
| Dashboard | `data/dashboard.html` | Visual portfolio overview (gitignored) |

## Installation

### Option A: Claude Code Marketplace (recommended)

Inside Claude Code, run:

```
/plugin marketplace add sebastiangrebe/autoportfolio
/plugin install autoportfolio
```

### Option B: Clone and install locally

```bash
git clone https://github.com/sebastiangrebe/autoportfolio.git
```

Then load the plugin directly from the clone:

```bash
claude --plugin-dir ./autoportfolio/plugin
```

This loads the plugin for the current session without installing it globally. The `bin/` directory is added to `PATH` so `fetch_data.py`, `execute_trade.py`, and `generate_dashboard.py` are callable by name.

### Prerequisite

```bash
pip install yfinance
```

## Usage

### First run — setup

```
/autoportfolio
```

On first run, Claude will ask for:
1. **Available cash** — your investable budget
2. **Recurring income** — how much you plan to invest regularly (e.g., "$2,000/month")
3. **Strategy** — your investment approach (e.g., "AI and tech growth + high-yield dividends")
4. **Tax residency** — US or non-US (determines dividend vehicle selection)

### Daily use — validate and discover

You can provide your own tickers for validation:

```
/autoportfolio NVDA, AAPL, MSFT, AMZN, VUSA.L, KO, O, JNJ
```

Or let Claude research based on your strategy:

```
/autoportfolio research mode
```

Or both — provide tickers AND get additional AI-discovered picks.

### What happens each session

1. **State loaded** — cash, holdings, trade history, cooldowns
2. **Your tickers validated** — every ticker you provide gets a STRONG BUY / HOLD / AVOID verdict with reasoning
3. **Existing holdings checked** — sell signals if momentum breaks (price < 50-DMA, RSI < 35)
4. **New picks discovered** — web research finds opportunities aligned to your strategy (no hardcoded lists)
5. **Full plan presented** — validation table, sell orders, buy orders, projected cash
6. **You approve / modify / reject** — nothing happens without your explicit approval
7. **Trades executed** — state updated with timestamps, portfolio value snapshotted
8. **Dashboard generated** — HTML page opens in your browser

### The dashboard

After each session, an HTML dashboard is generated at `data/dashboard.html` with:
- Portfolio value over time (chart)
- Total P&L
- Available cash and recurring income
- Current holdings with cost basis and dates
- Full trade ledger with timestamps and rationale

## Importing Existing Positions

You can backfill positions held in other brokers without debiting cash:

```bash
plugin/bin/execute_trade.py '{
  "import_position": {
    "ticker": "DTE.DE",
    "shares": 154,
    "avg_cost": 19.90,
    "first_buy": "2024-01-01",
    "currency": "EUR",
    "type": "dividend",
    "dividend_yield_pct": 3.44
  }
}'
```

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `ticker` | yes | — | Yahoo Finance ticker symbol |
| `shares` | yes | — | Number of shares |
| `avg_cost` | yes | — | Average cost per share in `currency` |
| `currency` | no | `USD` | Cost basis currency |
| `first_buy` | no | now | Date of first purchase (ISO date or datetime) |
| `type` | no | `growth` | Position type: `growth`, `dividend`, `commodity`, `special` |
| `dividend_yield_pct` | no | — | Annual dividend yield percentage (for `dividend` type) |

Imports automatically backfill existing value snapshots so the portfolio chart doesn't show a sudden jump. The position appears at cost basis (P&L = 0) in all historical snapshots.

## Trade Rules

| Rule | Detail |
|------|--------|
| Position sizing | 5% of available cash per pick |
| Cooldown | Cannot re-buy a ticker within 7 days of last purchase |
| Sell trigger | Price below 50-DMA, or RSI < 35, or momentum < -5% with broken trend |
| Budget enforcement | Never recommends trades exceeding available cash |
| Trade logging | Every trade timestamped with action, price, shares, rationale, and resulting cash balance |

## Cash Management

The `execute_trade.py` script supports several cash operations beyond trades:

```json
{"set_budget": 10000}              // SET available_cash (replaces current value)
{"deposit": 4000}                  // ADD to available_cash
{"adjust_cash": {"amount": -50, "reason": "broker fee"}}
{"undo_last": true}                // Reverse the most recent ledger entry
```

## Monitor Mode & Scheduling

autoportfolio can run autonomously on a schedule to monitor your holdings for sell signals and check watchlist conditions — no human interaction required.

### Run manually

```
/autoportfolio monitor
```

This produces a report with sell alerts, watchlist triggers, and a portfolio summary. No trades are executed.

### Schedule via cloud routines

Set up a weekday morning check using Claude Code's `/schedule` command:

```
/schedule create --cron "0 8 * * 1-5" --prompt "Run /autoportfolio monitor"
```

Or configure it via the [routines web UI](https://claude.ai/code/routines). The routine clones this repo, runs the skill, and the report is visible in your routines dashboard.

### Watchlist

Add tickers to your watchlist with conditions. Monitor mode checks these automatically:

```bash
# Add
plugin/bin/execute_trade.py '{"watchlist_add": {"ticker": "ASML", "condition": "RSI below 60"}}'

# Remove
plugin/bin/execute_trade.py '{"watchlist_remove": {"ticker": "ASML"}}'
```

Supported conditions (evaluated by the LLM during monitoring):
- `RSI below 60` / `RSI above 75`
- `Price above 50-DMA` / `Price below 200-DMA`
- `Price below 1400`

## Git & Version History

By default, `data/portfolio_state.json` and `data/dashboard.html` are **gitignored** since they contain personal financial data. The skill code, README, and marketplace config are tracked normally.

If you want full version history of your portfolio state (useful for auditing trades and rolling back mistakes), remove these lines from `.gitignore`:

```
data/portfolio_state.json
data/dashboard.html
```

You can then add a Step 7 to your SKILL.md that auto-commits after each session:

```bash
git add data/portfolio_state.json data/dashboard.html
git commit -m "autoportfolio session: <summary of trades>"
```

**Note**: Only do this on a private repo — the state file contains your holdings, cash balance, and trade history.

## Tax-Aware Selection

For **non-US tax residents**, dividend picks auto-substitute to Irish-domiciled UCITS ETFs:

| US Ticker | UCITS Alternative | Exchange | Tax Saving |
|-----------|-------------------|----------|------------|
| VOO       | VUSA.L            | LSE      | ~30%       |
| VYM       | VHYL.L            | LSE      | ~30%       |
| SCHD      | IUHD.L            | LSE      | ~30%       |
| SPY       | IUSA.L            | LSE      | ~30%       |
| VTI       | VWRL.L            | LSE      | ~30%       |

## License

MIT
