import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = os.environ.get("STATE_FILE", "wc_state.json")

DEFAULT_STATE = {
    "active_positions":        [],
    "closed_positions":        [],
    "current_phase":           "GROUP_STAGE",
    "dry_run":                 False,  # default LIVE — use DRY_RUN=true env var to override
    "phase_trade_counts": {
        "GROUP_STAGE": 0,
        "R32":         0,
        "R16":         0,
        "QF":          0,
        "SF":          0,
        "FINAL":       0,
    },
    "total_bankroll_deployed_pct": 0.0,
    "total_realized_loss_pct":     0.0,
    "last_signal_time":        None,
    "phase_epoch":             {},   # {sport_key: event_label} — identity for auto-reset
}

CAPS = {
    "CASCADE": 8,
    "BRACKET": 6,
    "IN_PLAY": 5,
    "LADDER":  5,
    "PROP":    5,
}

MAX_CONCURRENT       = 6   # 3 was too low for multi-sport (MLB + WNBA + Golf)
MAX_TRADES_PER_PHASE = 10  # per-phase cap; WC-only phases reset per tournament
MAX_WINNER_ENTRY_PCT = 25  # WC-only gate (see PF-08 below)
MAX_PROPS_TOTAL_PCT  = 15

# Scaling per-trade cap: 20% of live buying power, floor $10, ceiling $50.
# At $43 bankroll → $10 cap (same as before).
# At $100 → $20, at $200 → $40, at $300+ → $50 (hard ceiling).
MAX_TRADE_CAP_PCT   = 20.0   # % of buying power
MAX_TRADE_CAP_FLOOR = 10.0   # never below $10
MAX_TRADE_CAP_CEIL  = 50.0   # never above $50


def get_trade_cap(buying_power: float) -> float:
    """Return the per-trade dollar cap scaled to live buying power."""
    return max(MAX_TRADE_CAP_FLOOR,
               min(buying_power * MAX_TRADE_CAP_PCT / 100, MAX_TRADE_CAP_CEIL))


# Backward-compat alias used in display strings (Telegram/logs).
# At runtime this is the floor; call get_trade_cap(bp) for the live cap.
MAX_TRADE_USD = MAX_TRADE_CAP_FLOOR

# Halt ALL new trading once realized losses reach this % of bankroll.
KILL_SWITCH_DRAWDOWN_PCT = 5.0
NEAR_MISS_TTL_SEC = 1800  # 30 min — cap-blocked signals expire after this


def is_phase_cap_exhausted() -> bool:
    """Return True when the current phase has reached MAX_TRADES_PER_PHASE.
    Used to skip LLM calls entirely when no new trade slot is available."""
    state = load_state()
    phase = state.get("current_phase", "GROUP_STAGE")
    return state.get("phase_trade_counts", {}).get(phase, 0) >= MAX_TRADES_PER_PHASE


def reset_phase_for_event_change(sport_key: str, new_event_label: str) -> bool:
    """Zero the current phase's trade count when a real event/tournament change is
    detected for sport_key.  Only resets when the stored epoch differs from
    new_event_label — so mid-phase restarts never trigger a spurious reset (the
    epoch is persisted in state alongside the count).

    Returns True when the count was actually zeroed (caller should log/notify).
    Called by event_tracker after every confirmed tournament switch."""
    with STATE_LOCK:
        state     = load_state()
        epoch     = state.setdefault("phase_epoch", {})
        old_label = epoch.get(sport_key, "")
        if old_label == new_event_label:
            return False   # same event — cap holds, no reset
        phase     = state.get("current_phase", "GROUP_STAGE")
        old_count = state.get("phase_trade_counts", {}).get(phase, 0)
        state.setdefault("phase_trade_counts", {})[phase] = 0
        epoch[sport_key] = new_event_label
        save_state(state)
        print(f"[gates] Phase count auto-reset: {sport_key} event "
              f"'{old_label}' → '{new_event_label}' | cleared {old_count} trade(s) "
              f"in phase {phase}")
        return True



# Single process-wide lock guarding every load->mutate->save cycle. The
# Telegram bot, veto worker, and learning background threads all write state;
# unlocked read-modify-write loses updates (real-money positions/drawdown).
STATE_LOCK = threading.RLock()


def _iso_to_ts(iso: str) -> float:
    """Convert ISO-8601 string to Unix timestamp (0.0 on parse failure)."""
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0



def load_state() -> dict:
    with STATE_LOCK:
        if Path(STATE_FILE).exists():
            with open(STATE_FILE) as f:
                saved = json.load(f)
            merged = DEFAULT_STATE.copy()
            merged.update(saved)
            return merged
        return DEFAULT_STATE.copy()


