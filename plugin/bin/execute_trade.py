#!/usr/bin/env python3
"""
execute_trade.py — Execute trades against the local portfolio state.

Updates cash budget, holdings inventory, portfolio value snapshots, and
appends timestamped ledger entries to data/portfolio_state.json.

Schema v2 (multi-currency).

The `reporting_currency` (default USD) is the unit of `available_cash` and
all P&L summaries on the dashboard. Holdings store cost basis natively
(`avg_cost_native`, `currency`, `fx_rate_at_buy`); reporting-currency views
are computed at read time. Trades pass `price_native` + `fx_rate_at_trade`;
legacy `price` is accepted only when `currency == reporting_currency`.

Usage:
    python execute_trade.py '<json_payload>'

Payload keys (all optional, processed in order):

    {
      # Cash management — pick one:
      "set_budget": 10000.00,                  # SET available_cash to this value (SET_BUDGET ledger)
      "deposit": 4000.00,                      # ADD to available_cash (DEPOSIT, type=recurring)
      "deposit": {"amount": 98000, "type": "seed"},  # tag deposit_type explicitly
      "adjust_cash": {"amount": -50, "reason": "broker fee"},  # signed ADJUST ledger

      # Log a recurring/catchup contribution that is ALREADY inside available_cash:
      # writes DEPOSIT + offsetting ADJUST atomically (cash unchanged, card credits).
      "log_contribution": {
        "amount": 4000, "type": "catchup", "timestamp": "2026-03-17",
        "rationale": "Mar contribution already inside seed"
      },

      "recurring_income": {"amount": 2000.00, "frequency": "monthly",
                           "started_at": "2026-02-17T00:00:00Z"},  # started_at optional

      # Import an existing position without debiting cash:
      "import_position": {
        "ticker": "VUAA.L", "shares": 10, "avg_cost": 121.90,
        "first_buy": "2024-03-15", "currency": "USD"
      },

      # Standard trades. `price_native` is in the trade-currency unit (e.g.
      # EUR for BAS.DE). `fx_rate_at_trade` converts native -> reporting; if
      # omitted for non-reporting currencies, a live rate is fetched.
      "trades": [
        {"action": "BUY", "ticker": "BAS.DE", "shares": 5,
         "price_native": 64.26, "currency": "EUR", "fx_rate_at_trade": 1.105,
         "rationale": "…"},
        {"action": "BUY", "ticker": "NVDA", "shares": 2,
         "price_native": 950.00, "currency": "USD", "rationale": "…"},
        {"action": "SELL", "ticker": "META", "shares": 5,
         "price_native": 520.00, "currency": "USD", "rationale": "…"}
      ],

      # Override or correct a holding's currency tag / cost basis after
      # migration flagged it suspicious. Keeps a verification_history audit log.
      "verify_cost_basis": {
        "ticker": "JEQP.L", "actual_currency": "USD",
        "actual_avg_cost_native": 26.29, "fx_rate_at_buy": 1.0
      },

      # Snapshot current value. `holdings_values` maps ticker -> native price;
      # `fx_rates` maps currency -> native->reporting rate. Missing fx rates
      # fall back to a live yfinance fetch.
      "snapshot_value": true,
      "snapshot_mode": "daily",
      "holdings_values": {"NVDA": 955.00, "BAS.DE": 64.26},
      "fx_rates": {"EUR": 1.105, "GBP": 1.276},

      # Safety operations:
      "undo_last": true,                       # reverse the most recent ledger entry
      "edit_trade": {"index": 3, "fields": {"price_native": 120.00}}
                                               # recomputes total_native + total_reporting + cash chain
    }
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = Path("data/portfolio_state.json")
UTC_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime(UTC_FMT)


def _fetch_fx_rate(currency: str):
    """Lazily call fetch_data._fx_to_usd. Returns float or None on failure.

    Imported here (not at module top) so the script stays usable in test
    environments without network access — callers should always handle None.
    """
    if not currency or currency.upper() == "USD":
        return 1.0
    try:
        spec = importlib.util.spec_from_file_location(
            "_autoportfolio_fetch_data",
            str(Path(__file__).resolve().parent / "fetch_data.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rate = mod._fx_to_usd(currency)
        return float(rate) if rate else None
    except Exception:
        return None


def load_state():
    if STATE_FILE.exists() and STATE_FILE.stat().st_size > 0:
        state = json.loads(STATE_FILE.read_text())
        dirty = migrate_ledger_deposit_types(state)
        dirty = migrate_to_multicurrency(state) or dirty
        if dirty:
            save_state(state)
        return state
    return {
        "schema_version": 2,
        "reporting_currency": "USD",
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


VALID_DEPOSIT_TYPES = ("seed", "recurring", "catchup")


def op_deposit(state, amount: float, deposit_type: str = "recurring"):
    amount = round(float(amount), 2)
    if deposit_type not in VALID_DEPOSIT_TYPES:
        deposit_type = "recurring"
    state["available_cash"] = round(state["available_cash"] + amount, 2)
    state["ledger"].append({
        "timestamp": utcnow_iso(),
        "action": "DEPOSIT",
        "deposit_type": deposit_type,
        "total": amount,
        "cash_after": state["available_cash"],
    })


def op_log_contribution(state, amount: float, deposit_type: str = "catchup",
                        timestamp: str = None, rationale: str = ""):
    """Log a contribution already inside available_cash.

    Writes a DEPOSIT (visible to the contribution card) and an offsetting
    ADJUST atomically, so available_cash stays unchanged but the card credits
    the contribution. Useful for backfilling cycles that were funded out of an
    earlier seed deposit, or for normalizing manual top-ups.
    """
    amount = round(float(amount), 2)
    if deposit_type not in VALID_DEPOSIT_TYPES:
        deposit_type = "catchup"
    ts = timestamp or utcnow_iso()
    if len(ts) == 10:
        ts = ts + "T00:00:00Z"

    state["available_cash"] = round(state["available_cash"] + amount, 2)
    state["ledger"].append({
        "timestamp": ts,
        "action": "DEPOSIT",
        "deposit_type": deposit_type,
        "total": amount,
        "rationale": rationale,
        "cash_after": state["available_cash"],
    })
    state["available_cash"] = round(state["available_cash"] - amount, 2)
    state["ledger"].append({
        "timestamp": ts,
        "action": "ADJUST",
        "total": -amount,
        "rationale": f"Offset {deposit_type} contribution already inside cash",
        "cash_after": state["available_cash"],
    })


def migrate_ledger_deposit_types(state) -> bool:
    """One-shot: tag legacy DEPOSIT entries with `deposit_type`.

    Heuristic: if a DEPOSIT entry has no deposit_type and its timestamp is
    earlier than recurring_income.started_at, tag as 'seed'; otherwise
    'recurring'. Returns True if any entry was touched.
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


PRE_V2_BACKUP = Path("data/portfolio_state.pre-v2.json")


def migrate_to_multicurrency(state) -> bool:
    """One-shot v1 -> v2 migration.

    Adds `schema_version`, `reporting_currency`, and per-holding native cost
    basis fields (`avg_cost_native`, `fx_rate_at_buy`, `cost_basis_verified`,
    `migration_approx`). Backfills BUY/SELL/IMPORT ledger entries with
    `price_native`, `total_native`, `total_reporting`, `fx_rate_at_trade`.
    Tags legacy snapshots with `schema = "v1"` (values not rewritten).

    For non-reporting-currency holdings, fx_rate_at_buy is left None and
    `migration_approx = True`. The next snapshot fills the live FX rate.
    Suspicious-tag detection (avg_cost looks USD-equivalent under non-USD
    tag) runs lazily in `snapshot_value` where live prices are available.

    Idempotent. Backs up state to data/portfolio_state.pre-v2.json on
    first run.
    """
    if state.get("schema_version") == 2:
        return False

    # Backup before any field is added.
    if STATE_FILE.exists() and not PRE_V2_BACKUP.exists():
        PRE_V2_BACKUP.parent.mkdir(parents=True, exist_ok=True)
        PRE_V2_BACKUP.write_text(STATE_FILE.read_text())

    state.setdefault("reporting_currency", "USD")
    rep = state["reporting_currency"]
    now = utcnow_iso()

    # Holdings backfill — `avg_cost` is treated as native; cost_basis_verified
    # is True only when the trade currency matches reporting currency.
    for ticker, h in state.get("holdings", {}).items():
        if "avg_cost_native" in h:
            continue
        h["avg_cost_native"] = h.get("avg_cost")
        ccy = h.get("currency", rep)
        if ccy == rep:
            h["fx_rate_at_buy"] = 1.0
            h["cost_basis_verified"] = True
            h["migration_approx"] = False
        else:
            h["fx_rate_at_buy"] = None
            h["cost_basis_verified"] = False
            h["migration_approx"] = True
        h["migrated_to_multicurrency_at"] = now

    # Ledger backfill — only BUY/SELL/IMPORT carry currency-bearing prices.
    for entry in state.get("ledger", []):
        if entry.get("action") not in ("BUY", "SELL", "IMPORT"):
            continue
        if "price_native" in entry:
            continue
        ccy = entry.get("currency", rep)
        entry["price_native"] = entry.get("price")
        entry["total_native"] = entry.get("total")
        if ccy == rep:
            entry["fx_rate_at_trade"] = 1.0
            entry["total_reporting"] = entry.get("total")
        else:
            entry["fx_rate_at_trade"] = None
            entry["total_reporting"] = entry.get("total")
            entry["migration_approx"] = True

    # Tag legacy snapshots — values frozen.
    for snap in state.get("value_history", []):
        snap.setdefault("schema", "v1")

    state["schema_version"] = 2
    return True


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

