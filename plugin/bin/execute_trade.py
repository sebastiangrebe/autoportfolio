#!/usr/bin/env python3
"""
execute_trade.py — Execute trades against the local portfolio state.

Updates cash budget, holdings inventory, portfolio value snapshots, and
appends timestamped ledger entries to data/portfolio_state.json.

All monetary values are tracked in USD (the reference currency). Holdings
may carry a native `currency` field for context, but P&L and cash always
resolve to USD.

Usage:
    python execute_trade.py '<json_payload>'

Payload keys (all optional, processed in order):

    {
      # Cash management — pick one:
      "set_budget": 10000.00,                  # SET available_cash to this value (SET_BUDGET ledger)
      "deposit": 4000.00,                      # ADD to available_cash (DEPOSIT ledger)
      "adjust_cash": {"amount": -50, "reason": "broker fee"},  # signed ADJUST ledger

      "recurring_income": {"amount": 2000.00, "frequency": "monthly"},

      # Import an existing position without debiting cash:
      "import_position": {
        "ticker": "VUAA.L", "shares": 10, "avg_cost": 121.90,
        "first_buy": "2024-03-15", "currency": "USD"
      },

      # Standard trades:
      "trades": [
        {"action": "BUY",  "ticker": "NVDA", "shares": 2, "price": 950.00,
         "rationale": "…", "currency": "USD"},
        {"action": "SELL", "ticker": "META", "shares": 5, "price": 520.00,
         "rationale": "…"}
      ],

      # Snapshot current value. "mode" controls dedup:
      #   "daily"        — replace any snapshot from the same UTC day (default)
      #   "latest-only"  — keep only the most recent snapshot
      #   "keep-history" — append unconditionally
      "snapshot_value": true,
      "snapshot_mode": "daily",
      "holdings_values": {"NVDA": 955.00, "VUAA.L": 131.34},

      # Safety operations:
      "undo_last": true,                       # reverse the most recent ledger entry
      "edit_trade": {"index": 3, "fields": {"price": 120.00}}  # rewrites row and
                                                               # recomputes cash_after chain
    }
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path("data/portfolio_state.json")
UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime(UTC_FMT)


def load_state():
    if STATE_FILE.exists() and STATE_FILE.stat().st_size > 0:
        return json.loads(STATE_FILE.read_text())
    return {
        "available_cash": 0.0,
        "recurring_income": None,
        "holdings": {},
        "ledger": [],
        "value_history": [],
    }


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


# ---------- cash operations ----------

def op_set_budget(state, value: float):
    value = round(float(value), 2)
    delta = round(value - state["available_cash"], 2)
    state["available_cash"] = value
    state["ledger"].append({
        "timestamp": utcnow_iso(),
        "action": "SET_BUDGET",
        "total": value,
        "delta": delta,
        "cash_after": value,
    })


def op_deposit(state, amount: float):
    amount = round(float(amount), 2)
    state["available_cash"] = round(state["available_cash"] + amount, 2)
    state["ledger"].append({
        "timestamp": utcnow_iso(),
        "action": "DEPOSIT",
        "total": amount,
        "cash_after": state["available_cash"],
    })


def op_adjust_cash(state, amount: float, reason: str = ""):
    amount = round(float(amount), 2)
    state["available_cash"] = round(state["available_cash"] + amount, 2)
    state["ledger"].append({
        "timestamp": utcnow_iso(),
        "action": "ADJUST",
        "total": amount,
        "rationale": reason,
        "cash_after": state["available_cash"],
    })


# ---------- holdings operations ----------

def _update_holding(holdings, ticker, add_shares, add_cost, currency, at_timestamp):
    date_str = at_timestamp[:10]
    if ticker in holdings:
        existing = holdings[ticker]
        old_total = existing["shares"] * existing["avg_cost"]
        new_total = old_total + add_cost
        new_shares = existing["shares"] + add_shares
        holdings[ticker]["shares"] = new_shares
        # 4 decimals to avoid weighted-average rounding drift
        holdings[ticker]["avg_cost"] = round(new_total / new_shares, 4)
        holdings[ticker]["last_buy"] = at_timestamp
        if currency and "currency" not in existing:
            holdings[ticker]["currency"] = currency
    else:
        holdings[ticker] = {
            "shares": add_shares,
            "avg_cost": round(add_cost / add_shares, 4),
            "first_buy": at_timestamp,
            "last_buy": at_timestamp,
            "currency": currency or "USD",
        }


def execute_buy(state, ticker, shares, price, rationale="", currency="USD"):
    total_cost = round(shares * price, 2)
    if total_cost > state["available_cash"]:
        return {"ticker": ticker, "status": "rejected",
                "reason": f"Insufficient cash: need ${total_cost}, have ${state['available_cash']}"}

    state["available_cash"] = round(state["available_cash"] - total_cost, 2)
    now = utcnow_iso()
    _update_holding(state["holdings"], ticker, shares, total_cost, currency, now)

    state["ledger"].append({
        "timestamp": now,
        "action": "BUY",
        "ticker": ticker,
        "shares": shares,
        "price": price,
        "currency": currency,
        "total": total_cost,
        "rationale": rationale,
        "cash_after": state["available_cash"],
    })
    return {"ticker": ticker, "status": "executed", "action": "BUY",
            "shares": shares, "price": price, "total": total_cost,
            "cash_after": state["available_cash"]}


def execute_sell(state, ticker, shares, price, rationale=""):
    holdings = state["holdings"]
    if ticker not in holdings or holdings[ticker]["shares"] < shares:
        held = holdings.get(ticker, {}).get("shares", 0)
        return {"ticker": ticker, "status": "rejected",
                "reason": f"Cannot sell {shares} shares — only hold {held}"}

    total_proceeds = round(shares * price, 2)
    avg_cost = holdings[ticker]["avg_cost"]
    pnl = round((price - avg_cost) * shares, 2)
    state["available_cash"] = round(state["available_cash"] + total_proceeds, 2)

    holdings[ticker]["shares"] -= shares
    if holdings[ticker]["shares"] == 0:
        del holdings[ticker]

    state["ledger"].append({
        "timestamp": utcnow_iso(),
        "action": "SELL",
        "ticker": ticker,
        "shares": shares,
        "price": price,
        "avg_cost": avg_cost,
        "total": total_proceeds,
        "pnl": pnl,
        "rationale": rationale,
        "cash_after": state["available_cash"],
    })
    return {"ticker": ticker, "status": "executed", "action": "SELL",
            "shares": shares, "price": price, "total": total_proceeds,
            "pnl": pnl, "cash_after": state["available_cash"]}


def op_import_position(state, spec):
    ticker = spec["ticker"].strip().upper()
    shares = spec["shares"]
    avg_cost = float(spec["avg_cost"])
    currency = spec.get("currency", "USD")
    first_buy = spec.get("first_buy") or utcnow_iso()
    if len(first_buy) == 10:  # date only → pad with midnight UTC
        first_buy = first_buy + "T00:00:00Z"
    htype = spec.get("type", "growth")
    div_yield = spec.get("dividend_yield_pct")

    total = round(shares * avg_cost, 2)
    holdings = state["holdings"]
    if ticker in holdings:
        _update_holding(holdings, ticker, shares, total, currency, first_buy)
    else:
        holdings[ticker] = {
            "shares": shares,
            "avg_cost": round(avg_cost, 4),
            "first_buy": first_buy,
            "last_buy": first_buy,
            "currency": currency,
        }
    holdings[ticker]["type"] = htype
    if div_yield is not None:
        holdings[ticker]["dividend_yield_pct"] = div_yield

    # Backfill existing snapshots so imported position appears at cost
    import_value = round(shares * avg_cost, 2)
    for snap in state.get("value_history", []):
        positions = snap.setdefault("positions", {})
        if ticker not in positions:
            positions[ticker] = {
                "shares": shares,
                "price": avg_cost,
                "value": import_value,
                "pnl": 0.0,
                "currency": currency,
            }
            snap["holdings_value"] = round(snap.get("holdings_value", 0) + import_value, 2)
            snap["total_value"] = round(snap.get("available_cash", 0) + snap["holdings_value"], 2)

    state["ledger"].append({
        "timestamp": utcnow_iso(),
        "action": "IMPORT",
        "ticker": ticker,
        "shares": shares,
        "price": avg_cost,
        "currency": currency,
        "total": total,
        "rationale": spec.get("rationale", "Backfilled existing position (no cash debit)"),
        "cash_after": state["available_cash"],
    })
    return {"ticker": ticker, "status": "imported", "shares": shares,
            "avg_cost": avg_cost, "currency": currency, "type": htype}


# ---------- snapshots ----------

def snapshot_value(state, holdings_values, mode="daily"):
    now = utcnow_iso()
    holdings_value = 0.0
    positions = {}
    for ticker, info in state["holdings"].items():
        current_price = holdings_values.get(ticker, info["avg_cost"])
        position_value = round(info["shares"] * current_price, 2)
        holdings_value += position_value
        positions[ticker] = {
            "shares": info["shares"],
            "price": current_price,
            "value": position_value,
            "pnl": round((current_price - info["avg_cost"]) * info["shares"], 2),
            "currency": info.get("currency", "USD"),
        }

    total_value = round(state["available_cash"] + holdings_value, 2)
    snapshot = {
        "timestamp": now,
        "available_cash": state["available_cash"],
        "holdings_value": round(holdings_value, 2),
        "total_value": total_value,
        "positions": positions,
    }

    history = state.setdefault("value_history", [])
    if mode == "latest-only":
        state["value_history"] = [snapshot]
    elif mode == "daily":
        today = now[:10]
        state["value_history"] = [s for s in history if s.get("timestamp", "")[:10] != today]
        state["value_history"].append(snapshot)
    else:  # keep-history
        history.append(snapshot)
    return snapshot


# ---------- safety ops ----------

def op_undo_last(state):
    if not state["ledger"]:
        return {"status": "noop", "reason": "ledger empty"}
    entry = state["ledger"].pop()
    action = entry.get("action")
    ticker = entry.get("ticker")
    shares = entry.get("shares", 0)
    total = entry.get("total", 0)

    if action == "BUY" and ticker:
        state["available_cash"] = round(state["available_cash"] + total, 2)
        h = state["holdings"].get(ticker)
        if h:
            remaining = h["shares"] - shares
            if remaining <= 0:
                del state["holdings"][ticker]
            else:
                # naive reverse: drop shares, keep avg_cost (best effort)
                h["shares"] = remaining
    elif action == "SELL" and ticker:
        state["available_cash"] = round(state["available_cash"] - total, 2)
        state["holdings"].setdefault(ticker, {
            "shares": 0, "avg_cost": entry.get("avg_cost", entry.get("price", 0)),
            "first_buy": entry["timestamp"], "last_buy": entry["timestamp"],
            "currency": entry.get("currency", "USD"),
        })["shares"] += shares
    elif action in ("DEPOSIT", "ADJUST"):
        state["available_cash"] = round(state["available_cash"] - total, 2)
    elif action == "SET_BUDGET":
        state["available_cash"] = round(state["available_cash"] - entry.get("delta", 0), 2)
    elif action == "IMPORT" and ticker:
        h = state["holdings"].get(ticker)
        if h:
            remaining = h["shares"] - shares
            if remaining <= 0:
                del state["holdings"][ticker]
            else:
                h["shares"] = remaining

    return {"status": "undone", "undone_action": action, "ticker": ticker,
            "cash_after": state["available_cash"]}


def op_edit_trade(state, index, fields):
    """Rewrite a ledger row and recompute the cash_after chain from that point."""
    ledger = state["ledger"]
    if index < 0 or index >= len(ledger):
        return {"status": "rejected", "reason": f"index {index} out of range"}

    entry = ledger[index]
    for k, v in fields.items():
        entry[k] = v
    # If shares/price changed, recompute total
    if "shares" in fields or "price" in fields:
        shares = entry.get("shares", 0)
        price = entry.get("price", 0)
        if shares and price:
            entry["total"] = round(shares * price, 2)

    # Recompute cash_after chain from the start
    cash = 0.0
    for e in ledger:
        a = e.get("action")
        t = e.get("total", 0)
        if a in ("DEPOSIT", "ADJUST"):
            cash = round(cash + t, 2)
        elif a == "SET_BUDGET":
            cash = e.get("total", cash)
        elif a == "BUY" or a == "IMPORT":
            if a == "BUY":
                cash = round(cash - t, 2)
            # IMPORT does not debit cash
        elif a == "SELL":
            cash = round(cash + t, 2)
        e["cash_after"] = cash

    state["available_cash"] = cash
    return {"status": "edited", "index": index, "cash_after": cash}


# ---------- main ----------

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python execute_trade.py '<json>'"}))
        sys.exit(1)

    try:
        payload = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"Invalid JSON: {exc}"}))
        sys.exit(1)

    state = load_state()
    output = {"results": []}

    # Cash ops
    if "set_budget" in payload:
        op_set_budget(state, payload["set_budget"])
    if "deposit" in payload:
        op_deposit(state, payload["deposit"])
    if "adjust_cash" in payload:
        adj = payload["adjust_cash"]
        op_adjust_cash(state, adj["amount"], adj.get("reason", ""))

    if "recurring_income" in payload and payload["recurring_income"]:
        cfg = dict(payload["recurring_income"])
        if "started_at" not in cfg:
            existing = state.get("recurring_income") or {}
            cfg["started_at"] = existing.get("started_at") or utcnow_iso()
        state["recurring_income"] = cfg

    if "dividend_income_target" in payload and payload["dividend_income_target"]:
        state["dividend_income_target"] = payload["dividend_income_target"]

    if "strategy" in payload and payload["strategy"]:
        state["strategy"] = payload["strategy"]

    if "import_position" in payload:
        output["import"] = op_import_position(state, payload["import_position"])

    # Trades
    for trade in payload.get("trades", []):
        action = trade.get("action", "").upper()
        ticker = trade.get("ticker", "").strip().upper()
        shares = trade.get("shares", 0)
        price = trade.get("price", 0.0)
        rationale = trade.get("rationale", "")
        currency = trade.get("currency", "USD")

        if not ticker or shares <= 0 or price <= 0:
            output["results"].append({"ticker": ticker, "status": "skipped",
                                      "reason": "invalid ticker, shares, or price"})
            continue

        if action == "BUY":
            output["results"].append(execute_buy(state, ticker, shares, price, rationale, currency))
        elif action == "SELL":
            output["results"].append(execute_sell(state, ticker, shares, price, rationale))
        else:
            output["results"].append({"ticker": ticker, "status": "skipped",
                                      "reason": f"unknown action: {action}"})

    # Safety ops (after trades, so you can undo in same call if needed)
    if payload.get("undo_last"):
        output["undo"] = op_undo_last(state)
    if "edit_trade" in payload:
        et = payload["edit_trade"]
        output["edit"] = op_edit_trade(state, et["index"], et.get("fields", {}))

    # Watchlist
    if "watchlist_add" in payload:
        wl = state.setdefault("watchlist", [])
        entry = payload["watchlist_add"]
        ticker = entry["ticker"].strip().upper()
        wl = [w for w in wl if w["ticker"] != ticker]
        wl.append({"ticker": ticker, "condition": entry.get("condition", ""), "added": utcnow_iso()})
        state["watchlist"] = wl
        output["watchlist_add"] = {"ticker": ticker, "status": "added"}
    if "watchlist_remove" in payload:
        ticker = payload["watchlist_remove"]["ticker"].strip().upper()
        wl = state.get("watchlist", [])
        state["watchlist"] = [w for w in wl if w["ticker"] != ticker]
        output["watchlist_remove"] = {"ticker": ticker, "status": "removed"}

    # Session log
    if "log_session" in payload:
        log_path = Path("data/sessions.jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = payload["log_session"]
        entry["timestamp"] = utcnow_iso()
        with open(log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        output["session_logged"] = True

    # Snapshot
    if payload.get("snapshot_value") and payload.get("holdings_values"):
        mode = payload.get("snapshot_mode", "daily")
        output["snapshot"] = snapshot_value(state, payload["holdings_values"], mode)

    save_state(state)
    output["available_cash"] = state["available_cash"]
    output["holdings"] = state["holdings"]
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
