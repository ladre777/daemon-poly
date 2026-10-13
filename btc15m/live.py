#!/usr/bin/env python3
"""Live executor for Polymarket US 15m BTC Up/Down.

TRIPLE-GATED: --live + live_config.json live_ok=true + --key-id/--secret.
Without all three, NOTHING places. Default posture is refusal.

- Reuses paper.py's pure sleeve decision functions (A/B/C gates are
  bankroll-independent; only sizing is recomputed for the live bankroll).
- Own state: live_state.json + live_blotter.jsonl. NEVER touches the
  paper engine's files — paper stays the control sample.
- Dynamic bankroll: reads the live account balance every run via
  /v1/account/balances; sizes risk_per_window of it, capped at
  dollar_cap. Fail-closed: unreadable/zero balance -> abort, no orders.
- Kill switches mirrored from paper, scaled to the live bankroll.
- Settlement: same BRTI avg60(Te) logic as paper; the venue auto-settles,
  we track P&L from recorded fills (fill_assumed=true until verified
  against positions).

Paper-to-live gate ("poppin" criteria) is defined in live_config.json;
`--check-gate` evaluates the PAPER blotter against it. live_ok stays
false until LaDre flips it.

Side mapping (markets.parse_book): up = buy YES (ORDER_INTENT_BUY_LONG),
down = buy NO (ORDER_INTENT_BUY_SHORT).

Usage:
  python live.py --live --run-once --key-id ID --secret SECRET [--sleeves A,B]
  python live.py --live --settle --key-id ID --secret SECRET
  python live.py --report            # read-only
  python live.py --check-gate        # read-only: are we poppin yet?
"""
import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from common import append_jsonl, now_s, taker_fee_per_share  # noqa: E402
from markets import (window_start, get_event, parse_terms, get_book,  # noqa: E402
                     parse_book, mid_up, event_slug, market_slug)
from fairvalue import fair_snapshot, brier  # noqa: E402
from paper import (decide_sleeve_a, decide_sleeve_b, decide_sleeve_c,  # noqa: E402
                   blotter_base)
from golf_bot import _extract_balance  # noqa: E402  (dynamic bankroll helper)

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_STATE = os.path.join(HERE, "live_state.json")
LIVE_BLOTTER = os.path.join(HERE, "live_blotter.jsonl")
LIVE_CONFIG = os.path.join(HERE, "live_config.json")
PAPER_BLOTTER = os.path.join(HERE, "blotter.jsonl")
PAPER_STATE = os.path.join(HERE, "paper_state.json")

B_CURSE_MS = 0.3
# Winner's-curse veto, mirrored from paper.py (polybtc study).
B_VETO_EDGE = 0.15


def log(msg):
    print(msg, flush=True)


# ================= config & state =================

def load_config():
    with open(LIVE_CONFIG) as f:
        return json.load(f)


def load_state():
    try:
        with open(LIVE_STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"open": [], "disabled": [], "day": None, "day_pnl": 0.0,
                "consec_losses": 0, "brier_windows": [], "pending_brier": {},
                "decided_windows": []}


def save_state(s):
    # Atomic write (LaDre 2026-10-06 review fix, STAGED, revised per
    # review): same pattern as paper.py -- unique tmp per process, fsync
    # before replace. No other live.py behavior changed.
    tmp = f"{LIVE_STATE}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(s, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, LIVE_STATE)


def today_key():
    return time.strftime("%Y-%m-%d", time.gmtime())


def check_live_gates(cfg, key_id, secret):
    """Triple gate. Returns (ok: bool, reason: str)."""
    if not cfg.get("live_ok"):
        return False, "config live_ok is not true"
    if not key_id or not secret:
        return False, "--key-id/--secret required"
    return True, "ok"