def _update_holding(holdings, ticker, add_shares, price_native, fx_rate_at_trade,
                    currency, at_timestamp, first_buy=None):
    """Maintain weighted-average `avg_cost_native` AND `fx_rate_at_buy` on the
    holding. Both are cost-weighted (by `shares × price_native`).

    Keeps `avg_cost` as a legacy alias for one release.
    """
    add_native_cost = add_shares * price_native
    add_reporting_cost = add_native_cost * fx_rate_at_trade
    first_ts = first_buy or at_timestamp

    if ticker in holdings:
        h = holdings[ticker]
        old_native_per_share = h.get("avg_cost_native", h.get("avg_cost", 0))
        old_native_cost = h["shares"] * old_native_per_share
        old_fx = h.get("fx_rate_at_buy")
        if old_fx is None:
            # Legacy/migrated row without backfilled fx: best guess is the
            # current trade's fx — flag as approximation.
            old_fx = fx_rate_at_trade
            h["migration_approx"] = True
        old_reporting_cost = old_native_cost * old_fx

        new_shares = h["shares"] + add_shares
        new_native_cost = old_native_cost + add_native_cost
        new_reporting_cost = old_reporting_cost + add_reporting_cost
        h["shares"] = new_shares
        h["avg_cost_native"] = round(new_native_cost / new_shares, 4) if new_shares else 0.0
        h["fx_rate_at_buy"] = round(new_reporting_cost / new_native_cost, 6) if new_native_cost else 1.0
        h["avg_cost"] = h["avg_cost_native"]  # legacy alias
        h["last_buy"] = at_timestamp
        if currency and "currency" not in h:
            h["currency"] = currency
        h["cost_basis_verified"] = True
        h.pop("tag_suspicious", None)
    else:
        per_share = round(price_native, 4)
        holdings[ticker] = {
            "shares": add_shares,
            "currency": currency or "USD",
            "avg_cost_native": per_share,
            "fx_rate_at_buy": round(fx_rate_at_trade, 6),
            "avg_cost": per_share,  # legacy alias
            "first_buy": first_ts,
            "last_buy": at_timestamp,
            "cost_basis_verified": True,
            "migration_approx": False,
        }


