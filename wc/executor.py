import os
import csv
from datetime import datetime

import pm_us
from gates import MAX_TRADE_USD

TRADE_LOG = "wc_trade_log.csv"

# Reject an auto-trade if the live ask has drifted more than this many percentage
# points from the price the signal was generated at (the edge has moved/vanished).
PRICE_TOLERANCE_PCT = 3.0

def _norm_tokens(s: str) -> set:
    """Lowercase alphanumeric token set. NO stopword removal on purpose: tokens
    like 'City' / 'FC' are team-distinctive (Man City vs Man United, LAFC vs LA
    Galaxy), so stripping them would create false matches."""
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in str(s))
    return {t for t in cleaned.split() if t}


def _outcome_matches(signal_outcome: str, catalog_outcome: str) -> bool:
    """True only if one outcome's token set FULLY CONTAINS the other's — i.e. the
    model's label is an abbreviation/expansion of the catalog name, not merely a
    name that shares a city. Examples:
      'Dodgers' ⊆ 'Los Angeles Dodgers'         -> True  (abbreviation)
      'New York' ⊆ 'New York Liberty'           -> True  (catalog city-only)
      'New York Yankees' vs 'New York Mets'     -> False (shared city only)
      'Los Angeles Dodgers' vs 'L.A. Angels'    -> False (shared city only)
    Fails closed on an empty or merely-overlapping pair."""
    a, b = _norm_tokens(signal_outcome), _norm_tokens(catalog_outcome)
    if not a or not b:
        return False
    return a <= b or b <= a

FIELDNAMES = [
    "timestamp", "signal_type", "edge", "market", "direction",
    "outcome", "entry_price_pct", "target_exit_pct", "size_pct",
    "confidence", "rationale", "gate_check", "gate_notes",
    "executed", "execution_id", "error",
]


def log_signal(signal: dict, executed: bool = False, execution_id: str = "", error: str = ""):
    write_header = not os.path.exists(TRADE_LOG)
    with open(TRADE_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "timestamp":       datetime.utcnow().isoformat(),
            "signal_type":     signal.get("signal_type", ""),
            "edge":            signal.get("edge", ""),
            "market":          signal.get("market", ""),
            "direction":       signal.get("direction", ""),
            "outcome":         signal.get("outcome", ""),
            "entry_price_pct": signal.get("entry_price_pct", ""),
            "target_exit_pct": signal.get("target_exit_pct", ""),
            "size_pct":        signal.get("size_pct_bankroll", ""),
            "confidence":      signal.get("confidence", ""),
            "rationale":       signal.get("rationale", ""),
            "gate_check":      signal.get("gate_check", ""),
            "gate_notes":      signal.get("gate_notes", ""),
            "executed":        executed,
            "execution_id":    execution_id,
            "error":           error,
        })


def dry_run_signal(signal: dict) -> str:
    log_signal(signal, executed=False, execution_id="DRY_RUN")
    return (
        f"DRY RUN: Would {signal.get('direction')} {signal.get('outcome')} "
        f"at {signal.get('entry_price_pct')}¢ | "
        f"Market: {signal.get('market')}"
    )


def place_order(signal: dict, size_usdc: float, catalog=None) -> dict:
    """Place a REAL marketable-limit YES order on Polymarket US.

    This is the FINAL safety boundary before money moves. Every check fails closed
    (returns {"error": ...} and logs — never raises):
      * market_slug must be present AND in the current cycle's catalog (slug->meta)
      * the signal's outcome label must match that slug's catalog outcome
      * only YES / long is auto-executable (NO / short is alert-only here)
      * a live best-ask must exist and be within PRICE_TOLERANCE_PCT of the signal
      * notional is hard-capped at min(requested, MAX_TRADE_USD, buying power)
      * integer shares, notional re-verified after tick rounding
    """
    market_slug = (signal.get("market_slug") or "").strip()
    direction   = (signal.get("direction") or "YES").upper()
    entry_pct   = float(signal.get("entry_price_pct", 0) or 0)
    catalog     = catalog or {}
    meta        = catalog.get(market_slug, {})
    tick        = str(meta.get("tick") or signal.get("_tick") or "0.001")

    try:
        if not market_slug:
            raise ValueError("no market_slug — not an auto-executable US market")
        if not catalog:
            raise ValueError("no catalog supplied — refusing to trade without validation")
        if market_slug not in catalog:
            raise ValueError(f"slug '{market_slug}' not in current US catalog (stale/unverified)")
        cat_outcome = meta.get("outcome", "")
        if cat_outcome and not _outcome_matches(signal.get("outcome", ""), cat_outcome):
            raise ValueError(
                f"outcome mismatch: signal '{signal.get('outcome','')}' vs "
                f"catalog '{cat_outcome}' for slug {market_slug}"
            )
        if direction not in ("YES", "BUY", "LONG"):
            raise ValueError(f"direction '{direction}' not auto-executable (YES/long only)")

        client = pm_us.get_pm_client()
        if not client:
            raise ValueError("Polymarket US client unavailable — check POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY")

        ask = pm_us.live_ask(market_slug)
        if not ask or ask <= 0:
            raise ValueError(f"no live ask for {market_slug} (illiquid / no book)")
        ask_pct = ask * 100
        if entry_pct > 0 and abs(ask_pct - entry_pct) > PRICE_TOLERANCE_PCT:
            raise ValueError(
                f"price moved: live ask {ask_pct:.1f}¢ vs signal {entry_pct:.1f}¢ "
                f"(>±{PRICE_TOLERANCE_PCT}pp)"
            )

        bp     = pm_us.get_buying_power()
        budget = min(float(size_usdc), float(MAX_TRADE_USD), bp if bp > 0 else float(MAX_TRADE_USD))
        price  = pm_us.round_to_tick(ask, tick)
        if budget < price:
            raise ValueError(f"budget ${budget:.2f} below one share at {ask_pct:.1f}¢")

        shares = int(budget / max(price, 0.001))
        while shares > 0 and shares * price > MAX_TRADE_USD:
            shares -= 1
        if shares < 1:
            raise ValueError(f"cannot fit a whole share under ${MAX_TRADE_USD} cap at {price}")

        result = client.orders.create({
            "marketSlug": market_slug,
            "intent":     "ORDER_INTENT_BUY_LONG",
            "type":       "ORDER_TYPE_LIMIT",
            "price":      {"value": f"{price:.4f}", "currency": "USD"},
            "quantity":   shares,
            "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        })
        order_id = ""
        if isinstance(result, dict):
            order_id = result.get("id") or (result.get("order") or {}).get("id", "")
        log_signal(signal, executed=True, execution_id=order_id)
        return {
            "orderID":  order_id,
            "shares":   shares,
            "price":    price,
            "notional": round(shares * price, 2),
            "raw":      result,
        }

    except Exception as e:
        log_signal(signal, executed=False, error=str(e))
        return {"error": str(e)}


def close_position(market_slug: str) -> dict:
    """Close an open Polymarket US position by market slug (operator CLOSE flow)."""
    try:
        client = pm_us.get_pm_client()
        if not client:
            return {"error": "Polymarket US client unavailable"}
        result = client.orders.close_position({"marketSlug": market_slug})
        return {"ok": True, "raw": result}
    except Exception as e:
        return {"error": str(e)}


def read_trade_log() -> list:
    if not os.path.exists(TRADE_LOG):
        return []
    rows = []
    with open(TRADE_LOG, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows
