"""Tests for the BTC 15m live executor. No real orders: pmus is stubbed."""
import json
import os
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import live  # noqa: E402


def fake_pmus(calls):
    mod = types.ModuleType("pmus")

    def call(key_id, secret, method, path, body=None, query="", retries=2):
        calls.append((method, path, body))
        if path == "/v1/account/balances":
            return {"balances": [{"buyingPower": 32.1446, "currency": "USD"}]}
        if path == "/v1/order/preview":
            return {"ok": True}
        if path == "/v1/orders":
            return {"id": "order-123"}
        raise AssertionError(f"unexpected {method} {path}")

    mod.call = call
    return mod


BOOK = {"ask_up": (0.53, 100.0), "ask_down": (0.49, 100.0),
        "bid_up": (0.51, 100.0), "bid_down": (0.47, 100.0),
        "empty_book": False}


class TestGates(unittest.TestCase):
    def test_refuses_without_live_ok(self):
        ok, reason = live.check_live_gates({"live_ok": False}, "k", "s")
        self.assertFalse(ok)
        self.assertIn("live_ok", reason)

    def test_refuses_without_keys(self):
        ok, reason = live.check_live_gates({"live_ok": True}, None, None)
        self.assertFalse(ok)
        self.assertIn("key", reason)

    def test_passes_when_armed(self):
        ok, _ = live.check_live_gates({"live_ok": True}, "k", "s")
        self.assertTrue(ok)


class TestSizing(unittest.TestCase):
    def test_live_size_small_bankroll(self):
        cfg = {"risk_per_window": 0.015, "dollar_cap": 5.0}
        q = live.live_size(0.53, 100.0, 32.14, cfg)
        # min(100, 32.14*0.015/0.53=0.9096, 5.0) -> 0.90
        self.assertEqual(q, 0.90)

    def test_dollar_cap_binds(self):
        cfg = {"risk_per_window": 0.015, "dollar_cap": 5.0}
        q = live.live_size(0.53, 10000.0, 100000.0, cfg)
        self.assertEqual(q, 5.0)

    def test_zero_rounds_down(self):
        cfg = {"risk_per_window": 0.015, "dollar_cap": 5.0}
        self.assertEqual(live.live_size(0.99, 0.001, 1.0, cfg), 0.0)


class TestBankroll(unittest.TestCase):
    def test_extract_real_shape(self):
        out = {"balances": [{"currentBalance": 32.1446, "currency": "USD",
                             "buyingPower": 32.1446, "assetNotional": 0}]}
        from golf_bot import _extract_balance
        self.assertEqual(_extract_balance(out), 32.1446)


class TestPlaceLeg(unittest.TestCase):
    def test_down_side_price_inversion(self):
        # venue prices the LONG side only: buy Down @0.48 = BUY_SHORT @0.52
        calls = []
        sys.modules["pmus"] = fake_pmus(calls)
        try:
            rec = live.place_leg("k", "s", "cpc-x", "down", 0.48, 5.0)
            bodies = [b for m, p, b in calls if p == "/v1/orders"]
            self.assertEqual(len(bodies), 1)
            self.assertEqual(bodies[0]["intent"], "ORDER_INTENT_BUY_SHORT")
            self.assertEqual(bodies[0]["price"]["value"], "0.52")
            self.assertEqual(bodies[0]["tif"], "TIME_IN_FORCE_FILL_OR_KILL")
            self.assertEqual(rec["venue_price"], 0.52)
        finally:
            del sys.modules["pmus"]

    def test_up_side_long_price(self):
        calls = []
        sys.modules["pmus"] = fake_pmus(calls)
        try:
            live.place_leg("k", "s", "cpc-x", "up", 0.53, 5.0)
            bodies = [b for m, p, b in calls if p == "/v1/orders"]
            self.assertEqual(bodies[0]["intent"], "ORDER_INTENT_BUY_LONG")
            self.assertEqual(bodies[0]["price"]["value"], "0.53")
            self.assertEqual(bodies[0]["tif"], "TIME_IN_FORCE_FILL_OR_KILL")
        finally:
            del sys.modules["pmus"]

    def test_unwind_sells_at_best_bid(self):
        calls = []
        sys.modules["pmus"] = fake_pmus(calls)
        try:
            placed = [{"side": "up", "price": 0.53, "qty": 2.0,
                       "order_id": "o1"},
                      {"side": "down", "price": 0.48, "qty": 2.0,
                       "order_id": "o2"}]
            book = {"bid_up": (0.51, 50.0), "bid_down": (0.47, 50.0)}
            res = live.unwind_legs("k", "s", "cpc-x", placed, book)
            bodies = [b for m, p, b in calls if p == "/v1/orders"]
            self.assertEqual(len(bodies), 2)
            self.assertEqual(bodies[0]["intent"], "ORDER_INTENT_SELL_LONG")
            self.assertEqual(bodies[0]["price"]["value"], "0.51")
            self.assertEqual(bodies[1]["intent"], "ORDER_INTENT_SELL_SHORT")
            self.assertEqual(bodies[1]["price"]["value"], "0.53")  # 1-0.47
            self.assertTrue(all("order_id" in r for r in res))
        finally:
            del sys.modules["pmus"]


