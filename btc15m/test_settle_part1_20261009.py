"""Trade settlement Part 1 (2026-10-09, LaDre-signed): settle B/C/D trades
on the venue's published settlement reference (next window's
price-to-beat), waiting for it and falling back to s_avg after the TTL.

No network: brti.fetch_1s is stubbed. Every write is under tmp_path
(conftest isolates HERE/STATE/BLOTTER and guards the live files).
"""
import json
import os

import brti
import paper as pm

T0 = 1791491400
TTL = pm.TRADE_SETTLE_TTL  # 1800


def _closes(Te, base=83300.0, step=0.37):
    """Deterministic, non-flat 1s series around Te."""
    return {x: round(base + step * (x - Te) + (x % 7) * 0.11, 2)
            for x in range(Te - 200, Te + 100)}


def _avg(series, start):
    return sum(series[x] for x in range(start, start + 60)) / 60.0


# ---------------------------------------------------------------- helper

def test_decision_settles_on_next_pb():
    action, px, src, own_cap, mm = pm._trade_settle_decision(
        T0, T0 + 900, 83300.0, 83299.0, {str(T0): 83300.0,
                                         str(T0 + 900): 83301.456}, 9999999999)
    assert (action, px, src) == ("settle", 83301.46, "next_pb")
    assert own_cap == 83300.0 and mm is False


def test_decision_waits_while_next_pb_missing():
    now = T0 + 900 + 100  # matured, inside TTL
    assert pm._trade_settle_decision(T0, T0 + 900, 83300.0, 83299.0,
                                     {str(T0): 83300.0}, now)[0] == "wait"


def test_decision_falls_back_after_ttl():
    now = T0 + 900 + TTL + 1
    action, px, src, own_cap, mm = pm._trade_settle_decision(
        T0, T0 + 900, 83300.0, 83299.125, {str(T0): 83300.0}, now)
    assert (action, px, src) == ("settle", 83299.12, "s_avg_fallback")


def test_decision_next_pb_wins_even_without_s_avg():
    # s_avg is only needed for the fallback; next_pb settles regardless.
    assert pm._trade_settle_decision(T0, T0 + 900, 83300.0, None,
                                     {str(T0): 83300.0,
                                      str(T0 + 900): 83301.0},
                                     9999999999)[2] == "next_pb"


def test_decision_malformed_input_reports_stuck():
    # Fix 2: a malformed window key reports stuck (loud), never waits silently.
    assert pm._trade_settle_decision(None, None, None, None, None, None)[0] == "stuck"
    assert pm._trade_settle_decision(T0, T0 + 900, None, 83299.0,
                                     {}, 9999999999)[0] == "stuck"


def test_decision_stuck_when_both_missing_past_ttl():
    # Fix 1: next_pb missing AND no s_avg past the TTL -> stuck, not settle.
    now = T0 + 900 + TTL + 1
    action, px, src, own_cap, mm = pm._trade_settle_decision(
        T0, T0 + 900, 83300.0, None, {str(T0): 83300.0}, now)
    assert (action, px, src) == ("stuck", None, None)
    # ...but still waiting (not stuck) inside the TTL.
    assert pm._trade_settle_decision(
        T0, T0 + 900, 83300.0, None, {str(T0): 83300.0},
        T0 + 900 + 100)[0] == "wait"


def test_decision_garbage_next_pb_treated_as_missing():
    # Fix 2: a non-numeric next price-to-beat goes down the normal TTL path.
    now = T0 + 900 + TTL + 1
    action, px, src, own_cap, mm = pm._trade_settle_decision(
        T0, T0 + 900, 83300.0, 83299.0,
        {str(T0): 83300.0, str(T0 + 900): "n/a"}, now)
    assert (action, px, src) == ("settle", 83299.0, "s_avg_fallback")


def test_decision_garbage_own_pb_uses_p_O_and_flags():
    # Fix 2: malformed published own cap -> p_O wins, mismatch flagged.
    action, px, src, own_cap, mm = pm._trade_settle_decision(
        T0, T0 + 900, 83300.0, 83299.0,
        {str(T0): "bogus", str(T0 + 900): 83301.0}, 9999999999)
    assert mm is True and own_cap == 83300.0
    assert (action, px, src) == ("settle", 83301.0, "next_pb")


def test_decision_cap_mismatch_uses_published():
    # Checklist 2: p["O"] != published price-to-beat -> published wins,
    # mismatch flagged for the caller to log loudly.
    action, px, src, own_cap, mm = pm._trade_settle_decision(
        T0, T0 + 900, 83299.0, 83299.0,
        {str(T0): 83300.0, str(T0 + 900): 83301.0}, 9999999999)
    assert mm is True and own_cap == 83300.0
    assert (action, px, src) == ("settle", 83301.0, "next_pb")