def kill_check(state, bankroll, cfg, arm=True):
    reasons = []
    if today_key() != state.get("day"):
        state["day"] = today_key()
        state["day_pnl"] = 0.0
    if arm and state["day_pnl"] <= cfg["kill_day_pct"] * bankroll:
        reasons.append(f"day_pnl {state['day_pnl']:.2f} <= "
                       f"{cfg['kill_day_pct']:.0%} of bankroll")
    if arm and state["consec_losses"] >= cfg["kill_consec_losses"]:
        reasons.append(f"{state['consec_losses']} consecutive directional losses")
        for s in ("B", "C"):
            if s not in state["disabled"]:
                state["disabled"].append(s)
    bw = state.get("brier_windows", [])
    n = cfg["kill_brier_n"]
    if arm and len(bw) >= n:
        bm = sum(w["brier_model"] for w in bw[-n:]) / n
        bk = sum(w["brier_mkt"] for w in bw[-n:]) / n
        if bm > bk:
            reasons.append(f"Brier(model) {bm:.4f} > Brier(market) {bk:.4f}")
            for s in ("B", "C"):
                if s not in state["disabled"]:
                    state["disabled"].append(s)
    return (len(reasons) > 0, reasons)


# ================= bankroll & sizing =================

def fetch_live_bankroll(key_id, secret):
    """Live USD buying power. Raises RuntimeError if unreadable."""
    import pmus
    try:
        out = pmus.call(key_id, secret, "GET", "/v1/account/balances")
    except Exception as e:
        raise RuntimeError(f"balances endpoint failed: {type(e).__name__}: {e}")
    val = _extract_balance(out)
    if val is None:
        raise RuntimeError(f"balances unparseable: {str(out)[:200]}")
    if val <= 0:
        raise RuntimeError(f"live bankroll is ${val:.2f}; nothing to size")
    return round(val, 2)


def live_size(price, depth, bankroll, cfg, risk_key="risk_per_window"):
    qty = min(depth, (bankroll * cfg[risk_key]) / price, cfg["dollar_cap"])
    qty = math.floor(qty * 100) / 100
    return qty if qty > 0 else 0.0


# ================= execution =================

def place_leg(key_id, secret, mslug, side, price, qty):
    """Preview then place one leg (fill-or-kill). Returns order record.

    PRICE TRAP (venue prices the LONG side only): buying Down at 0.48 is
    ORDER_INTENT_BUY_SHORT with price.value = "0.52" = 1 - 0.48.
    """
    import pmus
    outcome = "yes" if side == "up" else "no"
    intent = ("ORDER_INTENT_BUY_LONG" if outcome == "yes"
              else "ORDER_INTENT_BUY_SHORT")
    px = price if side == "up" else round(1.0 - price, 4)
    body = {
        "marketSlug": mslug,
        "intent": intent,
        "type": "ORDER_TYPE_LIMIT",
        "price": {"value": str(px), "currency": "USD"},
        "quantity": qty,
        "tif": "TIME_IN_FORCE_FILL_OR_KILL",
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }
    prev = pmus.call(key_id, secret, "POST", "/v1/order/preview",
                     body={"request": body})
    log(f"preview ok: {side} {qty}@{px} ({str(prev)[:120]})")
    resp = pmus.call(key_id, secret, "POST", "/v1/orders", body=body)
    return {"order_id": resp.get("id"), "side": side, "price": price,
            "venue_price": px, "qty": qty, "fill_assumed": True,
            "preview": str(prev)[:200]}


def unwind_legs(key_id, secret, mslug, placed, book):
    """Best-effort unwind of partially placed legs (Jonmaa's one-leg rule):
    sell each filled leg at the best bid, fill-or-kill. Returns records."""
    import pmus
    results = []
    for leg in placed:
        side = leg["side"]
        if side == "up":
            bid = book.get("bid_up")
            if not bid:
                continue
            intent, px = "ORDER_INTENT_SELL_LONG", bid[0]
        else:
            bid = book.get("bid_down")
            if not bid:
                continue
            intent, px = "ORDER_INTENT_SELL_SHORT", round(1.0 - bid[0], 4)
        body = {
            "marketSlug": mslug,
            "intent": intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {"value": str(px), "currency": "USD"},
            "quantity": leg["qty"],
            "tif": "TIME_IN_FORCE_FILL_OR_KILL",
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
        }
        try:
            resp = pmus.call(key_id, secret, "POST", "/v1/orders", body=body)
            results.append({"order_id": resp.get("id"), "unwind_of": leg,
                            "unwind_px": px})
            log(f"unwind ok: sold {side} {leg['qty']}@{px}")
        except Exception as e:
            results.append({"error": f"{type(e).__name__}: {e}"[:200],
                            "unwind_of": leg})
            log(f"UNWIND FAILED for {side} leg: {e}")
    return results