def _resolve_trade_price(trade, reporting_currency):
    """Normalize a trade dict into (price_native, fx_rate_at_trade, currency).

    Accepts either:
      - new shape: {price_native, fx_rate_at_trade?, currency}
      - legacy shape: {price, currency} — only when currency == reporting_currency.

    Raises ValueError with a user-facing message on any unresolvable input.
    """
    currency = (trade.get("currency") or reporting_currency).upper()
    price_native = trade.get("price_native")
    fx = trade.get("fx_rate_at_trade")

    if price_native is None:
        legacy_price = trade.get("price")
        if legacy_price is None:
            raise ValueError("missing price_native (or legacy `price`)")
        if currency == reporting_currency:
            price_native = float(legacy_price)
            fx = 1.0
        else:
            raise ValueError(
                f"price_native required for non-{reporting_currency} trade "
                f"(currency={currency}); refusing to interpret legacy `price` as native"
            )

    if fx is None:
        if currency == reporting_currency:
            fx = 1.0
        else:
            fx = _fetch_fx_rate(currency)
            if fx is None:
                raise ValueError(
                    f"fx_rate_at_trade missing and live fetch failed for {currency}; "
                    f"pass fx_rate_at_trade explicitly"
                )
    return float(price_native), float(fx), currency


def execute_buy(state, ticker, shares, price_native, fx_rate_at_trade, currency, rationale=""):
    rep = state.get("reporting_currency", "USD")
    total_native = round(shares * price_native, 2)
    total_reporting = round(total_native * fx_rate_at_trade, 2)

    if total_reporting > state["available_cash"]:
        return {"ticker": ticker, "status": "rejected",
                "reason": f"Insufficient cash: need {total_reporting} {rep}, have {state['available_cash']}"}

    holding_before = dict(state["holdings"][ticker]) if ticker in state["holdings"] else None
    state["available_cash"] = round(state["available_cash"] - total_reporting, 2)
    now = utcnow_iso()
    _update_holding(state["holdings"], ticker, shares, price_native, fx_rate_at_trade, currency, now)

    state["ledger"].append({
        "timestamp": now,
        "action": "BUY",
        "ticker": ticker,
        "shares": shares,
        "currency": currency,
        "price_native": price_native,
        "fx_rate_at_trade": fx_rate_at_trade,
        "total_native": total_native,
        "total_reporting": total_reporting,
        # Legacy aliases (kept one release; `price` reflects reporting-currency value).
        "price": round(price_native * fx_rate_at_trade, 4),
        "total": total_reporting,
        "rationale": rationale,
        "cash_after": state["available_cash"],
        "_undo_state": holding_before,
    })
    return {"ticker": ticker, "status": "executed", "action": "BUY",
            "shares": shares, "price_native": price_native,
            "total_native": total_native, "total_reporting": total_reporting,
            "cash_after": state["available_cash"]}


def execute_sell(state, ticker, shares, price_native, fx_rate_at_trade, currency, rationale=""):
    holdings = state["holdings"]
    if ticker not in holdings or holdings[ticker]["shares"] < shares:
        held = holdings.get(ticker, {}).get("shares", 0)
        return {"ticker": ticker, "status": "rejected",
                "reason": f"Cannot sell {shares} shares — only hold {held}"}

    h = holdings[ticker]
    avg_cost_native = h.get("avg_cost_native", h.get("avg_cost"))
    fx_at_buy = h.get("fx_rate_at_buy")
    if fx_at_buy is None:
        # Legacy/migrated row — approximate with the trade's fx.
        fx_at_buy = fx_rate_at_trade

    total_native = round(shares * price_native, 2)
    total_reporting = round(total_native * fx_rate_at_trade, 2)

    pnl_native = round((price_native - avg_cost_native) * shares, 2)
    pnl_reporting = round(
        (price_native * fx_rate_at_trade - avg_cost_native * fx_at_buy) * shares, 2
    )
    pnl_fx = round(pnl_reporting - pnl_native * fx_rate_at_trade, 2)

    holding_before = dict(h)
    state["available_cash"] = round(state["available_cash"] + total_reporting, 2)

    h["shares"] -= shares
    if h["shares"] == 0:
        del holdings[ticker]

    state["ledger"].append({
        "timestamp": utcnow_iso(),
        "action": "SELL",
        "ticker": ticker,
        "shares": shares,
        "currency": currency,
        "price_native": price_native,
        "fx_rate_at_trade": fx_rate_at_trade,
        "avg_cost_native": avg_cost_native,
        "fx_rate_at_buy": fx_at_buy,
        "total_native": total_native,
        "total_reporting": total_reporting,
        "pnl_native": pnl_native,
        "pnl_reporting": pnl_reporting,
        "pnl_fx": pnl_fx,
        # Legacy aliases.
        "price": round(price_native * fx_rate_at_trade, 4),
        "avg_cost": avg_cost_native,
        "total": total_reporting,
        "pnl": pnl_reporting,
        "rationale": rationale,
        "cash_after": state["available_cash"],
        "_undo_state": holding_before,
    })
    return {"ticker": ticker, "status": "executed", "action": "SELL",
            "shares": shares, "price_native": price_native,
            "pnl_native": pnl_native, "pnl_reporting": pnl_reporting, "pnl_fx": pnl_fx,
            "cash_after": state["available_cash"]}