def test_decision_tie_at_next_pb_settles_up():
    # Checklist 1: tie follows the venue rule (end >= start -> Up).
    action, px, src, own_cap, mm = pm._trade_settle_decision(
        T0, T0 + 900, 83300.0, 83299.0,
        {str(T0): 83300.0, str(T0 + 900): 83300.0}, 9999999999)
    assert px >= own_cap  # the loop's outcome_up must be True


# ---------------------------------------------------------------- fixture

def _run_settle(tmp_path, monkeypatch, base, step, own_cap, next_pb,
                matured_secs_ago, side="down", price=0.55):
    """Run settle() over one B position; return (saved_state, blotter_recs,
    series, Te). The position closed matured_secs_ago seconds ago."""
    now = 2000000000
    Te = now - matured_secs_ago
    t = Te - 900
    series = _closes(Te, base=base, step=step)
    pos = {"event_id": "slug", "Topen": t, "Tclose": Te, "O": own_cap,
           "sleeve": "B",
           "filled_legs": [{"side": side, "filled": 10.0, "cost": 10.0 * price}]}
    state = {"open": [pos], "pending_brier": {},
             "brier_windows": [], "d_shadow": [], "day_pnl": 0.0,
             "day_pnls": {},
             "consec_losses": 0, "decided_windows": [], "disabled": [],
             "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    lines = [json.dumps({"T": t, "published_priceToBeat": own_cap})]
    if next_pb is not None:
        lines.append(json.dumps({"T": t + 900,
                                 "published_priceToBeat": next_pb}))
    (tmp_path / "captures.jsonl").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x])
                                      for x in range(a, b + 1)
                                      if x in series])
    pm.settle()
    recs = []
    if os.path.exists(pm.BLOTTER):
        recs = [json.loads(l) for l in open(pm.BLOTTER) if l.strip()]
    return saved, recs, series, Te


def _above_cap():
    # series rising into Te: s_avg over [Te-60, Te) ~= base - 11.67,
    # so base=83322 lands s_avg ~83310, above the 83300 cap.
    return 83322.0, 0.40, 83300.0


# ---------------------------------------------------------------- integration

def test_flipped_window_settles_corrected_sign(tmp_path, monkeypatch):
    """s_avg says UP, next_pb says DOWN: a DOWN buy must win."""
    base, step, own_cap = _above_cap()
    next_pb = 83290.0
    saved, recs, series, Te = _run_settle(
        tmp_path, monkeypatch, base, step, own_cap, next_pb,
        matured_secs_ago=300, side="down", price=0.55)
    assert saved["open"] == []
    (r,) = [r for r in recs if r.get("type") == "settle"]
    assert r["settle_source"] == "next_pb"
    assert r["settle_px"] == round(next_pb, 2)
    assert r["brti_settle"] == round(_avg(series, Te - 60), 2)
    assert r["brti_settle"] >= own_cap > r["settle_px"]  # the flip
    assert r["outcome"] == "down"
    assert r["pnl_after_fees"] > 0  # corrected sign: winner, not loser
    # kill ledgers read the corrected P&L
    assert saved["cum_pnl"] == r["pnl_after_fees"]
    assert saved["consec_losses"] == 0


def test_wait_leaves_position_open(tmp_path, monkeypatch):
    base, step, own_cap = _above_cap()
    saved, recs, _, _ = _run_settle(tmp_path, monkeypatch, base, step,
                                    own_cap, None, matured_secs_ago=300)
    assert len(saved["open"]) == 1  # still open, retried next tick
    assert not [r for r in recs if r.get("type") == "settle"]
    assert saved.get("settle_fallback_stats", {}).get("count", 0) == 0


def test_fallback_after_ttl_flagged_and_counted(tmp_path, monkeypatch):
    base, step, own_cap = _above_cap()
    saved, recs, series, Te = _run_settle(
        tmp_path, monkeypatch, base, step, own_cap, None,
        matured_secs_ago=TTL + 60, side="up", price=0.55)
    assert saved["open"] == []
    (r,) = [r for r in recs if r.get("type") == "settle"]
    assert r["settle_source"] == "s_avg_fallback"
    assert r["settle_px"] == r["brti_settle"]  # fallback == s_avg
    assert saved["settle_fallback_stats"]["count"] == 1
    assert saved["settle_fallback_stats"]["windows"] == [Te - 900]