def save_state(state: dict):
    with STATE_LOCK:
        # Atomic write: never leave a half-written state file if killed mid-dump.
        tmp = f"{STATE_FILE}.tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)


def update_state(mutator):
    """Locked read-modify-write: mutator(state) edits in place; returns state."""
    with STATE_LOCK:
        state = load_state()
        mutator(state)
        save_state(state)
        return state


def check_gates(signal: dict) -> tuple:
    state      = load_state()
    violations = []

    # KILL SWITCH (hard halt): block every new trade once realized drawdown
    # reaches the limit. Reset requires clearing total_realized_loss_pct.
    realized_loss = state.get("total_realized_loss_pct", 0.0)
    if realized_loss >= KILL_SWITCH_DRAWDOWN_PCT:
        violations.append(
            f"KILL_SWITCH: realized drawdown {realized_loss:.1f}% ≥ "
            f"{KILL_SWITCH_DRAWDOWN_PCT}% — all trading halted"
        )
        return False, violations

    edge         = signal.get("edge", "")
    size         = float(signal.get("size_pct_bankroll", 0))
    entry_price  = float(signal.get("entry_price_pct", 0))
    phase        = state.get("current_phase", "GROUP_STAGE")

    # PF-00: Direction must be YES — executor is long-only (buying NO is not
    # supported; the AI should buy the opposing outcome's YES market instead).
    direction = (signal.get("direction") or "YES").upper()
    if direction not in ("YES", "BUY", "LONG"):
        violations.append(
            f"PF-00: direction '{direction}' is not executable — "
            f"buy the opposing outcome's YES market instead of going NO/short"
        )

    # PF-01: Size cap by edge type
    cap = CAPS.get(edge, 5)
    if size > cap:
        violations.append(f"PF-01: Size {size}% exceeds {edge} cap of {cap}%")

    # PF-03: Max concurrent positions
    active = len(state.get("active_positions", []))
    if active >= MAX_CONCURRENT:
        violations.append(f"PF-03: Already {active} active positions (max {MAX_CONCURRENT})")

    # PF-04: Never hold duplicate positions in the same market+outcome.
    # Different outcomes in one market are allowed (e.g. multiple golfers in a
    # tournament-winner market is a valid hedge) — only stacking the identical
    # bet is blocked.
    sig_market  = (signal.get("market") or "").strip().lower()
    sig_outcome = (signal.get("outcome") or "").strip().lower()
    for p in state.get("active_positions", []):
        if ((p.get("market") or "").strip().lower() == sig_market
                and (p.get("outcome") or "").strip().lower() == sig_outcome):
            violations.append(
                f"PF-04: Already holding {p.get('outcome')} {p.get('direction')} "
                f"in '{signal.get('market')}' — no doubling up on the same outcome"
            )
            break


    # PF-09: No adding to a losing position.
    # A YES contract's price falling below the entry price means the position is
    # underwater (probability moved against us). Block any new trade on the same
    # market (matched by market_slug, or market name when no slug is present)
    # when the current signal price is below the stored entry price of an
    # existing open position — regardless of which outcome is being traded.
    sig_slug = (signal.get("market_slug") or "").strip()
    for p in state.get("active_positions", []):
        p_slug   = (p.get("market_slug") or "").strip()
        p_market = (p.get("market") or "").strip().lower()
        same_market = (
            (sig_slug and p_slug and sig_slug == p_slug) or
            (not sig_slug and sig_market and p_market and sig_market == p_market)
        )
        if not same_market:
            continue
        p_entry = float(p.get("entry_price", 0) or 0)
        if p_entry > 0 and entry_price < p_entry:
            violations.append(
                f"PF-09: existing '{p.get('outcome')}' position entered at {p_entry}¢; "
                f"current signal price {entry_price:.1f}¢ — adding to a losing position"
            )
            break

    # PF-08: Winner market price ceiling — World Cup knockout rounds only.
    # Golf/MLB/WNBA leaders routinely price above 25¢ in final rounds; this gate
    # must NOT fire for non-WC sports or it blocks every legitimate golf trade.
    if (signal.get("sport") == "world_cup"
            and phase in ("QF", "SF", "FINAL")
            and "winner" in signal.get("market", "").lower()):
        if entry_price > MAX_WINNER_ENTRY_PCT:
            violations.append(
                f"PF-08: Winner entry at {entry_price}% exceeds {MAX_WINNER_ENTRY_PCT}% ceiling in {phase}"
            )

    # PF-10: Max trades per phase
    phase_count = state.get("phase_trade_counts", {}).get(phase, 0)
    if phase_count >= MAX_TRADES_PER_PHASE:
        violations.append(f"PF-10: {phase_count} trades in {phase} phase (max {MAX_TRADES_PER_PHASE})")


    # PF-10-SAT: Saturday hard cap — maximum 2 trades placed on any Saturday (UTC).
    # Counts all positions opened today from both active and closed lists so the
    # cap cannot be bypassed by closing between trades. The existing 10-per-phase
    # cap (above) remains in force independently — both gates must pass.
    now_utc_sat = datetime.now(timezone.utc)
    if now_utc_sat.weekday() == 5:   # 0=Mon … 5=Sat … 6=Sun
        today_str = now_utc_sat.strftime("%Y-%m-%d")
        SAT_TRADE_CAP = 2
        sat_count = sum(
            1 for p in state.get("active_positions", []) + state.get("closed_positions", [])
            if (p.get("opened_at") or "").startswith(today_str)
        )
        if sat_count >= SAT_TRADE_CAP:
            violations.append(
                f"PF-10-SAT: {sat_count} trade(s) already placed today (Saturday cap is {SAT_TRADE_CAP})"
            )

    # Bankroll deployment ceiling
    deployed = state.get("total_bankroll_deployed_pct", 0)
    if deployed + size > 80:
        violations.append(f"BANKROLL: Adding {size}% → {deployed + size:.1f}% total deployed (max 80%)")

    # PF-WC-03: Prop markets total cap 15%
    if edge == "PROP":
        prop_deployed = sum(
            p.get("size_pct", 0) for p in state.get("active_positions", [])
            if p.get("edge") == "PROP"
        )
        if prop_deployed + size > MAX_PROPS_TOTAL_PCT:
            violations.append(f"PF-WC-03: Prop exposure {prop_deployed + size:.1f}% exceeds {MAX_PROPS_TOTAL_PCT}%")

    passed = len(violations) == 0
    return passed, violations