def run_once_live(key_id, secret, sleeves=("A", "B"), arm_kill=True):
    cfg = load_config()
    now = now_s()
    T = window_start(now)
    Te = T + 900
    tau = Te - now
    slug = event_slug(T)
    mslug = market_slug(T)
    state = load_state()

    try:
        bankroll = fetch_live_bankroll(key_id, secret)
    except RuntimeError as e:
        log(f"REFUSING live: {e}")
        return None
    log(f"live bankroll: ${bankroll:.2f}")

    halt, reasons = kill_check(state, bankroll, cfg, arm_kill)
    save_state(state)

    def mark_decided():
        decided = state.setdefault("decided_windows", [])
        if str(T) not in decided:
            decided.append(str(T))
            del decided[:-200]
        save_state(state)

    def abstain(reason):
        rec = {"ts": now, "type": "decision", "event_id": slug, "Topen": T,
               "sleeve": None, "reason": reason, "mode": "live"}
        append_jsonl(LIVE_BLOTTER, rec)
        mark_decided()
        log(f"{slug}: ABSTAIN ({reason})")
        return None

    if str(T) in state.get("decided_windows", []):
        log(f"{slug}: SKIP (already decided this window)")
        return None
    if any(p["Topen"] == T for p in state["open"]):
        return abstain("position already open for window")
    if halt:
        return abstain("kill switch: " + "; ".join(reasons))
    if tau <= 0:
        return abstain("window closed")

    try:
        ev = get_event(T)
    except Exception as e:
        return abstain(f"event fetch failed: {e}")
    if not ev or not ev.get("markets"):
        return abstain("window not listed")
    terms = parse_terms(ev["markets"][0])
    O = terms["priceToBeat"]
    if O is None:
        return abstain("priceToBeat not published")
    if terms["index"] != "BRTI":
        return abstain(f"unexpected index {terms['index']}")

    try:
        book = parse_book(get_book(T))
    except Exception as e:
        return abstain(f"book fetch failed: {e}")
    if book.get("empty_book"):
        return abstain("empty book (NO ONE TO TRADE AGAINST)")

    snap = fair_snapshot(T, O, now)
    if snap["p_up"] is None:
        return abstain("fair value unavailable")

    mkt_mid = mid_up(book)
    state["pending_brier"][str(T)] = {
        "p_fair": snap["p_up"], "mid_up": mkt_mid, "tau": tau}
    save_state(state)

    trade = None
    if "A" in sleeves and "A" not in state["disabled"]:
        trade = decide_sleeve_a(book)
    if trade is None and "B" in sleeves and "B" not in state["disabled"]:
        tb = decide_sleeve_b(book, snap["p_up"], tau)
        if tb and tb.get("veto_candidate"):
            time.sleep(B_CURSE_MS)
            try:
                book2 = parse_book(get_book(T))
                tb2 = decide_sleeve_b(book2, snap["p_up"], Te - now_s())
                if tb2 and tb2.get("edge", 0) >= B_VETO_EDGE:
                    return abstain("winner's curse veto: >=15c edge persisted")
                trade = tb2 if tb2 and not tb2.get("veto_candidate") else None
            except Exception:
                trade = None
        else:
            trade = tb
    if trade is None and "C" in sleeves and "C" not in state["disabled"]:
        trade = decide_sleeve_c(book, snap["p_up"], tau)
    if trade is None:
        return abstain("no sleeve gate passed")

    # recompute sizing for the LIVE bankroll (paper decide used $1000)
    risk_key = "pair_risk" if trade["sleeve"] == "A" else "risk_per_window"
    depth = {"up": book.get("ask_up"), "down": book.get("ask_down")}
    sized = []
    for leg in trade["legs"]:
        d = depth.get(leg["side"])
        if not d:
            return abstain(f"no depth for {leg['side']} leg at place time")
        q = live_size(leg["price"], d[1], bankroll, cfg, risk_key)
        if q <= 0:
            return abstain("live size rounds to zero")
        sized.append({**leg, "size": q})

    # place
    placed = []
    try:
        for leg in sized:
            placed.append(place_leg(key_id, secret, mslug,
                                    leg["side"], leg["price"], leg["size"]))
    except Exception as e:
        log(f"ORDER FAILED: {type(e).__name__}: {e} "
            f"({len(placed)}/{len(sized)} legs placed)")
        unwind = unwind_legs(key_id, secret, mslug, placed, book) if placed else []
        append_jsonl(LIVE_BLOTTER, {"ts": now, "type": "order_error",
                                    "event_id": slug, "error": str(e)[:300],
                                    "placed": placed, "unwind": unwind})
        return None

    pos = {**blotter_base(slug, T, O, snap, book),
           "ts": now, "type": "open", "mode": "live",
           "market_slug": mslug, "sleeve": trade["sleeve"],
           "edge": trade.get("edge"), "placed_legs": placed,
           "sigma": snap.get("sigma")}
    state["open"].append(pos)
    append_jsonl(LIVE_BLOTTER, pos)
    mark_decided()
    desc = ", ".join(f"{l['side']} {l['qty']}@{l['price']:.4f} "
                     f"(order {l['order_id']})" for l in placed)
    log(f"{slug}: LIVE {trade['sleeve']} -> {desc} edge={trade.get('edge')}")
    return pos


