"""Read-only accessors for BTC 15m agent state. No network, no orders.

Shared by mcp_server.py and api.py. Everything reads the live state files,
so every call reflects the agent's current reality.
"""
import contextlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PAPER_STATE = os.path.join(HERE, "paper_state.json")
PAPER_BLOTTER = os.path.join(HERE, "blotter.jsonl")
LIVE_STATE = os.path.join(HERE, "live_state.json")
LIVE_BLOTTER = os.path.join(HERE, "live_blotter.jsonl")
LIVE_CONFIG = os.path.join(HERE, "live_config.json")


def _load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _read_jsonl(path):
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def get_status():
    ps = _load_json(PAPER_STATE, {})
    ls = _load_json(LIVE_STATE, {})
    cfg = _load_json(LIVE_CONFIG, {})
    return {
        "paper": {
            "day": ps.get("day"),
            "day_pnl": round(ps.get("day_pnl", 0.0), 2),
            "open_positions": len(ps.get("open", [])),
            "consec_losses": ps.get("consec_losses", 0),
            "disabled_sleeves": ps.get("disabled", []),
            "decided_windows": len(ps.get("decided_windows", [])),
            "brier_windows": len(ps.get("brier_windows", [])),
        },
        "live": {
            "live_ok": bool(cfg.get("live_ok")),
            "day": ls.get("day"),
            "day_pnl": round(ls.get("day_pnl", 0.0), 2),
            "open_positions": len(ls.get("open", [])),
            "consec_losses": ls.get("consec_losses", 0),
            "disabled_sleeves": ls.get("disabled", []),
        },
    }


def _compact(rec):
    c = {"ts": rec.get("ts"), "type": rec.get("type"),
         "event": rec.get("event_id"), "mode": rec.get("mode", "paper")}
    if rec.get("sleeve"):
        c["sleeve"] = rec["sleeve"]
    if rec.get("type") == "decision":
        c["reason"] = rec.get("reason")
    if rec.get("type") in ("open",):
        legs = rec.get("filled_legs") or rec.get("placed_legs") or []
        c["legs"] = [{"side": l.get("side"), "qty": l.get("filled", l.get("qty")),
                      "price": l.get("price")} for l in legs]
        c["edge"] = rec.get("edge")
    if rec.get("type") == "settle":
        c["outcome"] = rec.get("outcome")
        c["pnl"] = rec.get("pnl_after_fees")
    if rec.get("type") == "order_error":
        c["error"] = rec.get("error")
    return c


def get_activity(n=10):
    recs = _read_jsonl(PAPER_BLOTTER) + _read_jsonl(LIVE_BLOTTER)
    recs.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return [_compact(r) for r in recs[:max(1, min(n, 100))]]


def get_positions():
    ps = _load_json(PAPER_STATE, {})
    ls = _load_json(LIVE_STATE, {})
    out = []
    for rec in ps.get("open", []):
        c = _compact({**rec, "mode": "paper"})
        out.append(c)
    for rec in ls.get("open", []):
        c = _compact({**rec, "mode": "live"})
        out.append(c)
    return out


def get_pnl():
    by_sleeve, totals = {}, {"paper": 0.0, "live": 0.0}
    n = 0
    for path, mode in ((PAPER_BLOTTER, "paper"), (LIVE_BLOTTER, "live")):
        for rec in _read_jsonl(path):
            if rec.get("type") != "settle":
                continue
            n += 1
            s = rec.get("sleeve", "?")
            pnl = rec.get("pnl_after_fees", 0.0) or 0.0
            by_sleeve[s] = round(by_sleeve.get(s, 0.0) + pnl, 2)
            totals[mode] = round(totals[mode] + pnl, 2)
    totals["all"] = round(totals["paper"] + totals["live"], 2)
    return {"settled_trades": n, "by_sleeve": by_sleeve, "net": totals}


def get_gate():
    from live import check_gate
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        poppin, report = check_gate()
    return {"poppin": poppin, "report": report}


def get_config():
    cfg = _load_json(LIVE_CONFIG, {})
    return {
        "live_ok": cfg.get("live_ok"),
        "risk_per_window": cfg.get("risk_per_window"),
        "pair_risk": cfg.get("pair_risk"),
        "dollar_cap": cfg.get("dollar_cap"),
        "kill_day_pct": cfg.get("kill_day_pct"),
        "kill_consec_losses": cfg.get("kill_consec_losses"),
        "poppin_gate": cfg.get("poppin_gate"),
    }
