"""Settlement forensics (2026-10-09): capture hook + research script.

No network: brti/markets calls are stubbed. Every write is under tmp_path.
"""
import json
import os

import brti
import paper as pm
import research.settle_forensics as sf

T0 = 1791491400


def _closes(Te, base=83300.0, step=0.37, lo=-200, hi=100):
    """Deterministic, non-flat 1s series around Te (so offsets differ)."""
    return {x: round(base + step * (x - Te) + (x % 7) * 0.11, 2)
            for x in range(Te + lo, Te + hi)}


def _avg(series, start):
    return sum(series[x] for x in range(start, start + 60)) / 60.0


def _read(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path)]


# ---------------------------------------------------------------- hook

def test_capture_hook_records_original_closes(tmp_path, monkeypatch):
    Te = T0 + 900
    series = _closes(Te)
    candles = [(x, series[x]) for x in range(Te - 61, Te + 1)]
    pm._forensics_capture("trade", T0, candles, 83300.12, Te + 5)
    (rec,) = _read(pm._forensics_capture_path())
    assert rec["type"] == "capture" and rec["path"] == "trade"
    assert rec["T"] == T0 and rec["Te"] == Te
    assert rec["closes"] == [series[x] for x in range(Te - 60, Te)]
    assert rec["fetch_delay_lo_s"] == 5
    assert rec["fetch_delay_hi_s"] >= rec["fetch_delay_lo_s"]
    assert rec["s_avg"] == 83300.12


def test_capture_hook_kill_switch_none_and_garbage(tmp_path, monkeypatch):
    path = pm._forensics_capture_path()
    pm._forensics_capture("brier", T0, [], None, 1)          # s_avg None
    pm._forensics_capture("brier", "x", None, 1.0, None)     # garbage
    pm._forensics_capture(None, None, None, None, None)
    monkeypatch.setenv("BTC_FORENSICS", "off")
    pm._forensics_capture("brier", T0, [(T0 + 840, 1.0)], 1.0, T0 + 905)
    assert _read(path) == []


def test_capture_hook_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(pm, "FORENSICS_CAPTURE_MAX_BYTES", 10)
    path = pm._forensics_capture_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write("x" * 11 + "\n")
    pm._forensics_capture("brier", T0, [(T0 + 840, 1.0)], 1.0, T0 + 905)
    assert open(path).read() == "x" * 11 + "\n"


def _settle_fixture(tmp_path, monkeypatch, forensics_on):
    """Run settle() over one Brier window, one D-shadow and one trade."""
    now = 2000000000
    t = now - 2000
    Te = t + 900
    series = _closes(Te, base=85000.0)
    pos = {"event_id": "slug", "Topen": t, "Tclose": Te, "O": 85000.0,
           "sleeve": "B", "filled_legs": [{"side": "up", "filled": 10.0,
                                           "cost": 5.5}]}
    shadow = {"T": t, "side": "up", "hold": None, "outcome": None}
    state = {"open": [pos], "pending_brier": {
                 str(t): {"p_fair": 0.7, "mid_up": 0.6, "tau": 300}},
             "brier_windows": [], "d_shadow": [shadow], "day_pnl": 0.0,
             "day_pnls": {},
             "consec_losses": 0, "decided_windows": [], "disabled": [],
             "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0}
    saved = {}
    monkeypatch.setattr(pm, "load_state", lambda: state)
    monkeypatch.setattr(pm, "save_state", lambda s: saved.update(json.loads(json.dumps(s))))
    monkeypatch.setattr(pm, "now_s", lambda: now)
    monkeypatch.setattr(pm, "_kc_snapshot_write", lambda s: None)
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": t, "published_priceToBeat": 85000.0}) + "\n" +
        # Part 1 (2026-10-09): trades settle on the next window's
        # price-to-beat; without it the trade waits and the fixture
        # would exercise the deferral path instead of settlement.
        json.dumps({"T": t + 900, "published_priceToBeat": 85000.0}) + "\n")
    monkeypatch.setattr(brti, "fetch_1s",
                        lambda a, b: [(x, series[x]) for x in range(a, b + 1)])
    if not forensics_on:
        monkeypatch.setenv("BTC_FORENSICS", "off")
    pm.settle()
    blotter = open(pm.BLOTTER).read()
    scored = open(pm._scored_windows_path()).read()
    return saved, blotter, scored, t, series


