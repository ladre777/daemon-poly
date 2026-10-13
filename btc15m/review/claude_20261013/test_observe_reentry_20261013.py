"""Observation re-entry (Claude 2026-10-13, LaDre-approved).

A window already decided for ABC must still be revisited for OBSERVATION
while a D band is open, even when D is disabled -- and that re-entry must
never trade. When D is enabled, behaviour is exactly as before.
"""
import paper as pm

BOOK = {"ask_up": (0.80, 100.0), "bid_up": (0.78, 100.0),
        "ask_down": (0.22, 100.0), "bid_down": (0.20, 100.0)}


def _env(monkeypatch, *, tau, disabled, decided, day_pnl=0.0,
         d_trigger=False, b_trade=False):
    now = 1999999800 + (900 - tau)
    T = pm.window_start(now)
    assert T + 900 - now == tau
    state = {"open": [], "decided_windows": [str(T)] if decided else [],
             "decided_d1": [], "decided_d2": [], "day_pnl": day_pnl,
             "consec_losses": 0, "disabled": list(disabled),
             "day_pnls": {pm.today_key(): day_pnl},
             "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0,
             "d_shadow": [], "brier_windows": [], "pending_brier": {},
             "day": pm.today_key()}
    saved, wrote, calls = {}, [], {"b": 0}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state", lambda s: saved.update(s))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "append_jsonl", lambda path, rec: wrote.append(rec))
    monkeypatch.setattr(pm, "get_event", lambda T: {"markets": [{"m": 1}]})
    monkeypatch.setattr(pm, "parse_terms",
                        lambda m: {"priceToBeat": 85000.0, "index": "BRTI"})
    monkeypatch.setattr(pm, "get_book", lambda T: {"raw": 1})
    monkeypatch.setattr(pm, "parse_book", lambda raw: BOOK)
    monkeypatch.setattr(pm, "mid_up", lambda b: 0.79)
    monkeypatch.setattr(pm, "_cached_sigma", lambda *a: 0.01)
    monkeypatch.setattr(pm, "fair_snapshot",
                        lambda *a, **k: {"p_up": 0.75, "S": 85010.0,
                                         "tau": tau, "sigma": 1e-5})
    monkeypatch.setattr(pm, "d_lead_vol", lambda O, Te, st: (8.0, 2.0))
    monkeypatch.setattr(pm, "decide_sleeve_d",
                        lambda b, lead, vol, tau, sub:
                        ({"sleeve": sub, "edge": 0.10} if d_trigger else None))

    def _b(book, p, tau_):
        calls["b"] += 1
        return ({"sleeve": "B", "edge": 0.10,
                 "legs": [{"side": "up", "price": 0.80, "size": 5}]}
                if b_trade else None)
    monkeypatch.setattr(pm, "decide_sleeve_b", _b)
    monkeypatch.setattr(pm, "b_waterfall", lambda *a: {"evaluated": True})
    monkeypatch.setattr(pm, "_tick_diagnostics", lambda *a: {})
    return T, state, saved, wrote, calls


def test_reentry_observes_when_d_disabled(monkeypatch):
    T, st, saved, wrote, _ = _env(monkeypatch, tau=300,
                                  disabled=["B", "C", "D"], decided=True)
    rec = pm.run_once()
    assert rec is not None, "decided window must be observed in the D1 band"
    assert rec["reason"].startswith("observation re-entry")
    grid = saved["pending_brier"][str(T)]["tau_grid"]
    assert 300 in {int(k) for k in grid}, "grid-300 must fill on re-entry"
    assert saved["open"] == []
    assert wrote and wrote[-1]["reason"].startswith("observation re-entry")


def test_reentry_never_trades_even_if_b_enabled(monkeypatch):
    T, st, saved, wrote, calls = _env(monkeypatch, tau=300,
                                      disabled=["D"], decided=True,
                                      b_trade=True)
    rec = pm.run_once()
    assert rec["reason"].startswith("observation re-entry")
    assert calls["b"] == 0, "B must not even be evaluated on re-entry"
    assert rec["b_waterfall"]["reason"] == "observe_reentry"
    assert saved["open"] == []


def test_outside_d_bands_still_skips(monkeypatch):
    _env(monkeypatch, tau=500, disabled=["B", "C", "D"], decided=True)
    assert pm.run_once() is None


def test_d_trigger_logged_disabled_then_band_closes(monkeypatch):
    T, st, saved, wrote, _ = _env(monkeypatch, tau=300,
                                  disabled=["B", "C", "D"], decided=True,
                                  d_trigger=True)
    pm.run_once()
    sh = saved.get("d_shadow", [])
    assert sh and sh[-1]["disposition"] == "disabled_no_trade"
    assert str(T) in saved["decided_d1"]
    assert saved["open"] == []
    # same window, same tick again: D1 now decided, D2 band not open at 300
    assert pm.run_once() is None


def test_d_enabled_path_unchanged(monkeypatch):
    T, st, saved, wrote, _ = _env(monkeypatch, tau=300,
                                  disabled=["B", "C"], decided=True)
    rec = pm.run_once()
    assert rec is not None
    assert rec["reason"] == "no sleeve gate passed"
    assert rec["b_waterfall"]["reason"] == "b_disabled"


def test_halt_reason_takes_precedence(monkeypatch):
    _env(monkeypatch, tau=300, disabled=["B", "C", "D"], decided=True,
         day_pnl=-10_000.0)
    rec = pm.run_once()
    assert rec["reason"].startswith("kill switch (observation only)")


def test_undecided_window_unchanged(monkeypatch):
    T, st, saved, wrote, _ = _env(monkeypatch, tau=600,
                                  disabled=["B", "C", "D"], decided=False)
    rec = pm.run_once()
    assert rec["reason"] == "no sleeve gate passed"


def test_hashes_unchanged():
    assert pm.signal_hashes() == pm.SIG
    assert pm.risk_hash() == pm.RISK


def test_reentry_never_runs_sleeve_a(monkeypatch):
    """A is not in `disabled` by default; re-entry must not give it extra
    evaluation passes on a window it already decided."""
    calls = {"a": 0}

    def _a(book):
        calls["a"] += 1
        return {"sleeve": "A", "edge": 0.05,
                "legs": [{"side": "up", "price": 0.40, "size": 5},
                         {"side": "down", "price": 0.40, "size": 5}]}
    T, st, saved, wrote, _ = _env(monkeypatch, tau=300,
                                  disabled=["B", "C", "D"], decided=True)
    monkeypatch.setattr(pm, "decide_sleeve_a", _a)
    rec = pm.run_once()
    assert rec["reason"].startswith("observation re-entry")
    assert calls["a"] == 0 and saved["open"] == []
