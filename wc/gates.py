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
    "dry_run":                 True,
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
}

CAPS = {
    "CASCADE": 8,
    "BRACKET": 6,
    "IN_PLAY": 5,
    "LADDER":  5,
    "PROP":    5,
}

MAX_CONCURRENT       = 3
MAX_TRADES_PER_PHASE = 5
MAX_WINNER_ENTRY_PCT = 25
MAX_PROPS_TOTAL_PCT  = 15

# Hard per-trade dollar ceiling for LIVE execution. This is the final money
# boundary — enforced again inside executor.place_order regardless of any
# upstream % sizing, so no signal can ever risk more than this on one trade.
MAX_TRADE_USD        = 10.0

# Halt ALL new trading once realized losses reach this % of bankroll.
KILL_SWITCH_DRAWDOWN_PCT = 5.0


# Single process-wide lock guarding every load->mutate->save cycle. The
# Telegram bot, veto worker, and learning background threads all write state;
# unlocked read-modify-write loses updates (real-money positions/drawdown).
STATE_LOCK = threading.RLock()


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

    # PF-01: Size cap by edge type
    cap = CAPS.get(edge, 5)
    if size > cap:
        violations.append(f"PF-01: Size {size}% exceeds {edge} cap of {cap}%")

    # PF-03: Max concurrent positions
    active = len(state.get("active_positions", []))
    if active >= MAX_CONCURRENT:
        violations.append(f"PF-03: Already {active} active positions (max {MAX_CONCURRENT})")

    # PF-04: Never hold duplicate positions in the same market
    sig_market = (signal.get("market") or "").strip().lower()
    for p in state.get("active_positions", []):
        if (p.get("market") or "").strip().lower() == sig_market:
            violations.append(
                f"PF-04: Already holding a position in '{signal.get('market')}' "
                f"({p.get('outcome')} {p.get('direction')}) — no doubling up"
            )
            break

    # PF-08: Winner market price ceiling in QF+
    if phase in ("QF", "SF", "FINAL") and "winner" in signal.get("market", "").lower():
        if entry_price > MAX_WINNER_ENTRY_PCT:
            violations.append(
                f"PF-08: Winner entry at {entry_price}% exceeds {MAX_WINNER_ENTRY_PCT}% ceiling in {phase}"
            )

    # PF-10: Max trades per phase
    phase_count = state.get("phase_trade_counts", {}).get(phase, 0)
    if phase_count >= MAX_TRADES_PER_PHASE:
        violations.append(f"PF-10: {phase_count} trades in {phase} phase (max {MAX_TRADES_PER_PHASE})")

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
            "direction":    signal.get("direction"),
            "outcome":      signal.get("outcome"),
            "entry_price":  signal.get("entry_price_pct"),
            "target_exit":  signal.get("target_exit_pct"),
            "size_pct":     signal.get("size_pct_bankroll"),
            "edge":         signal.get("edge"),
            "opened_at":    datetime.now(timezone.utc).isoformat(),
            "order_id":     signal.get("order_id", ""),
        }

        state["active_positions"].append(position)
        state["phase_trade_counts"][phase] = state["phase_trade_counts"].get(phase, 0) + 1
        state["total_bankroll_deployed_pct"] = (
            state.get("total_bankroll_deployed_pct", 0) + float(signal.get("size_pct_bankroll", 0))
        )
        save_state(state)


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


def reset_drawdown() -> None:
    """Operator recovery: clear realized-loss tally so the kill switch disarms."""
    with STATE_LOCK:
        state = load_state()
        state["total_realized_loss_pct"] = 0.0
        save_state(state)


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