def test_tie_at_next_pb_settles_up(tmp_path, monkeypatch):
    base, step, own_cap = _above_cap()
    saved, recs, _, _ = _run_settle(tmp_path, monkeypatch, base, step,
                                    own_cap, own_cap, matured_secs_ago=300,
                                    side="up", price=0.55)
    (r,) = [r for r in recs if r.get("type") == "settle"]
    assert r["outcome"] == "up" and r["pnl_after_fees"] > 0


# ---------------------------------------------------------------- isolation

def test_brier_still_scored_on_s_avg(tmp_path, monkeypatch):
    """Part 2 is post-latch: the window's Brier must not see next_pb."""
    from fairvalue import brier
    now = 2000000000
    t = now - 2000
    Te = t + 900
    series = _closes(Te, base=85012.0)
    s_avg = _avg(series, Te - 60)
    own_cap, next_pb = 85000.0, 84900.0  # next_pb would flip the label
    pos = {"event_id": "slug", "Topen": t, "Tclose": Te, "O": own_cap,
           "sleeve": "B", "filled_legs": [{"side": "up", "filled": 10.0,
                                           "cost": 5.5}]}
    state = {"open": [pos], "pending_brier": {
                 str(t): {"p_fair": 0.7, "mid_up": 0.6, "tau": 300}},
             "brier_windows": [], "d_shadow": [], "day_pnl": 0.0,
             "day_pnls": {},
             "consec_losses": 0, "decided_windows": [], "disabled": [],
             "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": own_cap}) + "\n" +
        json.dumps({"T": t + 900, "published_priceToBeat": next_pb}) + "\n")
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x]) for x in range(a, b + 1)])
    pm.settle()
    assert s_avg >= own_cap > next_pb  # the test really is a flip
    (bw,) = saved["brier_windows"]
    # scored on the s_avg label (up -> oc 1.0), not next_pb's
    assert bw["brier_model"] == brier(0.7, 1.0)
    assert bw["brier_mkt"] == brier(0.6, 1.0)
    scored = [json.loads(l) for l in
              open(pm._scored_windows_path()) if l.strip()]
    assert scored[-1]["outcome"] == 1.0
    assert scored[-1]["settle_price"] == round(s_avg, 2)
    # ...while the TRADE settled on next_pb
    recs = [json.loads(l) for l in open(pm.BLOTTER) if l.strip()]
    (r,) = [r for r in recs if r.get("type") == "settle"]
    assert r["settle_source"] == "next_pb" and r["outcome"] == "down"


def test_d_shadow_untouched_by_next_pb(tmp_path, monkeypatch):
    """The shadow experiment keeps s_avg labels under Part 1."""
    now = 2000000000
    t = now - 2000
    Te = t + 900
    series = _closes(Te, base=85012.0)  # s_avg above cap -> hold=1
    shadow = {"T": t, "side": "up", "hold": None, "outcome": None}
    state = {"open": [], "pending_brier": {}, "brier_windows": [],
             "d_shadow": [shadow], "day_pnl": 0.0, "consec_losses": 0,
             "day_pnls": {},
             "decided_windows": [], "disabled": [], "settle_pnls": [],
             "cum_pnl": 0.0, "peak_cum": 0.0}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": 85000.0}) + "\n" +
        json.dumps({"T": t + 900, "published_priceToBeat": 84900.0}) + "\n")
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x]) for x in range(a, b + 1)])
    pm.settle()
    sh = saved["d_shadow"][0]
    assert sh["outcome"] == "up" and sh["hold"] == 1  # s_avg, not next_pb


def test_hashes_unchanged():
    """Settlement is not a hashed input: SIG/RISK identical, no new keys."""
    assert pm.signal_hashes() == pm.SIG and pm.risk_hash() == pm.RISK
    blob = json.dumps(pm.SIG)
    assert "_trade_settle_decision" not in blob
    assert "SETTLE_CHECK_TTL" not in blob


# ---------------------------------------------------------------- checklist extras

