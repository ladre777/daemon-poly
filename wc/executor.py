import os
import csv
import json
import requests
from datetime import datetime

CLOB_API = "https://clob.polymarket.com"
TRADE_LOG = "wc_trade_log.csv"

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


def _get_pm_client():
    """
    Returns a Polymarket CLOB client using Ed25519 credentials (same as golf bot).
    Used only when DRY_RUN=False.
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        host   = "https://clob.polymarket.com"
        key_id = os.environ.get("POLYMARKET_KEY_ID", "")
        secret = os.environ.get("POLYMARKET_SECRET_KEY", "")
        pk     = os.environ.get("POLYMARKET_PK", "")

        creds  = ApiCreds(api_key=key_id, api_secret=secret, api_passphrase="")
        client = ClobClient(host, key=pk, chain_id=137, creds=creds, signature_type=1)
        return client
    except Exception as e:
        return None


def place_order(signal: dict, size_usdc: float) -> dict:
    """
    Places a real order on Polymarket.
    token_id must be resolved from the market slug before calling.
    Requires DRY_RUN=False and valid credentials.
    """
    market = signal.get("market", "")
    direction = signal.get("direction", "YES")
    outcome   = signal.get("outcome", "")
    price_pct = float(signal.get("entry_price_pct", 50))

    headers = {
        "Authorization": f"Bearer {os.environ.get('POLYMARKET_API_KEY', '')}",
        "Content-Type":  "application/json",
    }

    try:
        search = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": market},
            headers=headers,
            timeout=10,
        )
        search.raise_for_status()
        markets = search.json()
        if not markets:
            raise ValueError(f"Market slug not found: {market}")

        mkt    = markets[0] if isinstance(markets, list) else markets
        tokens = mkt.get("tokens", [])
        token  = next((t for t in tokens if t.get("outcome", "").lower() == outcome.lower()), None)
        if not token:
            raise ValueError(f"Outcome '{outcome}' not found in market tokens: {[t.get('outcome') for t in tokens]}")

        token_id = token.get("token_id") or token.get("id")

        client = _get_pm_client()
        if not client:
            return {"error": "CLOB client unavailable — check credentials"}

        price  = round(price_pct / 100, 4)
        shares = int(size_usdc / max(price, 0.001))
        if shares < 1:
            return {"error": f"Size ${size_usdc:.2f} too small at price {price}"}

        result = client.orders.create({
            "marketSlug": market,
            "intent":     "ORDER_INTENT_BUY_LONG" if direction == "YES" else "ORDER_INTENT_SELL_LONG",
            "type":       "ORDER_TYPE_LIMIT",
            "price":      {"value": f"{price:.4f}", "currency": "USD"},
            "quantity":   shares,
            "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        })
        order_id = result.get("order", result).get("id", "")
        log_signal(signal, executed=True, execution_id=order_id)
        return {"orderID": order_id, "shares": shares, "price": price, "raw": result}

    except Exception as e:
        log_signal(signal, executed=False, error=str(e))
        return {"error": str(e)}


def read_trade_log() -> list:
    if not os.path.exists(TRADE_LOG):
        return []
    rows = []
    with open(TRADE_LOG, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows
