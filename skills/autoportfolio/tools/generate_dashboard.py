#!/usr/bin/env python3
"""
generate_dashboard.py — Generate an HTML dashboard from portfolio state.

Usage:
    python generate_dashboard.py [--open]

Reads data/portfolio_state.json and outputs data/dashboard.html.
Pass --open to automatically open the file in the default browser.
"""

import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

STATE_FILE = Path("data/portfolio_state.json")
OUTPUT_FILE = Path("data/dashboard.html")


def load_state():
    if not STATE_FILE.exists():
        print(json.dumps({"error": f"{STATE_FILE} not found"}))
        sys.exit(1)
    return json.loads(STATE_FILE.read_text())


def build_holdings_rows(holdings, latest_snap=None):
    if not holdings:
        return '<tr><td colspan="10" style="text-align:center;color:#888;padding:24px">No holdings yet</td></tr>'
    snap_positions = (latest_snap or {}).get("positions", {})
    rows = []
    for ticker, info in sorted(holdings.items()):
        shares = info["shares"]
        avg_cost = info["avg_cost"]
        total_cost = round(shares * avg_cost, 2)
        last_buy = info.get("last_buy", "—")[:10]
        currency = info.get("currency", "USD")
        htype = info.get("type", "growth")
        div_yield = info.get("dividend_yield_pct")

        pos = snap_positions.get(ticker, {})
        price = pos.get("price")
        mkt_value = pos.get("value")
        pnl = pos.get("pnl")

        if isinstance(price, (int, float)):
            price_str = f"${price:,.2f}"
            mkt_value_str = f"${mkt_value:,.2f}"
            pnl_pct = (pnl / total_cost * 100) if total_cost else 0
            pnl_class = "positive" if pnl >= 0 else "negative"
            pnl_str = f'<span class="{pnl_class}">${pnl:+,.2f} ({pnl_pct:+.2f}%)</span>'
        else:
            price_str = mkt_value_str = pnl_str = "—"

        type_badge = f'<span class="badge htype-{htype}">{htype}</span>'
        # Annual income = current market value × dividend_yield_pct / 100
        if htype == "dividend" and div_yield and isinstance(mkt_value, (int, float)):
            annual = mkt_value * div_yield / 100
            income_str = f"<strong>${annual:,.2f}</strong>/yr<br><span style='color:#8b949e;font-size:12px'>{div_yield}% yield</span>"
        else:
            income_str = '<span style="color:#484f58">—</span>'

        rows.append(f"""<tr>
            <td><strong>{ticker}</strong></td>
            <td>{type_badge}</td>
            <td><span class="badge ccy">{currency}</span></td>
            <td>{shares}</td>
            <td>${avg_cost:,.2f}</td>
            <td>{price_str}</td>
            <td>{mkt_value_str}</td>
            <td>{pnl_str}</td>
            <td>{income_str}</td>
            <td>{last_buy}</td>
        </tr>""")
    return "\n".join(rows)


_ACTION_CLASS = {
    "BUY": "buy", "SELL": "sell",
    "DEPOSIT": "deposit", "SET_BUDGET": "deposit", "ADJUST": "deposit",
    "IMPORT": "import",
}


def build_ledger_rows(ledger):
    if not ledger:
        return '<tr><td colspan="7" style="text-align:center;color:#888;padding:24px">No trades yet</td></tr>'
    rows = []
    for entry in reversed(ledger[-50:]):
        ts = entry.get("timestamp", "")[:19].replace("T", " ")
        action = entry.get("action", "")
        ticker = entry.get("ticker", "—")
        shares = entry.get("shares", "—")
        price = entry.get("price", "")
        total = entry.get("total", 0)
        pnl = entry.get("pnl")
        cash_after = entry.get("cash_after", "")

        action_class = _ACTION_CLASS.get(action, "deposit")
        price_str = f"${price:,.2f}" if isinstance(price, (int, float)) else "—"
        pnl_str = ""
        if isinstance(pnl, (int, float)):
            pnl_class = "positive" if pnl >= 0 else "negative"
            pnl_str = f'<span class="{pnl_class}">${pnl:+,.2f}</span>'

        cash_str = f"${cash_after:,.2f}" if isinstance(cash_after, (int, float)) else "—"

        rows.append(f"""<tr>
            <td>{ts}</td>
            <td><span class="badge {action_class}">{action}</span></td>
            <td><strong>{ticker}</strong></td>
            <td>{shares}</td>
            <td>{price_str}</td>
            <td>${total:,.2f}</td>
            <td>{cash_str}</td>
        </tr>""")
    return "\n".join(rows)