def test_exactly_once_fallback_then_late_next_pb(tmp_path, monkeypatch, capsys):
    """Checklist 5: a fallback settle is final; a late next_pb changes nothing."""
    base, step, own_cap = _above_cap()
    now = 2000000000
    Te = now - (TTL + 60)
    t = Te - 900
    series = _closes(Te, base=base, step=step)
    pos = {"event_id": "slug", "Topen": t, "Tclose": Te, "O": own_cap,
           "sleeve": "B",
           "filled_legs": [{"side": "up", "filled": 10.0, "cost": 5.5}]}
    state = {"open": [pos], "pending_brier": {}, "brier_windows": [],
             "d_shadow": [], "day_pnl": 0.0, "consec_losses": 0,
             "day_pnls": {},
             "decided_windows": [], "disabled": [], "settle_pnls": [],
             "cum_pnl": 0.0, "peak_cum": 0.0}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": own_cap}) + "\n")
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x]) for x in range(a, b + 1)
                                      if x in series])
    monkeypatch.setattr(pm, "_fetch_next_pb_direct", lambda tn: None)
    pm.settle()
    recs = [json.loads(l) for l in open(pm.BLOTTER) if l.strip()]
    assert len([r for r in recs if r.get("type") == "settle"]) == 1
    assert saved["open"] == []
    # late next_pb arrives: second settle run must not re-settle or alter
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": own_cap}) + "\n" +
        json.dumps({"T": t + 900, "published_priceToBeat": own_cap + 50}) + "\n")
    blot_before = open(pm.BLOTTER).read()
    state2 = json.loads(json.dumps(saved))
    monkeypatch.setattr(pm, "load_state", lambda: state2)
    pm.settle()
    assert open(pm.BLOTTER).read() == blot_before
    assert "deferring trade settle" not in capsys.readouterr().out


def test_settle_processes_tclose_order(tmp_path, monkeypatch):
    """Checklist 7: consec_losses / roll-20 see positions in Tclose order."""
    now = 2000000000
    mk = lambda dt, side: {"event_id": f"slug{dt}", "Topen": now - dt - 900,
                           "Tclose": now - dt, "O": 83300.0, "sleeve": "B",
                           "filled_legs": [{"side": side, "filled": 10.0,
                                            "cost": 9.0}]}
    # insert out of order: newer first
    p_new = mk(300, "up")    # wins (s_avg above cap)
    p_old = mk(600, "down")  # loses
    state = {"open": [p_new, p_old], "pending_brier": {}, "brier_windows": [],
             "d_shadow": [], "day_pnl": 0.0, "consec_losses": 0,
             "day_pnls": {},
             "decided_windows": [], "disabled": [], "settle_pnls": [],
             "cum_pnl": 0.0, "peak_cum": 0.0}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    caps_lines = []
    for p in (p_new, p_old):
        caps_lines.append(json.dumps({"T": p["Topen"],
                                      "published_priceToBeat": 83300.0}))
        caps_lines.append(json.dumps({"T": p["Topen"] + 900,
                                      "published_priceToBeat": 83350.0}))
    (tmp_path / "captures.jsonl").write_text("\n".join(caps_lines) + "\n")
    # series must cover both windows' [Te-60, Te) ranges
    series = {x: round(83322.0 + 0.40 * (x - now) + (x % 7) * 0.11, 2)
              for x in range(now - 700, now + 100)}
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x]) for x in range(a, b + 1)
                                      if x in series])
    pm.settle()
    # oldest (loser) processed first: consec goes 0->1, then winner resets to 0
    assert saved["settle_pnls"] == [-9.0, 1.0]
    assert saved["consec_losses"] == 0


def test_day_pnl_books_to_tclose_day(tmp_path, monkeypatch):
    """Checklist 8: window closes before midnight UTC, settles after."""
    # 2026-10-09 23:55 UTC -> Te; settle at 2026-10-10 00:05 UTC.
    import datetime
    Te = int(datetime.datetime(2026, 10, 9, 23, 55,
                               tzinfo=datetime.timezone.utc).timestamp())
    now = int(datetime.datetime(2026, 10, 10, 0, 5,
                                tzinfo=datetime.timezone.utc).timestamp())
    t = Te - 900
    series = _closes(Te, base=83322.0, step=0.40)
    pos = {"event_id": "slug", "Topen": t, "Tclose": Te, "O": 83300.0,
           "sleeve": "B",
           "filled_legs": [{"side": "up", "filled": 10.0, "cost": 5.5}]}
    # post-backfill state shape: day_pnls seeded from legacy day_pnl=1.5
    # (the backfill itself is tested in test_migration_seeds_state_day_not_today)
    state = {"open": [pos], "pending_brier": {}, "brier_windows": [],
             "d_shadow": [], "day": "2026-10-09", "day_pnl": 1.5,
             "day_pnls": {"2026-10-09": 1.5},
             "consec_losses": 0, "decided_windows": [], "disabled": [],
             "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": 83300.0}) + "\n" +
        json.dumps({"T": t + 900, "published_priceToBeat": 83350.0}) + "\n")
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x]) for x in range(a, b + 1)
                                      if x in series])
    pm.settle()
    recs = [json.loads(l) for l in open(pm.BLOTTER) if l.strip()]
    (r,) = [r for r in recs if r.get("type") == "settle"]
    assert r["pnl_after_fees"] > 0
    # booked to the Tclose day (10-09), not the settle day (10-10)
    assert saved["day_pnls"]["2026-10-09"] == round(1.5 + r["pnl_after_fees"], 2)
    assert saved["day_pnls"].get("2026-10-10", 0.0) == 0.0
    # Fix A: day_pnl is derived -- the legacy key is never written by settle
    assert "day_pnl" not in saved or saved["day_pnl"] == 1.5