def test_settle_outputs_identical_with_and_without_capture(tmp_path, monkeypatch):
    """The hook changes nothing settle produces: state, blotter, scored."""
    on = _settle_fixture(tmp_path, monkeypatch, True)
    # the fixture really exercised all three paths
    assert on[0]["open"] == [] and '"type": "settle"' in on[1]
    assert len(on[0]["brier_windows"]) == 1 and on[0]["d_shadow"][0]["hold"] is not None
    caps = _read(pm._forensics_capture_path())
    assert {c["path"] for c in caps} == {"brier", "d_shadow", "trade"}
    t, series = on[3], on[4]
    for c in caps:
        assert c["closes"] == [series[x] for x in range(t + 840, t + 900)]
        assert abs(c["s_avg"] - _avg(series, t + 840)) < 1e-9
    # second run, hook off, fresh files
    for f in (pm.BLOTTER, pm._scored_windows_path(), pm._forensics_capture_path()):
        os.remove(f)
    off = _settle_fixture(tmp_path, monkeypatch, False)
    assert not os.path.exists(pm._forensics_capture_path())
    strip = lambda s: {k: v for k, v in s.items() if k != "brier_windows"}
    assert strip(on[0]) == strip(off[0])
    assert [{k: v for k, v in w.items() if k != "ts"} for w in on[0]["brier_windows"]] == \
           [{k: v for k, v in w.items() if k != "ts"} for w in off[0]["brier_windows"]]
    assert on[1] == off[1] and on[2] == off[2]


def test_signal_and_risk_hashes_unaffected():
    """The hook adds no hashed parameter (hashes come from param dicts)."""
    assert pm.signal_hashes() == pm.SIG and pm.risk_hash() == pm.RISK
    assert "FORENSICS_CAPTURE_MAX_BYTES" not in json.dumps(pm.SIG)


# ---------------------------------------------------------------- pure math

def test_offset_scan_finds_shift_and_exact_runs():
    Te = T0 + 900
    series = _closes(Te)
    target = round(_avg(series, Te - 60 - 7), 2)    # venue averaged 7s early
    r = sf.offset_scan(series, Te, target)
    assert r["best_offset"] == -7 and r["best_residual"] < 0.005
    assert -7 in r["exact_offsets"]
    assert r["n_scanned"] == sf.OFF_HI - sf.OFF_LO + 1
    r0 = sf.offset_scan(series, Te, round(_avg(series, Te - 60), 2))
    assert r0["best_offset"] == 0 and r0["residual_at_0"] < 0.005


def test_offset_scan_partial_data():
    assert sf.offset_scan({}, T0 + 900, 1.0) == {"n_scanned": 0}


def test_binom_sf():
    assert sf.binom_sf(0, 10, 0.1) == 1.0
    assert abs(sf.binom_sf(10, 10, 0.5) - 0.5 ** 10) < 1e-12
    assert sf.binom_sf(1, 10, 0.0) == 0.0


def _window(T, s_avg, truth, exact, best, O=83300.0, changed=0, refetch_gap=0.0,
            next_pb=None):
    return {"type": "window", "pass": "p60m", "T": T, "delay_s": 3600,
            "s_avg": s_avg, "truth": truth, "O": O, "venue_settle": truth,
            "next_pb": next_pb if next_pb is not None else truth,
            "gap_venue_vs_next_pb": 0.0 if next_pb is None else truth - next_pb,
            "mismatch": abs(s_avg - truth) > sf.TOL,
            "flip": (round(s_avg, 2) >= O) != (round(truth, 2) >= O),
            "captures": [{"s_avg": s_avg}], "capture_spread": 0.0,
            "refetch": {"avg0": s_avg + refetch_gap, "changed_seconds": changed,
                        "gap_refetch_vs_s_avg": refetch_gap,
                        "gap_refetch_vs_truth": s_avg + refetch_gap - truth,
                        "gap_filled_share": 0.0},
            "offsets": {"n_scanned": 181, "best_offset": best,
                        "exact_offsets": exact}}