def record_signal_sent(signal: dict):
    with STATE_LOCK:
        state = load_state()
        state["last_signal_time"] = datetime.now(timezone.utc).isoformat()
        save_state(state)


def record_trade_opened(signal: dict):
    with STATE_LOCK:
        state  = load_state()
        phase  = state.get("current_phase", "GROUP_STAGE")

        position = {
            "market":       signal.get("market"),
            "market_slug":  signal.get("market_slug", ""),
            "direction":    signal.get("direction"),
            "outcome":      signal.get("outcome"),
            "entry_price":  signal.get("entry_price_pct"),
            "target_exit":  signal.get("target_exit_pct"),
            "size_pct":     signal.get("size_pct_bankroll"),
            "edge":         signal.get("edge"),
            "sport":        signal.get("sport", ""),
            "opened_at":    datetime.now(timezone.utc).isoformat(),
            "order_id":     signal.get("order_id", ""),
            # Execution fill — stored so the P&L monitor can compute
            # unrealized profit without re-fetching position size.
            "shares":       int(signal.get("shares", 0) or 0),
            "notional_usd": float(signal.get("notional_usd", 0.0) or 0.0),
        }

        state["active_positions"].append(position)
        state["phase_trade_counts"][phase] = state["phase_trade_counts"].get(phase, 0) + 1
        state["total_bankroll_deployed_pct"] = (
            state.get("total_bankroll_deployed_pct", 0) + float(signal.get("size_pct_bankroll", 0))
        )
        save_state(state)


def dedupe_active_positions() -> int:
    """Collapse duplicate active positions (same market+outcome+direction) into
    one, keeping the earliest. Returns how many duplicates were removed. Cleans
    up state left behind before the PF-04 duplicate gate existed."""
    with STATE_LOCK:
        state   = load_state()
        seen    = set()
        kept    = []
        removed = 0
        for p in state.get("active_positions", []):
            key = (
                (p.get("market") or "").strip().lower(),
                (p.get("outcome") or "").strip().lower(),
                (p.get("direction") or "").strip().upper(),
            )
            if key in seen:
                removed += 1
                state["total_bankroll_deployed_pct"] = max(
                    0, state.get("total_bankroll_deployed_pct", 0) - float(p.get("size_pct", 0) or 0)
                )
            else:
                seen.add(key)
                kept.append(p)
        if removed:
            state["active_positions"] = kept
            save_state(state)
        return removed