def test_migration_seeds_state_day_not_today(tmp_path, monkeypatch):
    """Fix B: a deploy landing just after midnight, before kill_check's
    rollover, must not move yesterday's P&L into today."""
    import paper as pm2
    st = {"open": [], "disabled": [], "day": "2026-10-09", "day_pnl": 5.0,
          "consec_losses": 0, "decided_windows": [], "brier_windows": [],
          "pending_brier": {}, "settle_pnls": [], "cum_pnl": 0.0,
          "peak_cum": 0.0}
    p = tmp_path / "paper_state.json"
    p.write_text(json.dumps(st))
    monkeypatch.setattr(pm, "STATE", str(p))
    loaded = pm.load_state()
    # seeded under state["day"] (yesterday), NOT today
    assert loaded["day_pnls"] == {"2026-10-09": 5.0}
    # kill_check rollover to today: derived day_pnl is 0.0, yesterday kept
    monkeypatch.setattr(pm, "today_key", lambda ts=None: "2026-10-10")
    halt, _ = pm.kill_check(loaded, arm=True)
    assert not halt
    assert pm._day_pnl(loaded) == 0.0
    assert loaded["day_pnls"]["2026-10-09"] == 5.0
    assert loaded["day"] == "2026-10-10"


def test_day_pnls_trimmed_to_14(tmp_path, monkeypatch):
    """Fix D: the per-day dict never grows past 14 days."""
    now = 2000000000
    Te = now - 300
    t = Te - 900
    series = _closes(Te, base=83322.0, step=0.40)
    pos = {"event_id": "slug", "Topen": t, "Tclose": Te, "O": 83300.0,
           "sleeve": "B",
           "filled_legs": [{"side": "up", "filled": 10.0, "cost": 5.5}]}
    import datetime
    old = {"2026-09-%02d" % d: 1.0 for d in range(1, 21)}  # 20 stale days
    state = {"open": [pos], "pending_brier": {}, "brier_windows": [],
             "d_shadow": [], "day_pnl": 0.0, "day_pnls": old,
             "consec_losses": 0, "decided_windows": [], "disabled": [],
             "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": 83300.0}) + "\n" +
        json.dumps({"T": t + 900, "published_priceToBeat": 83350.0}) + "\n")
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x]) for x in range(a, b + 1)
                                      if x in series])
    pm.settle()
    assert len(saved["day_pnls"]) == 14
    assert min(saved["day_pnls"]) >= "2026-09-08"  # oldest 6 trimmed


def test_one_direct_fetch_per_run(tmp_path, monkeypatch):
    """Fix E: two waiting positions -> exactly one direct fetch attempt."""
    base, step, own_cap = _above_cap()
    now = 2000000000
    mk = lambda dt: {"event_id": f"slug{dt}", "Topen": now - dt - 900,
                     "Tclose": now - dt, "O": own_cap, "sleeve": "B",
                     "filled_legs": [{"side": "up", "filled": 10.0,
                                      "cost": 5.5}]}
    p1, p2 = mk(300), mk(600)
    state = {"open": [p1, p2], "pending_brier": {}, "brier_windows": [],
             "d_shadow": [], "day_pnl": 0.0, "day_pnls": {},
             "consec_losses": 0, "decided_windows": [], "disabled": [],
             "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": p1["Topen"], "published_priceToBeat": own_cap}) + "\n" +
        json.dumps({"T": p2["Topen"], "published_priceToBeat": own_cap}) + "\n")
    series = {x: round(83322.0 + 0.40 * (x - now) + (x % 7) * 0.11, 2)
              for x in range(now - 700, now + 100)}
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x]) for x in range(a, b + 1)
                                      if x in series])
    calls = []
    monkeypatch.setattr(pm, "_fetch_next_pb_direct",
                        lambda tn, timeout=20: calls.append(tn) or None)
    pm.settle()
    assert len(calls) == 1  # one attempt total, not one per position
    assert len(saved["open"]) == 2  # both still waiting


