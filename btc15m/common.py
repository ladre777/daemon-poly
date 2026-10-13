#!/usr/bin/env python3
"""Shared helpers for the Polymarket US 15m BTC Up/Down paper stack.

Network rule (proven in ~/workspace/polymarket/pmus.py): the sandbox egress
path intermittently RSTs Python's TLS fingerprint to gateway.polymarket.us,
so ALL HTTP goes through curl subprocesses with a Chrome UA.
"""
import json
import os
import subprocess
import time

os.environ["no_proxy"] = "localhost,127.0.0.1"
os.environ["NO_PROXY"] = "localhost,127.0.0.1"

GATEWAY = "https://gateway.polymarket.us"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SERIES = "btc-updown-15m"
WINDOW_SEC = 900  # 15-minute windows on :00/:15/:30/:45 UTC boundaries

# Paper-trading economics.
# Live fee schedule pulled from Gamma (feeType: crypto_fees_v2), via the
# 2026-10-03 edge notes: taker-only, makers pay 0, 20% of taker fees fund
# the rebate pool (not counted here).
#   fee_per_share = 0.07 * price * (1 - price)
# = 1.75c/share at 50c, 1.12c/share at 80c.
# A taker buying the open at 50c needs a 51.75% hit rate to break even.
TAKER_FEE_RATE = 0.07


def taker_fee_per_share(price):
    """Taker fee for one share bought at `price` (0..1). Makers pay 0."""
    p = min(max(float(price), 0.0), 1.0)
    return TAKER_FEE_RATE * p * (1.0 - p)


SLIPPAGE_PER_LEG = 0.005  # expected slippage haircut per contract


def http_get(url, timeout=30):
    """GET via curl. Returns (status:int, body:str)."""
    cmd = ["curl", "-s", "-m", str(timeout), "-A", UA, url,
           "-H", "Accept: application/json", "-w", "\nHTTPSTATUS:%{http_code}"]
    p = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
    text = p.stdout.decode("utf-8", "replace")
    if "HTTPSTATUS:" in text:
        body, _, code = text.rpartition("HTTPSTATUS:")
        try:
            return int(code.strip()), body
        except ValueError:
            return 0, text
    return 0, text


def get_json(url, timeout=30, retries=2):
    """GET + parse JSON, with retries. Raises RuntimeError on failure.

    Client errors (4xx, except 429) fail fast -- retrying a 404 is pure
    waste, and unlisted-window 404s are the common case. Only network
    failures (st 0), 429 rate-limits, and 5xx are retried.
    """
    last = None
    for _ in range(retries + 1):
        st, body = http_get(url, timeout)
        if st // 100 == 2:
            try:
                return json.loads(body or "{}")
            except Exception as e:
                last = f"bad json: {e}"
        elif st == 429 or st // 100 == 5 or st == 0:
            last = f"HTTP {st}: {body[:200]}"
        else:
            raise RuntimeError(f"GET failed {url}: HTTP {st}: {body[:200]}")
        time.sleep(1)
    raise RuntimeError(f"GET failed {url}: {last}")


def append_jsonl(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def now_s():
    return int(time.time())
