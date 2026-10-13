#!/usr/bin/env python3
"""Phase 4: paper-trading engine for Polymarket US 15m BTC Up/Down.

PAPER ONLY. There is no live order path in this file -- no signing, no keys,
no order submission. Fills are simulated against displayed book depth.

Sleeves (brief §5.4), evaluated in order A -> B -> C, default = abstain:
  A. Pair/sum capture: ask_up + ask_down + fees + slippage < 1.00, both legs
     fill or neither (abort unless displayed size covers both).
  B. Favorite-side stale quote ONLY: book already has a favorite with
     ask_fav in [0.52, 0.80]; BRTI p_fair agrees on sign; edge >= 0.08 after
     fees; tau in [90s, 12min]; winner's-curse veto (edge >= 0.15 persisting
     ~300ms -> skip; polybtc: 0-for-5 at >=15c). UNDERDOG BUYS ARE
     STRUCTURALLY IMPOSSIBLE: the function only ever returns the book's
     favorite side.
  C. Late certainty (tiny, default OFF): p_fair >= 0.997 or <= 0.003 with a
     real ask in [0.90, 0.98] and size; never 1c, never 99c+.

Kill switches (armed by default; --no-killswitches to disarm):
  - day realized P&L <= -5% of bankroll -> halt new entries
  - 8 consecutive directional (B/C) losses -> disable B and C
  - Brier(model) > Brier(market) over trailing 200 windows -> disable B and C

Blotter fields (acceptance #16): event_id, Topen, Tclose, O, S, tau, p_fair,
ask_up, ask_down, sleeve, side, size, filled, unfilled, brti_settle, outcome,
pnl_after_fees, reason.

Usage:
  python paper.py --run-once [--sleeves A,B] [--no-killswitches]
  python paper.py --settle
  python paper.py --report
"""
import argparse
import copy
import getpass
import hashlib
import json
import math
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import append_jsonl, now_s, taker_fee_per_share, SLIPPAGE_PER_LEG
from markets import (window_start, get_event, parse_terms, get_book,
                     parse_book, mid_up, event_slug)
from fairvalue import fair_snapshot, brier

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "paper_state.json")
BLOTTER = os.path.join(HERE, "blotter.jsonl")

# ---- paper economics ----
BANKROLL = 1000.0
RISK_PER_WINDOW = 0.015   # 1.5% on B/C directionals
PAIR_RISK = 0.02          # 2% on locked pair trades
CERTAINTY_RISK = 0.005    # 0.5% on sleeve C
DOLLAR_CAP = 25.0

# ---- fee model stamp (every blotter record, 2026-10-08) ----
# Canonical identifier for the taker fee schedule. Also referenced inside
# signal_hashes()' shared context (same value -> hashes unchanged).
FEE_MODEL = "taker:0.07*price*(1-price)"

# ---- sleeve gates ----
A_EDGE = 1.00             # ask_up + ask_down + fees + slip < 1.00
B_ASK_LO, B_ASK_HI = 0.52, 0.80
B_EDGE_MIN = 0.08
B_TAU_LO, B_TAU_HI = 90, 720
B_CURSE_MS = 0.3
# Winner's-curse veto (polybtc study: 1,853 markets — edges >= 0.15 went
# 0-for-5). Any first-sight edge >= 15c is re-polled after 300ms; if it
# persists >= 15c the trade is skipped. Edges that fade below 15c but
# still clear B_EDGE_MIN are taken.
B_VETO_EDGE = 0.15
C_P_HI, C_P_LO = 0.997, 0.003
C_ASK_LO, C_ASK_HI = 0.90, 0.98
C_TAU_MAX = 60  # polybtc late-snipe: last 15-60s only (was 120)

# ---- Sleeve D: lead persistence (validated 2026-10-03 on 548 BRTI windows;
# see SLEEVE_D_DESIGN.md). Buys the TWAP leader late in the window.
D1_TAU_LO, D1_TAU_HI = 240, 360   # 5 min left +/- 60s (band-robust: 88-95%)
D2_TAU_LO, D2_TAU_HI = 60, 180    # 2 min left +/- 60s
D1_LEAD_LO, D1_LEAD_HI = 5.0, 10.0   # bp of twap60 vs priceToBeat
D2_LEAD_LO, D2_LEAD_HI = 10.0, 15.0  # bp
D1_ASK_CAP, D2_ASK_CAP = 0.80, 0.90
D1_P, D2_P = 0.912, 0.987  # measured hold rates (point estimates, n=80/78)
D_EDGE_MIN = 0.05          # edge = p - ask - taker_fee(ask)
D_VETO_EDGE = 0.15         # winner's-curse veto, mirrors B
D_VOL_MEDIAN = 3.87        # bp; trailing-1h vol calm threshold (BRTI-measured)
D1_BREAKEVEN = 0.8112      # 0.80 + taker_fee(0.80); D live-hold halt level
D2_BREAKEVEN = 0.9063      # 0.90 + taker_fee(0.90)
D_HALT_N = 20              # sub-sleeve live-hold lookback for the D halt

# ---- Brier: score every closed window, traded or not (review 2026-10-06) ----
BRIER_DRAIN_PER_RUN = 24  # oldest closed windows scored per settle run

# ---- settlement integrity check (review 2026-10-08, Claude's recommendation)
# For window t, s_avg covers [t+840, t+900). The NEXT window's published
# price-to-beat covers the same 60s interval, so they should agree. A large
# gap means the settlement derivation is wrong (cf. the Oct 7 offline CSV
# bug: median gap $0.84, 4 direction flips). Logging only — never blocks
# settlement, never touches hashes/gates/P&L.
#
# v2 (chatbox review of 3a7a8ca1): settle runs ~5s after Tclose, when the
# next window's price-to-beat is often not yet published — an immediate
# check would silently cover ~nothing. So checks are QUEUED at settle time
# and DRAINED on later settle runs once next_pb appears (deduped by window
# start, expired after 1h). Outcome flips are flagged regardless of gap
# size — a flip changes the books; a $0.30 gap across the price-to-beat
# matters more than a $5 gap that doesn't.
SETTLE_CHECK_TOL = 0.50  # dollars; gap above this is a drift alert
SETTLE_CHECK_TTL = 3600  # seconds; queued entries older than this expire
# Part 1 (2026-10-09, LaDre-signed; TTL per Claude's review): trades wait
# for the next window's price-to-beat, then fall back to s_avg after this.
TRADE_SETTLE_TTL = 1800  # seconds; 30 minutes

# ---- latency: cache slow-moving inputs per window ----
SIGMA_CACHE_TTL = 600     # realized-vol sigma barely moves tick to tick;
                          # 10-min cache saves 3 BRTI fetches per tick

KILL_DAY_PCT = -0.05
KILL_CONSEC_LOSSES = 8
# Brier kill, option (c) enacted by LaDre 2026-10-06 (risk-only change under
# v2 hashes): the legacy point-estimate rule is retired. Fire only when there
# are >=KILL_BRIER_MIN_N distinct scored windows under the CURRENT model
# version AND the 95% paired CI lower bound (model - market) is > 0.
# Firing halts B/C/D and reports; re-enabling needs LaDre's word.
KILL_BRIER_MIN_N = 100
KILL_BRIER_CI_Z = 1.96
# Rolling / drawdown kill switches (review 2026-10-06). DEFAULT thresholds --
# LaDre sets the final numbers before any run that matters; these are sane
# starting points, not his word.
KILL_ROLL20_PCT = -0.07    # net P&L over last 20 settled <= -7% bankroll (LaDre 2026-10-06)
KILL_DRAWDOWN_PCT = 0.10   # peak-to-trough drawdown >= 10% of bankroll (LaDre 2026-10-06)

# ---- execution latency model (review 2026-10-06) ----
# Stale quotes are exactly what faster bots take first. After a sleeve fires,
# the engine re-polls the book after LATENCY_MS and only books the fill if
# the same sleeve still clears its gate at the fresh ask. 0 = off (pre-freeze
# behavior); 500ms is the starting default.
LATENCY_MS = 500

# ---- parameter freeze (round 4: per-sleeve signal hashes + risk hash) ----
# Each sleeve's signal/fill parameters hash independently, so tuning one
# sleeve never discards another sleeve's accumulated trades. Kill-switch
# thresholds and sizing live in RISK_HASH, outside every signal hash --
# changing a risk limit re-arms risk accounting without invalidating
# signal evidence. Blotter records carry all four signal hashes + the
# ---- round-5 corrective re-freeze (enacted 2026-10-06T07:47:20Z) ----
# Per-hash freeze times are METADATA, not hash inputs. Each is set only when
# that hash's inputs change, so a risk-only change no longer resets signal
# eras. Removing the global freeze timestamp from hash inputs and adding
# FAIRVALUE_MODEL_V is a hash-structure change only; no trading parameter,
# threshold, or sizing value changed in this freeze.
def _fairvalue_model_v():
    """Content hash of fairvalue.py, computed at import time.

    Editing fairvalue.py changes this value, which changes every signal
    hash — the era resets automatically. Produces the same value as the
    old literal for the current file, so this change alone resets nothing.
    """
    with open(os.path.join(HERE, "fairvalue.py"), "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()
    return ("fv1:sha256:" + digest + ":"
            "phi(ln(S/O)/(sig*sqrt(tau)));"
            "sig=max(rv7200s,rv900s,floor1e-5);"
            "S=last_close|running_avg60_final59s")


FAIRVALUE_MODEL_V = _fairvalue_model_v()
FROZEN_AT_A = FROZEN_AT_B = FROZEN_AT_C = FROZEN_AT_D = "2026-10-06T07:47:20Z"
FROZEN_AT_RISK = "2026-10-06T07:48:21Z"  # risk-only change: Brier kill -> interval rule (LaDre 2026-10-06)
HASH_FROZEN_AT = {"A": FROZEN_AT_A, "B": FROZEN_AT_B, "C": FROZEN_AT_C,
                  "D": FROZEN_AT_D, "risk": FROZEN_AT_RISK}


def _hash_params(d):
    canon = json.dumps(d, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def signal_hashes():
    """Per-sleeve signal/fill hashes + shared execution context."""
    shared = {"FEE_MODEL": FEE_MODEL,
              "LATENCY_MS": LATENCY_MS,
              "BRTI_SETTLE": "avg60[T-60,T-1]",
              "FAIRVALUE_MODEL_V": FAIRVALUE_MODEL_V}
    return {
        "A": _hash_params({**shared, "A_EDGE": A_EDGE}),
        "B": _hash_params({**shared,
                           "B_ASK": (B_ASK_LO, B_ASK_HI),
                           "B_EDGE_MIN": B_EDGE_MIN,
                           "B_TAU": (B_TAU_LO, B_TAU_HI),
                           "B_VETO_EDGE": B_VETO_EDGE,
                           "B_CURSE_MS": B_CURSE_MS}),
        "C": _hash_params({**shared,
                           "C_P": (C_P_HI, C_P_LO),
                           "C_ASK": (C_ASK_LO, C_ASK_HI),
                           "C_TAU_MAX": C_TAU_MAX}),
        "D": _hash_params({**shared,
                           "D1_TAU": (D1_TAU_LO, D1_TAU_HI),
                           "D1_LEAD": (D1_LEAD_LO, D1_LEAD_HI),
                           "D2_TAU": (D2_TAU_LO, D2_TAU_HI),
                           "D2_LEAD": (D2_LEAD_LO, D2_LEAD_HI),
                           "D_ASK_CAP": (D1_ASK_CAP, D2_ASK_CAP),
                           "D_P": (D1_P, D2_P),
                           "D_EDGE_MIN": D_EDGE_MIN,
                           "D_VETO_EDGE": D_VETO_EDGE,
                           "D_VOL_MEDIAN": D_VOL_MEDIAN,
                           "D_BREAKEVEN": (D1_BREAKEVEN, D2_BREAKEVEN),
                           "D_HALT_N": D_HALT_N}),
    }


def risk_hash():
    """Sizing + kill-switch thresholds. Changing these never resets signal eras."""
    return _hash_params({"BANKROLL": BANKROLL,
                         "RISK_PER_WINDOW": RISK_PER_WINDOW,
                         "PAIR_RISK": PAIR_RISK,
                         "CERTAINTY_RISK": CERTAINTY_RISK,
                         "DOLLAR_CAP": DOLLAR_CAP,
                         "KILL_DAY_PCT": KILL_DAY_PCT,
                         "KILL_CONSEC_LOSSES": KILL_CONSEC_LOSSES,
                         "KILL_BRIER_MIN_N": KILL_BRIER_MIN_N,
                         "KILL_BRIER_CI_Z": KILL_BRIER_CI_Z,
                         "KILL_ROLL20_PCT": KILL_ROLL20_PCT,
                         "KILL_DRAWDOWN_PCT": KILL_DRAWDOWN_PCT})


SIG = signal_hashes()
RISK = risk_hash()


def hash_stamp():
    """Stamp dict: all signal hashes + risk + per-hash freeze metadata."""
    return {"sig_A": SIG["A"], "sig_B": SIG["B"], "sig_C": SIG["C"],
            "sig_D": SIG["D"], "risk_hash": RISK,
            "frozen_at": dict(HASH_FROZEN_AT)}


# ---- gate v2 (ENACTED 2026-10-06 by LaDre) ----
GATE_B_TRADES = 150          # floor, not a green light
GATE_BRIER_TOL = 0.005       # 90% paired-interval upper bound must be below this
GATE_BRIER_N = 100           # scored windows needed for the Brier criterion

# ---- futility checkpoint (report-only, 2026-10-08; thresholds per LaDre) ----
# REPORT-ONLY: never auto-halts. An auto-halt is a risk-policy change needing
# LaDre's word. These constants are NOT in any signal or risk hash: they
# cannot affect signals, sizing, or era accounting.
FUTILITY_CHECKPOINTS = (75, 100, 125)  # B-era settled trades
FUTILITY_MIN_EDGE_USD = 1.26           # LaDre's pick 2026-10-08
FUTILITY_SD_FLOOR = 8.0                # analytical per-trade SD floor, USD
FUTILITY_Z = 1.2816                    # one-sided 90%


def futility_status(b_pnls):
    """Report-only futility check on B-era settled P&Ls. Pure function.

    Returns None for n<2. Otherwise the one-sided 90% upper bound on mean
    P&L/trade (with an analytical SD floor); futile=True once a checkpoint
    n is reached and the upper bound sits below FUTILITY_MIN_EDGE_USD.
    Touches no state and no blotter.
    """
    n = len(b_pnls)
    if n < 2:
        return None
    m = sum(b_pnls) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in b_pnls) / (n - 1))
    sd_eff = max(sd, FUTILITY_SD_FLOOR)
    ub = m + FUTILITY_Z * sd_eff / math.sqrt(n)
    cp = max((c for c in FUTILITY_CHECKPOINTS if n >= c), default=None)
    return {"n": n, "mean": m, "sd_eff": sd_eff, "ub90": ub,
            "checkpoint": cp,
            "futile": cp is not None and ub < FUTILITY_MIN_EDGE_USD}


# ---- Brier window accounting (2026-10-08) ----
# Single retention cap for scored Brier windows (was 2000 on the drain path,
# 500 on the position-settle path -- now one constant, both paths).
BRIER_WINDOWS_MAX = 2000

# Legacy model_v label: the 2026-10-06 08:00 UTC re-freeze switched the label
# from this literal to a sha256-prefixed form WITHOUT changing the model
# (same phi formula, verified). The one-time --stamp-legacy-model-v migration
# relabels these windows so kill(c) and gate [3] see one consistent set.
LEGACY_MODEL_V = ("fv1:phi(ln(S/O)/(sig*sqrt(tau)));sig=max(rv7200s,rv900s,"
                  "floor1e-5);S=last_close|running_avg60_final59s")


def _brier_windows_current(state):
    """Distinct scored windows under the CURRENT model version.

    One helper for kill_check, report()'s paired CI, and gate [3] so all
    three see the same window set. Windows missing model_v default to
    current (back-compat with pre-label records).
    """
    seen, out = set(), []
    for w in state.get("brier_windows", []):
        if w.get("brier_model") is None or w.get("brier_mkt") is None:
            continue
        if w.get("model_v", FAIRVALUE_MODEL_V) != FAIRVALUE_MODEL_V:
            continue
        if w["T"] in seen:
            continue
        seen.add(w["T"])
        out.append(w)
    return out


def _trim_brier_windows(state):
    """Enforce the single retention cap (in place)."""
    state["brier_windows"] = state["brier_windows"][-BRIER_WINDOWS_MAX:]


# ---- research logging (2026-10-08) ----
# Per-window research records go to an append-only jsonl (kept out of
# paper_state.json, which is rewritten with fsync every tick). Paths are
# derived from HERE at call time so tests stay isolated.
def _scored_windows_path():
    return os.path.join(HERE, "scored_windows.jsonl")


def _model_only_path():
    return os.path.join(HERE, "model_only.jsonl")


def _rotate_monthly(path):
    """Archive path to path.YYYY-MM.jsonl when its mtime is from an earlier
    month, then start fresh. Research jsonls (scored_windows, model_only)
    grow without bound; monthly rotation keeps the active file small and
    preserves history. No-op when the file doesn't exist or is current.
    Returns the (active) path.
    """
    import datetime
    if os.path.exists(path):
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        now = datetime.datetime.now()
        if (mt.year, mt.month) != (now.year, now.month):
            base = (path[:-len(".jsonl")]
                    if path.endswith(".jsonl") else path)
            arch = f"{base}.{mt.year:04d}-{mt.month:02d}.jsonl"
            if not os.path.exists(arch):
                os.replace(path, arch)
            else:
                # Archive slot taken (clock skew / double rotation): fall
                # back to a timestamped name so nothing is ever lost.
                arch = (f"{base}.{mt.year:04d}-{mt.month:02d}."
                        f"{int(now.timestamp())}.jsonl")
                os.replace(path, arch)
    return path