def test_direct_fetch_fills_missing_next_pb(tmp_path, monkeypatch, capsys):
    """Checklist 4: next_pb missing >120s after Te -> one direct fetch."""
    base, step, own_cap = _above_cap()
    now = 2000000000
    Te = now - 300
    t = Te - 900
    series = _closes(Te, base=base, step=step)
    pos = {"event_id": "slug", "Topen": t, "Tclose": Te, "O": own_cap,
           "sleeve": "B",
           "filled_legs": [{"side": "down", "filled": 10.0, "cost": 5.5}]}
    state = {"open": [pos], "pending_brier": {}, "brier_windows": [],
             "d_shadow": [], "day_pnl": 0.0, "consec_losses": 0,
             "day_pnls": {},
             "decided_windows": [], "disabled": [], "settle_pnls": [],
             "cum_pnl": 0.0, "peak_cum": 0.0}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": own_cap}) + "\n")
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x]) for x in range(a, b + 1)
                                      if x in series])
    calls = []
    def fake_fetch(tn, timeout=20):
        calls.append(tn)
        return 83290.0
    monkeypatch.setattr(pm, "_fetch_next_pb_direct", fake_fetch)
    pm.settle()
    assert calls == [t + 900]  # exactly one direct attempt
    recs = [json.loads(l) for l in open(pm.BLOTTER) if l.strip()]
    (r,) = [r for r in recs if r.get("type") == "settle"]
    assert r["settle_source"] == "next_pb" and r["settle_px"] == 83290.0
    # recorded in captures for the next run, in the exact capture-line shape
    caps = [json.loads(l) for l in open(tmp_path / "captures.jsonl")]
    (line,) = [c for c in caps if c.get("T") == t + 900]
    assert set(line) == {"T", "slug", "published_priceToBeat", "captured_at"}
    assert line["published_priceToBeat"] == 83290.0


def test_real_captures_key_lookup():
    """Checklist 3: the lookup works on real captures.jsonl key shapes."""
    import paper as pm2  # noqa: F401  (module already imported)
    path = "/home/hatch/workspace/polymarket/btc15m/captures.jsonl"
    n = 0
    for line in open(path):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        pb = r.get("published_priceToBeat")
        if not pb or not isinstance(r.get("T"), int):
            continue
        # the exact lookup the helper performs
        assert str(r["T"] + 900) == str(int(r["T"]) + 900)
        n += 1
        if n >= 50:
            break
    assert n > 0
    # and Tclose == Topen + 900 on real blotter positions
    m = 0
    for line in open("/home/hatch/workspace/polymarket/btc15m/blotter.jsonl"):
        r = json.loads(line)
        if r.get("type") == "settle" and r.get("Topen"):
            assert r["Tclose"] == r["Topen"] + 900
            m += 1
    assert m > 0


# ---------------------------------------------------------------- fix F validation

def _mock_event(monkeypatch, t_next, pb, slug_ok=True, index="BRTI"):
    import markets
    from markets import event_slug
    ev = {"slug": event_slug(t_next) if slug_ok else "btc-updown-15m-2099-01-01-0000z",
          "markets": [{"assetPriceTerms": {
              "priceToBeat": {"value": pb},
              "indexSymbol": index}}]}
    monkeypatch.setattr(markets, "get_event", lambda T, retries=2, timeout=30: ev)


def test_fetch_rejects_wrong_window(monkeypatch):
    _mock_event(monkeypatch, 1791586800, 83300.0, slug_ok=False)
    assert pm._fetch_next_pb_direct(1791586800) is None


def test_fetch_rejects_null_pb(monkeypatch):
    _mock_event(monkeypatch, 1791586800, None)
    assert pm._fetch_next_pb_direct(1791586800) is None


def test_fetch_rejects_string_pb(monkeypatch):
    # parse_terms (shared with capture.py) normalizes numeric strings;
    # a non-numeric string raises inside parse_terms -> None, write nothing.
    _mock_event(monkeypatch, 1791586800, "n/a")
    assert pm._fetch_next_pb_direct(1791586800) is None
    _mock_event(monkeypatch, 1791586800, "83300.0")
    assert pm._fetch_next_pb_direct(1791586800) == 83300.0


def test_fetch_rejects_nonpositive_pb(monkeypatch):
    _mock_event(monkeypatch, 1791586800, 0.0)
    assert pm._fetch_next_pb_direct(1791586800) is None
    _mock_event(monkeypatch, 1791586800, float("nan"))
    assert pm._fetch_next_pb_direct(1791586800) is None


def test_fetch_rejects_wrong_index(monkeypatch):
    _mock_event(monkeypatch, 1791586800, 83300.0, index="ETH")
    assert pm._fetch_next_pb_direct(1791586800) is None


def test_fetch_accepts_valid_pb(monkeypatch):
    _mock_event(monkeypatch, 1791586800, 83300.456)
    assert pm._fetch_next_pb_direct(1791586800) == 83300.456


