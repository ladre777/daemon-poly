#!/usr/bin/env python3
"""
Build a deterministic synthetic ledger for exercising the evidence report.

No production data and no credentials are involved. Every row here is
invented, and the numbers are chosen to exercise the report's edge cases
rather than to look plausible:

- live orders that filled, partially filled, were cancelled, and were
  rejected, so every execution-quality branch has a row
- settlements with real fees, so ``net_realized_pnl`` is computable
- one settlement with a NULL fee, to prove the report refuses to net rather
  than treating missing as zero
- paper and refused rows in both settled and unsettled states
- provider calls covering success, billing failure, rate limiting, timeout
  and both truncation dispositions
- stage events covering scan-cap, no-contract-spec, market-quality and
  zero-balance refusal, so the blocker section can distinguish lookalikes

Usage::

    python -m scripts.make_evidence_fixture --out /tmp/fixture.db
    python -m scripts.production_readiness_report --db /tmp/fixture.db

Seeded and time-anchored, so two runs produce byte-identical databases and
the example report in ``docs/`` can be regenerated and diffed.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Fixed anchor so output is reproducible. 2026-08-20T00:00:00Z.
T0 = 1787184000.0
DAY = 86400.0


def build(path: str) -> None:
    if os.path.exists(path):
        os.remove(path)
    from memory.edge_store import SCHEMA as EDGE_SCHEMA
    from memory.order_store import SCHEMA as ORDER_SCHEMA
    from memory.telemetry_store import SCHEMA as TELEMETRY_SCHEMA

    c = sqlite3.connect(path)
    c.executescript(ORDER_SCHEMA)
    c.executescript(EDGE_SCHEMA)
    c.executescript(TELEMETRY_SCHEMA)

    # -- live orders --------------------------------------------------
    orders = [
        # (coid, ticker, action, requested, limit, state, filled, avg, fees, dry)
        ("coid-1", "KXBTCD-A", "buy", 10, 42.0, "filled", 10, 43.0, 30.0, 0),
        ("coid-2", "KXBTCD-B", "buy", 20, 55.0, "partially_filled", 8, 55.5, 24.0, 0),
        ("coid-3", "KXETHD-A", "sell", 15, 61.0, "cancelled", 0, None, 0.0, 0),
        ("coid-4", "KXHIGHNY-A", "buy", 5, 30.0, "rejected", 0, None, 0.0, 0),
        # dry-run row: must never appear in any live total
        ("coid-5", "KXWTI-A", "buy", 50, 20.0, "dry_run", 50, 20.0, 99.0, 1),
    ]
    for i, (coid, tk, act, req, lim, st, fil, avg, fees, dry) in enumerate(orders):
        c.execute(
            """INSERT INTO orders (client_order_id, intent_key, ticker, action, side,
               requested_count, limit_price_cents, time_in_force, state,
               filled_count, remaining_count, avg_fill_price_cents, fees_cents,
               dry_run, created_at, submitted_at, terminal_at)
               VALUES (?,?,?,?,'yes',?,?,'ioc',?,?,?,?,?,?,?,?,?)""",
            (coid, f"ik-{i}", tk, act, req, lim, st, fil, req - fil, avg, fees,
             dry, T0 + i * 60, T0 + i * 60 + 1, T0 + i * 60 + 12),
        )

    c.execute(
        """INSERT INTO fills (fill_id, client_order_id, ticker, side, action,
           count, price_cents, fees_cents, created_at, recorded_at, settled)
           VALUES ('f-1','coid-1','KXBTCD-A','yes','buy',10,43.0,30.0,?,?,1)""",
        (T0 + 5, T0 + 6))
    c.execute(
        """INSERT INTO fills (fill_id, client_order_id, ticker, side, action,
           count, price_cents, fees_cents, created_at, recorded_at, settled)
           VALUES ('f-2','coid-2','KXBTCD-B','yes','buy',8,55.5,24.0,?,?,1)""",
        (T0 + 65, T0 + 66))

    # Two settlements with fees, one deliberately missing its fee so the
    # report has to refuse to compute a net figure.
    c.execute(
        """INSERT INTO settlements (settlement_key, ticker, client_order_id,
           fill_id, fill_count, fill_price_cents, fees_cents, settlement_result,
           realized_pnl, settled_at, recorded_at)
           VALUES ('s-1','KXBTCD-A','coid-1','f-1',10,43.0,30.0,'yes',570.0,?,?)""",
        (T0 + DAY, T0 + DAY))
    c.execute(
        """INSERT INTO settlements (settlement_key, ticker, client_order_id,
           fill_id, fill_count, fill_price_cents, fees_cents, settlement_result,
           realized_pnl, settled_at, recorded_at)
           VALUES ('s-2','KXBTCD-B','coid-2','f-2',8,55.5,24.0,'no',-444.0,?,?)""",
        (T0 + 2 * DAY, T0 + 2 * DAY))

    c.execute(
        """INSERT INTO account_snapshot (id, balance_cents, available_balance_cents,
           limits_json, positions_json, reconciled_at)
           VALUES (1, 0.0, 0.0, '{}', '[]', ?)""", (T0 + 3 * DAY,))
    c.execute("INSERT INTO bot_state (id, kill_switch_tripped) VALUES (1, 0)")

    # -- edges: live, paper, refused ----------------------------------
    def edge(tk, cat, src, action, prob, settled, outcome, pnl, cf_price, coid=None):
        c.execute(
            """INSERT INTO edges (ticker, category, source, created_at,
               maker_probability, market_implied_probability, edge_size,
               action_taken, counterfactual_price_cents, counterfactual_direction,
               client_order_id, settled, outcome, pnl, settled_at)
               VALUES (?,?,?,?,?,?,?,?,?,'yes',?,?,?,?,?)""",
            (tk, cat, src, T0 + 100, prob, prob - 0.05, 0.05, action,
             cf_price, coid, int(settled), outcome, pnl,
             T0 + DAY if settled else None))

    edge("KXBTCD-A", "Crypto", "quant", "executed", 0.62, 1, "yes", 570.0, None, "coid-1")
    edge("KXBTCD-B", "Crypto", "quant", "executed", 0.48, 1, "no", -444.0, None, "coid-2")
    for i in range(6):
        edge(f"KXETHD-{i}", "Crypto", "quant", "dry_run",
             0.40 + i * 0.05, 1, "yes" if i % 2 else "no", 12.0 - i, 44.0)
    for i in range(8):
        edge(f"KXHIGHNY-{i}", "Weather", "llm", "skipped_risk",
             0.25 + i * 0.04, 1, "yes" if i % 3 == 0 else "no", 9.0 - i, 31.0)
    for i in range(4):
        edge(f"KXWTI-{i}", "Finance", "llm", "skipped_risk",
             0.30 + i * 0.05, 0, None, None, 28.0)

    # -- telemetry ------------------------------------------------------
    c.execute(
        """INSERT INTO pass_telemetry (pass_id, started_at, finished_at, dry_run,
           kalshi_env, order_strategy, balance_cents, markets_seen, pages_scanned,
           scan_cap_reached, candidates, excluded_category, excluded_liquidity,
           excluded_no_spec, scout_ms, pricing_ms, checker_ms, risk_ms, execution_ms)
           VALUES (1,?,?,1,'prod','taker',0.0,82000,410,1,297,79685,413,4,
                   118000,900,5400,40,120)""",
        (T0 + 3 * DAY, T0 + 3 * DAY + 130))

    stages = [
        ("scouted", None, 297), ("priced", None, 149),
        ("priced", "no_contract_spec", 4),
        ("priced", "market_quality", 12),
        ("scouted", "scan_cap_reached", 1),
        ("proposed", None, 11),
        ("proposed", "capped_per_event", 6),
        ("proposed", "ladder_deduped", 0),
        ("checker_verdict", "checker_rejected", 7),
        ("checker_verdict", "truncated_reject_recovered", 2),
        ("checker_verdict", "truncated_approval_abstained", 1),
        ("risk_decision", "zero_balance", 11),
    ]
    for stage, reason, n in stages:
        if not n:
            continue
        c.execute(
            """INSERT INTO stage_events (pass_id, recorded_at, stage, reason, count)
               VALUES (1,?,?,?,?)""", (T0 + 3 * DAY, stage, reason, n))

    calls = [
        ("maker", "moonshot", "kimi-k2.6", True, None, 200, 4607),
        ("maker", "moonshot", "kimi-k2.6", True, None, 200, 6051),
        ("maker", "moonshot", "kimi-k2.6", False, "provider_billing", 429, 210),
        ("maker", "gemini", "gemini-3.5-flash-lite", False, "provider_rate_limited", 429, 180),
        ("checker", "gemini", "gemini-3.5-flash-lite", True, None, 200, 3200),
        ("checker", "anthropic", "claude-haiku-4-5", False, "provider_billing", 400, 150),
        ("checker", "gemini", "gemini-3.5-flash-lite", False, "provider_timeout", None, 25000),
    ]
    for i, (role, prov, model, ok, outcome, status, ms) in enumerate(calls):
        st = T0 + 3 * DAY + i
        c.execute(
            """INSERT INTO provider_calls (pass_id, recorded_at, role, provider,
               model, started_at, finished_at, elapsed_ms, ok, outcome, http_status)
               VALUES (1,?,?,?,?,?,?,?,?,?,?)""",
            (st, role, prov, model, st, st + ms / 1000.0, float(ms),
             int(ok), outcome, status))

    c.commit()
    c.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a synthetic evidence fixture.")
    ap.add_argument("--out", required=True, help="path to write the fixture to")
    args = ap.parse_args(argv)
    build(args.out)
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
