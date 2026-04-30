#!/usr/bin/env python3
"""
generate_dashboard.py — Generate an HTML dashboard from portfolio state.

Usage:
    python generate_dashboard.py [--open]

Reads data/portfolio_state.json and outputs data/dashboard.html.
Pass --open to automatically open the file in the default browser.
"""

import json
import math
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path("data/portfolio_state.json")
OUTPUT_FILE = Path("data/dashboard.html")


def load_state():
    if not STATE_FILE.exists():
        print(json.dumps({"error": f"{STATE_FILE} not found"}))
        sys.exit(1)
    state = json.loads(STATE_FILE.read_text())
    if _migrate_ledger_deposit_types(state):
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    return state


def _migrate_ledger_deposit_types(state) -> bool:
    """One-shot migration: tag legacy DEPOSIT entries with `deposit_type`.

    Mirrors execute_trade.migrate_ledger_deposit_types so the dashboard can
    self-heal even when invoked before any execute_trade call.
    """
    ledger = state.get("ledger", [])
    rec = state.get("recurring_income") or {}
    started_at = rec.get("started_at", "") or ""
    changed = False
    for entry in ledger:
        if entry.get("action") != "DEPOSIT":
            continue
        if "deposit_type" in entry:
            continue
        ts = entry.get("timestamp", "") or ""
        entry["deposit_type"] = "seed" if (started_at and ts < started_at) else "recurring"
        changed = True
    return changed


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


DEFAULT_GROWTH_RATE_PCT = 8.0  # historical S&P 500 nominal long-run; override via strategy.growth_rate_pct


def _div_at_month(p_now, contrib, r, t):
    """Monthly dividend income at month t with starting principal p_now,
    monthly contribution `contrib`, monthly compounding rate r."""
    if r <= 0 or t < 0:
        return p_now * r if r > 0 else 0.0
    p = p_now * (1 + r) ** t + contrib * ((1 + r) ** t - 1) / r
    return p * r