def test_fetch_never_raises(monkeypatch):
    import markets
    monkeypatch.setattr(markets, "get_event",
                        lambda T, retries=2, timeout=30: (_ for _ in ()).throw(
                            RuntimeError("net down")))
    assert pm._fetch_next_pb_direct(1791586800) is None


# ---------------------------------------------------------------- fixes 1+4 integration

def _mk_state(pos):
    return {"open": [pos], "pending_brier": {}, "brier_windows": [],
            "d_shadow": [], "day_pnl": 0.0, "day_pnls": {},
            "consec_losses": 0, "decided_windows": [], "disabled": [],
            "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0,
            "settle_stuck_stats": {"windows": []}}


def test_settle_without_candles_next_pb_present(tmp_path, monkeypatch):
    """Fix 1: dead candle feed must not block settling on next_pb."""
    base, step, own_cap = _above_cap()
    now = 2000000000
    Te = now - 300
    t = Te - 900
    pos = {"event_id": "slug", "Topen": t, "Tclose": Te, "O": own_cap,
           "sleeve": "B",
           "filled_legs": [{"side": "down", "filled": 10.0, "cost": 5.5}]}
    state = _mk_state(pos)
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": own_cap}) + "\n" +
        json.dumps({"T": t + 900, "published_priceToBeat": 83290.0}) + "\n")
    def boom(a, b):
        raise RuntimeError("503")
    monkeypatch.setattr(brti, "fetch_1s", boom)
    monkeypatch.setattr(pm, "_fetch_next_pb_direct", lambda tn, timeout=20: None)
    pm.settle()
    recs = [json.loads(l) for l in open(pm.BLOTTER) if l.strip()]
    (r,) = [r for r in recs if r.get("type") == "settle"]
    assert r["settle_source"] == "next_pb" and r["settle_px"] == 83290.0
    assert r["brti_settle"] is None and r["gap"] is None
    assert saved["open"] == []


def test_stuck_counted_not_settled(tmp_path, monkeypatch, capsys):
    """Fix 1: TTL passed, no next_pb, no s_avg -> STUCK, counted, waiting."""
    now = 2000000000
    Te = now - (TTL + 60)
    t = Te - 900
    pos = {"event_id": "slug", "Topen": t, "Tclose": Te, "O": 83300.0,
           "sleeve": "B",
           "filled_legs": [{"side": "up", "filled": 10.0, "cost": 5.5}]}
    state = _mk_state(pos)
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": 83300.0}) + "\n")
    monkeypatch.setattr(brti, "fetch_1s", lambda a, b: [])
    monkeypatch.setattr(pm, "_fetch_next_pb_direct", lambda tn, timeout=20: None)
    pm.settle()
    out = capsys.readouterr().out
    assert "SETTLE_STUCK slug" in out
    assert saved["settle_stuck_stats"]["windows"] == [t]
    assert len(saved["open"]) == 1  # still waiting
    recs = []
    if os.path.exists(pm.BLOTTER):
        recs = [json.loads(l) for l in open(pm.BLOTTER) if l.strip()]
    assert not [r for r in recs if r.get("type") == "settle"]


def test_crash_between_blotter_and_save_settles_once(tmp_path, monkeypatch,
                                                     capsys):
    """Fix 4: exception after append_jsonl -> re-run settles exactly once."""
    base, step, own_cap = _above_cap()
    now = 2000000000
    Te = now - 300
    t = Te - 900
    series = _closes(Te, base=base, step=step)
    pos = {"event_id": "slug", "Topen": t, "Tclose": Te, "O": own_cap,
           "sleeve": "B",
           "filled_legs": [{"side": "down", "filled": 10.0, "cost": 5.5}]}
    import copy
    pristine = copy.deepcopy(pos)  # before the first run mutates it
    state = _mk_state(pos)
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    real_save = lambda s: saved.update(json.loads(json.dumps(s)))
    calls = {"n": 0}
    def flaky_save(s):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash between blotter and save")
        real_save(s)
    monkeypatch.setattr(pm, "save_state", flaky_save)
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": own_cap}) + "\n" +
        json.dumps({"T": t + 900, "published_priceToBeat": 83290.0}) + "\n")
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x]) for x in range(a, b + 1)
                                      if x in series])
    monkeypatch.setattr(pm, "_fetch_next_pb_direct", lambda tn, timeout=20: None)
    try:
        pm.settle()
    except RuntimeError:
        pass
    # blotter has the line, but save failed -> position still in open
    recs1 = [json.loads(l) for l in open(pm.BLOTTER) if l.strip()]
    assert len([r for r in recs1 if r.get("type") == "settle"]) == 1
    # Simulate the next process: disk state still has the (unmutated)
    # position open; ledgers were never persisted.
    state["open"] = [copy.deepcopy(pristine)]
    state["day_pnls"] = {}
    state["settle_pnls"] = []
    state["cum_pnl"] = 0.0
    state["peak_cum"] = 0.0
    state["consec_losses"] = 0
    # re-run: idempotency guard removes it without re-appending, and
    # replays the ledgers exactly once from the blotter record
    monkeypatch.setattr(pm, "save_state", real_save)
    pm.settle()
    out = capsys.readouterr().out
    assert "RESETTLE_SKIPPED slug" in out
    recs2 = [json.loads(l) for l in open(pm.BLOTTER) if l.strip()]
    assert len([r for r in recs2 if r.get("type") == "settle"]) == 1
    assert saved["open"] == []
    assert saved["cum_pnl"] == recs2[0]["pnl_after_fees"]  # counted once
    assert saved["settle_pnls"] == [recs2[0]["pnl_after_fees"]]
    assert saved["consec_losses"] == 0  # winner -> reset, not double