def test_summarize_stable_offset_is_timing():
    rows = [_window(T0 + 900 * i, 83310.0, 83300.0, [-4, -3, -2], -3)
            for i in range(12)]
    s = sf.summarize(rows)
    assert s["mismatched"] == 12 and s["offset_k_star"] in (-4, -3, -2)
    assert s["offset_support"] == 12 and s["offset_p_adj"] < 0.01
    assert s["verdict"] == "timing"


def test_summarize_scattered_offsets_not_timing():
    rows = [_window(T0 + 900 * i, 83310.0, 83300.0, [(-110 + 17 * i) % 170 - 110],
                    (-110 + 17 * i) % 170 - 110) for i in range(12)]
    s = sf.summarize(rows)
    assert s["offset_support"] <= 2
    assert s["verdict"] == "source"


def test_summarize_revision_and_premise_and_flips():
    rows = [_window(T0 + 900 * i, 83310.0, 83300.0, [0], 0, changed=3,
                    refetch_gap=-10.0) for i in range(12)]
    rows[0] = _window(T0, 83299.0, 83301.0, [0], 0, O=83300.0, changed=3,
                      refetch_gap=2.0, next_pb=83305.0)
    s = sf.summarize(rows)
    assert s["revised"] == 12 and s["refetch_matches_truth"] == 12
    assert s["verdict"] == "revision"
    assert s["flips"] == 1
    assert s["venue_vs_next_pb_n"] == 12 and s["venue_vs_next_pb_agree"] == 11


def test_summarize_insufficient_and_no_gap():
    assert sf.summarize([])["verdict"] == "insufficient"
    rows = [_window(T0 + 900 * i, 83300.0, 83300.0, [0], 0) for i in range(12)]
    assert sf.summarize(rows)["verdict"] == "no_gap"


# ---------------------------------------------------------------- collection

def _seed_capture(base, T, series, s_avg=None, path="brier", delay=5):
    Te = T + 900
    p = sf.capture_path(str(base))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a") as f:
        f.write(json.dumps({"type": "capture", "path": path, "T": T, "Te": Te,
                            "s_avg": s_avg if s_avg is not None else _avg(series, Te - 60),
                            "closes": [series[x] for x in range(Te - 60, Te)],
                            "fetch_delay_lo_s": delay, "fetch_delay_hi_s": delay + 1}) + "\n")


def _stub_venue(monkeypatch, series_by_T, settle_by_T, pb_by_T, calls):
    def fetch(a, b, timeout=45, retries=2):
        calls.append(("fetch", a, b))
        for T, s in series_by_T.items():
            if T + 900 - 180 <= a <= T + 900:
                return [(x, s[x], 0 if x % 30 == 0 else 3) for x in range(a, b + 1) if x in s]
        return []
    monkeypatch.setattr(sf.brti, "fetch_1s_with_tickcount", fetch)

    def terms(T):
        calls.append(("terms", T))
        return {"priceToBeat": pb_by_T.get(T), "settlementPrice": settle_by_T.get(T)}
    monkeypatch.setattr(sf, "venue_terms", terms)


def test_collect_pass_schedule_and_three_numbers(tmp_path, monkeypatch):
    T = T0
    Te = T + 900
    series = _closes(Te)
    orig = dict(series)
    orig[Te - 5] = series[Te - 5] + 30.0           # original fetch was revised
    _seed_capture(tmp_path, T, orig)
    venue = round(_avg(series, Te - 60), 2)         # venue = refetched candles
    calls = []
    _stub_venue(monkeypatch, {T: series}, {T: venue}, {T: 83290.0, T + 900: venue + 0.01},
                calls)
    out = sf.out_path(str(tmp_path))
    assert sf.collect_windows(str(tmp_path), now=Te + 100) == 0      # not due yet
    assert sf.collect_windows(str(tmp_path), now=Te + 400) == 1      # p5m
    assert sf.collect_windows(str(tmp_path), now=Te + 500) == 0      # idempotent
    assert sf.collect_windows(str(tmp_path), now=Te + 3700) == 1     # p60m
    assert sf.collect_windows(str(tmp_path), now=Te + 9999) == 0
    r5, r60 = _read(out)
    assert (r5["pass"], r60["pass"]) == ("p5m", "p60m")
    # the three numbers
    assert abs(r60["s_avg"] - _avg(orig, Te - 60)) < 1e-3
    assert r60["venue_settle"] == venue and r60["next_pb"] == venue + 0.01
    assert r60["truth_src"] == "venue_settle"
    assert abs(r60["gap_venue_vs_next_pb"] + 0.01) < 1e-6
    # revision evidence: one changed second, refetch matches the venue
    assert r60["refetch"]["changed_seconds"] == 1
    assert abs(r60["refetch"]["max_close_change"] - 30.0) < 1e-6
    assert abs(r60["refetch"]["gap_refetch_vs_truth"]) < 0.005
    assert r60["offsets"]["best_offset"] == 0
    assert r60["refetch"]["gap_filled_share"] == round(2 / 60, 4)
    # fetch range covers the full offset scan
    fa, fb = calls[0][1], calls[0][2]
    assert fa == Te - 60 + sf.OFF_LO and fb == Te + sf.OFF_HI