def op_import_position(state, spec):
    """Backfill an existing holding without debiting cash.

    Accepts either the new (`avg_cost_native` + optional `fx_rate_at_buy`) or
    legacy (`avg_cost`) shape. Native cost basis is required; for non-reporting
    currencies, fx_rate_at_buy is requested explicitly or live-fetched.
    """
    ticker = spec["ticker"].strip().upper()
    shares = spec["shares"]
    rep = state.get("reporting_currency", "USD")
    currency = (spec.get("currency") or "USD").upper()
    avg_cost_native = float(spec.get("avg_cost_native", spec.get("avg_cost")))
    first_buy = spec.get("first_buy") or utcnow_iso()
    if len(first_buy) == 10:
        first_buy = first_buy + "T00:00:00Z"
    htype = spec.get("type", "growth")
    div_yield = spec.get("dividend_yield_pct")
    fx_rate_at_buy = spec.get("fx_rate_at_buy")

    if fx_rate_at_buy is None:
        if currency == rep:
            fx_rate_at_buy = 1.0
        else:
            fx_rate_at_buy = _fetch_fx_rate(currency)
            # If live fetch failed, leave it None — migration_approx applies.

    holdings = state["holdings"]
    if ticker in holdings:
        _update_holding(
            holdings, ticker, shares, avg_cost_native,
            fx_rate_at_buy if fx_rate_at_buy is not None else 1.0,
            currency, first_buy, first_buy=first_buy,
        )
    else:
        holdings[ticker] = {
            "shares": shares,
            "currency": currency,
            "avg_cost_native": round(avg_cost_native, 4),
            "fx_rate_at_buy": round(fx_rate_at_buy, 6) if fx_rate_at_buy is not None else None,
            "avg_cost": round(avg_cost_native, 4),  # legacy alias
            "first_buy": first_buy,
            "last_buy": first_buy,
            "cost_basis_verified": fx_rate_at_buy is not None,
            "migration_approx": fx_rate_at_buy is None,
        }
    holdings[ticker]["type"] = htype
    if div_yield is not None:
        holdings[ticker]["dividend_yield_pct"] = div_yield

    # Backfill existing snapshots so imported position appears at cost in
    # both native and reporting forms.
    fx_for_backfill = fx_rate_at_buy if fx_rate_at_buy is not None else 1.0
    value_native = round(shares * avg_cost_native, 2)
    value_reporting = round(value_native * fx_for_backfill, 2)
    for snap in state.get("value_history", []):
        positions = snap.setdefault("positions", {})
        if ticker not in positions:
            positions[ticker] = {
                "shares": shares,
                "currency": currency,
                "price_native": avg_cost_native,
                "fx_rate_at_snapshot": fx_for_backfill,
                "value_native": value_native,
                "value_reporting": value_reporting,
                "pnl_native": 0.0,
                "pnl_reporting": 0.0,
                "pnl_fx": 0.0,
                # Legacy aliases.
                "price": avg_cost_native,
                "value": value_reporting,
                "pnl": 0.0,
            }
            snap["holdings_value"] = round(snap.get("holdings_value", 0) + value_reporting, 2)
            snap["total_value"] = round(snap.get("available_cash", 0) + snap["holdings_value"], 2)

    total_native = round(shares * avg_cost_native, 2)
    total_reporting = round(total_native * fx_for_backfill, 2)
    state["ledger"].append({
        "timestamp": utcnow_iso(),
        "action": "IMPORT",
        "ticker": ticker,
        "shares": shares,
        "currency": currency,
        "price_native": avg_cost_native,
        "fx_rate_at_trade": fx_rate_at_buy,
        "total_native": total_native,
        "total_reporting": total_reporting,
        # Legacy aliases.
        "price": avg_cost_native,
        "total": total_native,
        "rationale": spec.get("rationale", "Backfilled existing position (no cash debit)"),
        "cash_after": state["available_cash"],
    })
    return {"ticker": ticker, "status": "imported", "shares": shares,
            "avg_cost_native": avg_cost_native, "currency": currency,
            "fx_rate_at_buy": fx_rate_at_buy, "type": htype}


