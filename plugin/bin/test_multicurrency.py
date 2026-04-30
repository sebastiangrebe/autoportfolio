"""Multi-currency correctness tests for execute_trade.py and generate_dashboard.py.

Run: python3 -m unittest plugin/bin/test_multicurrency.py -v
   or: python3 plugin/bin/test_multicurrency.py
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent


def _load_module(name, file_name):
    spec = importlib.util.spec_from_file_location(name, str(THIS_DIR / file_name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fresh_state(holdings=None, ledger=None, available_cash=100000.0,
                 reporting_currency="USD", schema_version=2):
    return {
        "schema_version": schema_version,
        "reporting_currency": reporting_currency,
        "available_cash": available_cash,
        "recurring_income": None,
        "holdings": holdings or {},
        "ledger": ledger or [],
        "value_history": [],
    }


class _MultiCurrencyTestCase(unittest.TestCase):
    def setUp(self):
        # Per-test scratch dir so STATE_FILE writes don't bleed between tests.
        self._tmp = tempfile.TemporaryDirectory()
        self._cwd = Path.cwd()
        import os
        os.chdir(self._tmp.name)
        Path("data").mkdir(exist_ok=True)
        # Re-load the modules with the patched cwd so STATE_FILE points here.
        self.et = _load_module("et_test", "execute_trade.py")
        self.gd = _load_module("gd_test", "generate_dashboard.py")
        # Block live fx fetches — tests must pass fx explicitly.
        self.et._fetch_fx_rate = lambda c: 1.0 if c == "USD" else None

    def tearDown(self):
        import os
        os.chdir(self._cwd)
        self._tmp.cleanup()


# ---------- FX math primitives ----------

class FXMathTests(_MultiCurrencyTestCase):
    def test_buy_native_currency(self):
        state = _fresh_state(available_cash=10000.0)
        self.et.execute_buy(state, "BAS.DE", 10, 51.93, 1.077, "EUR", "test")
        self.assertAlmostEqual(state["available_cash"], 10000 - 10 * 51.93 * 1.077, places=2)
        h = state["holdings"]["BAS.DE"]
        self.assertEqual(h["avg_cost_native"], 51.93)
        self.assertAlmostEqual(h["fx_rate_at_buy"], 1.077, places=5)
        self.assertEqual(h["currency"], "EUR")
        self.assertTrue(h["cost_basis_verified"])

    def test_buy_weighted_average_fx(self):
        state = _fresh_state(available_cash=10000.0)
        self.et.execute_buy(state, "BAS.DE", 10, 50.00, 1.10, "EUR")
        self.et.execute_buy(state, "BAS.DE", 10, 60.00, 1.20, "EUR")
        h = state["holdings"]["BAS.DE"]
        # Cost-weighted avg of native cost: (10*50 + 10*60) / 20 = 55
        self.assertEqual(h["avg_cost_native"], 55.0)
        # Cost-weighted fx: (10*50*1.10 + 10*60*1.20) / (10*50 + 10*60)
        # = (550 + 720) / 1100 = 1270/1100 = 1.15454...
        self.assertAlmostEqual(h["fx_rate_at_buy"], 1270 / 1100, places=4)

    def test_sell_pnl_decomposition(self):
        # Buy 10 @ €50, fx=1.0; Sell 10 @ €60, fx=1.2.
        state = _fresh_state(available_cash=10000.0)
        self.et.execute_buy(state, "X.DE", 10, 50.00, 1.0, "EUR")
        result = self.et.execute_sell(state, "X.DE", 10, 60.00, 1.2, "EUR")
        self.assertEqual(result["pnl_native"], 100.0)              # (60-50)*10
        self.assertEqual(result["pnl_reporting"], 220.0)           # (60*1.2 - 50*1.0)*10
        self.assertEqual(result["pnl_fx"], 100.0)                  # 220 - 100*1.2 = 100

    def test_sell_pure_fx_gain_with_zero_native_pnl(self):
        # Buy 10 @ €50 fx=1.0; Sell 10 @ €50 fx=1.2 — pure FX gain.
        state = _fresh_state(available_cash=10000.0)
        self.et.execute_buy(state, "X.DE", 10, 50.00, 1.0, "EUR")
        result = self.et.execute_sell(state, "X.DE", 10, 50.00, 1.2, "EUR")
        self.assertEqual(result["pnl_native"], 0.0)
        self.assertAlmostEqual(result["pnl_reporting"], 100.0, places=2)
        self.assertAlmostEqual(result["pnl_fx"], 100.0, places=2)


# ---------- Migration ----------

class MigrationTests(_MultiCurrencyTestCase):
    def _legacy_state(self):
        return {
            "available_cash": 1000.0,
            "holdings": {
                "BAS.DE": {"shares": 11, "avg_cost": 51.93, "currency": "EUR"},
                "NVDA":   {"shares": 13, "avg_cost": 189.31, "currency": "USD"},
            },
            "ledger": [
                {"timestamp": "2024-01-01T00:00:00Z", "action": "DEPOSIT", "total": 1000.0},
                {"timestamp": "2024-06-01T00:00:00Z", "action": "BUY", "ticker": "BAS.DE",
                 "shares": 11, "price": 51.93, "currency": "EUR", "total": 571.23},
            ],
            "value_history": [
                {"timestamp": "2024-06-02T00:00:00Z", "available_cash": 0,
                 "holdings_value": 571.23, "total_value": 571.23, "positions": {}},
            ],
        }

    def test_migration_idempotent(self):
        s = self._legacy_state()
        self.assertTrue(self.et.migrate_to_multicurrency(s))
        self.assertFalse(self.et.migrate_to_multicurrency(s))

    def test_migration_preserves_eur_native(self):
        s = self._legacy_state()
        self.et.migrate_to_multicurrency(s)
        h = s["holdings"]["BAS.DE"]
        self.assertEqual(h["avg_cost_native"], 51.93)
        self.assertIsNone(h["fx_rate_at_buy"])
        self.assertTrue(h["migration_approx"])
        self.assertFalse(h["cost_basis_verified"])

    def test_migration_v1_snapshot_frozen(self):
        s = self._legacy_state()
        before_holdings_value = s["value_history"][0]["holdings_value"]
        self.et.migrate_to_multicurrency(s)
        snap = s["value_history"][0]
        self.assertEqual(snap["schema"], "v1")
        # holdings_value untouched
        self.assertEqual(snap["holdings_value"], before_holdings_value)

    def test_suspicious_tag_detection(self):
        # Holding stored as USD-equivalent under GBP tag (the JEQP.L class).
        # Native price: ~1966.9 GBp; current fx GBP→USD ≈ 0.0136 (per share quoted in pence).
        # avg_cost stored as 26.29 (matches USD-equivalent), not 1966.9 native.
        h = {
            "shares": 74, "avg_cost_native": 26.29, "currency": "GBP",
            "cost_basis_verified": False, "migration_approx": True,
            "fx_rate_at_buy": None,
        }
        # Snapshot path runs the suspicious-tag check.
        self.et._apply_suspicious_tag(h, "JEQP.L", price_native=1966.9, fx_rate=0.0136,
                                      reporting_currency="USD")
        self.assertTrue(h.get("tag_suspicious"))
        self.assertIn("verify currency tag", h["tag_suspicious_reason"])

        # Verified holding does not get flagged.
        h2 = {"shares": 11, "avg_cost_native": 51.93, "currency": "EUR",
              "cost_basis_verified": True}
        self.et._apply_suspicious_tag(h2, "BAS.DE", price_native=64.26, fx_rate=1.105,
                                      reporting_currency="USD")
        self.assertFalse(h2.get("tag_suspicious"))


# ---------- Snapshot ----------

class SnapshotTests(_MultiCurrencyTestCase):
    def test_snapshot_dual_schema(self):
        state = _fresh_state(available_cash=10000.0)
        self.et.execute_buy(state, "BAS.DE", 10, 50.0, 1.10, "EUR")
        self.et.execute_buy(state, "NVDA", 5, 200.0, 1.0, "USD")
        snap = self.et.snapshot_value(
            state,
            holdings_values={"BAS.DE": 55.0, "NVDA": 210.0},
            fx_rates={"EUR": 1.15, "USD": 1.0},
        )
        self.assertEqual(snap["schema"], "v2")
        self.assertEqual(snap["reporting_currency"], "USD")
        # Holdings value sums in reporting currency: 10*55*1.15 + 5*210*1.0 = 632.5 + 1050 = 1682.5
        self.assertAlmostEqual(snap["holdings_value"], 1682.5, places=2)

    def test_snapshot_fx_backfill_first_time(self):
        # Holding has fx_rate_at_buy=None (migration approx). Snapshot fills it.
        state = _fresh_state()
        state["holdings"]["BAS.DE"] = {
            "shares": 10, "currency": "EUR", "avg_cost_native": 50.0,
            "fx_rate_at_buy": None, "migration_approx": True,
            "cost_basis_verified": False,
        }
        self.et.snapshot_value(
            state,
            holdings_values={"BAS.DE": 55.0},
            fx_rates={"EUR": 1.15},
        )
        self.assertAlmostEqual(state["holdings"]["BAS.DE"]["fx_rate_at_buy"], 1.15, places=5)
        self.assertIn("fx_rate_backfilled_at", state["holdings"]["BAS.DE"])

    def test_holdings_value_no_double_count_eur(self):
        # The exact bug class from the user's report: EUR position must contribute
        # shares × price_native × fx, not raw native.
        state = _fresh_state(available_cash=10000.0)
        self.et.execute_buy(state, "X.DE", 100, 30.0, 1.20, "EUR")
        snap = self.et.snapshot_value(
            state,
            holdings_values={"X.DE": 30.0},
            fx_rates={"EUR": 1.20},
        )
        # Expected: shares × price × fx = 100 × 30 × 1.20 = 3600.
        # Bug-class would have produced 100 × 30 = 3000 (raw native summed as USD).
        self.assertAlmostEqual(snap["holdings_value"], 3600.0, places=2)


# ---------- Dashboard ----------

class DashboardTests(_MultiCurrencyTestCase):
    def _state_with_pnl_decomposition(self):
        state = _fresh_state(available_cash=10000.0)
        self.et.execute_buy(state, "X.DE", 10, 50.0, 1.0, "EUR")
        self.et.snapshot_value(
            state,
            holdings_values={"X.DE": 60.0},
            fx_rates={"EUR": 1.20},
        )
        self.et.save_state(state)
        return state

    def test_dashboard_total_pnl_decomposes(self):
        self._state_with_pnl_decomposition()
        html = self.gd.generate_html(self.gd.load_state())
        self.assertIn("Unrealized:", html)
        self.assertIn("Stock", html)
        self.assertIn("FX", html)

    def test_dashboard_handles_v1_v2_mixed(self):
        # Build state with a v1 snapshot first, then a v2 snapshot.
        state = _fresh_state(available_cash=10000.0)
        state["value_history"].append({
            "timestamp": "2024-01-01T00:00:00Z", "schema": "v1",
            "available_cash": 10000.0, "holdings_value": 0.0, "total_value": 10000.0,
            "positions": {},
        })
        self.et.execute_buy(state, "NVDA", 5, 200.0, 1.0, "USD")
        self.et.snapshot_value(
            state,
            holdings_values={"NVDA": 210.0},
            fx_rates={"USD": 1.0},
        )
        self.et.save_state(state)
        html = self.gd.generate_html(self.gd.load_state())
        # Latest is v2, banner should NOT appear.
        self.assertNotIn("pre-dates", html)

        # Now drop the v2 snapshot so the v1 is the latest.
        state2 = self.gd.load_state()
        state2["value_history"] = state2["value_history"][:1]
        self.et.save_state(state2)
        html2 = self.gd.generate_html(self.gd.load_state())
        self.assertIn("pre-dates", html2)


# ---------- Edit / undo ----------

class EditUndoTests(_MultiCurrencyTestCase):
    def test_edit_trade_recomputes_dual_totals(self):
        # Seed via SET_BUDGET so the cash chain rebuilds from a real baseline.
        state = _fresh_state(available_cash=0.0)
        self.et.op_set_budget(state, 10000.0)
        self.et.execute_buy(state, "X.DE", 10, 50.0, 1.10, "EUR")
        # Edit price_native from 50 to 60.
        self.et.op_edit_trade(state, len(state["ledger"]) - 1,
                              {"price_native": 60.0})
        entry = state["ledger"][-1]
        self.assertEqual(entry["total_native"], 600.0)
        self.assertAlmostEqual(entry["total_reporting"], 660.0, places=2)
        # Cash chain rebuilt from SET_BUDGET=10000 minus 660 = 9340.
        self.assertAlmostEqual(state["available_cash"], 9340.0, places=2)

    def test_undo_buy_restores_fx_weighted_avg(self):
        state = _fresh_state(available_cash=10000.0)
        # First buy at fx=1.0, second at fx=1.2 (changes weighted average).
        self.et.execute_buy(state, "X.DE", 10, 50.0, 1.0, "EUR")
        before_avg = state["holdings"]["X.DE"]["avg_cost_native"]
        before_fx = state["holdings"]["X.DE"]["fx_rate_at_buy"]
        self.et.execute_buy(state, "X.DE", 10, 60.0, 1.2, "EUR")
        # Undo last buy — should restore the pre-second-buy snapshot.
        self.et.op_undo_last(state)
        h = state["holdings"]["X.DE"]
        self.assertEqual(h["avg_cost_native"], before_avg)
        self.assertEqual(h["fx_rate_at_buy"], before_fx)


if __name__ == "__main__":
    unittest.main()