class TestGateCheck(unittest.TestCase):
    def test_not_poppin_on_one_trade(self):
        poppin, rep = live.check_gate()
        self.assertFalse(poppin)
        # real blotter grows as paper trades settle; the invariant is that
        # it stays below the 150-trade gate v2 floor, not an exact count.
        self.assertLess(rep["n_b_settled"], 150)
        fails = [c for c in rep["checks"] if not c["passed"]]
        self.assertTrue(any("b_trades_150" in c["check"] for c in fails))

    def test_poppin_with_strong_sample(self):
        # synthetic blotter: 150 profitable Sleeve B settles under sig_B
        from paper import SIG
        path = os.path.join(HERE, "live_blotter.jsonl")
        orig = live.PAPER_BLOTTER
        tmp = "/tmp/test_poppin_blotter.jsonl"
        with open(tmp, "w") as f:
            for i in range(150):
                f.write(json.dumps({"type": "settle", "sleeve": "B",
                                    "sig_B": SIG["B"],
                                    "pnl_after_fees": 1.5}) + "\n")
        live.PAPER_BLOTTER = tmp
        # synthetic paper state: no disabled sleeves, model beats market
        # on 200 distinct windows with a tight interval under tolerance
        ps_tmp = "/tmp/test_poppin_state.json"
        with open(ps_tmp, "w") as f:
            json.dump({"disabled": [],
                       "brier_windows": [{"T": 1000000 + i,
                                          "brier_model": 0.10,
                                          "brier_mkt": 0.20}
                                         for i in range(200)]}, f)
        live.PAPER_STATE = ps_tmp
        try:
            poppin, rep = live.check_gate()
            self.assertTrue(poppin, rep)
            self.assertEqual(rep["n_b_settled"], 150)
        finally:
            live.PAPER_BLOTTER = orig
            live.PAPER_STATE = os.path.join(HERE, "paper_state.json")
            for p in (tmp, ps_tmp):
                if os.path.exists(p):
                    os.remove(p)
                if os.path.exists(p):
                    os.remove(p)


class TestPaperUntouched(unittest.TestCase):
    def test_live_run_never_touches_paper(self):
        calls = []
        sys.modules["pmus"] = fake_pmus(calls)
        import time as _time
        # pin clock mid-window so tau gate passes regardless of real time
        live.now_s = lambda: ((_time.time() // 900) * 900) + 300
        # stub the network-dependent flow fns
        live.get_event = lambda T: {"markets": [{
            "assetPriceTerms": {
                "priceToBeat": {"value": "84500"},
                "indexSymbol": "BRTI"}}]}
        live.get_book = lambda T: {}
        live.parse_book = lambda md: dict(BOOK)
        live.fair_snapshot = lambda T, O, now: {"p_up": 0.65, "S": 84510.0,
                                                "tau": 300, "sigma": 0.001}
        # isolate live state
        live.LIVE_STATE = "/tmp/test_live_state.json"
        live.LIVE_BLOTTER = "/tmp/test_live_blotter.jsonl"
        for p in (live.LIVE_STATE, live.LIVE_BLOTTER):
            if os.path.exists(p):
                os.remove(p)
        before = open(os.path.join(HERE, "paper_state.json"), "rb").read()
        try:
            pos = live.run_once_live("k", "s", sleeves=("B",), arm_kill=False)
            self.assertIsNotNone(pos)
            self.assertEqual(pos["mode"], "live")
            methods = [m for m, _, _ in calls]
            self.assertIn("/v1/orders", [p for _, p, _ in calls])
            after = open(os.path.join(HERE, "paper_state.json"), "rb").read()
            self.assertEqual(before, after, "paper state was modified!")
        finally:
            for p in (live.LIVE_STATE, live.LIVE_BLOTTER):
                if os.path.exists(p):
                    os.remove(p)
            live.LIVE_STATE = os.path.join(HERE, "live_state.json")
            live.LIVE_BLOTTER = os.path.join(HERE, "live_blotter.jsonl")
            del sys.modules["pmus"]
            import common as _common
            live.now_s = _common.now_s


if __name__ == "__main__":
    unittest.main()