def op_verify_cost_basis(state, spec):
    """Manual override for a holding's currency / cost basis after migration
    flagged it as suspicious. Records the prior values for audit.
    """
    ticker = spec["ticker"].strip().upper()
    h = state["holdings"].get(ticker)
    if not h:
        return {"ticker": ticker, "status": "rejected", "reason": "no such holding"}

    prior = {
        "currency": h.get("currency"),
        "avg_cost_native": h.get("avg_cost_native"),
        "fx_rate_at_buy": h.get("fx_rate_at_buy"),
        "verified_at": utcnow_iso(),
    }
    history = h.setdefault("verification_history", [])
    history.append(prior)

    if "actual_currency" in spec:
        h["currency"] = spec["actual_currency"].upper()
    if "actual_avg_cost_native" in spec:
        h["avg_cost_native"] = float(spec["actual_avg_cost_native"])
        h["avg_cost"] = h["avg_cost_native"]
    if "fx_rate_at_buy" in spec:
        h["fx_rate_at_buy"] = float(spec["fx_rate_at_buy"])
    h["cost_basis_verified"] = True
    h["migration_approx"] = False
    h.pop("tag_suspicious", None)
    h["cost_basis_verified_at"] = utcnow_iso()

    return {"ticker": ticker, "status": "verified",
            "currency": h["currency"], "avg_cost_native": h["avg_cost_native"],
            "fx_rate_at_buy": h["fx_rate_at_buy"]}


# ---------- snapshots ----------

def _apply_suspicious_tag(holding, ticker, price_native, fx_rate, reporting_currency):
    """If a holding's currency != reporting, check whether avg_cost_native
    actually looks like a reporting-currency value (the JEQP.L class of bug).
    Sets `tag_suspicious=True` with a reason; leaves `cost_basis_verified=False`.
    Idempotent — safe to call every snapshot.
    """
    if holding.get("cost_basis_verified"):
        holding.pop("tag_suspicious", None)
        return
    ccy = holding.get("currency", reporting_currency)
    if ccy == reporting_currency:
        return
    avg = holding.get("avg_cost_native")
    if avg is None or price_native is None or fx_rate is None or price_native <= 0:
        return
    delta_native = abs(avg - price_native) / price_native
    reporting_equiv = price_native * fx_rate
    delta_reporting_equiv = (
        abs(avg - reporting_equiv) / reporting_equiv if reporting_equiv > 0 else 1.0
    )
    if delta_reporting_equiv * 2 < delta_native and delta_native > 0.3:
        holding["tag_suspicious"] = True
        holding["tag_suspicious_reason"] = (
            f"avg_cost_native ({avg}) closer to {reporting_currency}-equivalent of price "
            f"(Δ={delta_reporting_equiv:.3f}) than to native price "
            f"(Δ={delta_native:.3f}) — verify currency tag"
        )
    else:
        holding.pop("tag_suspicious", None)
        holding.pop("tag_suspicious_reason", None)