def record_trade_closed(market: str, outcome: str, pnl_pct: float = 0.0) -> float:
    """Close a position. Optionally pass realized pnl_pct (negative = loss) to
    feed the drawdown kill switch. Returns total_realized_loss_pct after update."""
    with STATE_LOCK:
        state      = load_state()
        dry        = state.get("dry_run", True)
        remaining  = []
        closed_any = False
        for pos in state["active_positions"]:
            if pos.get("market") == market and pos.get("outcome") == outcome:
                closed_any       = True
                pos["closed_at"] = datetime.now(timezone.utc).isoformat()
                pos["pnl_pct"]   = pnl_pct
                state["closed_positions"].append(pos)
                size = float(pos.get("size_pct", 0))
                state["total_bankroll_deployed_pct"] = max(
                    0, state.get("total_bankroll_deployed_pct", 0) - size
                )
            else:
                remaining.append(pos)
        state["active_positions"] = remaining

        # Track realized losses for the kill switch. In LIVE mode only count a loss
        # when a tracked position actually closed (guards against a typo'd CLOSE
        # arming the switch). In DRY RUN positions aren't persisted, so trust the
        # operator-supplied pnl so the switch can be exercised during testing.
        if pnl_pct < 0 and (closed_any or dry):
            state["total_realized_loss_pct"] = round(
                state.get("total_realized_loss_pct", 0.0) + abs(pnl_pct), 2
            )

        save_state(state)
        return state.get("total_realized_loss_pct", 0.0)


def auto_close_resolved_positions(
    sport_key: str,
    valid_slugs: set,
    consecutive_empty: int,
    consecutive_empty_threshold: int = 3,
) -> list:
    """Remove active positions for a sport whose market has resolved on Polymarket.

    A position is considered resolved when its market_slug is absent from the live
    catalog.  Two modes:

    1. Non-empty catalog (valid_slugs is non-empty):
       Any position for this sport whose market_slug is not in valid_slugs is
       resolved — the outcome-level market is gone from the live exchange.

    2. Empty catalog, consecutive threshold reached:
       When the catalog fetch returns nothing for N consecutive cycles the market
       is very likely settled/delisted.  Only then do we act, to avoid wiping
       positions on a single transient API failure.

    Removed positions are appended to closed_positions with closed_reason so the
    operator has a full audit trail.  total_bankroll_deployed_pct is decremented.
    Returns the list of position dicts that were removed."""
    resolved = []
    with STATE_LOCK:
        state = load_state()
        remaining = []
        now_iso = datetime.now(timezone.utc).isoformat()
        for pos in state.get("active_positions", []):
            slug = (pos.get("market_slug") or "").strip()
            pos_sport = (pos.get("sport") or "").strip()

            # Only inspect positions that belong to this sport and have a slug.
            if pos_sport != sport_key or not slug:
                remaining.append(pos)
                continue

            stale = False
            if valid_slugs:
                # Catalog healthy — slug is absent → market resolved.
                stale = slug not in valid_slugs
            elif consecutive_empty >= consecutive_empty_threshold:
                # Catalog persistently empty → treat as season/tournament over.
                stale = True

            if stale:
                resolved.append(pos)
                closed = dict(pos)
                closed["closed_at"]     = now_iso
                closed["pnl_pct"]       = 0.0
                closed["closed_reason"] = "auto_resolved/slug_missing"
                state["closed_positions"].append(closed)
                state["total_bankroll_deployed_pct"] = max(
                    0,
                    state.get("total_bankroll_deployed_pct", 0)
                    - float(pos.get("size_pct", 0) or 0),
                )
            else:
                remaining.append(pos)

        if resolved:
            state["active_positions"] = remaining
            save_state(state)
    return resolved


def reset_positions() -> int:
    """Operator recovery: wipe all active positions and reset deployed-% counter.
    Use when Railway state contains stale/orphaned positions (e.g. after a
    tournament ends and those markets resolved on Polymarket).
    Returns the number of positions that were cleared."""
    with STATE_LOCK:
        state = load_state()
        n = len(state.get("active_positions", []))
        state["active_positions"] = []
        state["total_bankroll_deployed_pct"] = 0.0
        save_state(state)
        return n