def _append_scored_windows(obj):
    _rotate_monthly(_scored_windows_path())
    append_jsonl(_scored_windows_path(), obj)


def _append_model_only(obj):
    _rotate_monthly(_model_only_path())
    append_jsonl(_model_only_path(), obj)


def _read_scored_windows():
    """Read scored-window research records, oldest archive first, deduped
    by T keeping the LAST record per window.

    Dedupe rationale: if settle() crashes after the jsonl append but before
    save_state, the window re-scores on the next run and appends a second
    line. The blotter/state path pops the pending entry, so only the jsonl
    can double-write; keeping the last line per T is the correct recovery.
    """
    import glob
    base = _scored_windows_path()
    stem = base[:-len(".jsonl")] if base.endswith(".jsonl") else base
    paths = sorted(glob.glob(stem + ".*.jsonl"))
    if os.path.exists(base):
        paths.append(base)
    by_t = {}
    for p in paths:
        try:
            with open(p) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("T") is not None:
                        by_t[r["T"]] = r  # last wins
        except OSError:
            continue
    return [by_t[t] for t in sorted(by_t)]


# Fixed tau grid for paired by-tau calibration: each tick records its
# snapshot at the nearest grid point within TAU_GRID_TOL, nearest-wins
# (a later tick closer to the grid point displaces an earlier, farther
# one). Tolerance 30 is below half the smallest grid spacing (60s), so
# one tick can never fill two points.
TAU_GRID = (600, 450, 300, 180, 120)
TAU_GRID_TOL = 30


def _fill_tau_grid(tau_grid, tau, entry):
    """Nearest-wins fixed-tau grid fill. Pure function.

    tau_grid may carry string keys after a JSON round-trip; they are
    normalized to int on read. A tick fills a grid point only if it is
    strictly closer than TAU_GRID_TOL, and displaces the current entry
    only if strictly closer than it. With tol=30 < 60/2, at most one
    point is filled per tick. Returns the (possibly new) grid dict.
    """
    grid = {}
    for k, v in (tau_grid or {}).items():
        try:
            grid[int(k)] = v
        except (TypeError, ValueError):
            continue
    for g in TAU_GRID:
        d = abs(tau - g)
        if d >= TAU_GRID_TOL:
            continue
        prev = grid.get(g)
        if prev is None or d < abs(prev.get("tau_actual", 10**9) - g):
            grid[g] = dict(entry, tau_actual=tau)
    return grid


def _bootstrap_lb(xs, q=0.10, n_boot=2000, seed=7):
    """q-th percentile of bootstrap resample means (90% lower bound, q=0.10)."""
    import random
    rng = random.Random(seed)
    n = len(xs)
    means = sorted(sum(rng.choice(xs) for _ in range(n)) / n
                   for _ in range(n_boot))
    return means[int(q * n_boot)]


def _wilson(holds, n, z=1.96):
    """Wilson 95% interval for a hold rate. Returns (lo, hi)."""
    if n == 0:
        return (0.0, 1.0)
    p = holds / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


# ================= pure decision functions (testable, no network) =================

def decide_sleeve_a(book):
    """Pair capture. Returns trade dict or None."""
    au, ad = book.get("ask_up"), book.get("ask_down")
    if not au or not ad:
        return None
    p_up_ask, s_up = au
    p_dn_ask, s_dn = ad
    total = (p_up_ask + p_dn_ask + taker_fee_per_share(p_up_ask)
             + taker_fee_per_share(p_dn_ask) + 2 * SLIPPAGE_PER_LEG)
    if total >= A_EDGE:
        return None
    matched = min(s_up, s_dn)
    qty = min(matched, (BANKROLL * PAIR_RISK) / (p_up_ask + p_dn_ask), DOLLAR_CAP)
    qty = math.floor(qty * 100) / 100
    if qty <= 0:
        return None
    return {"sleeve": "A", "legs": [
        {"side": "up", "price": p_up_ask, "size": qty},
        {"side": "down", "price": p_dn_ask, "size": qty}],
        "edge": round(1.0 - total, 4)}


def decide_sleeve_b(book, p_fair, tau):
    """Favorite-side only. Structurally cannot emit an underdog buy."""
    au, ad = book.get("ask_up"), book.get("ask_down")
    if not au or not ad or p_fair is None:
        return None
    if not (B_TAU_LO <= tau <= B_TAU_HI):
        return None
    # favorite = pricier ask (higher price = more likely outcome)
    fav = "up" if au[0] > ad[0] else "down"
    ask_fav = au[0] if fav == "up" else ad[0]
    size_fav = au[1] if fav == "up" else ad[1]
    if not (B_ASK_LO <= ask_fav <= B_ASK_HI):
        return None
    p_fav = p_fair if fav == "up" else 1.0 - p_fair
    # same sign as model: model must agree with the book's favorite
    model_says_up = p_fair > 0.5
    if model_says_up != (fav == "up"):
        return None
    edge = p_fav - ask_fav - taker_fee_per_share(ask_fav)
    if edge < B_EDGE_MIN:
        return None
    if edge >= B_VETO_EDGE:
        return {"sleeve": "B", "veto_candidate": True, "edge": round(edge, 4),
                "reason": "winners_curse_check"}
    qty = min(size_fav, (BANKROLL * RISK_PER_WINDOW) / ask_fav, DOLLAR_CAP)
    qty = math.floor(qty * 100) / 100
    if qty <= 0:
        return None
    assert fav in ("up", "down")
    return {"sleeve": "B", "legs": [{"side": fav, "price": ask_fav, "size": qty}],
            "edge": round(edge, 4)}


def b_waterfall(book, p_fair, tau):
    """Log-only B gate waterfall (LaDre 2026-10-06 review fix, STAGED).

    Mirrors decide_sleeve_b's gates in order and reports the first failing
    gate plus the intermediates (fav side, ask_fav, ask-band, model-agree,
    edge, edge-0.08). NEVER used for trading decisions -- observability
    only. decide_sleeve_b itself is untouched.
    """
    wf = {"evaluated": True, "fav": None, "ask_fav": None,
          "in_ask_band": None, "model_agrees": None, "edge": None,
          "edge_minus_08": None, "first_failing_gate": None}
    au, ad = book.get("ask_up"), book.get("ask_down")
    if not au or not ad or p_fair is None:
        wf["first_failing_gate"] = "missing_data"
        return wf
    if not (B_TAU_LO <= tau <= B_TAU_HI):
        wf["first_failing_gate"] = "tau_out_of_band"
        return wf
    fav = "up" if au[0] > ad[0] else "down"
    ask_fav = au[0] if fav == "up" else ad[0]
    size_fav = au[1] if fav == "up" else ad[1]
    wf["fav"] = fav
    wf["ask_fav"] = round(ask_fav, 4)
    wf["in_ask_band"] = bool(B_ASK_LO <= ask_fav <= B_ASK_HI)
    if not wf["in_ask_band"]:
        wf["first_failing_gate"] = "ask_band"
        return wf
    p_fav = p_fair if fav == "up" else 1.0 - p_fair
    wf["model_agrees"] = bool((p_fair > 0.5) == (fav == "up"))
    if not wf["model_agrees"]:
        wf["first_failing_gate"] = "model_disagrees"
        return wf
    edge = p_fav - ask_fav - taker_fee_per_share(ask_fav)
    wf["edge"] = round(edge, 4)
    wf["edge_minus_08"] = round(edge - B_EDGE_MIN, 4)
    if edge < B_EDGE_MIN:
        wf["first_failing_gate"] = "edge_below_min"
        return wf
    if edge >= B_VETO_EDGE:
        wf["first_failing_gate"] = "veto_candidate"
        return wf
    qty = min(size_fav, (BANKROLL * RISK_PER_WINDOW) / ask_fav, DOLLAR_CAP)
    qty = math.floor(qty * 100) / 100
    if qty <= 0:
        wf["first_failing_gate"] = "qty_zero"
        return wf
    wf["first_failing_gate"] = "pass"
    return wf


def decide_sleeve_c(book, p_fair, tau):
    """Late certainty. Never 1c, never 99c+."""
    if p_fair is None or tau > C_TAU_MAX or tau <= 0:
        return None
    au, ad = book.get("ask_up"), book.get("ask_down")
    if p_fair >= C_P_HI and au:
        px, sz = au
        side = "up"
    elif p_fair <= C_P_LO and ad:
        px, sz = ad
        side = "down"
    else:
        return None
    if px <= 0.01 or px >= 0.99:      # never 1c, never 99c+
        return None
    if not (C_ASK_LO <= px <= C_ASK_HI) or sz <= 0:
        return None
    qty = min(sz, (BANKROLL * CERTAINTY_RISK) / px, DOLLAR_CAP)
    qty = math.floor(qty * 100) / 100
    if qty <= 0:
        return None
    return {"sleeve": "C", "legs": [{"side": side, "price": px, "size": qty}],
            "edge": round(abs((1.0 if side == "up" else 0.0) - px), 4)}


_D_CFG = {
    # sub: (tau_lo, tau_hi, lead_lo, lead_hi, ask_cap, p_hold, needs_calm)
    "D1": (D1_TAU_LO, D1_TAU_HI, D1_LEAD_LO, D1_LEAD_HI,
           D1_ASK_CAP, D1_P, True),
    "D2": (D2_TAU_LO, D2_TAU_HI, D2_LEAD_LO, D2_LEAD_HI,
           D2_ASK_CAP, D2_P, False),
}


def decide_sleeve_d(book, lead_bp, vol_bp, tau, sub):
    """Lead-persistence sleeve. sub in ("D1", "D2").

    Pure function (testable, no network). lead_bp/vol_bp come from
    d_lead_vol(). Returns a trade dict, a veto_candidate dict, a fade
    dict (D1 volatile regime), or None (no setup this tick -- the band
    stays open for the next tick).
    """
    t_lo, t_hi, l_lo, l_hi, ask_cap, p_hold, needs_calm = _D_CFG[sub]
    if not (t_lo <= tau <= t_hi):
        return None
    if lead_bp is None or abs(lead_bp) < l_lo or abs(lead_bp) > l_hi:
        return None
    if needs_calm:
        if vol_bp is None:
            return None
        if vol_bp >= D_VOL_MEDIAN:
            return {"sleeve": sub, "fade": True,
                    "reason": "fade: volatile regime"}
    side = "up" if lead_bp > 0 else "down"
    q = book.get("ask_up") if side == "up" else book.get("ask_down")
    if not q:
        return None
    ask, size = q
    if ask <= 0.01 or ask >= 0.99 or ask > ask_cap or size <= 0:
        return None
    edge = p_hold - ask - taker_fee_per_share(ask)
    if edge < D_EDGE_MIN:
        return None
    if edge >= D_VETO_EDGE:
        return {"sleeve": sub, "veto_candidate": True, "edge": round(edge, 4),
                "reason": "winners_curse_check"}
    qty = min(size, (BANKROLL * RISK_PER_WINDOW) / ask, DOLLAR_CAP)
    qty = math.floor(qty * 100) / 100
    if qty <= 0:
        return None
    return {"sleeve": sub, "legs": [{"side": side, "price": ask, "size": qty}],
            "edge": round(edge, 4)}


def _cached_sigma(state, T, now):
    """Per-window sigma cache. estimate_sigma needs 3 BRTI fetches (2h + 15m
    realized vol); the value barely moves between 2-min ticks, so cache it
    for SIGMA_CACHE_TTL. Misses compute and store. Never raises."""
    try:
        c = state.get("sigma_cache") or {}
        if (c.get("T") == T and c.get("sigma")
                and now - c.get("ts", 0) < SIGMA_CACHE_TTL):
            return c["sigma"]
        from fairvalue import estimate_sigma
        sig = estimate_sigma(now)
        if sig:
            # in-memory only: run_once persists via its own save_state calls.
            # (Helpers must never save synthetic dicts to the real file.)
            state["sigma_cache"] = {"T": T, "ts": now, "sigma": sig}
        return sig
    except Exception:
        return None


# Diagnostics cache lives here, NOT in paper_state.json: it is derived data
# (recomputable from the venue) and must never bloat the fsync'd state.
_DIAG_CACHE = {}


def _tick_diagnostics(state, T, now):
    """Raw sigma components + data-quality fields for research logging.

    Cached per window in memory (same TTL as sigma): the inputs barely
    move tick to tick. Never raises; missing pieces are None. Pure
    diagnostics -- never used for signals or sizing. The `state` arg is
    kept for call-site compatibility; the cache is module-level and is
    never persisted.

    Kill switch (2026-10-08, per Claude): BTC_DIAG=off disables the venue
    fetch entirely (returns {}). The shadow then no-ops for that tick.
    """
    if os.environ.get("BTC_DIAG", "").lower() == "off":
        return {}
    c = _DIAG_CACHE
    if (c.get("T") == T and c.get("diag")
            and now - c.get("ts", 0) < SIGMA_CACHE_TTL):
        return c["diag"]
    diag = _compute_diagnostics(now)
    # The 2h rows ride out via a popped private key: _compute_diagnostics
    # keeps its dict interface (tests pin it), and the rows land in the
    # memory-only cache for the post-decision shadow. Never persisted.
    rows = diag.pop("_rows", None)
    _DIAG_CACHE.clear()
    _DIAG_CACHE.update({"T": T, "ts": now, "diag": diag, "rows": rows})
    return diag


def _sd_of_logrets(pairs):
    """Sample sd of 1s log returns, exactly matching
    fairvalue.realized_sigma's math (a>0/b>0 filter, ddof, None if <60
    returns). Pure; no I/O. fairvalue.py itself is never touched."""
    rets = []
    for i in range(1, len(pairs)):
        a, b = pairs[i - 1][1], pairs[i][1]
        if a > 0 and b > 0:
            rets.append(math.log(b / a))
    if len(rets) < 60:
        return None, rets
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var), rets


def _diagnostics_from_rows(rows, t_now):
    """Pure diagnostics math on 1s rows [(ts, close, tickCount)].

    Same math _compute_diagnostics always used; split out (2026-10-08,
    per Claude) so the incremental buffer path and unit tests compute
    identical numbers from identical rows. Never raises.
    """
    from fairvalue import LOOKBACK_LONG, LOOKBACK_SHORT, SIGMA_FLOOR
    d = {"rv_long": None, "rv_short": None, "sigma_used": None,
         "floor_bound": None, "zero_ret_share_15m": None,
         "gapfill_share_15m": None}
    try:
        # Buffer carries a 60s margin; the 2h lookback is exact.
        in_2h = [r for r in rows if r[0] >= t_now - LOOKBACK_LONG]
        closes = [(ts, c) for ts, c, _ in in_2h]
        d["rv_long"], _ = _sd_of_logrets(closes)
        short = [(ts, c) for ts, c in closes
                 if ts >= t_now - LOOKBACK_SHORT]
        d["rv_short"], rets_short = _sd_of_logrets(short)
        if rets_short:
            d["zero_ret_share_15m"] = (
                sum(1 for r in rets_short if r == 0.0) / len(rets_short))
        short_tc = [tc for ts, _, tc in in_2h
                    if ts >= t_now - LOOKBACK_SHORT]
        if short_tc and all(tc is not None for tc in short_tc):
            d["gapfill_share_15m"] = (
                sum(1 for tc in short_tc if tc == 0) / len(short_tc))
    except Exception:
        pass
    cands = [x for x in (d["rv_long"], d["rv_short"]) if x]
    if cands:
        raw = max(cands)
        d["sigma_used"] = max(raw, SIGMA_FLOOR)
        d["floor_bound"] = bool(raw < SIGMA_FLOOR)
    return d


# Last diagnostics outcome this tick (fresh process per tick, so this is
# per-tick state). Read by the HEARTBEAT printer for the three-state
# diag= flag. _compute_diagnostics is the only writer.
_DIAG_LAST = {"ok": True, "age": -1}


def _compute_diagnostics(t_now):
    """Fetch-once diagnostics for one timestamp. Never raises.

    (2026-10-08, per Claude's second re-review): rows now come from the
    incremental diag_buffer sidecar instead of one 2h venue fetch per
    tick. Per-tick fetch is only the seconds since the last buffered ts
    (~60 candles; small ranges are proven to work). A big backfill runs
    at most once per 10 min; after a failed backfill the breaker skips
    diagnostics for 10 min. No retries inside the decision tick.

    (2026-10-08, per Claude's third review): the venue fetch is bounded
    on the trade path -- small ranges get timeout=8/retries=0, big
    ranges (>1000s) get timeout=30/retries=0. Worst case +8s incremental,
    +30s backfill; every failure trips a breaker. brti.py's own defaults
    (timeout=45, retries=2) are untouched so nothing else moves.
    """
    from brti import fetch_1s_with_tickcount
    import diag_buffer

    def _diag_fetch(a, b):
        big = (b - a) > 1000
        return fetch_1s_with_tickcount(a, b,
                                       timeout=(30 if big else 8),
                                       retries=0)

    rows, ok = diag_buffer.refresh_rows(
        diag_buffer.buffer_path(STATE), t_now, _diag_fetch)
    _DIAG_LAST["ok"] = bool(ok)
    try:
        _DIAG_LAST["age"] = int(t_now - rows[-1][0]) if rows else -1
    except Exception:
        _DIAG_LAST["age"] = -1
    d = _diagnostics_from_rows(rows, t_now) if ok else {
        "rv_long": None, "rv_short": None, "sigma_used": None,
        "floor_bound": None, "zero_ret_share_15m": None,
        "gapfill_share_15m": None}
    d["_rows"] = rows if ok else None  # popped by _tick_diagnostics
    return d