def _solve_n_months(p_now, contrib, r, p_target):
    """Months until principal grows from p_now to p_target with monthly
    contribution `contrib` compounding at rate r. Returns None if unreachable."""
    if r <= 0 or p_target <= p_now:
        return 0 if p_target <= p_now else None
    try:
        denom = (p_now + contrib / r) if (p_now + contrib / r) != 0 else None
        if denom is None or denom <= 0:
            return None
        ratio = (p_target + contrib / r) / denom
        if ratio <= 0:
            return None
        n = math.log(ratio) / math.log(1 + r)
        return n if math.isfinite(n) and n > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def _build_scenarios(target, monthly_income, holdings, snap_positions, recurring, strategy):
    """Build dividend-goal scenarios for the dashboard.

    Returns a dict with structured scenario data (or `None` if inputs are
    missing). Two scenarios:

      A. always — keep the strategy's growth/dividend split forever.
         Only the dividend-share of recurring contributions feeds the
         dividend bucket; growth bucket is invisible to the projection.

      B. flip   — let the growth bucket compound at `growth_rate_pct` for
         X months, then rotate it (and continue 100% of recurring) into
         the dividend bucket. The chosen X minimizes total time-to-target.
    """
    if not target or not target.get("amount") or not recurring or not recurring.get("amount"):
        return None

    freq = target.get("frequency", "monthly")
    m_target = target["amount"] / 12 if freq == "annual" else target["amount"]

    # Weighted average yield across dividend-type holdings.
    div_value_total = 0.0
    div_yield_weighted = 0.0
    for t, h in holdings.items():
        if h.get("type") == "dividend" and h.get("dividend_yield_pct"):
            val = snap_positions.get(t, {}).get("value") or (h["shares"] * h["avg_cost"])
            div_value_total += val
            div_yield_weighted += val * h["dividend_yield_pct"]
    avg_yield = (div_yield_weighted / div_value_total) if div_value_total else 0.0

    # Growth-bucket value (everything not tagged dividend) for the flip scenario.
    growth_value_total = 0.0
    for t, h in holdings.items():
        if h.get("type") != "dividend":
            val = snap_positions.get(t, {}).get("value") or (h["shares"] * h["avg_cost"])
            growth_value_total += val

    # Split contribution per strategy. "60/40" means 60 growth, 40 dividend.
    split = (strategy.get("growth_dividend_split") or "").strip()
    div_share = 1.0
    if "/" in split:
        try:
            div_share = float(split.split("/")[1]) / 100
        except (ValueError, IndexError):
            div_share = 1.0
    contrib_total = recurring["amount"]
    contrib_div = contrib_total * div_share
    contrib_grow = contrib_total - contrib_div

    growth_rate_pct = float(strategy.get("growth_rate_pct") or DEFAULT_GROWTH_RATE_PCT)

    if avg_yield <= 0:
        return {"reachable": False, "reason": "no dividend yield available",
                "m_target": m_target, "avg_yield": avg_yield,
                "contrib_div": contrib_div, "contrib_grow": contrib_grow,
                "growth_rate_pct": growth_rate_pct,
                "p_now": 0, "g_now": growth_value_total, "p_target": 0}

    r = (avg_yield / 100) / 12
    g = (growth_rate_pct / 100) / 12
    p_now = div_value_total
    p_target = m_target * 12 / (avg_yield / 100)

    if monthly_income >= m_target:
        return {"reachable": True, "already_at_target": True,
                "m_target": m_target, "avg_yield": avg_yield,
                "contrib_div": contrib_div, "contrib_grow": contrib_grow,
                "growth_rate_pct": growth_rate_pct,
                "p_now": p_now, "g_now": growth_value_total, "p_target": p_target,
                "scenarios": []}

    # Scenario A: always 60/40
    n_a = _solve_n_months(p_now, contrib_div, r, p_target)

    # Scenario B: flip after X. Search X = 0, 1, 2 ... 80 years (1-year resolution).
    best = None
    max_years_search = 80
    for x_years in range(0, max_years_search + 1):
        x_months = x_years * 12
        # Phase 1
        if g > 0:
            G_x = growth_value_total * (1 + g) ** x_months + contrib_grow * ((1 + g) ** x_months - 1) / g
        else:
            G_x = growth_value_total + contrib_grow * x_months
        D_x = p_now * (1 + r) ** x_months + contrib_div * ((1 + r) ** x_months - 1) / r
        P_after_flip = G_x + D_x
        if P_after_flip >= p_target:
            total_months = x_months
            n2 = 0
        else:
            n2 = _solve_n_months(P_after_flip, contrib_total, r, p_target)
            if n2 is None:
                continue
            total_months = x_months + n2
        if best is None or total_months < best["total_months"]:
            best = {
                "flip_at_months": x_months, "flip_at_years": x_years,
                "total_months": total_months,
                "phase2_months": n2,
                "G_at_flip": G_x, "D_at_flip": D_x, "P_at_flip": P_after_flip,
            }

    # Trajectories — sample every ~horizon/40 months (min 6) so chart is smooth.
    horizon = max(int(n_a or 0), int(best["total_months"]) if best else 0)
    if horizon <= 0:
        horizon = 12 * 40
    step = max(6, horizon // 40)

    traj_a = []
    traj_b = []
    def _grow(p, contrib, rate, t):
        if rate <= 0:
            return p + contrib * t
        return p * (1 + rate) ** t + contrib * ((1 + rate) ** t - 1) / rate

    for t in range(0, horizon + step, step):
        # A — div bucket compounds at r, growth bucket compounds at g separately FOREVER.
        D_a = _grow(p_now, contrib_div, r, t)
        G_a = _grow(growth_value_total, contrib_grow, g, t)
        traj_a.append((t, D_a * r, D_a + G_a))
        # B — phase 1 (split) then phase 2 (combined into div bucket).
        if best:
            flip = best["flip_at_months"]
            if t <= flip:
                D_b = _grow(p_now, contrib_div, r, t)
                G_b = _grow(growth_value_total, contrib_grow, g, t)
                d_inc = D_b * r
                total_b = D_b + G_b
            else:
                tt = t - flip
                P_flip = best["P_at_flip"]
                P = _grow(P_flip, contrib_total, r, tt)
                d_inc = P * r
                total_b = P
            traj_b.append((t, d_inc, total_b))

    return {
        "reachable": True, "already_at_target": False,
        "m_target": m_target, "avg_yield": avg_yield,
        "p_now": p_now, "g_now": growth_value_total, "p_target": p_target,
        "contrib_div": contrib_div, "contrib_grow": contrib_grow,
        "contrib_total": contrib_total,
        "growth_rate_pct": growth_rate_pct,
        "split_str": split or "100/0",
        "scenario_a": {
            "label": f"Always {split or '100% div'}",
            "months": n_a,
            "trajectory": traj_a,
        },
        "scenario_b": ({
            "label": f"Flip @ year {best['flip_at_years']}",
            "months": best["total_months"],
            "flip_at_months": best["flip_at_months"],
            "flip_at_years": best["flip_at_years"],
            "phase2_months": best["phase2_months"],
            "P_at_flip": best["P_at_flip"],
            "trajectory": traj_b,
        } if best else None),
    }


def _render_dividend_goal(scenarios, monthly_income):
    """Build the HTML section + Chart.js JS for the Dividend Goal scenarios."""
    if scenarios is None:
        empty = """    <section>
        <h2>Dividend Goal</h2>
        <div class="card" style="padding:16px;color:#8b949e">
            Set <code>recurring_income</code> and <code>dividend_income_target</code>
            in portfolio_state.json to project a timeline.
        </div>
    </section>"""
        return empty, ""

    if scenarios.get("already_at_target"):
        body = f"""    <section>
        <h2>Dividend Goal</h2>
        <div class="card" style="padding:24px">
            <div style="font-size:24px;font-weight:600;color:#3fb950">🎯 Target reached</div>
            <div class="sub">Currently producing ${monthly_income:,.2f}/mo against a ${scenarios['m_target']:,.0f}/mo goal.</div>
        </div>
    </section>"""
        return body, ""

    if not scenarios.get("reachable"):
        body = f"""    <section>
        <h2>Dividend Goal</h2>
        <div class="card" style="padding:16px;color:#f85149">
            Cannot project: {scenarios.get('reason', 'check inputs')}.
        </div>
    </section>"""
        return body, ""

    m_target = scenarios["m_target"]
    avg_yield = scenarios["avg_yield"]
    p_now = scenarios["p_now"]
    g_now = scenarios["g_now"]
    p_target = scenarios["p_target"]
    contrib_div = scenarios["contrib_div"]
    contrib_grow = scenarios["contrib_grow"]
    contrib_total = scenarios["contrib_total"]
    growth_rate_pct = scenarios["growth_rate_pct"]
    split_str = scenarios["split_str"]
    progress_pct = (monthly_income / m_target * 100) if m_target else 0

    a = scenarios["scenario_a"]
    b = scenarios["scenario_b"]

    # Parse the split for human-readable labelling.
    div_pct = int(round((contrib_div / contrib_total * 100))) if contrib_total else 100
    growth_pct = 100 - div_pct
    split_human = f"{growth_pct}% growth / {div_pct}% dividend"

    a_years = (a["months"] / 12) if a["months"] else None
    a_label = f"~{a_years:.1f} years" if a_years else "Never reached"

    b_label = "—"
    b_bullets = ""
    delta_label = ""
    if b:
        b_years = b["months"] / 12
        phase2_years = b["phase2_months"] / 12
        b_label = f"~{b_years:.1f} years"
        b_bullets = (
            f"<li>Years 0–{b['flip_at_years']}: keep {split_human} split.</li>"
            f"<li>Year {b['flip_at_years']}: sell all growth holdings (~${b['P_at_flip']:,.0f}), reinvest in {avg_yield:.2f}% dividend instruments.</li>"
            f"<li>Year {b['flip_at_years']} onward: full ${contrib_total:,.0f}/mo to dividends. Target hit {phase2_years:.1f} yr later.</li>"
        )
        if a_years:
            delta_yr = a_years - b_years
            sign = "sooner" if delta_yr >= 0 else "later"
            delta_label = f"{abs(delta_yr):.1f} years {sign} than A"

    # Trajectory data — format for Chart.js. Each tuple is (month, monthly_div, total_assets).
    def _series_js(traj, idx):
        return "[" + ",".join(
            f"{{x:{row[0]},y:{round(row[idx], 2)}}}" for row in traj
        ) + "]"

    series_a_div = _series_js(a["trajectory"], 1) if a["trajectory"] else "[]"
    series_b_div = _series_js(b["trajectory"], 1) if (b and b["trajectory"]) else "[]"
    series_a_total = _series_js(a["trajectory"], 2) if a["trajectory"] else "[]"
    series_b_total = _series_js(b["trajectory"], 2) if (b and b["trajectory"]) else "[]"
    target_line = round(m_target, 2)
    flip_x = b["flip_at_months"] if b else 0

    # Final-state totals (at horizon end) — drives the trade-off framing.
    final_total_a = a["trajectory"][-1][2] if a["trajectory"] else 0
    final_total_b = b["trajectory"][-1][2] if (b and b["trajectory"]) else 0

    annual_target = m_target * 12
    annual_income = monthly_income * 12

    body = f"""    <section>
        <h2>Dividend Goal</h2>
        <div class="card" style="padding:20px 24px;margin-bottom:16px">
            <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px;margin-bottom:8px">
                <div style="font-size:13px;color:#8b949e;text-transform:uppercase;letter-spacing:0.5px">Progress toward ${m_target:,.0f}/mo · ${annual_target:,.0f}/yr</div>
                <div style="color:#8b949e;font-size:13px">${monthly_income:,.2f}/mo · ${annual_income:,.2f}/yr · {progress_pct:.2f}%</div>
            </div>
            <div style="background:#0f1117;height:10px;border-radius:6px;overflow:hidden">
                <div style="background:linear-gradient(90deg,#3fb950,#58a6ff);height:100%;width:{min(progress_pct, 100):.2f}%"></div>
            </div>
            <div class="sub" style="margin-top:10px;display:flex;gap:18px;flex-wrap:wrap">
                <span>Yield <strong>{avg_yield:.2f}%</strong></span>
                <span>Dividend holdings <strong>${p_now:,.0f}</strong></span>
                <span>Growth holdings <strong>${g_now:,.0f}</strong></span>
                <span>Principal needed <strong>${p_target:,.0f}</strong></span>
            </div>
        </div>

        <div class="cards" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin-bottom:16px">
            <div class="card">
                <div class="label">A · Keep the split forever</div>
                <div class="value">{a_label}</div>
                <ul class="bullets">
                    <li>Each month: ${contrib_div:,.0f} → dividends @ {avg_yield:.2f}%, ${contrib_grow:,.0f} → growth @ {growth_rate_pct:.1f}%.</li>
                    <li>Growth holdings never sold. Only dividend bucket counts toward target.</li>
                    <li>Portfolio at target: <strong>${final_total_a:,.0f}</strong>.</li>
                </ul>
            </div>
            <div class="card">
                <div class="label">B · Pivot growth into dividends at year {b['flip_at_years'] if b else '—'}</div>
                <div class="value positive">{b_label}</div>
                <ul class="bullets">
                    {b_bullets or '<li>—</li>'}
                    <li style="color:#3fb950"><strong>{delta_label}</strong>. Portfolio at target: <strong>${final_total_b:,.0f}</strong>.</li>
                </ul>
            </div>
        </div>

        <div class="card" style="padding:14px 18px;margin-bottom:16px;background:#1c2129;border-color:#2d3440">
            <div class="sub" style="color:#e1e4e8;line-height:1.6">
                <strong>Trade-off:</strong> B converts growth holdings (compounding at {growth_rate_pct:.1f}%) into {avg_yield:.2f}%-yielding dividend instruments earlier — faster monthly income, smaller terminal pile.
                A keeps growth compounding forever — slower income, larger terminal pile.
            </div>
        </div>

        <div class="chart-container">
            <h2 style="margin-bottom:16px">Monthly dividend income over time</h2>
            <div class="chart-canvas-wrap"><canvas id="divGoalChart"></canvas></div>
        </div>

        <div class="chart-container">
            <h2 style="margin-bottom:16px">Total portfolio value over time</h2>
            <div class="chart-canvas-wrap"><canvas id="totalAssetsChart"></canvas></div>
            <div class="sub" style="margin-top:10px">Growth-rate assumption {growth_rate_pct:.1f}%/yr. Override via <code>strategy.growth_rate_pct</code>.</div>
        </div>
    </section>"""

    horizon_months = max(int(a["months"] or 0), int(b["months"]) if b else 0)

    chart_js = f"""
const divGoalEl = document.getElementById('divGoalChart');
if (divGoalEl) {{
    new Chart(divGoalEl.getContext('2d'), {{
        type: 'line',
        data: {{
            datasets: [
                {{
                    label: 'A · Keep split',
                    data: {series_a_div},
                    borderColor: '#fbbf24',
                    backgroundColor: 'rgba(251,191,36,0.08)',
                    fill: true,
                    tension: 0.25,
                    borderWidth: 2,
                    pointRadius: 0,
                }},
                {{
                    label: 'B · Pivot at year {b["flip_at_years"] if b else 0}',
                    data: {series_b_div},
                    borderColor: '#3fb950',
                    backgroundColor: 'rgba(63,185,80,0.10)',
                    fill: true,
                    tension: 0.25,
                    borderWidth: 2,
                    pointRadius: 0,
                }},
                {{
                    label: 'Target ${target_line:,.0f}/mo',
                    data: [{{x:0,y:{target_line}}},{{x:{horizon_months},y:{target_line}}}],
                    borderColor: '#f85149',
                    borderWidth: 1.5,
                    borderDash: [5,5],
                    pointRadius: 0,
                    fill: false,
                }}
            ]
        }},
        options: {{
            responsive: true,
            maintainAspectRatio: false,
            interaction: {{ mode: 'index', intersect: false }},
            plugins: {{
                legend: {{ labels: {{ color: '#8b949e', font: {{ size: 12 }} }} }},
                tooltip: {{
                    callbacks: {{
                        title: items => 'Year ' + (items[0].parsed.x / 12).toFixed(1),
                        label: item => item.dataset.label + ': $' + item.parsed.y.toLocaleString(undefined, {{maximumFractionDigits:0}}) + ' per month'
                    }}
                }}
            }},
            scales: {{
                x: {{
                    type: 'linear',
                    min: 0,
                    max: {horizon_months},
                    title: {{ display: true, text: 'Years from today', color: '#8b949e' }},
                    ticks: {{ color: '#484f58', callback: v => (v / 12).toFixed(0) + 'y' }},
                    grid: {{ color: '#21262d' }},
                }},
                y: {{
                    title: {{ display: true, text: 'Monthly dividend income (USD)', color: '#8b949e' }},
                    ticks: {{ color: '#484f58', callback: v => '$' + v.toLocaleString() }},
                    grid: {{ color: '#21262d' }},
                }},
            }},
        }},
    }});
}}

const totalAssetsEl = document.getElementById('totalAssetsChart');
if (totalAssetsEl) {{
    new Chart(totalAssetsEl.getContext('2d'), {{
        type: 'line',
        data: {{
            datasets: [
                {{
                    label: 'A · Keep split (growth keeps compounding)',
                    data: {series_a_total},
                    borderColor: '#fbbf24',
                    backgroundColor: 'rgba(251,191,36,0.08)',
                    fill: true,
                    tension: 0.25,
                    borderWidth: 2,
                    pointRadius: 0,
                }},
                {{
                    label: 'B · Pivot at year {b["flip_at_years"] if b else 0}',
                    data: {series_b_total},
                    borderColor: '#3fb950',
                    backgroundColor: 'rgba(63,185,80,0.10)',
                    fill: true,
                    tension: 0.25,
                    borderWidth: 2,
                    pointRadius: 0,
                }}
            ]
        }},
        options: {{
            responsive: true,
            maintainAspectRatio: false,
            interaction: {{ mode: 'index', intersect: false }},
            plugins: {{
                legend: {{ labels: {{ color: '#8b949e', font: {{ size: 12 }} }} }},
                tooltip: {{
                    callbacks: {{
                        title: items => 'Year ' + (items[0].parsed.x / 12).toFixed(1),
                        label: item => (item.dataset.label.startsWith('A') ? 'Scenario A' : 'Scenario B') + ' total portfolio value: $' + item.parsed.y.toLocaleString(undefined, {{maximumFractionDigits:0}})
                    }}
                }}
            }},
            scales: {{
                x: {{
                    type: 'linear',
                    min: 0,
                    max: {horizon_months},
                    title: {{ display: true, text: 'Years from today', color: '#8b949e' }},
                    ticks: {{ color: '#484f58', callback: v => (v / 12).toFixed(0) + 'y' }},
                    grid: {{ color: '#21262d' }},
                }},
                y: {{
                    title: {{ display: true, text: 'Total portfolio value (USD)', color: '#8b949e' }},
                    ticks: {{ color: '#484f58', callback: v => '$' + (v >= 1e6 ? (v/1e6).toFixed(1)+'M' : v >= 1e3 ? (v/1e3).toFixed(0)+'k' : v) }},
                    grid: {{ color: '#21262d' }},
                }},
            }},
        }},
    }});
}}
"""
    return body, chart_js


def _months_between(start_iso, now_dt):
    """Whole monthly cycles elapsed from start_iso to now_dt (UTC)."""
    try:
        start_dt = datetime.strptime(start_iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0
    months = (now_dt.year - start_dt.year) * 12 + (now_dt.month - start_dt.month)
    if now_dt.day < start_dt.day:
        months -= 1
    return max(0, months)


def _build_contribution_card(state, recurring, ledger):
    """Reconciliation card. Lazy-fills started_at if missing (writes state back)."""
    if not recurring or not recurring.get("amount"):
        return ""

    if not recurring.get("started_at"):
        recurring["started_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["recurring_income"] = recurring
        STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")

    started_at = recurring["started_at"]
    freq = recurring.get("frequency", "monthly")
    amount = recurring["amount"]

    months_elapsed = _months_between(started_at, datetime.now(timezone.utc))
    cycles_elapsed = months_elapsed // 12 if freq == "annual" else months_elapsed
    expected = round(cycles_elapsed * amount, 2)

    # Only recurring/catchup contributions count toward the card.
    # Seed deposits (initial portfolio funding) are excluded so the card
    # measures contribution discipline, not total cash brought in.
    actual = round(sum(
        e.get("total", 0) for e in ledger
        if e.get("action") == "DEPOSIT"
        and e.get("timestamp", "") >= started_at
        and e.get("deposit_type", "recurring") != "seed"
    ), 2)

    delta = round(expected - actual, 2)
    if abs(delta) < 0.01:
        delta_class = ""
        sub = f"{cycles_elapsed} cycle{'s' if cycles_elapsed != 1 else ''} tracked — on pace"
    elif delta > 0:
        delta_class = "negative"
        sub = (f"{cycles_elapsed} cycle{'s' if cycles_elapsed != 1 else ''} tracked — "
               f"behind ${delta:,.0f}")
    else:
        delta_class = "positive"
        sub = (f"{cycles_elapsed} cycle{'s' if cycles_elapsed != 1 else ''} tracked — "
               f"ahead ${-delta:,.0f}")

    return f"""        <div class="card">
            <div class="label">Contributions</div>
            <div class="value {delta_class}">${actual:,.0f} <span style="color:#8b949e;font-size:18px;font-weight:400">/ ${expected:,.0f}</span></div>
            <div class="sub">{sub}</div>
        </div>"""


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

    # Dividend goal scenarios (Always vs Flip)
    scenarios = _build_scenarios(
        target=target,
        monthly_income=monthly_income,
        holdings=holdings,
        snap_positions=snap_positions,
        recurring=recurring,
        strategy=state.get("strategy") or {},
    )
    dividend_goal_html, dividend_chart_js = _render_dividend_goal(scenarios, monthly_income)

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

    contribution_card_html = _build_contribution_card(state, recurring, ledger)

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
.chart-container > canvas {{ display: block; }}
.chart-canvas-wrap {{ position: relative; height: 300px; width: 100%; }}
ul.bullets {{ list-style: none; padding: 0; margin: 8px 0 0; font-size: 13px; color: #8b949e; }}
ul.bullets li {{ padding: 3px 0 3px 16px; position: relative; line-height: 1.5; }}
ul.bullets li::before {{ content: "·"; position: absolute; left: 4px; color: #484f58; font-weight: bold; }}
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
            <div class="sub">${holdings_value:,.0f} holdings ({holdings_label}) · {num_positions} position{'s' if num_positions != 1 else ''}</div>
        </div>
        <div class="card">
            <div class="label">Available Cash</div>
            <div class="value">${cash:,.2f}</div>
            <div class="sub">Recurring: {recurring_str}</div>
        </div>
{contribution_card_html}
        <div class="card">
            <div class="label">Total P&L</div>
            <div class="value {'positive' if total_pnl >= 0 else 'negative'}">${total_pnl:+,.2f}</div>
            <div class="sub">{pnl_pct:+.2f}% overall</div>
        </div>
    </div>

    <div class="chart-container">
        <h2 style="margin-bottom:16px">Portfolio Value Over Time</h2>
        <div class="chart-canvas-wrap"><canvas id="valueChart"></canvas></div>
    </div>

{dividend_goal_html}

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
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif";
Chart.defaults.font.size = 12;
Chart.defaults.color = '#8b949e';
Chart.defaults.devicePixelRatio = window.devicePixelRatio || 2;
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

{dividend_chart_js}
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
