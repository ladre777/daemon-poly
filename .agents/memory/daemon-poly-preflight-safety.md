---
name: DAEMON-POLY preflight safety gates
description: How the real-money preflight safety nets work and the persistence rule any new stateful gate must follow
---

# Preflight safety gates (`run_preflight`)

DAEMON-POLY keeps aggressive sizing (operator's explicit choice) but layers protective gates in `run_preflight`, the single chokepoint every order passes through (Claude + deterministic + movement paths all funnel here via `queue_trade` → veto worker → `execute_trade`). Putting a gate here makes it bulletproof regardless of entry source.

Gates that exist beyond the edge/position/shot thresholds:
- `suspended` hard stop (cleared only by `AUTOPILOT: RESUME`).
- 20% drawdown auto-suspend, measured against `drawdown_baseline` (falls back to `starting_bankroll`).
- PF-09 loss-position gate: never add to a player whose open position is already down >30% (current `last_odds` vs stored `entry_pct`; guarded by `entry_pct > 0`).
- PF-10 Saturday cap: max 2 trades when `round_num == 3`, tracked by `saturday_trade_count`.

## Rule: any new stateful safety flag MUST be in `PERSIST_KEYS`
**Why:** a drawdown auto-suspend set `state["suspended"]=True` and called `save_state()`, but `suspended` was not in `PERSIST_KEYS`, so a process restart reloaded it as `False` and silently bypassed the suspension — defeating the whole safety net on a real-money bot.
**How to apply:** when adding any flag that gates trading or tracks a cap, add it to both the `state` defaults AND `PERSIST_KEYS`. If it counts historical events (like `saturday_trade_count`), also backfill it in `load_state` from `open_positions`/`closed_positions` so a mid-event restart can't reset the cap.

## Rule: drawdown RESUME must re-anchor, not just clear
**Why:** clearing `suspended` alone is useless if bankroll is still below the 20% threshold — the next preflight instantly re-suspends, contradicting the "resume" message.
**How to apply:** `AUTOPILOT: RESUME` sets `drawdown_baseline = bankroll` so the gate measures the *next* 20% from the resume point. `starting_bankroll` is left untouched so session P&L reporting stays accurate.

## Slash Golf API quota = silent no-trade
**Why:** the bot stopped trading entirely because the Slash Golf (RapidAPI live-golf-data) leaderboard returned HTTP 429 "exceeded MONTHLY quota for BASIC plan". The old `fetch_leaderboard` parsed the 429 JSON, found no `leaderboardRows`, and stored an empty leaderboard with `data_stale=False` — a silent fallback. No leaderboard ⇒ `_estimate_true_probs` empty ⇒ zero edge candidates ⇒ no auto-trades, with no alert.
**How to apply:** any external-feed fetch must treat non-200 status AND empty payloads as failures (raise → set `data_stale=True`, keep last good data, alert throttled). If the live bot "isn't trading," check the data feed first (leaderboard count vs odds count) before suspecting the trade gates. Polymarket odds working while leaderboard is empty is the tell. Fix requires the user to upgrade/reset the RapidAPI plan — not a code change.