def settle_live():
    """Settle matured LIVE positions against BRTI avg60(Te). Venue
    auto-settles; we track P&L from recorded fills."""
    from brti import fetch_1s, avg60
    now = now_s()
    state = load_state()
    matured = [p for p in state["open"] if p["Tclose"] <= now - 5]
    if not matured:
        log("nothing to settle")
        return
    t_min = min(p["Tclose"] for p in matured) - 60
    t_max = max(p["Tclose"] for p in matured)
    candles = fetch_1s(t_min, t_max)
    for p in matured:
        Te = p["Tclose"]
        s_avg = avg60(candles, Te)
        if s_avg is None:
            log(f"{p['event_id']}: settlement prints missing, retry later")
            continue
        outcome_up = round(s_avg, 2) >= round(p["O"], 2)
        pnl = 0.0
        for leg in p["placed_legs"]:
            win = (leg["side"] == "up") == outcome_up
            cost = (leg["qty"] * leg["price"]
                    + leg["qty"] * taker_fee_per_share(leg["price"]))
            pnl += leg["qty"] * (1.0 if win else 0.0) - cost
        p.update({"type": "settle", "brti_settle": round(s_avg, 2),
                  "outcome": "up" if outcome_up else "down",
                  "pnl_after_fees": round(pnl, 2),
                  "settled_at": now})
        append_jsonl(LIVE_BLOTTER, p)
        state["open"].remove(p)
        state["day_pnl"] = state.get("day_pnl", 0.0) + pnl
        if p["sleeve"] in ("B", "C"):
            state["consec_losses"] = state["consec_losses"] + 1 if pnl < 0 else 0
        pb = state["pending_brier"].pop(str(p["Topen"]), None)
        if pb and pb.get("p_fair") is not None and pb.get("mid_up") is not None:
            oc = 1.0 if outcome_up else 0.0
            state["brier_windows"].append({
                "T": p["Topen"], "tau": pb["tau"],
                "brier_model": brier(pb["p_fair"], oc),
                "brier_mkt": brier(pb["mid_up"], oc)})
            state["brier_windows"] = state["brier_windows"][-500:]
        log(f"{p['event_id']}: settled {p['outcome']} @ {s_avg:.2f} "
            f"pnl={pnl:+.2f} ({p['sleeve']}) live")
    save_state(state)


# ================= paper-to-live gate =================