def build_watchlist_rows(watchlist):
    if not watchlist:
        return '<tr><td colspan="3" style="text-align:center;color:#888;padding:24px">No watchlist entries. Add via execute_trade.py with watchlist_add.</td></tr>'
    rows = []
    for entry in watchlist:
        ticker = entry.get("ticker", "")
        condition = entry.get("condition", "—")
        added = entry.get("added", "")[:10]
        rows.append(f"""<tr>
            <td><strong>{ticker}</strong></td>
            <td>{condition}</td>
            <td>{added}</td>
        </tr>""")
    return "\n".join(rows)


def build_value_chart_data(value_history):
    if not value_history:
        return "[]"
    points = []
    for snap in value_history:
        ts = snap.get("timestamp", "")[:10]
        total = snap.get("total_value", 0)
        cash = snap.get("available_cash", 0)
        holdings = snap.get("holdings_value", 0)
        points.append({"date": ts, "total": total, "cash": cash, "holdings": holdings})
    return json.dumps(points)


def generate_html(state):
    cash = state.get("available_cash", 0)
    holdings = state.get("holdings", {})
    ledger = state.get("ledger", [])
    watchlist = state.get("watchlist", [])
    value_history = state.get("value_history", [])
    recurring = state.get("recurring_income")

    # Compute totals — prefer latest snapshot's market value, fall back to cost basis
    latest_snap = value_history[-1] if value_history else None
    if latest_snap and latest_snap.get("holdings_value") is not None:
        holdings_value = latest_snap["holdings_value"]
        total_value = latest_snap.get("total_value", cash + holdings_value)
        holdings_label = "at market"
    else:
        holdings_value = sum(h["shares"] * h["avg_cost"] for h in holdings.values())
        total_value = cash + holdings_value
        holdings_label = "at cost"
    num_positions = len(holdings)

    # Estimated annual dividend income across all dividend-type positions
    annual_income = 0.0
    snap_positions = (latest_snap or {}).get("positions", {})
    for t, h in holdings.items():
        if h.get("type") == "dividend" and h.get("dividend_yield_pct"):
            val = snap_positions.get(t, {}).get("value") or (h["shares"] * h["avg_cost"])
            annual_income += val * h["dividend_yield_pct"] / 100
    annual_income = round(annual_income, 2)
    monthly_income = round(annual_income / 12, 2)

    target = state.get("dividend_income_target")  # {"amount": 10000, "frequency": "monthly"}
    if target and target.get("amount"):
        amt = target["amount"]
        freq = target.get("frequency", "monthly")
        if freq == "monthly":
            target_str = f"target ${amt:,.0f}/mo"
            progress_pct = (monthly_income / amt * 100) if amt else 0
        elif freq == "annual":
            target_str = f"target ${amt:,.0f}/yr"
            progress_pct = (annual_income / amt * 100) if amt else 0
        else:
            target_str = f"target ${amt:,.0f} {freq}"
            progress_pct = 0
        income_sub = f"≈ ${monthly_income:,.2f}/mo — {target_str} ({progress_pct:.1f}%)"
    else:
        income_sub = f"≈ ${monthly_income:,.2f} / month"

    # Net capital in = DEPOSIT + ADJUST amounts, plus the FIRST SET_BUDGET as baseline.
    # Subsequent SET_BUDGETs are corrections: absorb their delta so the baseline tracks
    # true capital-in, not an arbitrary reset.
    net_in = 0.0
    seen_baseline = False
    for e in ledger:
        a = e.get("action")
        if a == "SET_BUDGET":
            if not seen_baseline:
                net_in = e.get("total", 0)
                seen_baseline = True
            else:
                net_in = round(net_in + e.get("delta", 0), 2)
        elif a in ("DEPOSIT", "ADJUST", "IMPORT"):
            if not seen_baseline:
                seen_baseline = True
            net_in = round(net_in + e.get("total", 0), 2)
    total_pnl = round(total_value - net_in, 2) if net_in > 0 else 0
    pnl_pct = round((total_pnl / net_in) * 100, 2) if net_in > 0 else 0

    recurring_str = "None configured"
    if recurring:
        recurring_str = f"${recurring['amount']:,.2f} / {recurring['frequency']}"

    now = datetime.now().strftime("%B %d, %Y at %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>autoportfolio Dashboard</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1117; color: #e1e4e8; line-height: 1.6; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 32px; padding-bottom: 16px; border-bottom: 1px solid #21262d; }}
header h1 {{ font-size: 24px; font-weight: 600; }}
header h1 span {{ color: #58a6ff; }}
.updated {{ color: #8b949e; font-size: 13px; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 32px; }}
.card {{ background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 20px; }}
.card .label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: #8b949e; margin-bottom: 4px; }}
.card .value {{ font-size: 28px; font-weight: 700; }}
.card .value.positive {{ color: #3fb950; }}
.card .value.negative {{ color: #f85149; }}
.card .sub {{ font-size: 13px; color: #8b949e; margin-top: 4px; }}
section {{ margin-bottom: 32px; }}
section h2 {{ font-size: 18px; font-weight: 600; margin-bottom: 16px; padding-bottom: 8px; border-bottom: 1px solid #21262d; }}
table {{ width: 100%; border-collapse: collapse; background: #161b22; border-radius: 8px; overflow: hidden; }}
th {{ text-align: left; padding: 12px 16px; background: #1c2129; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: #8b949e; border-bottom: 1px solid #21262d; }}
td {{ padding: 10px 16px; border-bottom: 1px solid #21262d; font-size: 14px; }}
tr:last-child td {{ border-bottom: none; }}
tr:hover {{ background: #1c2129; }}
.badge {{ padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; }}
.badge.buy {{ background: #0d4429; color: #3fb950; }}
.badge.sell {{ background: #49130f; color: #f85149; }}
.badge.deposit {{ background: #1a3a5c; color: #58a6ff; }}
.badge.import {{ background: #3a2c5c; color: #a78bfa; }}
.badge.ccy {{ background: #2d3440; color: #8b949e; letter-spacing: 0; font-size: 10px; }}
.badge.htype-growth {{ background: #1e3a5f; color: #60a5fa; }}
.badge.htype-dividend {{ background: #3d2f1a; color: #fbbf24; }}
.positive {{ color: #3fb950; }}
.negative {{ color: #f85149; }}
.chart-container {{ background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 24px; margin-bottom: 32px; }}
canvas {{ width: 100% !important; height: 300px !important; }}
footer {{ text-align: center; color: #484f58; font-size: 12px; padding: 24px 0; border-top: 1px solid #21262d; }}
</style>
</head>
<body>
<div class="container">
    <header>
        <h1><span>auto</span>portfolio</h1>
        <div class="updated">Last updated: {now}</div>
    </header>

    <div class="cards">
        <div class="card">
            <div class="label">Total Portfolio Value</div>
            <div class="value">${total_value:,.2f}</div>
            <div class="sub">Cash + Holdings ({holdings_label})</div>
        </div>
        <div class="card">
            <div class="label">Available Cash</div>
            <div class="value">${cash:,.2f}</div>
            <div class="sub">Recurring: {recurring_str}</div>
        </div>
        <div class="card">
            <div class="label">Holdings Value</div>
            <div class="value">${holdings_value:,.2f}</div>
            <div class="sub">{num_positions} position{'s' if num_positions != 1 else ''}</div>
        </div>
        <div class="card">
            <div class="label">Total P&L</div>
            <div class="value {'positive' if total_pnl >= 0 else 'negative'}">${total_pnl:+,.2f}</div>
            <div class="sub">{pnl_pct:+.2f}% overall</div>
        </div>
        <div class="card">
            <div class="label">Est. Dividend Income</div>
            <div class="value">${annual_income:,.2f}</div>
            <div class="sub">{income_sub}</div>
        </div>
    </div>

    <div class="chart-container">
        <h2 style="margin-bottom:16px">Portfolio Value Over Time</h2>
        <canvas id="valueChart"></canvas>
    </div>

    <section>
        <h2>Current Holdings</h2>
        <table>
            <thead><tr>
                <th>Ticker</th><th>Type</th><th>Ccy</th><th>Shares</th><th>Avg Cost</th><th>Price</th><th>Market Value</th><th>Unrealized P&L</th><th>Est. Income</th><th>Last Buy</th>
            </tr></thead>
            <tbody>
                {build_holdings_rows(holdings, latest_snap)}
            </tbody>
        </table>
    </section>

    <section>
        <h2>Watchlist</h2>
        <table>
            <thead><tr>
                <th>Ticker</th><th>Condition</th><th>Added</th>
            </tr></thead>
            <tbody>
                {build_watchlist_rows(watchlist)}
            </tbody>
        </table>
    </section>

    <section>
        <h2>Trade Ledger</h2>
        <table>
            <thead><tr>
                <th>Date</th><th>Action</th><th>Ticker</th><th>Shares</th><th>Price</th><th>Total</th><th>Cash After</th>
            </tr></thead>
            <tbody>
                {build_ledger_rows(ledger)}
            </tbody>
        </table>
    </section>

    <footer>
        autoportfolio — AI-driven portfolio management via Claude Code
    </footer>
</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
const data = {build_value_chart_data(value_history)};
if (data.length > 0) {{
    const ctx = document.getElementById('valueChart').getContext('2d');
    new Chart(ctx, {{
        type: 'line',
        data: {{
            labels: data.map(d => d.date),
            datasets: [
                {{
                    label: 'Total Value',
                    data: data.map(d => d.total),
                    borderColor: '#58a6ff',
                    backgroundColor: 'rgba(88,166,255,0.1)',
                    fill: true,
                    tension: 0.3,
                    borderWidth: 2,
                    pointRadius: 3,
                }},
                {{
                    label: 'Holdings',
                    data: data.map(d => d.holdings),
                    borderColor: '#3fb950',
                    borderWidth: 1.5,
                    borderDash: [4,4],
                    pointRadius: 0,
                    tension: 0.3,
                }},
                {{
                    label: 'Cash',
                    data: data.map(d => d.cash),
                    borderColor: '#8b949e',
                    borderWidth: 1.5,
                    borderDash: [2,2],
                    pointRadius: 0,
                    tension: 0.3,
                }},
            ]
        }},
        options: {{
            responsive: true,
            maintainAspectRatio: false,
            plugins: {{
                legend: {{ labels: {{ color: '#8b949e', font: {{ size: 12 }} }} }},
            }},
            scales: {{
                x: {{ ticks: {{ color: '#484f58' }}, grid: {{ color: '#21262d' }} }},
                y: {{
                    ticks: {{
                        color: '#484f58',
                        callback: v => '$' + v.toLocaleString()
                    }},
                    grid: {{ color: '#21262d' }},
                }},
            }},
        }},
    }});
}} else {{
    document.getElementById('valueChart').parentElement.innerHTML +=
        '<p style="text-align:center;color:#484f58;padding:40px">No value snapshots yet. Run the skill a few times to build history.</p>';
}}
</script>
</body>
</html>"""


def main():
    state = load_state()
    html = generate_html(state)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(html)
    print(json.dumps({"status": "ok", "path": str(OUTPUT_FILE)}))

    if "--open" in sys.argv:
        webbrowser.open(f"file://{OUTPUT_FILE.resolve()}")


if __name__ == "__main__":
    main()