def snapshot_value(state, holdings_values, mode="daily", fx_rates=None):
    """Capture a v2 snapshot.

    `holdings_values[ticker]` is the current native price (e.g. EUR price for
    BAS.DE). `fx_rates[currency]` is the current native→reporting rate. Both
    fall back to live fetch via fetch_data._fx_to_usd if absent. Holdings whose
    `fx_rate_at_buy` was None (migration approx) get backfilled to the snapshot's
    fx rate, marked `fx_rate_backfilled_at`.
    """
    now = utcnow_iso()
    rep = state.get("reporting_currency", "USD")
    fx_rates = dict(fx_rates or {})
    fx_rates.setdefault(rep, 1.0)

    holdings_value = 0.0
    positions = {}
    for ticker, info in state["holdings"].items():
        ccy = info.get("currency", rep)
        avg_cost_native = info.get("avg_cost_native", info.get("avg_cost", 0))
        # Resolve current native price.
        price_native = holdings_values.get(ticker)
        if price_native is None:
            price_native = avg_cost_native
        # Resolve current fx (native -> reporting).
        fx = fx_rates.get(ccy)
        if fx is None:
            fx = _fetch_fx_rate(ccy)
            if fx is not None:
                fx_rates[ccy] = fx
            else:
                fx = 1.0  # last-resort, will be flagged as approximate

        # Backfill holding's fx_rate_at_buy lazily if migration left it None.
        if info.get("fx_rate_at_buy") is None:
            info["fx_rate_at_buy"] = round(float(fx), 6)
            info["fx_rate_backfilled_at"] = now
            info["migration_approx"] = True
        fx_at_buy = info["fx_rate_at_buy"]

        # Suspicious-tag check (live prices available here).
        _apply_suspicious_tag(info, ticker, price_native, fx, rep)

        value_native = round(info["shares"] * price_native, 2)
        value_reporting = round(value_native * fx, 2)
        pnl_native = round((price_native - avg_cost_native) * info["shares"], 2)
        pnl_reporting = round(
            (price_native * fx - avg_cost_native * fx_at_buy) * info["shares"], 2
        )
        pnl_fx = round(pnl_reporting - pnl_native * fx, 2)
        holdings_value += value_reporting

        positions[ticker] = {
            "shares": info["shares"],
            "currency": ccy,
            "price_native": round(price_native, 4),
            "fx_rate_at_snapshot": round(float(fx), 6),
            "value_native": value_native,
            "value_reporting": value_reporting,
            "pnl_native": pnl_native,
            "pnl_reporting": pnl_reporting,
            "pnl_fx": pnl_fx,
            # Legacy aliases — `price` and `value` reflect reporting-currency values.
            "price": round(price_native * fx, 4),
            "value": value_reporting,
            "pnl": pnl_reporting,
        }

    total_value = round(state["available_cash"] + holdings_value, 2)
    snapshot = {
        "timestamp": now,
        "schema": "v2",
        "reporting_currency": rep,
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
    # Prefer reporting-currency total for cash math; fall back to legacy `total`.
    total_reporting = entry.get("total_reporting", entry.get("total", 0))
    undo_state = entry.get("_undo_state")

    if action == "BUY" and ticker:
        state["available_cash"] = round(state["available_cash"] + total_reporting, 2)
        if undo_state is None:
            # Pre-undo-snapshot ledger row (legacy). Fall back to shares-only reversal.
            h = state["holdings"].get(ticker)
            if h:
                remaining = h["shares"] - shares
                if remaining <= 0:
                    del state["holdings"][ticker]
                else:
                    h["shares"] = remaining
        elif not undo_state:
            # The ticker did not exist before the BUY — drop it now.
            state["holdings"].pop(ticker, None)
        else:
            state["holdings"][ticker] = dict(undo_state)
    elif action == "SELL" and ticker:
        state["available_cash"] = round(state["available_cash"] - total_reporting, 2)
        if undo_state:
            state["holdings"][ticker] = dict(undo_state)
        else:
            # Legacy: best-effort restore — readd shares with the entry's avg_cost.
            state["holdings"].setdefault(ticker, {
                "shares": 0,
                "avg_cost": entry.get("avg_cost", entry.get("price", 0)),
                "avg_cost_native": entry.get("avg_cost_native", entry.get("avg_cost", 0)),
                "fx_rate_at_buy": entry.get("fx_rate_at_buy"),
                "first_buy": entry["timestamp"],
                "last_buy": entry["timestamp"],
                "currency": entry.get("currency", "USD"),
            })["shares"] += shares
    elif action in ("DEPOSIT", "ADJUST"):
        state["available_cash"] = round(state["available_cash"] - entry.get("total", 0), 2)
    elif action == "SET_BUDGET":
        state["available_cash"] = round(state["available_cash"] - entry.get("delta", 0), 2)
    elif action == "IMPORT" and ticker:
        if undo_state is not None:
            if not undo_state:
                state["holdings"].pop(ticker, None)
            else:
                state["holdings"][ticker] = dict(undo_state)
        else:
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
    """Rewrite a ledger row and recompute totals + cash_after chain from start.

    Recomputes both `total_native` and `total_reporting` when `shares`,
    `price_native`, or `fx_rate_at_trade` is touched. Cash chain uses
    `total_reporting` (falling back to legacy `total`).
    """
    ledger = state["ledger"]
    if index < 0 or index >= len(ledger):
        return {"status": "rejected", "reason": f"index {index} out of range"}

    entry = ledger[index]
    for k, v in fields.items():
        entry[k] = v

    if any(k in fields for k in ("shares", "price_native", "fx_rate_at_trade", "price")):
        shares = entry.get("shares", 0)
        price_native = entry.get("price_native", entry.get("price", 0))
        fx = entry.get("fx_rate_at_trade")
        if fx is None:
            ccy = entry.get("currency", "USD")
            rep = state.get("reporting_currency", "USD")
            fx = 1.0 if ccy == rep else None
        if shares and price_native and fx is not None:
            total_native = round(shares * price_native, 2)
            total_reporting = round(total_native * fx, 2)
            entry["total_native"] = total_native
            entry["total_reporting"] = total_reporting
            entry["fx_rate_at_trade"] = fx
            entry["price_native"] = price_native
            # Legacy aliases.
            entry["total"] = total_reporting
            entry["price"] = round(price_native * fx, 4)

    # Recompute cash_after chain from start.
    cash = 0.0
    for e in ledger:
        a = e.get("action")
        amt = e.get("total_reporting", e.get("total", 0))
        if a in ("DEPOSIT", "ADJUST"):
            cash = round(cash + e.get("total", 0), 2)
        elif a == "SET_BUDGET":
            cash = e.get("total", cash)
        elif a == "BUY":
            cash = round(cash - amt, 2)
        elif a == "SELL":
            cash = round(cash + amt, 2)
        # IMPORT does not affect cash
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
        dep = payload["deposit"]
        if isinstance(dep, dict):
            op_deposit(state, dep["amount"], dep.get("type", "recurring"))
        else:
            op_deposit(state, dep)
    if "adjust_cash" in payload:
        adj = payload["adjust_cash"]
        op_adjust_cash(state, adj["amount"], adj.get("reason", ""))

    if "log_contribution" in payload:
        lc = payload["log_contribution"]
        op_log_contribution(
            state,
            lc["amount"],
            lc.get("type", "catchup"),
            lc.get("timestamp"),
            lc.get("rationale", ""),
        )

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

    if "verify_cost_basis" in payload:
        output["verify_cost_basis"] = op_verify_cost_basis(state, payload["verify_cost_basis"])

    # Trades
    rep = state.get("reporting_currency", "USD")
    for trade in payload.get("trades", []):
        action = trade.get("action", "").upper()
        ticker = trade.get("ticker", "").strip().upper()
        shares = trade.get("shares", 0)
        rationale = trade.get("rationale", "")

        if not ticker or shares <= 0:
            output["results"].append({"ticker": ticker, "status": "skipped",
                                      "reason": "invalid ticker or shares"})
            continue

        try:
            price_native, fx, currency = _resolve_trade_price(trade, rep)
        except ValueError as exc:
            output["results"].append({"ticker": ticker, "status": "rejected",
                                      "reason": str(exc)})
            continue

        if price_native <= 0:
            output["results"].append({"ticker": ticker, "status": "skipped",
                                      "reason": "invalid price_native"})
            continue

        if action == "BUY":
            output["results"].append(execute_buy(state, ticker, shares, price_native, fx, currency, rationale))
        elif action == "SELL":
            output["results"].append(execute_sell(state, ticker, shares, price_native, fx, currency, rationale))
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
        output["snapshot"] = snapshot_value(
            state,
            payload["holdings_values"],
            mode,
            fx_rates=payload.get("fx_rates"),
        )

    save_state(state)
    output["available_cash"] = state["available_cash"]
    output["holdings"] = state["holdings"]
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