def reconcile_positions() -> tuple:
    """Cross-check active_positions in state against real Polymarket US holdings.

    Removes any position whose market_slug is NOT in the live portfolio (ghost
    entries caused by orders that were recorded but never actually filled, or by
    resolved markets whose per-outcome slug never appeared in the futures catalog
    so auto_close_resolved_positions never cleaned them).

    Positions with no market_slug are kept — they can't be verified and must be
    closed manually.

    Returns (removed: list[dict], kept: list[dict]).
    Fails SAFE: if the portfolio API call returns an empty dict (error), the state
    is left untouched so real positions are never wiped on a transient failure.
    """
    from pm_us import get_real_positions   # local import to avoid circular dep
    real = get_real_positions()
    # None = API call failed; {} = genuine empty portfolio. Only act on successful calls.
    if real is None:
        return [], []

    removed, kept = [], []
    with STATE_LOCK:
        state = load_state()
        for pos in state.get("active_positions", []):
            slug = (pos.get("market_slug") or "").strip()
            if not slug:
                # Can't verify — leave in place.
                kept.append(pos)
            elif slug in real:
                kept.append(pos)
            else:
                removed.append(pos)
                state["total_bankroll_deployed_pct"] = max(
                    0,
                    state.get("total_bankroll_deployed_pct", 0)
                    - float(pos.get("size_pct", 0) or 0),
                )
        if removed:
            state["active_positions"] = kept
            save_state(state)
    return removed, kept


def reset_drawdown() -> None:
    """Operator recovery: clear realized-loss tally so the kill switch disarms."""
    with STATE_LOCK:
        state = load_state()
        state["total_realized_loss_pct"] = 0.0
        save_state(state)


def reset_phase_counts() -> dict:
    """Operator recovery: reset all phase trade counters to zero.
    Returns the old counts so the operator can confirm what was cleared.
    Use RESET_PHASE_COUNT command — separate from RESET_POSITIONS which
    clears positions but leaves phase counts untouched."""
    with STATE_LOCK:
        state = load_state()
        old = dict(state.get("phase_trade_counts", {}))
        zeroed = {k: 0 for k in old} if old else {k: 0 for k in DEFAULT_STATE["phase_trade_counts"]}
        state["phase_trade_counts"] = zeroed
        save_state(state)
        return old


def add_near_miss(signal: dict) -> None:
    """Store a cap-blocked (PF-10 only) signal in the persisted near-miss
    watchlist so it can be re-evaluated when a trade slot opens up.
    Duplicate market+outcome entries are replaced; entries expire after
    NEAR_MISS_TTL_SEC seconds and are pruned on every write."""
    with STATE_LOCK:
        state = load_state()
        now_ts = datetime.now(timezone.utc).timestamp()
        cutoff = now_ts - NEAR_MISS_TTL_SEC
        key = (
            (signal.get("market") or "").strip().lower(),
            (signal.get("outcome") or "").strip().lower(),
        )
        # Prune expired + remove any existing entry for the same market+outcome.
        watchlist = [
            e for e in state.get("near_miss_watchlist", [])
            if _iso_to_ts(e.get("stored_at", "")) > cutoff
            and (
                (e["signal"].get("market") or "").strip().lower(),
                (e["signal"].get("outcome") or "").strip().lower(),
            ) != key
        ]
        watchlist.append({
            "signal":    signal,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        })
        state["near_miss_watchlist"] = watchlist
        save_state(state)


def pop_near_misses(sport_key: str = "") -> list:
    """Return and remove all non-expired near-miss entries for sport_key
    (or all sports when sport_key is ""). Expired entries are also pruned.
    Entries for other sports are left in state untouched."""
    with STATE_LOCK:
        state = load_state()
        cutoff = datetime.now(timezone.utc).timestamp() - NEAR_MISS_TTL_SEC
        watchlist = state.get("near_miss_watchlist", [])
        valid, kept = [], []
        for e in watchlist:
            if _iso_to_ts(e.get("stored_at", "")) <= cutoff:
                continue  # expired — discard silently
            if sport_key and e["signal"].get("sport", "") != sport_key:
                kept.append(e)  # different sport — leave for its own scan cycle
            else:
                valid.append(e)
        state["near_miss_watchlist"] = kept
        save_state(state)
        return valid


def update_phase(new_phase: str):
    with STATE_LOCK:
        state = load_state()
        state["current_phase"] = new_phase
        save_state(state)
        return state


def set_dry_run(enabled: bool):
    with STATE_LOCK:
        state = load_state()
        state["dry_run"] = enabled
        save_state(state)


def get_state_summary() -> str:
    state = load_state()
    phase = state.get("current_phase", "?")
    return (
        f"Phase: {phase} | "
        f"Active: {len(state['active_positions'])} | "
        f"Deployed: {state.get('total_bankroll_deployed_pct', 0):.1f}% | "
        f"Phase trades: {state.get('phase_trade_counts', {}).get(phase, 0)}/{MAX_TRADES_PER_PHASE} | "
        f"Mode: {'DRY RUN ⚪' if state.get('dry_run', True) else '🔴 LIVE'}"
    )