def test_collect_late_first_run_does_p60m_once(tmp_path, monkeypatch):
    Te = T0 + 900
    series = _closes(Te)
    _seed_capture(tmp_path, T0, series)
    calls = []
    _stub_venue(monkeypatch, {T0: series}, {T0: 1.0}, {}, calls)
    assert sf.collect_windows(str(tmp_path), now=Te + 4000) == 1
    assert sf.collect_windows(str(tmp_path), now=Te + 5000) == 0
    (r,) = _read(sf.out_path(str(tmp_path)))
    assert r["pass"] == "p60m" and r["skipped_passes"] == ["p5m"]


def test_collect_stops_new_windows_at_target_and_bounds_run(tmp_path, monkeypatch):
    series_by_T = {}
    for i in range(5):
        T = T0 + 900 * i
        series_by_T[T] = _closes(T + 900)
        _seed_capture(tmp_path, T, series_by_T[T])
    calls = []
    _stub_venue(monkeypatch, series_by_T, {}, {}, calls)
    now = T0 + 900 * 6 + 3700
    # target=2: two windows complete at p60m, then no new window starts
    assert sf.collect_windows(str(tmp_path), now=now, target=2, max_windows=10) == 2
    assert sf.collect_windows(str(tmp_path), now=now + 60, target=2) == 0
    # raising the target (--target 90) resumes collection
    assert sf.collect_windows(str(tmp_path), now=now + 120, target=90) == 3
    # per-run bound
    other = tmp_path / "b"
    for i in range(5):
        _seed_capture(other, T0 + 900 * i, series_by_T[T0 + 900 * i])
    assert sf.collect_windows(str(other), now=now, max_windows=3) == 3


def test_collect_fetch_error_retries_then_stops(tmp_path, monkeypatch):
    Te = T0 + 900
    _seed_capture(tmp_path, T0, _closes(Te))

    def boom(*a, **k):
        raise RuntimeError("503")
    monkeypatch.setattr(sf.brti, "fetch_1s_with_tickcount", boom)
    for k in range(5):
        sf.collect_windows(str(tmp_path), now=Te + 400 + k)
    rows = _read(sf.out_path(str(tmp_path)))
    assert len(rows) == sf.MAX_ATTEMPTS and all("503" in r["error"] for r in rows)


def test_collect_ignores_malformed_capture_lines(tmp_path, monkeypatch):
    p = sf.capture_path(str(tmp_path))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write('not json\n{"type": "capture", "T": "x", "s_avg": 1}\n'
                       '{"type": "capture", "T": 5, "s_avg": "nan"}\n')
    _stub_venue(monkeypatch, {}, {}, {}, [])
    assert sf.collect_windows(str(tmp_path), now=T0) == 0


# ---------------------------------------------------------------- trades