def _floor_bound_from(sigma_used, rv_long, rv_short):
    """Floor flag consistent with the sigma actually used for p_fair."""
    from fairvalue import SIGMA_FLOOR
    raw = max([x for x in (rv_long, rv_short) if x is not None],
              default=None)
    if raw is not None:
        return bool(raw < SIGMA_FLOOR)
    if sigma_used is not None:
        return bool(sigma_used <= SIGMA_FLOOR)
    return None


def _shadow_fill(T, tau, snap, state):
    """72h sigma shadow (2026-10-08, LaDre's word). Post-decision ONLY.

    Computes shadow p values (horizon60 + floor-3e-5 control) from the
    SAME 2h rows _tick_diagnostics already fetched -- zero added API
    calls, zero decision-path latency. Writes into the pending_brier
    entry and its tau grid; scored at settle into scored_windows.jsonl.
    Grid shadow values are written only for entries filled this tick
    (Fix B: no lookahead re-pricing). Never raises; never touches
    signals, sizing, or hashes.
    """
    try:
        from shadow_sigma import (sigma_horizon60, sigma_floor_3e5,
                                  p_up as _shadow_p_up)
        c = _DIAG_CACHE
        if c.get("T") != T:
            return
        rows = c.get("rows")
        if not rows:
            return
        diag = c.get("diag") or {}
        S, O = snap.get("S"), snap.get("O")
        # (2026-10-08, per Claude's second re-review): staleness
        # alignment. Every tick is a fresh process, so without this the
        # shadow sigmas are computed from rows up to NOW while the live
        # sigma was cached up to SIGMA_CACHE_TTL earlier -- tilting every
        # shadow window toward the shadow. Truncate rows to the live
        # sigma's timestamp so both see the same data vintage.
        # (2026-10-08, per Claude's third review): alignment guards.
        # Only honor the sigma cache when it's for THIS window -- another
        # window's cache would misalign the vintage.
        _sc = state.get("sigma_cache") or {}
        _sigma_ts = _sc.get("ts") if _sc.get("T") == T else None
        if _sigma_ts:
            # Live's exact 2h window at the cached ts.
            _rows_2h = [r for r in rows
                        if _sigma_ts - 7200 <= r[0] <= _sigma_ts]
            # h60 keeps the 60-second pre-roll: filter only the upper
            # bound (sigma_horizon60 needs closes at ts-60).
            _rows_h60 = [r for r in rows if r[0] <= _sigma_ts]
            if not _rows_2h or _rows_2h[-1][0] < _sigma_ts - 120:
                return
            rows = _rows_2h
        else:
            _rows_h60 = rows
        if not rows:
            return
        if _sigma_ts:
            _rl, _ = _sd_of_logrets([(ts, cl) for ts, cl, _ in rows])
            _sh = [(ts, cl) for ts, cl, _ in rows
                   if ts >= _sigma_ts - 900]
            _rs, _ = _sd_of_logrets(_sh)
        else:
            _rl, _rs = diag.get("rv_long"), diag.get("rv_short")
        sig_h60 = sigma_horizon60(_rows_h60)
        sig_f3 = sigma_floor_3e5(_rl, _rs)
        p_h60 = _shadow_p_up(S, O, sig_h60, tau)
        p_f3 = _shadow_p_up(S, O, sig_f3, tau)
        pb = (state.get("pending_brier") or {}).get(str(T))
        if not pb:
            return
        pb["shadow"] = {"sigma_h60": sig_h60, "p_h60": p_h60,
                        "sigma_f3": sig_f3, "p_f3": p_f3}
        # (Fix B, 2026-10-08, per Claude): only write shadow values for
        # grid entries written THIS tick. Re-pricing older entries with the
        # current tick's sigma is lookahead -- it favors the shadow because
        # live's grid entry keeps its causal p. _fill_tau_grid builds a
        # fresh dict (no p_h60) when it displaces an entry, so the
        # "p_h60" in _gs check reliably detects this-tick writes.
        # Sigmas are stored so z can be computed at grid points.
        for _gs in (pb.get("tau_grid") or {}).values():
            if _gs.get("tau_actual") != tau or "p_h60" in _gs:
                continue
            _gt, _gS = _gs.get("tau_actual"), _gs.get("S")
            if _gt and _gS:
                _gs.update(
                    p_h60=_shadow_p_up(_gS, O, sig_h60, _gt),
                    p_f3=_shadow_p_up(_gS, O, sig_f3, _gt),
                    sigma_h60=sig_h60, sigma_f3=sig_f3)
        state["pending_brier"][str(T)] = pb
        save_state(state)
    except Exception:
        pass


def _veto_fields(*, fav, ask_fav, depth, edge, p_fair, sigma_used,
                 floor_bound, S, O, tau):
    """Research enrichment for winner's-curse veto records. Pure."""
    return {
        "veto": True, "fav": fav,
        "ask_fav": round(ask_fav, 4) if ask_fav is not None else None,
        "depth": depth, "edge": edge,
        "p_fair": round(p_fair, 6) if p_fair is not None else None,
        "sigma": sigma_used, "floor_bound": floor_bound,
        "S": S, "O": O, "tau": tau,
    }


def _score_window_record(t, pb, s_avg, cap_price, now):
    """Build scored-window records from a pending entry + settlement.

    Pure function (no I/O). Returns (brier_windows_entry, research_record).
    The brier_windows entry stays minimal for the gates; the research
    record carries the full logging field list. model_v is carried from
    the pending entry (stamped at creation), never re-derived here.
    """
    oc = 1.0 if round(s_avg, 2) >= round(cap_price, 2) else 0.0
    settle_px = round(s_avg, 2)
    model_v = pb.get("model_v", FAIRVALUE_MODEL_V)
    bw_entry = {
        "T": t, "tau": pb.get("tau"),
        "brier_model": brier(pb["p_fair"], oc),
        "brier_mkt": brier(pb["mid_up"], oc),
        "model_v": model_v,
    }
    grid_scores = {}
    for g, gs in (pb.get("tau_grid") or {}).items():
        if gs.get("p_fair") is None or gs.get("mid_up") is None:
            continue
        grid_scores[str(g)] = {
            "brier_model": brier(gs["p_fair"], oc),
            "brier_mkt": brier(gs["mid_up"], oc),
            "tau_actual": gs.get("tau_actual"),
            # 72h shadow: paired shadow Briers per grid point (None if
            # the shadow didn't run for this window).
            "brier_h60": (brier(gs["p_h60"], oc)
                          if gs.get("p_h60") is not None else None),
            "brier_f3": (brier(gs["p_f3"], oc)
                         if gs.get("p_f3") is not None else None),
            # (Fix B follow-up, 2026-10-08, per Claude): carry the shadow
            # sigmas + S so z can be computed at grid points, not just
            # the last tick.
            "sigma_h60": gs.get("sigma_h60"),
            "sigma_f3": gs.get("sigma_f3"),
            "S": gs.get("S"),
        }
    # 72h shadow: window-level shadow values + Briers beside live/market.
    sh = pb.get("shadow") or {}
    shadow_rec = {
        "sigma_h60": sh.get("sigma_h60"), "p_h60": sh.get("p_h60"),
        "sigma_f3": sh.get("sigma_f3"), "p_f3": sh.get("p_f3"),
        "brier_h60": (brier(sh["p_h60"], oc)
                      if sh.get("p_h60") is not None else None),
        "brier_f3": (brier(sh["p_f3"], oc)
                     if sh.get("p_f3") is not None else None),
    }
    research = {
        "event_id": pb.get("event_id"), "T": t, "O": pb.get("O"),
        "S": pb.get("S"), "S_source": pb.get("S_source"),
        "tau_at_score": pb.get("tau_at_score", pb.get("tau")),
        "p_fair": pb.get("p_fair"), "outcome": oc,
        "settle_price": settle_px,
        "rv_long": pb.get("rv_long"), "rv_short": pb.get("rv_short"),
        "sigma_used": pb.get("sigma_used"),
        "floor_bound": pb.get("floor_bound"),
        "zero_ret_share_15m": pb.get("zero_ret_share_15m"),
        "gapfill_share_15m": pb.get("gapfill_share_15m"),
        "mid_up": pb.get("mid_up"),
        "bid_up": pb.get("bid_up"), "ask_up": pb.get("ask_up"),
        "bid_down": pb.get("bid_down"), "ask_down": pb.get("ask_down"),
        "depth_up": pb.get("depth_up"), "depth_down": pb.get("depth_down"),
        "model_v": model_v,
        "shadow": shadow_rec,
        "tau_grid_scores": grid_scores,
        "scored_at": now,
    }
    return bw_entry, research


def _settle_check_enqueue(state, t, s_avg, cap_price, now):
    """Queue a window for a deferred settlement cross-check.

    The next window's price-to-beat is often unpublished at settle time,
    so the check can't run immediately — it is queued here (deduped by
    window start: first enqueue wins) and drained later by
    _settle_check_drain once next_pb appears. Never raises.
    """
    try:
        if s_avg is None or cap_price is None:
            return
        q = state.setdefault("settle_check_queue", {})
        key = str(t)
        if key not in q:
            q[key] = {"s_avg": float(s_avg), "cap": float(cap_price),
                      "enqueued": now}
    except Exception:
        pass


def _settle_check_drain(state, caps, now):
    """Run queued cross-checks whose next_pb is now available.

    For each queued window t: if caps has the next window's published
    price-to-beat, compare it to the queued s_avg; count the check, track
    max_gap and gaps > $0.01, and flag drift (gap > SETTLE_CHECK_TOL) or
    outcome flips (which are flagged regardless of gap size). Entries still
    waiting past SETTLE_CHECK_TTL expire and are counted. Each entry is
    handled in its own try/except: a malformed entry is dropped and counted
    in stats["errors"] instead of aborting the whole drain. Logging only —
    never raises, never affects settlement, hashes, gates, or P&L.
    """
    try:
        q = state.get("settle_check_queue")
        # One-line hardening (both 2026-10-08 reviews): a truthy non-dict
        # here would raise outside the per-entry guards. Only reachable via
        # hand edit, but fail-safe is cheaper than fail-loud here.
        if not isinstance(q, dict):
            q = {}
        stats = state.setdefault("settle_check_stats",
                                 {"checked": 0, "expired": 0, "drift": 0,
                                  "flips": 0, "gap_gt_1c": 0, "max_gap": 0.0,
                                  "errors": 0})
    except Exception:
        return
    for key in list(q.keys()):
        try:
            t = int(key)
            entry = q[key]
            # Expiry is independent of next_pb: a stuck entry must not
            # block the queue forever.
            if now - entry.get("enqueued", now) > SETTLE_CHECK_TTL:
                stats["expired"] = stats.get("expired", 0) + 1
                del q[key]
                continue
            next_pb = (caps or {}).get(str(t + 900))
            if next_pb is None:
                continue
            s_avg = entry["s_avg"]
            cap = entry["cap"]
            npb = float(next_pb)
            gap = abs(s_avg - npb)
            stats["checked"] = stats.get("checked", 0) + 1
            if gap > 0.01:
                stats["gap_gt_1c"] = stats.get("gap_gt_1c", 0) + 1
            stats["max_gap"] = max(stats.get("max_gap", 0.0), round(gap, 2))
            # Outcome flip: s_avg and next_pb disagree on up/down vs the
            # window's own price-to-beat (same rounding as settlement).
            flip = (round(s_avg, 2) >= round(cap, 2)) != \
                   (round(npb, 2) >= round(cap, 2))
            if gap > SETTLE_CHECK_TOL or flip:
                if flip:
                    stats["flips"] = stats.get("flips", 0) + 1
                if gap > SETTLE_CHECK_TOL:
                    stats["drift"] = stats.get("drift", 0) + 1
                print(f"SETTLE_MISMATCH T={t} s_avg={s_avg:.2f} "
                      f"next_pb={npb:.2f} gap={gap:.2f}"
                      + (" FLIP" if flip else ""))
                mm = state.setdefault("settle_mismatches", [])
                mm.append({"T": t, "s_avg": round(s_avg, 2),
                           "next_pb": round(npb, 2), "gap": round(gap, 2),
                           "flip": flip, "ts": now})
                del mm[:-20]
            del q[key]
        except Exception:
            # Malformed entry or bad next_pb (captures.jsonl accepts any
            # truthy published_priceToBeat): drop it and count it rather
            # than aborting the whole drain.
            try:
                stats["errors"] = stats.get("errors", 0) + 1
                if key in q:
                    del q[key]
            except Exception:
                pass


def _forensics_capture_path():
    return os.path.join(HERE, "research", "settle_forensics_capture.jsonl")


FORENSICS_CAPTURE_MAX_BYTES = 5_000_000  # ~25 days at ~200 KB/day; then stop


def _forensics_capture(path_tag, t, candles, s_avg, settle_now):
    """Settlement forensics capture (2026-10-09, Claude's prescription).

    Appends the ORIGINAL 60 one-second closes behind s_avg for window t
    (the [Te-60, Te) prints settle just averaged) plus fetch-delay bounds,
    so research/settle_forensics.py can later test whether the candles
    were revised. Research/logging only: reads nothing settle uses, writes
    only research/settle_forensics_capture.jsonl, makes zero API calls,
    never raises. Kill switch: BTC_FORENSICS=off. Size-capped.
    """
    try:
        if os.environ.get("BTC_FORENSICS", "").lower() == "off":
            return
        if s_avg is None:
            return
        t = int(t)
        Te = t + 900
        by_ts = {int(ts): float(c) for ts, c in candles}
        closes = [by_ts.get(x) for x in range(Te - 60, Te)]
        path = _forensics_capture_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if (os.path.exists(path)
                and os.path.getsize(path) > FORENSICS_CAPTURE_MAX_BYTES):
            return
        captured_at = time.time()
        append_jsonl(path, {
            "type": "capture", "path": path_tag, "T": t, "Te": Te,
            "s_avg": float(s_avg), "closes": closes,
            # the fetch happened between settle start and this capture
            "fetch_delay_lo_s": int(settle_now) - Te,
            "fetch_delay_hi_s": round(captured_at - Te, 2),
            "captured_at": round(captured_at, 2)})
    except Exception:  # noqa: BLE001 - logging only, must never break settle
        pass