def check_gate():
    """Evaluate the PAPER blotter against gate v2 (enacted 2026-10-06).
    Returns (poppin: bool, report: dict). Read-only."""
    from paper import SIG, _bootstrap_lb
    cfg = load_config()
    g = cfg["poppin_gate"]
    b_settled = []
    try:
        with open(PAPER_BLOTTER) as f:
            for line in f:
                r = json.loads(line)
                if (r.get("type") == "settle" and r.get("sleeve") == "B"
                        and r.get("sig_B") == SIG["B"]):
                    b_settled.append(r)
    except FileNotFoundError:
        pass
    checks = []
    ok = True

    def chk(name, passed, detail):
        nonlocal ok
        checks.append({"check": name, "passed": passed, "detail": detail})
        if not passed:
            ok = False

    n_b = len(b_settled)
    chk("b_trades_150", n_b >= g["min_settled_b_trades"],
        f"{n_b} Sleeve B settled under sig_B / need {g['min_settled_b_trades']}")
    pnls = [r.get("pnl_after_fees", 0.0) for r in b_settled]
    if len(pnls) >= 2:
        lb = _bootstrap_lb(pnls)
        chk("bootstrap_lb_positive", lb > 0,
            f"90% bootstrap LB of mean after-fee P&L/trade = {lb:+.4f}")
    else:
        chk("bootstrap_lb_positive", False,
            f"n={len(pnls)} B trades (need >=2)")

    try:
        ps = json.load(open(PAPER_STATE))
    except FileNotFoundError:
        ps = {}
    bw = [w for w in ps.get("brier_windows", [])
          if w.get("brier_model") is not None and w.get("brier_mkt") is not None]
    distinct = len({w["T"] for w in bw})
    if distinct >= g["brier_min_distinct_windows"]:
        diffs = [w["brier_model"] - w["brier_mkt"] for w in bw]
        n = len(diffs)
        m = sum(diffs) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in diffs) / (n - 1))
        se = sd / math.sqrt(n)
        ub90 = m + 1.645 * se
        passed = (m <= 0) and (ub90 < g["brier_tolerance"])
        chk("brier_rule", passed,
            f"point {m:+.4f}<=0 and 90% upper {ub90:+.4f} < "
            f"{g['brier_tolerance']} over {distinct} distinct windows")
    else:
        chk("brier_rule", False,
            f"{distinct} distinct scored windows / need "
            f"{g['brier_min_distinct_windows']}")
    if g["require_no_kill_switch"]:
        dis = ps.get("disabled", [])
        chk("no_kill_switch", not dis, f"disabled sleeves: {dis or 'none'}")

    for c in checks:
        log(f"  [{'PASS' if c['passed'] else 'FAIL'}] {c['check']}: {c['detail']}")
    log("POPPIN: we stay paper." if not ok else
        "POPPIN: paper sample clears gate v2 — LaDre decides.")
    return ok, {"checks": checks, "n_b_settled": n_b}


def report():
    state = load_state()
    cfg = load_config()
    log(f"bankroll(live): dynamic per run  day_pnl: {state.get('day_pnl', 0):+.2f} "
        f"({today_key()})")
    log(f"live_ok: {cfg.get('live_ok')}  disabled: {state.get('disabled') or 'none'} "
        f"consec_losses: {state.get('consec_losses', 0)}")
    log(f"open live positions: {len(state['open'])}")
    bw = state.get("brier_windows", [])
    if bw:
        bm = sum(w["brier_model"] for w in bw) / len(bw)
        bk = sum(w["brier_mkt"] for w in bw) / len(bw)
        log(f"Brier live: n={len(bw)} model={bm:.4f} market={bk:.4f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="REQUIRED for any order placement or live settle")
    ap.add_argument("--run-once", action="store_true")
    ap.add_argument("--settle", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--check-gate", action="store_true",
                    help="read-only: evaluate paper sample vs poppin gate")
    ap.add_argument("--key-id")
    ap.add_argument("--secret")
    ap.add_argument("--sleeves", default="A,B")
    ap.add_argument("--no-killswitches", action="store_true")
    a = ap.parse_args()
    cfg = load_config()

    if a.check_gate:
        check_gate()
        return 0
    if a.report:
        report()
        return 0

    # everything below mutates live state or places orders: triple-gated
    if not a.live:
        log("REFUSING: live executor requires --live "
            "(paper engine is paper.py)")
        return 2
    ok, reason = check_live_gates(cfg, a.key_id, a.secret)
    if not ok:
        log(f"REFUSING live: {reason}")
        return 2

    sleeves = tuple(s.strip().upper() for s in a.sleeves.split(",") if s.strip())
    if a.run_once:
        run_once_live(a.key_id, a.secret, sleeves,
                      arm_kill=not a.no_killswitches)
    elif a.settle:
        settle_live()
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