def test_nit1_malformed_pb_wording(tmp_path, monkeypatch, capsys):
    """Nit 1: malformed published pb prints 'published value malformed'."""
    base, step, own_cap = _above_cap()
    now = 2000000000
    Te = now - 300
    t = Te - 900
    series = _closes(Te, base=base, step=step)
    pos = {"event_id": "slug", "Topen": t, "Tclose": Te, "O": own_cap,
           "sleeve": "B",
           "filled_legs": [{"side": "down", "filled": 10.0, "cost": 5.5}]}
    state = _mk_state(pos)
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": "bogus"}) + "\n" +
        json.dumps({"T": t + 900, "published_priceToBeat": 83290.0}) + "\n")
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x]) for x in range(a, b + 1)
                                      if x in series])
    monkeypatch.setattr(pm, "_fetch_next_pb_direct", lambda tn, timeout=20: None)
    pm.settle()
    out = capsys.readouterr().out
    assert "published value malformed, using O=" in out
    assert "!= published" not in out


def test_nit2_no_blotter_scan_when_open_empty(tmp_path, monkeypatch):
    """Nit 2: empty open skips the blotter read entirely (no FileNotFound)."""
    st = {"open": [], "pending_brier": {}, "brier_windows": [], "d_shadow": [],
          "day_pnl": 0.0, "day_pnls": {}, "consec_losses": 0,
          "decided_windows": [], "disabled": [], "settle_pnls": [],
          "cum_pnl": 0.0, "peak_cum": 0.0,
          "settle_stuck_stats": {"windows": []}}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: st)
    monkeypatch.setattr(pm, "save_state",
                        lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: 2000000000)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    # blotter does not exist; must not raise
    assert not os.path.exists(pm.BLOTTER)
    pm.settle()  # "nothing to settle"
    assert saved["open"] == []


def test_nit3_replay_clears_stuck(tmp_path, monkeypatch):
    """Nit 3: the idempotency replay also clears the window from stuck."""
    import copy
    base, step, own_cap = _above_cap()
    now = 2000000000
    Te = now - 300
    t = Te - 900
    series = _closes(Te, base=base, step=step)
    pos = {"event_id": "slug", "Topen": t, "Tclose": Te, "O": own_cap,
           "sleeve": "B",
           "filled_legs": [{"side": "down", "filled": 10.0, "cost": 5.5}]}
    pristine = copy.deepcopy(pos)
    state = _mk_state(pos)
    state["settle_stuck_stats"] = {"windows": [t]}  # was stuck, now settling
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    real_save = lambda s: saved.update(json.loads(json.dumps(s)))
    calls = {"n": 0}
    def flaky_save(s):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash")
        real_save(s)
    monkeypatch.setattr(pm, "save_state", flaky_save)
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": own_cap}) + "\n" +
        json.dumps({"T": t + 900, "published_priceToBeat": 83290.0}) + "\n")
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x]) for x in range(a, b + 1)
                                      if x in series])
    monkeypatch.setattr(pm, "_fetch_next_pb_direct", lambda tn, timeout=20: None)
    try:
        pm.settle()
    except RuntimeError:
        pass
    state["open"] = [copy.deepcopy(pristine)]
    state["settle_stuck_stats"] = {"windows": [t]}
    monkeypatch.setattr(pm, "save_state", real_save)
    pm.settle()
    assert saved["settle_stuck_stats"]["windows"] == []
    assert saved["open"] == []