def test_resolve_trades_against_venue(tmp_path, monkeypatch):
    rows = [
        {"type": "settle", "sleeve": "B", "Topen": T0, "event_id": "a", "O": 100.0,
         "brti_settle": 100.4, "outcome": "up", "pnl_after_fees": 4.5,
         "filled_legs": [{"side": "up", "filled": 10.0, "cost": 5.5}]},
        {"type": "settle", "sleeve": "B", "Topen": T0 + 900, "event_id": "b",
         "O": 100.0, "brti_settle": 101.0, "outcome": "up", "pnl_after_fees": 4.5,
         "filled_legs": [{"side": "up", "filled": 10.0, "cost": 5.5}]},
        {"type": "settle", "sleeve": "D1", "Topen": T0 + 1800, "O": 1.0,
         "filled_legs": []},
        {"type": "open", "sleeve": "B", "Topen": T0 + 2700},
    ]
    (tmp_path / "blotter.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    calls = []
    # venue: window a settled BELOW the line (flip), window b unpublished yet
    _stub_venue(monkeypatch, {}, {T0: 99.9}, {T0: 100.0}, calls)
    assert sf.resolve_trades(str(tmp_path), now=1) == 2
    a, b = _read(sf.out_path(str(tmp_path)))
    assert a["outcome_venue"] == "down" and a["outcome_match"] is False
    assert a["pnl_venue"] == -5.5 and a["pnl_delta"] == -10.0
    assert abs(a["gap"] - 0.5) < 1e-9
    assert b["venue_settle"] is None and "outcome_match" not in b
    # a resolved, b retried until MAX_ATTEMPTS, D1/open never touched
    for _ in range(5):
        sf.resolve_trades(str(tmp_path), now=2)
    out = _read(sf.out_path(str(tmp_path)))
    assert sum(r["T"] == T0 for r in out) == 1
    assert sum(r["T"] == T0 + 900 for r in out) == sf.MAX_ATTEMPTS
    assert all(c[1] in (T0, T0 + 900) for c in calls)
    s = sf.summarize(out)
    assert s["trades_resolved"] == 1 and s["trades_outcome_mismatch"] == 1


# ---------------------------------------------------------------- isolation

def test_script_writes_only_its_own_file(tmp_path, monkeypatch, capsys):
    Te = T0 + 900
    series = _closes(Te)
    _seed_capture(tmp_path, T0, series)
    (tmp_path / "captures.jsonl").write_text(json.dumps({"T": T0 + 900,
                                                         "published_priceToBeat": 1.0}) + "\n")
    (tmp_path / "blotter.jsonl").write_text("")
    (tmp_path / "paper_state.json").write_text("{}")
    _stub_venue(monkeypatch, {T0: series}, {T0: 1.0}, {}, [])
    monkeypatch.setattr(sf, "LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.setattr(sf.time, "time", lambda: Te + 4000)

    def snap():
        return {str(p.relative_to(tmp_path)): p.read_bytes()
                for p in tmp_path.rglob("*") if p.is_file() and p.name != "lock"}
    before = snap()
    assert sf.main(["--base", str(tmp_path)]) == 0
    after = snap()
    changed = {k for k in after if before.get(k) != after[k]}
    assert changed == {os.path.join("research", "settle_forensics.jsonl")}
    assert "VERDICT" in capsys.readouterr().out
    # --summary writes nothing
    before = snap()
    assert sf.main(["--summary", "--base", str(tmp_path)]) == 0
    assert snap() == before


def test_script_has_no_engine_mutators():
    src = open(sf.__file__).read()
    for bad in ("save_state", "_atomic_json_write", "BLOTTER", "import paper",
                "from paper", "run_once(", "pm.settle(", "paper.settle("):
        assert bad not in src, bad


# ---------------------------------------------------------------- chatbox msg 34

def test_extract_resolution_variants():
    assert sf.extract_resolution({"winningOutcome": "Up"})[0] == "up"
    assert sf.extract_resolution({"result": {"side": "DOWN"}})[0] == "down"
    assert sf.extract_resolution({"outcomes": [{"name": "Up", "winner": False},
                                               {"name": "Down", "winner": True}]})[0] == "down"
    # ambiguous / irrelevant values never guess
    assert sf.extract_resolution({"resolution": "upcoming"})[0] is None
    assert sf.extract_resolution({"outcome": "up or down"})[0] is None
    assert sf.extract_resolution({}) == (None, None)
    assert sf.extract_resolution(None) == (None, None)


def test_resolve_windows_first(tmp_path, monkeypatch):
    now = T0 + 20 * 900
    sw = [  # our outcome up (100.4 >= 100), next pb says down, venue says down
        {"T": T0, "O": 100.0, "settle_price": 100.4, "scored_at": T0 + 905},
        # agree everywhere
        {"T": T0 + 900, "O": 100.0, "settle_price": 99.0, "scored_at": T0 + 1800 + 240},
        # venue silent -> retried, capped
        {"T": T0 + 1800, "O": 100.0, "settle_price": 99.0},
        # not closed yet -> skipped
        {"T": now - 100, "O": 1.0, "settle_price": 1.0},
    ]
    (tmp_path / "scored_windows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in sw))
    (tmp_path / "captures.jsonl").write_text(
        json.dumps({"T": T0 + 900, "published_priceToBeat": 99.9}) + "\n"
        + json.dumps({"T": T0 + 1800, "published_priceToBeat": 99.0}) + "\n")
    terms = {T0: {"priceToBeat": 100.0, "settlementPrice": 99.9, "_resolution": "down",
                  "_resolution_raw": {"winningOutcome": "Down"}, "_market_keys": ["a"]},
             T0 + 900: {"priceToBeat": 100.0, "settlementPrice": 99.0, "_resolution": None}}
    monkeypatch.setattr(sf, "venue_terms", lambda T: terms.get(T))
    assert sf.resolve_windows(str(tmp_path), now=now) == 3
    rows = {r["T"]: r for r in _read(sf.out_path(str(tmp_path)))}
    a, b = rows[T0], rows[T0 + 900]
    assert a["venue_outcome_src"] == "venue_resolution" and a["agree_ours"] is False
    assert a["agree_next_pb"] is True and a["fetch_delay_s"] == 5
    assert b["venue_outcome_src"] == "venue_settlementPrice"
    assert b["agree_ours"] is True and b["agree_next_pb"] is True
    assert b["fetch_delay_s"] == 240
    assert rows[T0 + 1800]["venue_outcome"] is None
    assert a["market_keys"] == ["a"]
    for _ in range(5):
        sf.resolve_windows(str(tmp_path), now=now)
    out = [r for r in _read(sf.out_path(str(tmp_path))) if r["type"] == "resolution"]
    assert sum(r["T"] == T0 for r in out) == 1
    assert sum(r["T"] == T0 + 1800 for r in out) == sf.MAX_ATTEMPTS
    s = sf.summarize(out)
    assert (s["res_compared"], s["res_agree_ours"], s["res_n_ours"]) == (2, 1, 2)
    assert (s["res_agree_next_pb"], s["res_n_next_pb"], s["res_src_resolution"]) == (2, 2, 1)
    assert sf.resolve_windows(str(tmp_path), now=now, max_calls=0) == 0


def test_summary_rate_per_path_and_delay(tmp_path):
    rows = []
    for i in range(6):
        w = _window(T0 + 900 * i, 83300.0 + (2.0 * i), 83300.0, [0], 0)
        w["captures"] = [{"path": "trade", "s_avg": 83300.0, "fetch_delay_hi_s": 5},
                         {"path": "brier", "s_avg": 83300.0 + 2.0 * i,
                          "fetch_delay_hi_s": 60 * (i + 1)}]
        rows.append(w)
    s = sf.summarize(rows)
    assert s["with_truth"] == 6 and s["mismatched"] == 5
    assert abs(s["mismatch_rate"] - 5 / 6) < 1e-9
    assert s["per_path"]["trade"] == {"n": 6, "mismatched": 0, "rate": 0.0,
                                      "median_gap": 0.0, "median_delay_s": 5.0}
    assert s["per_path"]["brier"]["mismatched"] == 5
    assert s["delay_vs_gap_n"] == 12 and s["delay_vs_gap_r"] > 0.9


# ---- quick tests on existing data (archive + scored windows), no venue calls

import gzip  # noqa: E402

import research.settle_gap_quicktests as qt  # noqa: E402


def _write_archive(cdir, series):
    os.makedirs(cdir, exist_ok=True)
    with gzip.open(os.path.join(cdir, "2026-10-08.csv.gz"), "wt") as f:
        f.write("ts,close,tickCount\n")
        for ts in sorted(series):
            f.write(f"{ts},{series[ts]},3\n")


def _drift_series(T_list, k_true=0):
    """Piecewise-linear series with a different drift per window."""
    s = {}
    for j, T in enumerate(T_list):
        slope = ((j * 7) % 11 - 5) * 0.8          # -4..+4 $/s, varies by window
        for x in range(T - 120, T + 900 + 120):
            s.setdefault(x, round(83000 + slope * (x - T) + j * 3.0, 2))
    return s


def test_quicktests_detect_revision(tmp_path):
    Ts = [T0 + 900 * 4 * j for j in range(6)]
    series = _drift_series(Ts)
    sw, caps = [], []
    for j, T in enumerate(Ts):
        Te = T + 900
        true_avg = round(sum(series[x] for x in range(Te - 60, Te)) / 60, 2)
        ours = true_avg + (3.0 if j % 2 else 0.0)   # half were fetched before revision
        sw.append({"T": T, "settle_price": ours, "scored_at": Te + 5})
        caps.append({"T": Te, "published_priceToBeat": true_avg})
    (tmp_path / "scored_windows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in sw))
    (tmp_path / "captures.jsonl").write_text("".join(json.dumps(r) + "\n" for r in caps))
    _write_archive(str(tmp_path / "research" / "candles"), series)
    s = qt.analyze(str(tmp_path))
    assert s["with_archive"] == 6 and s["revised"] == 3
    assert s["archive_matches_next_pb"] == 6 and s["archive_vs_next_pb_n"] == 6
    assert s["gap_mismatched"] == 3


def test_quicktests_detect_timing_offset(tmp_path):
    Ts = [T0 + 900 * 4 * j for j in range(8)]
    series = _drift_series(Ts)
    sw, caps = [], []
    for T in Ts:
        Te = T + 900
        ours = round(sum(series[x] for x in range(Te - 60, Te)) / 60, 2)
        venue = round(sum(series[x] for x in range(Te - 60 - 6, Te - 6)) / 60, 2)  # 6s early
        sw.append({"T": T, "settle_price": ours, "scored_at": Te + 5})
        caps.append({"T": Te, "published_priceToBeat": venue})
    (tmp_path / "scored_windows.jsonl").write_text("".join(json.dumps(r) + "\n" for r in sw))
    (tmp_path / "captures.jsonl").write_text("".join(json.dumps(r) + "\n" for r in caps))
    _write_archive(str(tmp_path / "research" / "research" / "candles"), series)  # alt dir
    s = qt.analyze(str(tmp_path))
    assert s["candles_dir"].endswith(os.path.join("research", "research", "candles"))
    assert s["revised"] == 0
    assert s["r_gap_move60"] < -0.99                 # strong correlation
    assert abs(s["implied_offset_s"] - (-6)) <= 0.5


def test_quicktests_read_only_and_empty(tmp_path, capsys):
    assert qt.analyze(str(tmp_path))["windows"] == 0
    (tmp_path / "scored_windows.jsonl").write_text(json.dumps({"T": T0, "settle_price": 1.0}) + "\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert qt.main(["--base", str(tmp_path), "--rows"]) == 0
    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after
    assert "(a) revisions" in capsys.readouterr().out


def test_collect_stale_logged_once_and_bad_window_isolated(tmp_path, monkeypatch):
    Te = T0 + 900
    _seed_capture(tmp_path, T0, _closes(Te))
    _stub_venue(monkeypatch, {}, {}, {}, [])
    late = Te + sf.STALE_S + 10
    assert sf.collect_windows(str(tmp_path), now=late) == 2      # p5m + p60m, stale
    assert sf.collect_windows(str(tmp_path), now=late + 900) == 0
    # a window whose analysis blows up is logged and the next one still runs
    other = tmp_path / "o"
    _seed_capture(other, T0, _closes(Te))
    T1 = T0 + 900
    _seed_capture(other, T1, _closes(T1 + 900))
    real = sf.analyze_window

    def flaky(T, *a, **k):
        if T == T0:
            raise ValueError("boom")
        return real(T, *a, **k)
    monkeypatch.setattr(sf, "analyze_window", flaky)
    _stub_venue(monkeypatch, {T0: _closes(Te), T1: _closes(T1 + 900)}, {}, {}, [])
    assert sf.collect_windows(str(other), now=T1 + 900 + 400) == 2
    a, b = _read(sf.out_path(str(other)))
    assert "boom" in a["error"] and b["T"] == T1 and "error" not in b