def _tick_lock_held():
    """True if a tick.sh run currently holds the run lock."""
    import fcntl
    p = os.environ.get("BTC_LOCK", "/tmp/btc-paper-run.lock")
    try:
        with open(p, "a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
    except OSError:
        return False  # no lock file -> no tick running


def _stamp_legacy_model_v():
    """One-time migration: relabel legacy model_v windows to current.

    The 2026-10-06 08:00 UTC label switch was format-only (same phi
    formula, verified); relabeling gives kill(c) and gate [3] one
    consistent window set. Safety: refuses when a kill_check_error sidecar
    flag is set or a tick holds the run lock; backs up STATE first;
    idempotent (second run stamps 0). Returns the stamped count.
    Raises SystemExit on refusal.
    """
    flag = _kc_flag_read()
    if flag is not None:
        print(f"REFUSED: kill_check_error flag set "
              f"({flag.get('type')}: {flag.get('message')}) -- clear it first")
        raise SystemExit(1)
    if _tick_lock_held():
        print("REFUSED: tick lock held -- run between ticks")
        raise SystemExit(1)
    raw = _kc_load_raw()
    if raw is None:
        print("REFUSED: STATE unreadable")
        raise SystemExit(1)
    ts = int(time.time())
    bak = f"{STATE}.pre_stamp.{ts}"
    shutil.copy2(STATE, bak)
    n = 0
    for w in raw.get("brier_windows", []):
        if w.get("model_v") == LEGACY_MODEL_V:
            w["model_v"] = FAIRVALUE_MODEL_V
            w["model_v_stamped_from"] = "legacy_label"
            n += 1
    _atomic_json_write(STATE, raw)
    with open(STATE + ".model_v_audit.log", "a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                f"stamped={n} attestation='legacy label "
                f"fv1:phi(ln(S/O)/(sig*sqrt(tau)));... was a format-only "
                f"switch at 2026-10-06 08:00 UTC; same phi formula verified; "
                f"model unchanged'\n")
    print(f"stamped {n} legacy windows (backup: {bak})")
    return n


def d_lead_vol(O, Te, state=None):
    """BRTI inputs for Sleeve D. Returns (lead_bp, vol_bp), Nones on failure.

    lead_bp = (twap60(now) - O) / O * 1e4; vol_bp = trailing-1h std of
    1-min log returns in bp. Network: 2 BRTI calls (60 + 3600 candles).
    """
    from brti import fetch_1s, avg60
    now = now_s()
    try:
        rows = fetch_1s(now - 60, now - 1)
    except Exception:
        return None, None
    tw = avg60(rows, now)
    if tw is None or not O:
        return None, None
    lead = (tw - O) / O * 1e4
    # Hourly trailing vol depends only on Te (fully historical range), so
    # cache it per window: saves 1 BRTI fetch on every tick after the first,
    # and on the veto re-poll within a tick.
    vol = None
    if state is not None:
        vc = state.get("vol_cache") or {}
        if vc.get("Te") == Te and vc.get("vol"):
            vol = vc["vol"]
    if vol is None:
        try:
            rows = fetch_1s(Te - 3660, Te - 60)
        except Exception:
            return lead, None
        by_ts = {ts: c for ts, c in rows}
        closes = [by_ts.get(t) for t in range(Te - 3660, Te - 59, 60)]
        if any(x is None for x in closes) or len(closes) < 61:
            return lead, None
        rets = [math.log(closes[i + 1] / closes[i]) for i in range(60)]
        mu = sum(rets) / 60
        vol = math.sqrt(sum((r - mu) ** 2 for r in rets) / 60) * 1e4
        if state is not None:
            # in-memory only: run_once persists via its own save_state calls.
            state["vol_cache"] = {"Te": Te, "vol": vol}
    return lead, vol


def d_disabled(state, sub):
    """True if Sleeve D (or one sub-sleeve) is disabled."""
    dis = state.get("disabled", [])
    return "D" in dis or sub in dis


def migrate_decided(state):
    """Ensure per-sleeve decision tracking exists.

    ABC keeps the legacy decided_windows list untouched; D1/D2 get their
    own lists. Idempotent.
    """
    state.setdefault("decided_d1", [])
    state.setdefault("decided_d2", [])
    return state


def paper_fill(legs, book):
    """Simulate fills against displayed depth. Returns (filled_legs, unfilled_legs).

    A leg fills min(size, displayed size) at the decision ask. Anything not
    filled is marked unfilled -- never assumed, never booked as a win (#15).
    """
    depth = {"up": book.get("ask_up"), "down": book.get("ask_down")}
    filled, unfilled = [], []
    for leg in legs:
        d = depth.get(leg["side"])
        if not d:
            unfilled.append({**leg, "filled": 0.0})
            continue
        fq = min(leg["size"], d[1])
        fq = math.floor(fq * 100) / 100
        if fq > 0:
            filled.append({**leg, "filled": fq,
                           "cost": fq * leg["price"]
                           + fq * taker_fee_per_share(leg["price"])})
        if leg["size"] - fq > 0.005:
            unfilled.append({**leg, "filled": 0.0,
                             "unfilled": round(leg["size"] - fq, 2)})
    return filled, unfilled


# ================= state =================

def _backfill_day_pnls(d):
    """Fix 5/B (Claude review): shared backfill of day_pnls from the legacy
    day_pnl, keyed by d["day"] -- the day that day_pnl belongs to. NEVER
    today_key(): a deploy landing just after midnight, before kill_check's
    rollover, must not move yesterday's P&L into today. Returns d."""
    if isinstance(d, dict) and "day_pnls" not in d:
        dk = d.get("day") or today_key()
        try:
            d["day_pnls"] = {dk: round(float(d.get("day_pnl", 0.0)), 2)}
        except Exception:
            d["day_pnls"] = {}
    return d


def load_state():
    try:
        with open(STATE) as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {"open": [], "disabled": [], "day": None, "day_pnl": 0.0,
             "consec_losses": 0, "brier_windows": [], "pending_brier": {},
             "decided_windows": [], "decided_d1": [], "decided_d2": [],
             "settle_pnls": [], "cum_pnl": 0.0, "peak_cum": 0.0,
             "day_pnls": {}, **hash_stamp()}
    # Fix B (Claude review): backfill day_pnls from legacy day_pnl.
    s = _backfill_day_pnls(s)
    # D6 (Claude v4, LaDre-signed 2026-10-13): one-time rebuild of the
    # kill P&L ledgers from the pt1 point-rule ledger. The kills were fed
    # the recorded ledger; the pt1 ledger is now the certified input.
    s = _migrate_pt1_kill_ledgers(s)
    return s


def save_state(s):
    # A flagged tick works on an in-memory state that may hold restored or
    # scaffolded values. It must never reach disk.
    if isinstance(s.get("kill_check_error"), dict):
        return
    # Guard against clobbering the real state with a synthetic/partial dict
    # (e.g. a unit-test fixture). A valid paper state always carries the
    # ledger keys; refuse loudly instead of silently wiping the file.
    # (Fix C: the ledger is _KC_LEDGER -- day_pnls replaced day_pnl there.)
    for k in _KC_LEDGER:
        if k not in s:
            raise ValueError(f"save_state refused: missing key {k!r}")
    # Atomic write (LaDre 2026-10-06 review fix, STAGED, revised per
    # review): unique tmp name per process so concurrent writers can't
    # clobber each other's tmp file; fsync before the atomic replace so a
    # crash can't leave a torn STATE behind. A crashed tmp is removed
    # best-effort; stale ones are tiny and rare.
    _atomic_json_write(STATE, s)


def _atomic_json_write(path, obj):
    """Write obj as JSON to path atomically (per-pid tmp + fsync + replace)."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


# ---- kill_check crash handling: sidecar flag + validated last-good snapshot
# (LaDre 2026-10-06 review fix, v3 per 2026-10-07 review). A flagged tick
# never writes STATE. The flag lives in a sidecar file next to STATE so it
# survives corrupt state; restored values stay in memory until a human
# explicitly restores them at clear time.
_KC_STRUCTURAL_DEFAULTS = {
    "open": [], "disabled": [], "day_pnl": 0.0, "day_pnls": {},
    "consec_losses": 0,
    "brier_windows": [], "pending_brier": {}, "decided_windows": [],
    "decided_d1": [], "decided_d2": [], "settle_pnls": [],
    "cum_pnl": 0.0, "peak_cum": 0.0,
}
# Fix C (Claude review): day_pnls (the kill's day-P&L source of truth)
# replaces day_pnl in the ledger. A restore from last_good after a
# kill-check error must bring a consistent pair, never one without the
# other.
_KC_LEDGER = ("open", "decided_windows", "day_pnls", "consec_losses")
# Keys the --clear-kc-error path checks: the ledger plus the extra keys
# kill_check reads directly. (cum_pnl/peak_cum are only ever .get-read with
# defaults, so a legacy state missing them clears fine.)
_KC_CLEAR_KEYS = _KC_LEDGER + ("disabled",)
_KC_TYPES = {
    "open": list, "disabled": list, "day_pnl": (int, float),
    "day_pnls": dict, "consec_losses": int,
    "brier_windows": list, "pending_brier": dict, "decided_windows": list,
    "decided_d1": list, "decided_d2": list, "settle_pnls": list,
    "cum_pnl": (int, float), "peak_cum": (int, float),
}


def _kc_flag_path():
    return STATE + ".kc_error.json"  # STATE read at call time


def _kc_snapshot_path():
    return STATE + ".last_good"


def _kc_ok(k, v):
    return isinstance(v, _KC_TYPES[k]) and not isinstance(v, bool)


def _kc_bad_keys(state, keys=tuple(_KC_TYPES)):
    """Keys that are missing OR present with the wrong type."""
    return sorted(k for k in keys if k not in state or not _kc_ok(k, state[k]))


def _kc_load_raw():
    """STATE as raw JSON. No backfill, no defaults. None if unreadable."""
    try:
        with open(STATE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _kc_flag_read():
    """None = no flag. Fails CLOSED: an unreadable/odd flag file still halts."""
    p = _kc_flag_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {"type": "UnreadableFlag",
            "message": f"{p} unreadable or not an object"}


def _kc_flag_write(flag):
    try:
        _atomic_json_write(_kc_flag_path(), flag)
        return True
    except Exception:  # noqa: BLE001 - the crash handler must not raise
        return False


def _kc_snapshot_load():
    """A VALID snapshot or None. Corrupt/partial counts as no snapshot."""
    try:
        with open(_kc_snapshot_path()) as f:
            good = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(good, dict):
        return None
    # Fix 5 (Claude review): pre-Part-1 snapshots predate day_pnls --
    # backfill in memory before validating, or a kill-check error at deploy
    # would find "no usable last-good snapshot".
    _backfill_day_pnls(good)
    if _kc_bad_keys(good, _KC_LEDGER):
        return None
    return good


def _kc_snapshot_write(state):
    """Best-effort; only a flag-free state with a valid ledger is snapshotted."""
    if state.get("kill_check_error") or _kc_bad_keys(state, _KC_LEDGER):
        return
    # Keep one older generation: a valid-shaped but wrong snapshot (e.g. a
    # fixture's consec_losses=0) would otherwise become "last good" at once.
    # Rotate based on .prev's age, not the snapshot's: the snapshot is
    # rewritten every clean tick, so its own mtime is always recent.
    snap = _kc_snapshot_path()
    prev = snap + ".prev"
    try:
        if os.path.exists(snap) and (not os.path.exists(prev)
                                      or time.time() - os.path.getmtime(prev) > 3600):
            shutil.copy2(snap, prev)  # copy2 keeps the snapshot's mtime as .prev's age
    except OSError:
        pass
    try:
        _atomic_json_write(_kc_snapshot_path(), state)
    except Exception:  # noqa: BLE001
        pass


def _kc_fill(state):
    """IN-MEMORY ONLY. Fix missing/wrong-typed structural keys: first from a
    valid snapshot, then with empty scaffolding. Nothing here ever reaches
    disk. Returns (restored, scaffolded, snapshot_age_s)."""
    bad = _kc_bad_keys(state)
    good = _kc_snapshot_load()
    restored, age = [], None
    if good:
        restored = [k for k in bad if k in good and _kc_ok(k, good[k])]
        for k in restored:
            state[k] = copy.deepcopy(good[k])
        try:
            age = int(time.time() - os.path.getmtime(_kc_snapshot_path()))
        except OSError:
            pass
    scaffolded = [k for k in bad if k not in restored]
    for k in scaffolded:
        state[k] = copy.deepcopy(_KC_STRUCTURAL_DEFAULTS[k])
    return restored, scaffolded, age


def today_key(ts=None):
    # Fix A (Claude review): single day-key function AND timezone (UTC)
    # for the kill's day, the rollover, and Tclose-day booking. Pass a
    # timestamp to key a different day; None means now.
    return time.strftime("%Y-%m-%d", time.gmtime() if ts is None
                         else time.gmtime(ts))


def _day_pnl(state):
    """Fix A (Claude review): day_pnl is DERIVED from day_pnls -- one source
    of truth. The kill switch reads this; nothing writes state["day_pnl"]
    anymore (the legacy key survives on old states but is ignored)."""
    dpd = state.get("day_pnls")
    if not isinstance(dpd, dict):
        return 0.0
    v = dpd.get(today_key(), 0.0)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0.0
    return v


def _trim_day_pnls(state, keep=14):
    """Fix D (Claude review): day_pnls grows forever otherwise -- keep the
    last 14 days. Keys are YYYY-MM-DD, so string sort is chronological."""
    dpd = state.get("day_pnls")
    if isinstance(dpd, dict) and len(dpd) > keep:
        for k in sorted(dpd)[:-keep]:
            del dpd[k]


def kill_check(state, arm=True):
    """Returns (halt_new: bool, reasons: list)."""
    reasons = []
    if today_key() != state.get("day"):
        state["day"] = today_key()
        # Rollover: nothing to zero -- _day_pnl() reads today's bucket,
        # which starts empty for a new day. Deferred settles for windows
        # closing today will fill it.
    _dp = _day_pnl(state)
    if arm and _dp <= KILL_DAY_PCT * BANKROLL:
        reasons.append(f"day_pnl {_dp:.2f} <= -5% bankroll")
    if arm and state["consec_losses"] >= KILL_CONSEC_LOSSES:
        reasons.append(f"{state['consec_losses']} consecutive directional losses")
        for s in ("B", "C", "D"):
            if s not in state["disabled"]:
                state["disabled"].append(s)
    _bw = _brier_windows_current(state)  # one record per distinct window,
    # current model version only (legacy labels relabeled by migration)
    if arm and len(_bw) >= KILL_BRIER_MIN_N:
        _diffs = [w["brier_model"] - w["brier_mkt"] for w in _bw]
        _n = len(_diffs)
        _m = sum(_diffs) / _n
        _sd = math.sqrt(sum((x - _m) ** 2 for x in _diffs) / (_n - 1)) if _n > 1 else 0.0
        _lb = _m - KILL_BRIER_CI_Z * _sd / math.sqrt(_n) if _n else 0.0
        if _lb > 0:
            reasons.append(f"Brier 95% CI lower bound {_lb:+.4f} > 0 "
                           f"(model significantly worse) over {len(_bw)} "
                           f"distinct windows, model {FAIRVALUE_MODEL_V[:8]}")
            for _s in ("B", "C", "D"):
                if _s not in state["disabled"]:
                    state["disabled"].append(_s)
    # Rolling-20 and drawdown kill switches (review 2026-10-06). Thresholds
    # are DEFAULTS -- LaDre sets the final numbers.
    sp = state.get("settle_pnls", [])
    if arm and len(sp) >= 20 and sum(sp[-20:]) <= KILL_ROLL20_PCT * BANKROLL:
        reasons.append(f"rolling-20 P&L {sum(sp[-20:]):+.2f} <= "
                       f"{KILL_ROLL20_PCT:.0%} of bankroll")
        for s in ("B", "C", "D"):
            if s not in state["disabled"]:
                state["disabled"].append(s)
    dd = state.get("peak_cum", 0.0) - state.get("cum_pnl", 0.0)
    if arm and dd >= KILL_DRAWDOWN_PCT * BANKROLL:
        reasons.append(f"drawdown {dd:.2f} >= {KILL_DRAWDOWN_PCT:.0%} "
                       f"of bankroll from peak {state.get('peak_cum', 0.0):.2f}")
        for s in ("B", "C", "D"):
            if s not in state["disabled"]:
                state["disabled"].append(s)
    return (len(reasons) > 0, reasons)


def _kill_id(reason):
    """Stable id for a kill reason (LaDre 2026-10-06 P1: deduplicated
    kill-transition alert). Reason strings carry live values, so the id
    keys on the kill type, not the formatted text."""
    r = reason.lower()
    if r.startswith("day_pnl"):
        return "day"
    if "consecutive directional losses" in r:
        return "consec"
    if r.startswith("brier"):
        return "brier"
    if r.startswith("rolling-20"):
        return "rolling20"
    if r.startswith("drawdown"):
        return "drawdown"
    return "other:" + reason[:40]


def kill_transition_alert(state, reasons):
    """Deduplicated kill-state-transition alert (LaDre 2026-10-06 P1).

    Prints one KILL TRANSITION line per newly-active or newly-cleared kill
    and records the alerted ids in state["kill_alerted"], so a kill that
    stays active across ticks alerts exactly once. Returns
    (new_active_ids, cleared_ids).
    """
    prev = set(state.get("kill_alerted", []))
    cur = {_kill_id(r) for r in reasons}
    new_active = sorted(cur - prev)
    cleared = sorted(prev - cur)
    for kid in new_active:
        msg = next(r for r in reasons if _kill_id(r) == kid)
        print(f"KILL TRANSITION (active): {msg}")
    for kid in cleared:
        # Wording (LaDre 2026-10-06 review fix, STAGED, revised per review):
        # a cleared sleeve-disabling kill does NOT re-enable sleeves -- say
        # so on the line. The day kill disables nothing (trading resumes on
        # recovery / next UTC day), so it gets its own text.
        if kid in ("consec", "brier", "rolling20", "drawdown"):
            print(f"KILL TRANSITION (cleared; reason cleared but B/C/D remain "
                  f"disabled -- manual re-enable required): {kid}")
        elif kid == "day":
            print(f"KILL TRANSITION (cleared; day kill disables nothing -- "
                  f"trading resumes on recovery / next UTC day): {kid}")
        else:
            print(f"KILL TRANSITION (cleared): {kid}")
    if new_active or cleared:
        state["kill_alerted"] = sorted(cur)
    return new_active, cleared


def reverify_after_latency(trade, T, O, Te, snap, state):
    """Latency model (review 2026-10-06): stale quotes are what faster bots
    take first. Re-poll the book after LATENCY_MS and require the SAME sleeve
    to still clear its gate at the fresh ask. Returns
    (ok, new_trade, edge2, book2)."""
    time.sleep(LATENCY_MS / 1000.0)
    try:
        book2 = parse_book(get_book(T))
    except Exception:
        return False, None, None, None
    if book2.get("empty_book"):
        return False, None, None, book2
    tau2 = Te - now_s()
    sleeve = trade["sleeve"]
    t2 = None
    try:
        if sleeve == "A":
            t2 = decide_sleeve_a(book2)
        elif sleeve == "B":
            t2 = decide_sleeve_b(book2, snap["p_up"], tau2)
        elif sleeve == "C":
            t2 = decide_sleeve_c(book2, snap["p_up"], tau2)
        elif sleeve in ("D1", "D2"):
            lead2, vol2 = d_lead_vol(O, Te, state)
            t2 = decide_sleeve_d(book2, lead2, vol2, tau2, sleeve)
    except Exception:
        return False, None, None, book2
    if not t2 or t2.get("veto_candidate") or t2.get("fade"):
        return False, None, (t2.get("edge") if t2 else None), book2
    min_edge = {"A": 0.0, "B": B_EDGE_MIN, "C": 0.0,
                "D1": D_EDGE_MIN, "D2": D_EDGE_MIN}[sleeve]
    if t2.get("edge", 0) < min_edge:
        return False, None, t2.get("edge"), book2
    s1 = {l["side"] for l in trade["legs"]}
    s2 = {l["side"] for l in t2["legs"]}
    if s1 != s2:
        return False, None, t2.get("edge"), book2
    return True, t2, t2.get("edge"), book2


# ================= engine =================

def blotter_base(ev_slug, T, O, snap, book):
    return {
        "event_id": ev_slug, "Topen": T, "Tclose": T + 900, "O": O,
        "S": snap.get("S"), "tau": snap.get("tau"),
        **hash_stamp(),
        "fee_model": FEE_MODEL,
        "p_fair": round(snap["p_up"], 4) if snap.get("p_up") is not None else None,
        "ask_up": round(book["ask_up"][0], 4) if book.get("ask_up") else None,
        "ask_down": round(book["ask_down"][0], 4) if book.get("ask_down") else None,
    }


def run_once(sleeves=("A", "B", "D"), arm_kill=True):
    now = now_s()
    T = window_start(now)
    Te = T + 900
    tau = Te - now
    slug = event_slug(T)
    state = load_state()
    # A hand-edited or legacy kill_check_error key inside STATE would make
    # save_state skip silently while no sidecar flag exists. Drop it; the
    # sidecar is the only flag source.
    state.pop("kill_check_error", None)
    state.update(hash_stamp())
    # kill_check crash handling (LaDre 2026-10-06 review fix, v3 per
    # 2026-10-07 review): a crash writes the flag to a SIDECAR file and the
    # tick goes observe-only. A flagged tick never writes STATE; restored /
    # scaffolded values stay in memory. The human clears via --clear-kc-error
    # (optionally --restore-from-snapshot).
    kc_err = _kc_flag_read()
    if kc_err is None:
        try:
            _bad = _kc_bad_keys(state, _KC_LEDGER)  # same 4 keys save_state needs
            if _bad:
                raise ValueError(f"ledger keys missing/invalid: {','.join(_bad)}")
            halt, reasons = kill_check(state, arm_kill)
        except Exception as e:  # noqa: BLE001 - the whole point is to catch it
            restored, scaffolded, age = _kc_fill(state)
            kc_err = {"type": type(e).__name__, "message": str(e), "ts": now,
                      "restored": restored, "scaffolded": scaffolded,
                      "snapshot_age_s": age}
            persisted = _kc_flag_write(kc_err)
            msg = (f"KILL_CHECK_ERROR: {type(e).__name__}: {e} -- kill_check "
                   f"crashed; sticky observe-only until human clears; "
                   f"STATE not written")
            if restored:
                msg += (f"; {len(restored)} keys restored in-memory from "
                        f"last-good (age {age} s): {','.join(restored[:5])}")
            else:
                msg += "; no usable last-good snapshot"
            if scaffolded:
                msg += (f"; {len(scaffolded)} keys scaffolded in-memory only")
            if not persisted:
                msg += ("; WARNING flag file NOT written "
                        "(next tick will not be sticky)")
            print(msg)
            halt, reasons = True, ["kill_check_error"]
    else:
        _kc_fill(state)  # same in-memory restore-then-scaffold as crash tick
        print(f"KILL_CHECK_ERROR (sticky): {kc_err.get('type')}: "
              f"{kc_err.get('message')} -- observe-only until human clears")
        halt, reasons = True, ["kill_check_error(sticky)"]

    if kc_err is not None:
        state["kill_check_error"] = kc_err  # in-memory marker; save_state skips it
    else:
        if arm_kill:
            kill_transition_alert(state, reasons)
        save_state(state)
        _kc_snapshot_write(state)

    rec = {"ts": now, "type": "decision", "event_id": slug, "Topen": T,
           "sleeve": None, "reason": None,
           # Log-only B waterfall (STAGED): overwritten in the B branch when
           # B is evaluated; old readers ignore the key.
           "b_waterfall": {"evaluated": False,
                           "reason": "b_branch_not_reached"},
           "fee_model": FEE_MODEL,
           **hash_stamp()}

    migrate_decided(state)

    def mark_decided(which="ABC"):
        key = {"ABC": "decided_windows", "D1": "decided_d1",
               "D2": "decided_d2"}[which]
        lst = state.setdefault(key, [])
        if str(T) not in lst:
            lst.append(str(T))
            del lst[:-200]  # bounded: ~2 days of windows
        save_state(state)

    def abstain(reason, which="ABC"):
        rec["reason"] = reason
        append_jsonl(BLOTTER, rec)
        mark_decided(which)
        print(f"{slug}: ABSTAIN ({reason})")
        return rec

    def soft_fail(reason):
        # Transient failure (listing lag, gateway hiccup): don't write a
        # blotter rec and don't mark the window decided -- the next 2-min
        # tick retries from scratch.
        print(f"{slug}: RETRY LATER ({reason})")
        return None

    d1_band = D1_TAU_LO <= tau <= D1_TAU_HI
    d2_band = D2_TAU_LO <= tau <= D2_TAU_HI
    d1_open = ("D" in sleeves and not d_disabled(state, "D1") and d1_band
               and str(T) not in state.get("decided_d1", []))
    d2_open = ("D" in sleeves and not d_disabled(state, "D2") and d2_band
               and str(T) not in state.get("decided_d2", []))
    # Shadow observation runs even while D is disabled (LaDre 2026-10-06:
    # observation must not stop). The fill stays gated on enabled.
    d1_observe = ("D" in sleeves and d1_band
                  and str(T) not in state.get("decided_d1", []))
    d2_observe = ("D" in sleeves and d2_band
                  and str(T) not in state.get("decided_d2", []))

    if str(T) in state.get("decided_windows", []) and not (d1_open or d2_open):
        print(f"{slug}: SKIP (decision already recorded for window)")
        return None

    if any(p["Topen"] == T for p in state["open"]):
        return abstain("position already open for window")
    # A kill stops fills, never evidence (LaDre 2026-10-06 P1): while halted,
    # the tick still fetches the book, logs asks, queues the Brier intake and
    # runs D shadow observation. Every trade-taking branch below is gated on
    # not observe_only.
    observe_only = bool(halt)
    halt_reason = ("kill switch (observation only): " + "; ".join(reasons)
                   if halt else None)
    if tau <= 0:
        return abstain("window closed")

    # Event + book are independent (both keyed by T): fetch in parallel.
    # The pair is the tick's latency floor. The book result is discarded
    # when the window isn't listed yet.
    try:
        with ThreadPoolExecutor(max_workers=2) as ex:
            fev = ex.submit(get_event, T)
            fbook = ex.submit(get_book, T)
            ev = fev.result()
            try:
                book_raw = fbook.result()
            except Exception as be:
                book_raw = be
    except Exception as e:
        return soft_fail(f"event fetch failed: {e}")
    if not ev or not ev.get("markets"):
        return soft_fail("window not listed")
    terms = parse_terms(ev["markets"][0])
    O = terms["priceToBeat"]
    if O is None:
        return soft_fail("priceToBeat not published")
    if terms["index"] != "BRTI":
        return abstain(f"unexpected index {terms['index']}")

    if isinstance(book_raw, Exception):
        return soft_fail(f"book fetch failed: {book_raw}")
    try:
        book = parse_book(book_raw)
    except Exception as e:
        return soft_fail(f"book parse failed: {e}")
    # log observed asks (+depth) on every evaluated window (round 4, review
    # Step 4): future edge analysis needs market prices, not just model P.
    _qu, _qd = book.get("ask_up"), book.get("ask_down")
    rec["ask_up"] = [round(_qu[0], 4), _qu[1]] if _qu else None
    rec["ask_down"] = [round(_qd[0], 4), _qd[1]] if _qd else None
    if book.get("empty_book"):
        # Model-only calibration (2026-10-08): log p_fair/sigma even with no
        # book to trade against. Never fatal -- diagnostics must not kill
        # the tick.
        try:
            _msnap = fair_snapshot(T, O, now,
                                   sigma=_cached_sigma(state, T, now))
            _mdiag = _tick_diagnostics(state, T, now)
            _append_model_only({
                "ts": now, "event_id": slug, "T": T, "O": O,
                "tau": Te - now,
                "p_fair": _msnap.get("p_up"), "S": _msnap.get("S"),
                "S_source": _msnap.get("S_source"),
                "sigma": _msnap.get("sigma"),
                "rv_long": _mdiag.get("rv_long"),
                "rv_short": _mdiag.get("rv_short"),
                "floor_bound": _mdiag.get("floor_bound"),
                **hash_stamp()})
        except Exception:
            pass
        return abstain("empty book (NO ONE TO TRADE AGAINST)")

    try:
        snap = fair_snapshot(T, O, now, sigma=_cached_sigma(state, T, now))
    except Exception as e:
        return soft_fail(f"spot fetch failed: {e}")
    if snap["p_up"] is None:
        # Model-only calibration: sigma was computed even though p_fair
        # wasn't (e.g. tau<=0 edge). Log what we have.
        try:
            _mdiag2 = _tick_diagnostics(state, T, now)
            _append_model_only({
                "ts": now, "event_id": slug, "T": T, "O": O,
                "tau": Te - now,
                "p_fair": None, "S": snap.get("S"),
                "S_source": snap.get("S_source"),
                "sigma": snap.get("sigma"),
                "rv_long": _mdiag2.get("rv_long"),
                "rv_short": _mdiag2.get("rv_short"),
                "floor_bound": _mdiag2.get("floor_bound"),
                **hash_stamp()})
        except Exception:
            pass
        return abstain("fair value unavailable")

    # log fair values for later Brier scoring (traded or not)
    mkt_mid = mid_up(book)
    # Preserve the tau grid accumulated by earlier ticks this window: the
    # pending entry is rewritten every tick.
    _prev_pb = state["pending_brier"].get(str(T)) or {}
    # Fixed tau grid (2026-10-08, nearest-wins): this tick's snapshot
    # displaces a grid entry only if strictly closer to the grid point.
    _tau_grid = _fill_tau_grid(
        _prev_pb.get("tau_grid"), tau,
        {"p_fair": snap["p_up"], "mid_up": mkt_mid,
         "S": snap.get("S"), "sigma": snap.get("sigma")})
    _bid_up, _ask_up = book.get("bid_up"), book.get("ask_up")
    _bid_down, _ask_down = book.get("bid_down"), book.get("ask_down")
    state["pending_brier"][str(T)] = {
        "p_fair": snap["p_up"], "mid_up": mkt_mid, "tau": tau,
        # (D) model_v stamped at creation, carried through to scoring.
        "model_v": FAIRVALUE_MODEL_V,
        # (E) research fields; scored to scored_windows.jsonl at settle.
        # Diagnostics (rv_long/rv_short/...) are filled post-decision
        # (Fix 2, 2026-10-08): carry forward whatever an earlier tick
        # logged; None until the first post-decision fill.
        "event_id": slug, "O": O, "S": snap.get("S"),
        "S_source": snap.get("S_source"), "tau_at_score": tau,
        "rv_long": _prev_pb.get("rv_long"),
        "rv_short": _prev_pb.get("rv_short"),
        "sigma_used": snap.get("sigma"),
        "floor_bound": _prev_pb.get("floor_bound"),
        "zero_ret_share_15m": _prev_pb.get("zero_ret_share_15m"),
        "gapfill_share_15m": _prev_pb.get("gapfill_share_15m"),
        "bid_up": _bid_up[0] if _bid_up else None,
        "ask_up": _ask_up[0] if _ask_up else None,
        "bid_down": _bid_down[0] if _bid_down else None,
        "ask_down": _ask_down[0] if _ask_down else None,
        "depth_up": _ask_up[1] if _ask_up else None,
        "depth_down": _ask_down[1] if _ask_down else None,
        # (Fix A, 2026-10-08, per Claude): carry the shadow value forward
        # across the per-tick rebuild so a tick that never reaches
        # _shadow_fill doesn't wipe it. _shadow_fill overwrites with fresh
        # values when it runs.
        "shadow": _prev_pb.get("shadow"),
        "tau_grid": _tau_grid}
    # Log-only decision fields (LaDre 2026-10-06 review fix, STAGED): tau,
    # p_fair, S, sigma so the edge waterfall can be rebuilt from the blotter
    # later. Additive only -- old readers ignore unknown keys. Brier
    # snapshotting is untouched by this change.
    rec["tau"] = tau
    rec["p_fair"] = (round(snap["p_up"], 6)
                     if snap["p_up"] is not None else None)
    rec["S"] = snap.get("S")
    rec["sigma"] = snap.get("sigma")
    save_state(state)

    # Post-decision diagnostics accessor (Fix 2, 2026-10-08). Defined here
    # so every branch below can use it, but the venue fetches only run when
    # it is first CALLED -- always after that branch's decision is made.
    # Logging-only, memoized per tick; never raises; never touches signals,
    # sizing, or hashes.
    _post_diag_memo = {}

    def post_diag():
        if not _post_diag_memo:
            try:
                _d = _tick_diagnostics(state, T, now)
            except Exception:
                _d = {}
            _fb = _floor_bound_from(snap.get("sigma"),
                                    _d.get("rv_long"), _d.get("rv_short"))
            _post_diag_memo["d"] = _d
            _post_diag_memo["fb"] = _fb
            try:
                _pb = state["pending_brier"].get(str(T)) or {}
                _pb.update({"rv_long": _d.get("rv_long"),
                            "rv_short": _d.get("rv_short"),
                            "floor_bound": _fb,
                            "zero_ret_share_15m":
                                _d.get("zero_ret_share_15m"),
                            "gapfill_share_15m":
                                _d.get("gapfill_share_15m")})
                state["pending_brier"][str(T)] = _pb
                save_state(state)
            except Exception:
                pass
            # Shadow fill rides on post_diag (Fix A, 2026-10-08, per Claude):
            # memoized, so this runs once per tick on every path that calls
            # post_diag(), including veto/fade abstains. Never raises.
            try:
                _shadow_fill(T, tau, snap, state)
            except Exception:
                pass
        return _post_diag_memo["d"], _post_diag_memo["fb"]

    def d_veto(sub):
        """Winner's-curse re-poll for a D veto candidate.
        Returns (trade|None, abstain_reason|None)."""
        time.sleep(B_CURSE_MS)
        try:
            book2 = parse_book(get_book(T))
            lead2, vol2 = d_lead_vol(O, Te, state)
            td2 = decide_sleeve_d(book2, lead2, vol2, Te - now_s(), sub)
        except Exception:
            return None, "D veto re-poll failed"
        if td2 and td2.get("edge", 0) >= D_VETO_EDGE:
            return None, "winner's curse veto: >=15c edge persisted"
        if td2 and not td2.get("veto_candidate") and not td2.get("fade"):
            return td2, None
        return None, "D veto re-poll: edge faded"

    trade = None
    d_shadow = None  # observation-only record for a D trigger (round 4)

    def note_d_shadow(sub, lead_bp, vol_bp, td):
        """Build the shadow record for a D trigger. Never places a trade."""
        _t_lo, _t_hi, _l_lo, _l_hi, ask_cap, _p, _nc = _D_CFG[sub]
        side = "up" if (lead_bp or 0) > 0 else "down"
        q = book.get("ask_up") if side == "up" else book.get("ask_down")
        ask, depth = (round(q[0], 4), q[1]) if q else (None, None)
        return {"ts": now, "T": T, "event_id": slug, "sub": sub,
                "tau": tau, "lead_bp": lead_bp, "vol_bp": vol_bp,
                "regime": ("calm" if (vol_bp is not None
                                      and vol_bp < D_VOL_MEDIAN)
                           else "volatile"),
                "side": side, "ask": ask, "depth": depth,
                "ask_cap": ask_cap,
                "ask_cap_pass": bool(ask is not None and ask <= ask_cap),
                "edge": td.get("edge"),
                "trigger": ("fade" if td.get("fade")
                            else "veto_candidate" if td.get("veto_candidate")
                            else "trade"),
                "sig_D": SIG["D"], "frozen_at": dict(HASH_FROZEN_AT),
                "disposition": None, "latency_pass": None,
                "outcome": None, "hold": None}

    def flush_d_shadow(disposition, extra=None):
        nonlocal d_shadow
        if d_shadow is None:
            return
        d_shadow["disposition"] = disposition
        if extra:
            d_shadow.update(extra)
        lst = state.setdefault("d_shadow", [])
        lst.append(d_shadow)
        del lst[:-500]  # bounded working set; scored outcomes persist below
        save_state(state)
        d_shadow = None

    if "D" in sleeves:
        # Sleeve D first: validated lead-persistence edge outranks B/C.
        for sub, is_open, observe in (("D1", d1_open, d1_observe),
                                      ("D2", d2_open, d2_observe)):
            if trade is not None or not observe:
                continue
            lead_bp, vol_bp = d_lead_vol(O, Te, state)
            td = decide_sleeve_d(book, lead_bp, vol_bp, tau, sub)
            if td is None:
                continue  # no setup this tick; band stays open
            d_shadow = note_d_shadow(sub, lead_bp, vol_bp, td)
            if d_disabled(state, sub) or observe_only:
                flush_d_shadow("disabled_no_trade" if d_disabled(state, sub)
                               else "halt_no_trade")
                mark_decided(sub)
                continue  # observe only; never fill while disabled/halted
            if td.get("fade"):
                flush_d_shadow("fade_volatile")
                # (Fix A, 2026-10-08, per Claude): run post_diag so the
                # shadow fill rides along -- this path returns before the
                # main post-decision block. Post-decision only, no signal
                # impact.
                try:
                    post_diag()
                except Exception:
                    pass
                return abstain(f"fade: volatile regime ({sub})", which=sub)
            if td.get("veto_candidate"):
                _ve, _vside = td.get("edge"), (d_shadow or {}).get("side")
                td, reason = d_veto(sub)
                if reason:
                    flush_d_shadow("vetoed", {"veto_reason": reason})
                    # Veto record enrichment (2026-10-08, research only).
                    # Diagnostics are fetched here, AFTER the veto decision.
                    _vd2, _vfb2 = post_diag()
                    _vq = (book.get("ask_up") if _vside == "up"
                           else book.get("ask_down"))
                    rec.update(_veto_fields(
                        fav=_vside,
                        ask_fav=_vq[0] if _vq else None,
                        depth=_vq[1] if _vq else None, edge=_ve,
                        p_fair=snap["p_up"],
                        sigma_used=_vd2.get("sigma_used"),
                        floor_bound=_vfb2, S=snap.get("S"), O=O,
                        tau=tau))
                    return abstain(reason, which=sub)
            if td is not None:
                trade = td
    if trade is None and "A" in sleeves and "A" not in state["disabled"] \
            and not observe_only:
        trade = decide_sleeve_a(book)
    if trade is None and "B" in sleeves and "B" not in state["disabled"] \
            and not observe_only:
        # B gate waterfall (log-only, STAGED): observability for the abstain
        # funnel. decide_sleeve_b itself is untouched; this never affects
        # fills. Hardened per review: a bug in the waterfall must not kill
        # the tick.
        try:
            rec["b_waterfall"] = b_waterfall(book, snap["p_up"], tau)
        except Exception as e:  # noqa: BLE001 - log-only path, never fatal
            rec["b_waterfall"] = {"evaluated": False,
                                 "error": f"{type(e).__name__}: {e}"}
        tb = decide_sleeve_b(book, snap["p_up"], tau)
        if tb and tb.get("veto_candidate"):
            # winner's-curse check: re-poll after 300ms; persistence = veto
            time.sleep(B_CURSE_MS)
            try:
                book2 = parse_book(get_book(T))
                tb2 = decide_sleeve_b(book2, snap["p_up"], Te - now_s())
                if tb2 and tb2.get("edge", 0) >= B_VETO_EDGE:
                    # Veto record enrichment (2026-10-08, research only).
                    # Diagnostics are fetched here, AFTER the veto decision.
                    _vdiag, _vfb = post_diag()
                    _vbu, _vbd = book2.get("ask_up"), book2.get("ask_down")
                    _vfav = ("up" if _vbu and _vbd and _vbu[0] > _vbd[0]
                             else "down" if _vbu and _vbd else None)
                    _vq = (_vbu if _vfav == "up" else _vbd) if _vfav else None
                    rec.update(_veto_fields(
                        fav=_vfav,
                        ask_fav=_vq[0] if _vq else None,
                        depth=_vq[1] if _vq else None,
                        edge=tb2.get("edge"), p_fair=snap["p_up"],
                        sigma_used=_vdiag.get("sigma_used"),
                        floor_bound=_vfb, S=snap.get("S"), O=O,
                        tau=Te - now_s()))
                    return abstain("winner's curse veto: >=15c edge persisted")
                trade = tb2 if tb2 and not tb2.get("veto_candidate") else None
            except Exception:
                trade = None
        else:
            trade = tb
    else:
        # B branch not reached: record why, for the funnel (replaces the
        # generic default set at record creation).
        if observe_only:
            rec["b_waterfall"]["reason"] = "halted"
        elif "B" in state["disabled"]:
            rec["b_waterfall"]["reason"] = "b_disabled"
        elif "B" not in sleeves:
            rec["b_waterfall"]["reason"] = "b_not_in_sleeves"
        elif trade is not None:
            rec["b_waterfall"]["reason"] = (
                str(trade.get("sleeve", "?")).lower() + "_traded")
    if trade is None and "C" in sleeves and "C" not in state["disabled"] \
            and not observe_only:
        trade = decide_sleeve_c(book, snap["p_up"], tau)

    # Research diagnostics, computed AFTER the sleeve decisions (Fix 2,
    # 2026-10-08): the venue fetches (~1-3s on a cache miss) must never age
    # the book the decision was made on. post_diag() is defined up front
    # but only *called* after a decision; logging-only, memoized per tick;
    # never raises, never affects signals, sizing, or hashes.
    # (Fix C, 2026-10-08, per Claude): stamp the decision time so the
    # decision-to-repoll delay can be logged below. Restructuring
    # post_diag() after paper_fill is deferred -- the delay is measured
    # first.
    _decided_at = time.time()
    diag, floor_bound = post_diag()

    # 72h sigma shadow (2026-10-08, LaDre's word: "whatever Claude
    # suggests goes"). Post-decision ONLY: _shadow_fill now rides inside
    # post_diag() (Fix A) so veto/fade abstains get shadow values too.
    # It reuses the rows post_diag() just fetched -- no added API calls,
    # no decision-path latency. Never touches signals, sizing, or hashes.

    if trade is None:
        return abstain(halt_reason or "no sleeve gate passed")

    dsub = trade["sleeve"] if trade["sleeve"] in ("D1", "D2") else "ABC"
    edge_first = trade.get("edge")
    lat = {"latency_ms": 0, "edge_first": edge_first, "edge_repoll": None,
           "latency_pass": None}
    if LATENCY_MS > 0:
        # (Fix C, 2026-10-08, per Claude): log the actual
        # decision-to-repoll delay. post_diag() runs between the decision
        # and this re-poll; on a cache-miss tick the book is 1-3s older
        # than the signed model, biasing paper toward fewer fills.
        _repoll_delay_s = round(time.time() - _decided_at, 3)
        print(f"{slug}: decision-to-repoll delay={_repoll_delay_s}s")
        ok, trade2, edge2, book2 = reverify_after_latency(
            trade, T, O, Te, snap, state)
        lat.update({"latency_ms": LATENCY_MS, "edge_repoll": edge2,
                    "latency_pass": bool(ok)})
        if not ok:
            rec["reason"] = (f"latency: edge faded on re-poll "
                             f"(first={edge_first}, re-poll={edge2})")
            rec.update(lat)
            append_jsonl(BLOTTER, rec)
            mark_decided(dsub)
            flush_d_shadow("latency_faded",
                           {"edge_first": edge_first, "edge_repoll": edge2,
                            "latency_pass": False})
            print(f"{slug}: ABSTAIN (latency: edge faded on re-poll)")
            return rec
        trade, book = trade2, book2

    filled, unfilled = paper_fill(trade["legs"], book)
    if not filled:
        rec["reason"] = "no fill available at decision asks"
        append_jsonl(BLOTTER, rec)
        mark_decided(dsub)
        flush_d_shadow("no_fill")
        print(f"{slug}: ABSTAIN (no fill)")
        return rec

    # lead_z = ln(S/O)/(sigma*sqrt(tau)): standardized moneyness, for
    # research (2026-10-08). Guards: never divide by zero / log of garbage.
    _lz = None
    _s, _o, _sg = snap.get("S"), O, snap.get("sigma")
    if (_s and _o and _s > 0 and _o > 0 and _sg and _sg > 0
            and tau and tau > 0):
        _lz = math.log(_s / _o) / (_sg * math.sqrt(tau))

    pos = {**blotter_base(slug, T, O, snap, book),
           "ts": now, "type": "open", "sleeve": trade["sleeve"],
           "edge": trade.get("edge"),
           "filled_legs": filled, "unfilled_legs": unfilled,
           "sigma": snap.get("sigma"),
           # research fields (2026-10-08, additive)
           "mid_up": mkt_mid,
           "rv_long": diag.get("rv_long"),
           "rv_short": diag.get("rv_short"),
           "floor_bound": floor_bound,
           "lead_z": _lz,
           **lat}
    state["open"].append(pos)
    append_jsonl(BLOTTER, pos)
    mark_decided(dsub)
    flush_d_shadow("traded", {"filled": [(l["side"], l["filled"], l["price"])
                                         for l in filled],
                              "latency_pass": lat.get("latency_pass")})
    desc = ", ".join(f"{l['side']} {l['filled']}@{l['price']:.4f}" for l in filled)
    print(f"{slug}: PAPER {trade['sleeve']} -> {desc} edge={trade.get('edge')}")
    if unfilled:
        print(f"  unfilled: {unfilled}")
    return pos


def _num(x):
    """Fix 2 (Claude review): safe number parse for settlement inputs.
    Returns the float when x is a finite number above 0 (and not a bool),
    else None. A malformed published price is data corruption, never a
    value to settle on."""
    if isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return v


def _load_venue_truth():
    """Load research/venue_truth.jsonl into {T: row}. Never raises;
    returns {} on any problem (caller falls back)."""
    out = {}
    try:
        with open(os.path.join(HERE, "research", "venue_truth.jsonl")) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                try:
                    out[int(r["T"])] = r
                except (KeyError, TypeError, ValueError):
                    pass
    except FileNotFoundError:
        pass
    return out


def _pt1_kill_pnl(p, venue_truth):
    """D6 (Claude v4, LaDre-signed 2026-10-13): pt1 point-rule P&L for a
    settling position. The kills read the pt1 ledger.

    Outcome from the venue's published point prices
    (venue_truth: end_price_pt >= start_price_pt, tie -> up); P&L from the
    position's filled legs with the same leg math as settle().

    Returns (pnl, outcome_str, src). If venue_truth has no usable row for
    the window, returns (None, None, None) and the caller falls back to
    the Part-1 P&L (pt1-equivalent via next_pb; verified 46/46 on recent
    settles). Pure function: no state, no I/O beyond its args."""
    try:
        t = int(p.get("Topen"))
    except (TypeError, ValueError):
        return (None, None, None)
    vt = (venue_truth or {}).get(t)
    if not vt:
        return (None, None, None)
    try:
        end_px = vt.get("end_price_pt")
        start_px = vt.get("start_price_pt")
        if end_px is None or start_px is None:
            return (None, None, None)
        oc_up = float(end_px) >= float(start_px)
    except (TypeError, ValueError):
        return (None, None, None)
    pnl = 0.0
    for leg in p.get("filled_legs", []):
        try:
            win = (leg["side"] == "up") == oc_up
            pnl += float(leg["filled"]) * (1.0 if win else 0.0) \
                - float(leg["cost"])
        except (TypeError, ValueError, KeyError):
            continue
    return (round(pnl, 2), "up" if oc_up else "down", "venue_truth")


def _migrate_pt1_kill_ledgers(s):
    """D6 one-time migration (Claude v4, LaDre-signed 2026-10-13): rebuild
    the kill P&L ledgers from the pt1 point-rule ledger.

    The kills were fed the recorded ledger (pre-Part-1 s_avg settles),
    which over-triggered the drawdown kill (recorded max_dd 108.07 vs
    pt1 74.13; threshold 100). This replays research/pnl_point_rule.jsonl
    through _book_settle_ledgers in settled_at order so the kill inputs
    equal the pt1 ledger. B/C/D stay in state["disabled"] regardless --
    clearing a kill never re-enables a sleeve.

    Idempotent via state["_pt1_kill_migrated"]. Fails safe: on any
    problem (missing file, coverage gap, backup failure) it logs loudly
    and leaves the existing ledgers untouched. Returns s."""
    if not isinstance(s, dict) or s.get("_pt1_kill_migrated"):
        return s
    try:
        with open(os.path.join(HERE, "research",
                               "pnl_point_rule.jsonl")) as f:
            pt1 = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        print("D6-MIGRATE: pnl_point_rule.jsonl missing "
              "-- kill ledgers left as-is")
        return s
    except ValueError:
        print("D6-MIGRATE: pnl_point_rule.jsonl corrupt "
              "-- kill ledgers left as-is")
        return s
    try:
        with open(BLOTTER) as f:
            n_blotter = sum(1 for line in f if line.strip()
                            and json.loads(line).get("type") == "settle")
    except FileNotFoundError:
        n_blotter = 0
    except ValueError:
        n_blotter = 0
    n_pt1 = sum(1 for r in pt1 if r.get("point_pnl") is not None)
    if n_pt1 < n_blotter:
        print(f"D6-MIGRATE: coverage gap (pt1 {n_pt1} < blotter "
              f"{n_blotter}) -- kill ledgers left as-is")
        return s
    try:
        with open(STATE + ".pre-pt1-migrate.bak", "w") as f:
            json.dump(s, f, indent=1)
    except Exception as e:
        print(f"D6-MIGRATE: backup failed ({e}) -- kill ledgers left as-is")
        return s
    s["settle_pnls"] = []
    s["cum_pnl"] = 0.0
    s["peak_cum"] = 0.0
    s["day_pnls"] = {}
    s["consec_losses"] = 0
    n = 0
    for r in sorted(pt1, key=lambda r: (r.get("settled_at") or 0,
                                       r.get("T") or 0)):
        pp = r.get("point_pnl")
        if pp is None:
            continue
        try:
            te = int(r["T"]) + 900
            ppf = float(pp)
        except (TypeError, ValueError, KeyError):
            continue
        _book_settle_ledgers(s, r.get("sleeve"), te, ppf)
        n += 1
    s["_pt1_kill_migrated"] = True
    print(f"D6-MIGRATE: kill ledgers rebuilt from pt1 ledger ({n} trades); "
          f"cum={s['cum_pnl']:+.2f} peak={s['peak_cum']:.2f} "
          f"drawdown={s['peak_cum'] - s['cum_pnl']:.2f}")
    return s


def _book_settle_ledgers(state, sleeve, Te, pnl):
    """Book a settled trade's P&L into the kill ledgers. Shared by the
    normal settle path and the idempotency-guard replay (fix 4) so the two
    can never drift. Never raises."""
    # Fix A: day_pnl is DERIVED (_day_pnl reads this bucket). Book to
    # the window's Tclose day via the single today_key() function+tz.
    # (Fix 6 note: the 23:45-00:00 UTC window's Tclose falls after
    # midnight, so it books to the next day's bucket -- consistent with
    # the old day_pnl behaviour, which booked on settle day.)
    try:
        dk = today_key(int(Te))
    except (TypeError, ValueError):
        dk = None
    if dk is not None:
        dpd = state.setdefault("day_pnls", {})
        if not isinstance(dpd, dict):
            print("DAY_PNLS_CORRUPT: day_pnls not a dict -- rebuilding")
            dpd = state["day_pnls"] = {}
        _cur = dpd.get(dk, 0.0)
        if isinstance(_cur, bool) or not isinstance(_cur, (int, float)):
            print(f"DAY_PNLS_CORRUPT {dk}: bucket not a number -- rebuilding")
            _cur = 0.0
        try:
            _pnl = float(pnl)
        except (TypeError, ValueError):
            _pnl = 0.0
        dpd[dk] = round(_cur + _pnl, 2)
        _trim_day_pnls(state)  # Fix D: keep the last 14 days
    try:
        _pnl = float(pnl)
    except (TypeError, ValueError):
        _pnl = 0.0
    # rolling / drawdown ledgers (review 2026-10-06)
    sp = state.setdefault("settle_pnls", [])
    if isinstance(sp, list):
        sp.append(round(_pnl, 2))
        del sp[:-60]
    try:
        state["cum_pnl"] = round(float(state.get("cum_pnl", 0.0)) + _pnl, 2)
    except (TypeError, ValueError):
        state["cum_pnl"] = round(_pnl, 2)
    try:
        state["peak_cum"] = max(float(state.get("peak_cum", 0.0)),
                                float(state.get("cum_pnl", 0.0)))
    except (TypeError, ValueError):
        pass
    if sleeve in ("B", "C", "D1", "D2"):
        try:
            _cl = int(state.get("consec_losses", 0))
        except (TypeError, ValueError):
            _cl = 0
        state["consec_losses"] = _cl + 1 if _pnl < 0 else 0
    # D-specific halt: sub-sleeve live hold rate below breakeven over
    # the last D_HALT_N settled trades -> disable that sub-sleeve.
    if sleeve in ("D1", "D2"):
        dh = state.setdefault("d_hold", {}).setdefault(sleeve, [])
        if isinstance(dh, list):
            dh.append(1 if _pnl >= 0 else 0)
            del dh[:-D_HALT_N]
            be = D1_BREAKEVEN if sleeve == "D1" else D2_BREAKEVEN
            if len(dh) >= D_HALT_N and sum(dh) / len(dh) < be:
                if sleeve not in state.get("disabled", []):
                    state.setdefault("disabled", []).append(sleeve)
                    print(f"D halt: {sleeve} hold "
                          f"{sum(dh) / len(dh):.1%} < {be:.1%} over "
                          f"{len(dh)} -> disabled")


def _trade_settle_decision(Topen, Tclose, p_O, s_avg, caps, now):
    """Part 1 (2026-10-09, LaDre-signed): pick a trade's settlement price.

    The venue's rules define the next window's published price-to-beat as
    the settlement reference, so trades settle on it -- not on our own
    feed average (which runs ~26s off). Pure function: no state, no I/O.

    Returns (action, settle_px, settle_source, own_cap, cap_mismatch):
      action "settle", settle_source "next_pb":        next_pb published,
                                                      settle on it now.
      action "settle", settle_source "s_avg_fallback": next_pb still missing
        after TRADE_SETTLE_TTL: fall back to s_avg, flagged and counted by
        the caller.
      action "wait": next_pb not yet published and the TTL not reached:
        leave the position open; a later settle run retries.
      action "stuck": TTL passed and NEITHER price exists (or the window
        key itself is malformed): loud, counted, kept waiting -- never a
        silent infinite wait.
      own_cap: the window's own price-to-beat used for the outcome
        comparison -- the PUBLISHED value from caps when present
        (checklist 2: p["O"] must equal it; a mismatch is reported by the
        caller, never silent).
      cap_mismatch: True when p_O differed from the published value (or
        the published value was malformed and p_O was used instead).

    Fix 2: each input is parsed separately. A malformed own price-to-beat
    is ignored in favour of p_O (flagged); a malformed next price-to-beat
    is treated as missing (normal TTL path). The catch-all survives only
    for a malformed Topen/p_O, and reports stuck, not silent waiting.

    Experiment isolation: the Brier/shadow paths keep receiving s_avg
    explicitly (Part 2, post-latch). Trade settlement never feeds them.
    """
    try:
        t = int(Topen)
        Te = int(Tclose)
    except (TypeError, ValueError):
        return ("stuck", None, None, None, False)
    own = _num(p_O)
    if own is None:
        return ("stuck", None, None, None, False)
    own_cap = round(own, 2)
    cap_mismatch = False
    pub = (caps or {}).get(str(t))
    if pub is not None:
        pv = _num(pub)
        if pv is None:
            # Malformed published own cap: use p_O, flag it loudly.
            cap_mismatch = True
        elif round(pv, 2) != own_cap:
            cap_mismatch = True
            own_cap = round(pv, 2)  # published value wins
    nxt_raw = (caps or {}).get(str(t + 900))
    nxt = _num(nxt_raw) if nxt_raw is not None else None
    past_ttl = now - Te > TRADE_SETTLE_TTL
    if nxt is not None:
        return ("settle", round(nxt, 2), "next_pb", own_cap, cap_mismatch)
    s = _num(s_avg) if s_avg is not None else None
    if s is None:
        # No next_pb and no usable s_avg: stuck past the TTL, waiting in it.
        return (("stuck", None, None, own_cap, cap_mismatch) if past_ttl
                else ("wait", None, None, own_cap, cap_mismatch))
    if past_ttl:
        return ("settle", round(s, 2), "s_avg_fallback", own_cap, cap_mismatch)
    return ("wait", None, None, own_cap, cap_mismatch)


def _fetch_next_pb_direct(t_next, timeout=20):
    """Fix F (Claude review): one direct venue fetch for window t_next's
    price-to-beat. Single attempt, bounded timeout, no retries. Returns the
    float price-to-beat, or None. Never raises; on anything odd (wrong
    window, null/string/non-finite/non-positive value) returns None so the
    caller writes nothing and the position waits for the next run."""
    try:
        from markets import get_event, parse_terms, event_slug
        try:
            t_next = int(t_next)
        except (TypeError, ValueError):
            return None
        ev = get_event(t_next, retries=0, timeout=timeout)
        if not ev or ev.get("slug") != event_slug(t_next):
            return None  # wrong window (or nothing listed): write nothing
        if not ev.get("markets"):
            return None
        terms = parse_terms(ev["markets"][0])
        if terms.get("index") != "BRTI":
            return None
        pb = terms.get("priceToBeat")
        if isinstance(pb, bool) or not isinstance(pb, (int, float)):
            return None  # null, string, or other non-number: write nothing
        if not math.isfinite(pb) or pb <= 0:
            return None
        return float(pb)
    except Exception:
        return None


def settle():
    """Settle matured paper positions.

    Part 1 (2026-10-09, LaDre-signed): trades settle on the venue's
    published settlement reference (the next window's price-to-beat),
    waiting for it through the normal settle cadence and falling back to
    the BRTI avg60(Te) only after TRADE_SETTLE_TTL. Brier and shadow
    labels still use avg60 (Part 2, post-latch).
    """
    from brti import fetch_1s, avg60
    now = now_s()
    # A flagged tick never writes STATE (v3): settle mutates the risk
    # ledgers, so it must not run on a corrupt/flagged state.
    kc_err = _kc_flag_read()
    if kc_err is not None:
        print(f"SETTLE_SKIPPED (kill_check_error sticky): {kc_err.get('type')}: "
              f"{kc_err.get('message')} -- settle skipped until human clears")
        return
    state = load_state()
    state.pop("kill_check_error", None)  # stray key must not disable saves
    # (Fix B lives in load_state(): day_pnls backfilled from legacy day_pnl,
    # keyed by state["day"].)
    # An invalid ledger with no flag (flag write failed, or nothing flagged
    # yet): settle touches state["open"] directly and would KeyError mid-run.
    # Skip; the next run_once flags it properly.
    _bad = _kc_bad_keys(state, _KC_LEDGER)
    if _bad:
        print(f"SETTLE_SKIPPED (ledger keys missing/invalid): {','.join(_bad)}")
        return
    state.update(hash_stamp())
    # Brier for EVERY closed window, traded or not (review 2026-10-06).
    # Runs on every settle tick, even when no position matured — previously
    # this block sat after the early return below and never executed.
    # pending_brier is logged for all evaluated windows; previously only
    # traded windows were ever scored. Drain the oldest closed windows each
    # run (bounded) so the sample grows to the full evaluation history.
    try:
        caps = {}
        with open(os.path.join(HERE, "captures.jsonl")) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("published_priceToBeat"):
                    caps[str(r["T"])] = r["published_priceToBeat"]
    except FileNotFoundError:
        caps = {}
    # D6 (Claude v4, LaDre-signed 2026-10-13): the kills read the pt1
    # ledger. Load venue_truth once per settle run for the pt1 outcome.
    _venue_truth = _load_venue_truth()
    # Settlement integrity: drain queued cross-checks now that caps are
    # fresh (v2 — deferred so next_pb has time to be published).
    _settle_check_drain(state, caps, now)
    closed = sorted(int(t) for t in state.get("pending_brier", {})
                    if int(t) + 900 <= now - 5 and str(t) in caps)
    for t in closed[:BRIER_DRAIN_PER_RUN]:
        Te = t + 900
        try:
            candles = fetch_1s(Te - 61, Te)
            s_avg = avg60(candles, Te)
        except Exception:
            continue
        # Settlement forensics (2026-10-09): logging only, never raises.
        _forensics_capture("brier", t, candles, s_avg, now)
        pb = state["pending_brier"].pop(str(t), None)
        if s_avg is None or not pb:
            continue
        if pb.get("p_fair") is None or pb.get("mid_up") is None:
            continue
        # (D)+(E): score via the pure helper; model_v carried from the
        # pending entry; full research record -> append-only jsonl.
        bw_entry, research = _score_window_record(
            t, pb, s_avg, caps[str(t)], now)
        _append_scored_windows(research)
        state["brier_windows"].append(bw_entry)
        # Settlement integrity check (review 2026-10-08): queue for
        # deferred cross-check; drained once next_pb is published.
        _settle_check_enqueue(state, t, s_avg, caps[str(t)], now)
    _trim_brier_windows(state)
    if closed:
        print(f"brier drain: scored {min(len(closed), BRIER_DRAIN_PER_RUN)} "
              f"closed windows (total scored: {len(state['brier_windows'])})")
    # Sleeve D shadow scoring (round 4): settle closed shadow triggers so
    # hold rates are measured on unseen windows with observed asks.
    n_sh = 0
    for sh in state.get("d_shadow", []):
        if sh.get("hold") is not None:
            continue
        t = sh["T"]
        if t + 900 > now - 5 or str(t) not in caps:
            continue
        Te_sh = t + 900
        try:
            candles = fetch_1s(Te_sh - 61, Te_sh)
            s_avg = avg60(candles, Te_sh)
        except Exception:
            continue
        # Settlement forensics (2026-10-09): logging only, never raises.
        _forensics_capture("d_shadow", t, candles, s_avg, now)
        if s_avg is None:
            continue
        oc_up = round(s_avg, 2) >= round(caps[str(t)], 2)
        sh["outcome"] = "up" if oc_up else "down"
        sh["hold"] = 1 if (sh["side"] == "up") == oc_up else 0
        # Settlement integrity check (review 2026-10-08): D-shadow uses the
        # same s_avg derivation — queue it too (one flip counts most here).
        _settle_check_enqueue(state, t, s_avg, caps[str(t)], now)
        n_sh += 1
    if n_sh:
        print(f"D shadow: scored {n_sh} closed triggers "
              f"(total scored: {sum(1 for sh in state['d_shadow'] if sh.get('hold') is not None)})")
    # Fix 4 (Claude review): idempotency guard. append_jsonl(BLOTTER, p)
    # runs before save_state(); a crash between the two would otherwise
    # settle the same position twice (duplicate blotter line, double-counted
    # toward gate v2). Any matured position already in the blotter is
    # removed from open without being appended again -- and its ledger
    # effects are replayed from the blotter record, exactly once, so the
    # kill inputs are correct (the crashed save never persisted them).
    # Nit 2 (Claude review): skip the blotter scan when nothing is open --
    # the file grows forever and this runs every minute.
    _settled = {}
    if state["open"]:
        try:
            with open(BLOTTER) as _bf:
                for _line in _bf:
                    try:
                        _r = json.loads(_line)
                    except ValueError:
                        continue
                    if _r.get("type") == "settle" and _r.get("Topen") is not None:
                        _settled[(_r.get("event_id"), _r.get("sleeve"),
                                  _r.get("Topen"))] = _r
        except FileNotFoundError:
            pass
    for p in list(state["open"]):
        _key = (p.get("event_id"), p.get("sleeve"), p.get("Topen"))
        _rec = _settled.get(_key)
        if _rec is None:
            continue
        print(f"RESETTLE_SKIPPED {p.get('event_id')}: already in blotter; "
              f"replaying ledgers once")
        # D6: replay the pt1 P&L (stored at settle time); pre-D6 blotter
        # rows without pnl_pt1 fall back to pnl_after_fees.
        _book_settle_ledgers(state, _rec.get("sleeve"), _rec.get("Tclose"),
                             _rec.get("pnl_pt1",
                                      _rec.get("pnl_after_fees", 0.0)))
        # Nit 3 (Claude review): the replay path also clears the window
        # from the stuck set -- it settled.
        _ss = state.get("settle_stuck_stats")
        if isinstance(_ss, dict) and isinstance(_ss.get("windows"), list):
            _rt = _rec.get("Topen")
            if _rt in _ss["windows"]:
                _ss["windows"].remove(_rt)
        state["open"].remove(p)
    matured = [p for p in state["open"] if p["Tclose"] <= now - 5]
    if not matured:
        print("nothing to settle")
        save_state(state)
        if _kc_flag_read() is None:
            _kc_snapshot_write(state)
        return
    t_min = min(p["Tclose"] for p in matured) - 60
    t_max = max(p["Tclose"] for p in matured)
    # Fix 1 (Claude review): settlement no longer depends on candles. A
    # failed 1-second feed must not block settling on the published
    # price-to-beat (possibly forever).
    try:
        candles = fetch_1s(t_min, t_max)
    except Exception as e:
        print(f"settle: candle fetch failed ({e}); "
              f"settling on next_pb where available")
        candles = []
    # Fix E (Claude review): at most ONE direct fetch per settle run -- the
    # 20s timeout is the whole budget (T_SETTLE is 50s). Other waiting
    # positions retry on the next run; a partial kill mid-settle with
    # partial writes is worse than waiting one more tick.
    _fetch_attempted = False
    for p in matured:
        t_next = p["Topen"] + 900
        if (not _fetch_attempted and str(t_next) not in caps
                and now - p["Tclose"] > 120):
            _fetch_attempted = True
            pb = _fetch_next_pb_direct(t_next)
            if pb is not None:
                caps[str(t_next)] = pb
                try:
                    from common import append_jsonl as _aj
                    from markets import event_slug as _slug
                    # Fix F: exact capture-line shape (T, slug,
                    # published_priceToBeat, captured_at) -- nothing extra.
                    _aj(os.path.join(HERE, "captures.jsonl"),
                        {"T": t_next, "slug": _slug(t_next),
                         "published_priceToBeat": pb,
                         "captured_at": now})
                except Exception:
                    pass
                print(f"{p['event_id']}: next_pb for T={t_next} fetched "
                      f"directly ({pb:.2f})")
    # Checklist 7: process matured positions in Tclose order so the
    # kill ledgers (consec_losses, roll-20) see them chronologically.
    for p in sorted(matured, key=lambda p: p["Tclose"]):
        Te = p["Tclose"]
        # Fix 1: s_avg may be None (dead candle feed); the settlement
        # decision handles None and settles on next_pb regardless.
        s_avg = avg60(candles, Te) if candles else None
        # Part 1 (2026-10-09, LaDre-signed): settle the TRADE on the
        # venue's published settlement reference (next window's
        # price-to-beat). The Brier backstop below still scores the window
        # on s_avg (Part 2, post-latch) -- trade settlement does not feed
        # the shadow's Brier.
        action, settle_px, settle_source, own_cap, cap_mismatch = \
            _trade_settle_decision(p["Topen"], Te, p["O"], s_avg, caps, now)
        if cap_mismatch:
            # Checklist 2: O must equal the published price-to-beat --
            # never a silent mismatch; the published value wins.
            # Nit 1 (Claude review): when the published value itself is
            # malformed, say so instead of printing "!= published X".
            _pub_raw = caps.get(str(p["Topen"]))
            if _pub_raw is not None and _num(_pub_raw) is None:
                print(f"CAP_MISMATCH {p['event_id']}: published value "
                      f"malformed, using O={p['O']}")
            else:
                print(f"CAP_MISMATCH {p['event_id']}: position O={p['O']} != "
                      f"published {own_cap:.2f} -- using published")
        if action == "wait":
            print(f"{p['event_id']}: next_pb not yet published, "
                  f"deferring trade settle")
            continue
        if action == "stuck":
            # Fix 1: TTL passed and NEITHER price exists. Never silent:
            # loud line, counted, kept waiting (a later next_pb or a
            # recovered feed still settles it).
            print(f"SETTLE_STUCK {p['event_id']}: next_pb missing and no "
                  f"s_avg after TTL -- waiting, counted")
            ss = state.setdefault("settle_stuck_stats", {"windows": []})
            if isinstance(ss.get("windows"), list) and \
                    p["Topen"] not in ss["windows"]:
                ss["windows"].append(p["Topen"])
            continue
        # Fix 3: forensics captures the trade path only on settle -- a
        # waiting position must not write ~30 identical lines per run.
        _forensics_capture("trade", p["Topen"], candles, s_avg, now)
        # Settlement integrity check (review 2026-10-08): queue for
        # deferred cross-check; runs before the outcome is decided but
        # never affects it. Only when s_avg exists.
        if s_avg is not None:
            _settle_check_enqueue(state, p["Topen"], s_avg, p["O"], now)
        if settle_source == "s_avg_fallback":
            fb = state.setdefault("settle_fallback_stats",
                                  {"count": 0, "windows": []})
            fb["count"] = fb.get("count", 0) + 1
            fb["windows"].append(p["Topen"])
            del fb["windows"][:-20]
            print(f"{p['event_id']}: next_pb missing after TTL "
                  f"({TRADE_SETTLE_TTL}s), s_avg fallback "
                  f"(fallback #{fb['count']})")
        outcome_up = settle_px >= own_cap  # tie settles Up (venue rule)
        pnl = 0.0
        for leg in p["filled_legs"]:
            win = (leg["side"] == "up") == outcome_up
            pnl += leg["filled"] * (1.0 if win else 0.0) - leg["cost"]
        # D6 (Claude v4, LaDre-signed 2026-10-13): the kills read the pt1
        # ledger. pt1 outcome from venue_truth when available; otherwise
        # the Part-1 P&L (pt1-equivalent via next_pb, verified 46/46).
        _pt1 = _pt1_kill_pnl(p, _venue_truth)
        if _pt1[0] is None:
            pt1_pnl, pt1_outcome, pt1_src = round(pnl, 2), \
                ("up" if outcome_up else "down"), "part1_equiv"
        else:
            pt1_pnl, pt1_outcome, pt1_src = _pt1
        p.update({"type": "settle",
                  "brti_settle": round(s_avg, 2) if s_avg is not None else None,
                  "settle_px": settle_px, "settle_source": settle_source,
                  "gap": (round(abs(settle_px - round(s_avg, 2)), 2)
                          if s_avg is not None else None),
                  "outcome": "up" if outcome_up else "down",
                  "pnl_after_fees": round(pnl, 2),
                  "pnl_pt1": pt1_pnl, "pt1_outcome": pt1_outcome,
                  "pt1_src": pt1_src,
                  "settled_at": now})
        append_jsonl(BLOTTER, p)
        state["open"].remove(p)
        # A stuck position that later settles leaves the stuck set.
        _ss = state.get("settle_stuck_stats")
        if isinstance(_ss, dict) and isinstance(_ss.get("windows"), list) \
                and p["Topen"] in _ss["windows"]:
            _ss["windows"].remove(p["Topen"])
        # Kill ledgers (shared with the idempotency-guard replay, fix 4).
        # D6: the kills read the pt1 ledger -- book pt1_pnl, not the
        # Part-1 pnl (they agree via next_pb in the common case).
        _book_settle_ledgers(state, p["sleeve"], Te, pt1_pnl)
        # Brier tracking (backstop: the drain above usually scored this
        # window already and popped its pending entry). Only when s_avg
        # exists -- otherwise the pending entry stays for the drain.
        pb = None
        if s_avg is not None:
            pb = state["pending_brier"].pop(str(p["Topen"]), None)
        if pb and pb.get("p_fair") is not None \
                and pb.get("mid_up") is not None:
            bw_entry, research = _score_window_record(
                p["Topen"], pb, s_avg, p["O"], now)
            _append_scored_windows(research)
            state["brier_windows"].append(bw_entry)
            _trim_brier_windows(state)
        print(f"{p['event_id']}: settled {p['outcome']} "
              f"@ {settle_px:.2f} [{settle_source}]  pnl={pnl:+.2f} ({p['sleeve']})")
    save_state(state)
    if _kc_flag_read() is None:
        _kc_snapshot_write(state)


def report():
    state = load_state()
    _now = int(time.time())
    for _key, _label in (("last_run_once_ok", "run-once"),
                         ("last_settle_ok", "settle")):
        _ts = state.get(_key)
        if _ts:
            _age = _now - int(_ts)
            _iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(_ts)))
            _flag = "  <-- STALE (>3min)" if _age > 180 else ""
            print(f"  health: last {_label}: {_iso} ({_age}s ago){_flag}")
        else:
            print(f"  health: last {_label}: never recorded  <-- STALE")
    print(f"bankroll(paper): {BANKROLL:.2f}  day_pnl: {_day_pnl(state):+.2f} "
          f"({today_key()})")
    print(f"disabled sleeves: {state.get('disabled') or 'none'}  "
          f"consec_losses: {state.get('consec_losses',0)}")
    mm = state.get("settle_mismatches", [])
    st = state.get("settle_check_stats", {})
    qlen = len(state.get("settle_check_queue", {}))
    fb = state.get("settle_fallback_stats", {})
    ss = state.get("settle_stuck_stats", {})
    print(f"settle integrity: checked={st.get('checked',0)} "
          f"expired={st.get('expired',0)} drift={st.get('drift',0)} "
          f"flips={st.get('flips',0)} gap>1c={st.get('gap_gt_1c',0)} "
          f"max_gap=${st.get('max_gap',0.0):.2f} "
          f"errors={st.get('errors',0)} queued={qlen} "
          f"s_avg_fallbacks={fb.get('count',0)} "
          f"stuck={len(ss.get('windows', [])) if isinstance(ss.get('windows'), list) else 0} "
          f"(tol ${SETTLE_CHECK_TOL:.2f})"
          + (f"  latest T={mm[-1]['T']} gap=${mm[-1]['gap']:.2f}"
             f"{' FLIP' if mm[-1].get('flip') else ''}" if mm else ""))
    print(f"open positions: {len(state['open'])}")
    bw = state.get("brier_windows", [])
    if bw:
        buckets = {(90, 180): [], (180, 360): [], (360, 720): []}
        for w in bw:
            for (lo, hi), lst in buckets.items():
                if lo <= w["tau"] < hi:
                    lst.append(w)
        print(f"\nBrier over last {len(bw)} settled windows:")
        for (lo, hi), lst in buckets.items():
            if lst:
                bm = sum(w["brier_model"] for w in lst) / len(lst)
                bk = sum(w["brier_mkt"] for w in lst) / len(lst)
                print(f"  tau {lo}-{hi}s: n={len(lst)} model={bm:.4f} "
                      f"market={bk:.4f} {'MODEL WINS' if bm < bk else 'MARKET WINS'}")
        # paired interval on (model - market), review 2026-10-06.
        # Same window set as kill(c) and gate [3] (2026-10-08).
        _bwc = _brier_windows_current(state)
        diffs = [w["brier_model"] - w["brier_mkt"] for w in _bwc]
        n = len(diffs)
        if n > 1:
            m = sum(diffs) / n
            sd = math.sqrt(sum((x - m) ** 2 for x in diffs) / (n - 1))
            se = sd / math.sqrt(n)
            # ~95% normal interval (n is large once the drain runs)
            print(f"  paired diff (model-market): {m:+.4f} "
                  f"95% CI [{m - 1.96 * se:+.4f}, {m + 1.96 * se:+.4f}] "
                  f"n={n} {'UNRESOLVED' if m - 1.96 * se < 0 < m + 1.96 * se else ('MODEL WINS' if m < 0 else 'MARKET WINS')}")
    # Sleeve D shadow forward-test (round 4): hold rates on unseen windows
    # with observed asks, under the current D signal hash only.
    sh_all = state.get("d_shadow", [])
    sh_scored = [r for r in sh_all
                 if r.get("hold") is not None and r.get("sig_D") == SIG["D"]]
    sh_pend = [r for r in sh_all if r.get("hold") is None]
    if sh_scored or sh_pend:
        print(f"\nD shadow forward-test [{SIG['D']}]: "
              f"{len(sh_scored)} scored, {len(sh_pend)} pending")
        for sub in ("D1", "D2"):
            lst = [r for r in sh_scored if r["sub"] == sub]
            if not lst:
                continue
            n = len(lst)
            holds = sum(r["hold"] for r in lst)
            lo, hi = _wilson(holds, n)
            asks = [r["ask"] for r in lst if r.get("ask") is not None]
            ask_s = (f"median ask {sorted(asks)[len(asks)//2]:.2f} "
                     f"(n={len(asks)})" if asks else "no asks")
            print(f"  {sub}: hold {holds}/{n} = {holds/n:.1%}, "
                  f"Wilson 95% [{lo:.1%}, {hi:.1%}], {ask_s}")
    # latency era stats (review 2026-10-06)
    lat_pass = lat_fade = pre_lat = 0
    try:
        with open(BLOTTER) as f:
            for line in f:
                r = json.loads(line)
                if r.get("type") == "open":
                    if r.get("latency_pass") is True:
                        lat_pass += 1
                    elif r.get("latency_pass") is False:
                        lat_fade += 1
                    else:
                        pre_lat += 1
                elif (r.get("type") == "decision"
                      and str(r.get("reason", "")).startswith("latency:")):
                    lat_fade += 1
    except FileNotFoundError:
        pass
    print(f"\nlatency era (>{LATENCY_MS}ms re-poll): passed={lat_pass} "
          f"faded={lat_fade} pre-latency trades={pre_lat}")
    # rolling / drawdown status
    sp = state.get("settle_pnls", [])
    if sp:
        r20 = sum(sp[-20:])
        dd = state.get("peak_cum", 0.0) - state.get("cum_pnl", 0.0)
        print(f"rolling-20 P&L: {r20:+.2f} (kill at {KILL_ROLL20_PCT * BANKROLL:+.0f})  "
              f"drawdown from peak: {dd:.2f} (kill at {KILL_DRAWDOWN_PCT * BANKROLL:.0f})")
    # gate progress per sleeve under its CURRENT signal hash (round 4).
    # A sleeve's era restarts only when its own signal params change.
    era = {}
    try:
        with open(BLOTTER) as f:
            for line in f:
                r = json.loads(line)
                if r.get("type") != "settle":
                    continue
                s = r.get("sleeve", "?")
                sub = s if s in ("D1", "D2") else s  # D1/D2 settle under D's hash
                key = "D" if sub in ("D1", "D2") else s
                e = era.setdefault(key, {"n": 0, "pnl": 0.0})
                if r.get("sig_" + key) == SIG.get(key):
                    e["n"] += 1
                    e["pnl"] += r.get("pnl_after_fees", 0.0)
    except FileNotFoundError:
        pass
    print(f"\ngate eras (frozen A:{FROZEN_AT_A} B:{FROZEN_AT_B} C:{FROZEN_AT_C} D:{FROZEN_AT_D} risk:{FROZEN_AT_RISK}):")
    for s in ("A", "B", "C", "D"):
        e = era.get(s, {"n": 0, "pnl": 0.0})
        print(f"  sleeve {s} [{SIG[s]}]: {e['n']} settled, {e['pnl']:+.2f} net")
    # ---- gate v2 (ENACTED 2026-10-06, LaDre's word) ----
    print("\ngate v2 status (Sleeve B):")
    b = era.get("B", {"n": 0, "pnl": 0.0})
    b_pnls = []
    try:
        with open(BLOTTER) as f:
            for line in f:
                r = json.loads(line)
                if (r.get("type") == "settle" and r.get("sleeve") == "B"
                        and r.get("sig_B") == SIG["B"]):
                    b_pnls.append(r.get("pnl_after_fees", 0.0))
    except FileNotFoundError:
        pass
    g1 = b["n"] >= 150
    print(f"  [1] >=150 settled B trades: {b['n']}/150 "
          f"{'PASS' if g1 else '...'}")
    if len(b_pnls) >= 2:
        lb = _bootstrap_lb(b_pnls)
        g2 = lb > 0
        print(f"  [2] 90% bootstrap LB of mean after-fee P&L/trade > 0: "
              f"{lb:+.4f} {'PASS' if g2 else '...'}")
    else:
        g2 = False
        print(f"  [2] 90% bootstrap LB of mean after-fee P&L/trade > 0: "
              f"n={len(b_pnls)} (need >=2) ...")
    # Futility checkpoint (2026-10-08, LaDre's $1.26 threshold). REPORT-ONLY:
    # this line never halts anything; an auto-halt needs LaDre's word.
    fs = futility_status(b_pnls)
    if fs is None:
        print(f"  futility (report-only): n={len(b_pnls)} (need >=2)")
    else:
        _cp = (f"checkpoint {fs['checkpoint']}" if fs["checkpoint"]
               else "pre-checkpoint")
        print(f"  futility (report-only): n={fs['n']} "
              f"mean={fs['mean']:+.2f} ub90={fs['ub90']:+.2f} vs "
              f"min-edge {FUTILITY_MIN_EDGE_USD:.2f} ({_cp}): "
              f"{'FUTILE' if fs['futile'] else 'not futile'}")
    # ---- 72h sigma shadow (2026-10-08, LaDre's word). REPORT-ONLY: this
    # line never gates anything; the adoption decision is pre-registered
    # for the 72h mark. Counts scored windows carrying shadow p values.
    try:
        _sh_recs = _read_scored_windows()
        _n_sh = sum(1 for _r in _sh_recs
                    if (_r.get("shadow") or {}).get("p_h60") is not None)
        _sh_ts = [_r.get("scored_at") for _r in _sh_recs
                  if (_r.get("shadow") or {}).get("p_h60") is not None
                  and _r.get("scored_at")]
        _sh_since = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(min(_sh_ts)))
                     if _sh_ts else "n/a")
        print(f"  SHADOW (report-only, non-gating): {_n_sh} scored windows "
              f"with shadow p_h60/p_f3 (since {_sh_since})")
    except Exception:
        print("  SHADOW (report-only, non-gating): n/a (read failed)")
    # Gate [3] uses the same window set as kill(c) (2026-10-08).
    diffs90 = [w["brier_model"] - w["brier_mkt"]
               for w in _brier_windows_current(state)]
    n90 = len(diffs90)
    if n90 >= 100:
        m90 = sum(diffs90) / n90
        sd90 = math.sqrt(sum((x - m90) ** 2 for x in diffs90) / (n90 - 1))
        se90 = sd90 / math.sqrt(n90)
        ub90 = m90 + 1.645 * se90  # one-sided 90% upper bound
        g3 = (m90 <= 0) and (ub90 < GATE_BRIER_TOL)
        print(f"  [3] Brier: point {m90:+.4f}<=0 and 90% upper {ub90:+.4f} "
              f"< {GATE_BRIER_TOL} (n={n90}): {'PASS' if g3 else '...'}")
    else:
        g3 = False
        print(f"  [3] Brier: n={n90}/100 scored windows ...")
    # Fixed-grid tau 300-600 slice (NON-GATING, 2026-10-08): paired
    # model-minus-market Brier on the by-tau snapshots, since that is
    # where B trades. Report-only; the gate still uses the logged
    # (arbitrary) tau. Reads the deduped research jsonl (active + archives).
    _grid_diffs = []
    for _r in _read_scored_windows():
        _gs = _r.get("tau_grid_scores") or {}
        for _g in ("300", "450", "600"):
            _s = _gs.get(_g)
            if _s and _s.get("brier_model") is not None \
                    and _s.get("brier_mkt") is not None:
                _grid_diffs.append(_s["brier_model"] - _s["brier_mkt"])
    _ng = len(_grid_diffs)
    if _ng > 1:
        _mg = sum(_grid_diffs) / _ng
        _sdg = math.sqrt(sum((x - _mg) ** 2 for x in _grid_diffs)
                         / (_ng - 1))
        _seg = _sdg / math.sqrt(_ng)
        _ubg = _mg + 1.645 * _seg  # one-sided 90% upper bound
        print(f"  [research] fixed-grid tau 300-600 paired diff "
              f"(model-market): {_mg:+.4f} 90% UB {_ubg:+.4f} "
              f"(n={_ng} snapshots, NON-GATING)")
    else:
        print(f"  [research] fixed-grid tau 300-600: n={_ng} snapshots "
              f"(NON-GATING)")
    sp4 = state.get("settle_pnls", [])
    r20_4 = sum(sp4[-20:]) if len(sp4) >= 20 else 0.0
    dd4 = state.get("peak_cum", 0.0) - state.get("cum_pnl", 0.0)
    g4 = (not state.get("disabled")
          and not (len(sp4) >= 20 and r20_4 <= KILL_ROLL20_PCT * BANKROLL)
          and not (dd4 >= KILL_DRAWDOWN_PCT * BANKROLL))
    print(f"  [4] drawdown <= {KILL_DRAWDOWN_PCT:.0%} "
          f"(${KILL_DRAWDOWN_PCT * BANKROLL:.0f}), rolling-20 >= "
          f"{KILL_ROLL20_PCT:.0%} ({KILL_ROLL20_PCT * BANKROLL:+.0f}), "
          f"no kill active: {'PASS' if g4 else '...'}")
    print(f"  [5] LaDre's explicit word to go live: NOT GIVEN")
    if g1 and g2 and g3 and g4:
        print("  *** GATE V2 EVIDENCE COMPLETE — awaiting LaDre's word ***")
    # blotter P&L by sleeve
    pnl = {}
    try:
        with open(BLOTTER) as f:
            for line in f:
                r = json.loads(line)
                if r.get("type") == "settle":
                    s = r.get("sleeve", "?")
                    pnl[s] = pnl.get(s, 0.0) + r.get("pnl_after_fees", 0.0)
    except FileNotFoundError:
        pass
    if pnl:
        print("\nsettled P&L by sleeve:",
              "  ".join(f"{s}: {v:+.2f}" for s, v in sorted(pnl.items())))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-once", action="store_true")
    ap.add_argument("--settle", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--sleeves", default="A,B,D",
                    help="comma list, e.g. A,B,D (C default off)")
    ap.add_argument("--no-killswitches", action="store_true")
    ap.add_argument("--clear-kc-error", action="store_true",
                    help="clear a sticky kill_check_error flag and exit")
    ap.add_argument("--stamp-legacy-model-v", action="store_true",
                    help="one-time: relabel legacy model_v Brier windows to "
                         "the current label and exit")
    ap.add_argument("--restore-from-snapshot", action="store_true",
                    help="with --clear-kc-error: rebuild broken ledger keys "
                         "from the last-good snapshot (explicit consent)")
    a = ap.parse_args()
    sleeves = tuple(s.strip().upper() for s in a.sleeves.split(",") if s.strip())
    if a.stamp_legacy_model_v:
        _stamp_legacy_model_v()
        return
    if a.clear_kc_error:
        flag = _kc_flag_read()
        if flag is None:
            print("no kill_check_error flag set")
            return
        raw = _kc_load_raw()
        bad = (_kc_bad_keys(raw, _KC_CLEAR_KEYS) if raw is not None
               else list(_KC_CLEAR_KEYS))
        if raw is None or bad:
            good = _kc_snapshot_load() if a.restore_from_snapshot else None
            unrestorable = [k for k in bad
                            if good is None or k not in good
                            or not _kc_ok(k, good[k])]
            if good is None or unrestorable:
                print(f"REFUSED: STATE unreadable or keys missing/invalid "
                      f"{bad} (not restorable from snapshot: {unrestorable}). "
                      f"Fix by hand, or re-run with --restore-from-snapshot.")
                raise SystemExit(1)
            if os.path.exists(STATE):  # keep the broken file as evidence
                shutil.copy2(STATE, f"{STATE}.corrupt.{int(time.time())}")
            age = int(time.time() - os.path.getmtime(_kc_snapshot_path()))
            if raw is None:
                raw = dict(good)
                print(f"  STATE unreadable: rebuilt wholly from snapshot "
                      f"(age {age} s)")
            else:
                for k in bad:
                    raw[k] = copy.deepcopy(good[k])
                    print(f"  restoring {k} = "
                          f"{json.dumps(good[k])[:80]} (snapshot age {age} s)")
            _atomic_json_write(STATE, raw)
        try:
            os.remove(_kc_flag_path())
        except FileNotFoundError:
            pass
        print(f"cleared kill_check_error: {flag.get('type')}: "
              f"{flag.get('message')}")
        try:
            with open(STATE + ".kc_audit.log", "a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
                        f"cleared by {getpass.getuser()} "
                        f"flag={flag.get('type')} "
                        f"restore={a.restore_from_snapshot}\n")
        except Exception:  # noqa: BLE001 - audit must never block a clear
            pass
        return
    if a.run_once:
        t0 = time.time()
        ok = False
        try:
            run_once(sleeves, arm_kill=not a.no_killswitches)
            ok = True
        finally:
            dt = time.time() - t0
            # Per-tick heartbeat (LaDre 2026-10-06 P1): one greppable line per
            # phase invocation so tick durations are measurable from paper.log.
            # try/finally: the heartbeat prints even if the phase raises.
            # diag= flag (2026-10-08, per Claude): the BTC_DIAG kill-switch
            # state is printed visibly every tick so it can't be forgotten.
            # (2026-10-08, per Claude's third review): three-state --
            # on / off / unavail -- with buffer age, e.g.
            # "diag=unavail age=412s".
            if os.environ.get("BTC_DIAG", "").lower() == "off":
                _diag_state = "off"
            elif not _DIAG_LAST.get("ok", True):
                _diag_state = "unavail"
            else:
                _diag_state = "on"
            _diag_age = _DIAG_LAST.get("age", -1)
            if _diag_state != "off" and _diag_age < 0:
                # Diagnostics didn't run this tick (e.g. decision already
                # recorded): report the sidecar's age from the file so
                # the log still shows buffer freshness every tick.
                try:
                    import diag_buffer
                    _b = diag_buffer.load_buffer(
                        diag_buffer.buffer_path(STATE))
                    if _b["rows"]:
                        _diag_age = int(time.time() - _b["rows"][-1][0])
                except Exception:
                    pass
            _age_str = (f" age={_diag_age}s"
                        if _diag_state != "off" and _diag_age >= 0 else "")
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t0))} "
                  f"HEARTBEAT phase=run-once status={'ok' if ok else 'error'} "
                  f"secs={dt:.1f} diag={_diag_state}{_age_str}")
        _st = load_state()
        _st["last_run_once_ok"] = int(time.time())
        if _kc_flag_read() is None:
            try:
                save_state(_st)
            except ValueError as e:
                # Belt-and-braces: if the flag file itself failed to write,
                # the state may still be corrupt here. Never traceback.
                print(f"STATE NOT SAVED (evidence-only tick): {e}")
        else:
            # Flagged tick: STATE is never written (v3). The stale-watch
            # alerts on the untouched watermark as the backstop.
            print("STATE NOT SAVED (flagged tick): kill_check_error active")
    elif a.settle:
        t0 = time.time()
        ok = False
        try:
            settle()
            ok = True
        finally:
            dt = time.time() - t0
            # Same heartbeat for the settle phase: a slow settle (drains up to
            # 24 closed windows with sequential API calls) is invisible otherwise.
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t0))} "
                  f"HEARTBEAT phase=settle status={'ok' if ok else 'error'} "
                  f"secs={dt:.1f}")
        _st = load_state()
        _st["last_settle_ok"] = int(time.time())
        if _kc_flag_read() is None:
            try:
                save_state(_st)
            except ValueError as e:
                print(f"STATE NOT SAVED (evidence-only tick): {e}")
        else:
            print("STATE NOT SAVED (flagged tick): kill_check_error active")
    elif a.report:
        report()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
