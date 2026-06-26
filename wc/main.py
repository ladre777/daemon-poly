import schedule
import time
import os
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

from sports_config import active_sports, SPORT_CONFIGS
from scout import get_all_matches, get_live_matches, get_match_detail, is_in_play_window
from market_reader import (
    get_sport_futures, scan_stage_elimination_ladders,
    find_game_market_odds, search_markets,
)
from signal_engine import run_signal, run_in_play_signal, run_checker
from telegram_ops import send_trade_signal, send_monitor_signal, send_status, send_error, get_updates
from gates import (
    check_gates, record_signal_sent, record_trade_opened,
    record_trade_closed, update_phase, set_dry_run,
    get_state_summary, load_state, save_state, STATE_FILE,
    KILL_SWITCH_DRAWDOWN_PCT, reset_drawdown,
)
from executor import log_signal, dry_run_signal, place_order, read_trade_log

POLL_INTERVAL_MIN    = 3
IN_PLAY_INTERVAL_SEC = 45
STATUS_PORT          = int(os.environ.get("PORT", 8099))

_tg_offset = 0

# In-memory alert throttle: looping 4 sports every few minutes can flood Telegram
# with repeat gate-fail / checker-kill / monitor notices. Approved TRADE signals
# and the kill-switch alert are NEVER throttled — only routine repeats are deduped.
_alert_history     = {}
ALERT_COOLDOWN_SEC = 1800


def _should_send(key: str) -> bool:
    now  = time.time()
    last = _alert_history.get(key, 0)
    if now - last >= ALERT_COOLDOWN_SEC:
        _alert_history[key] = now
        return True
    return False


def _active_label() -> str:
    return ", ".join(f"{c['emoji']} {c['label']}" for c in active_sports())


# ── STATUS SERVER (keep-alive ping target) ──────────────────────────────────

class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = load_state()
        body  = (
            f"DÆMON-POLY MULTI-SPORT ONLINE\n"
            f"Sports: {_active_label()}\n"
            f"Phase: {state.get('current_phase')} | "
            f"Active: {len(state.get('active_positions', []))} | "
            f"Mode: {'DRY RUN' if state.get('dry_run', True) else 'LIVE'}\n"
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_status_server():
    server = HTTPServer(("0.0.0.0", STATUS_PORT), StatusHandler)
    server.serve_forever()


# ── TELEGRAM COMMAND LISTENER ───────────────────────────────────────────────

def handle_command(text: str):
    text = text.strip()
    cmd  = text.upper()
    state = load_state()

    # STATUS
    if cmd in ("STATUS", "/STATUS"):
        summary = get_state_summary()
        positions = state.get("active_positions", [])
        pos_lines = "\n".join(
            f"  {p.get('edge','?')} | {p.get('outcome','?')} {p.get('direction','?')} "
            f"@ {p.get('entry_price','?')}¢ | {p.get('size_pct','?')}% bankroll"
            for p in positions
        ) or "  None"
        send_status(f"Sports: {_active_label()}\n{summary}\n\nOPEN POSITIONS:\n{pos_lines}")

    # SCAN
    elif cmd in ("SCAN", "/SCAN"):
        send_status("🔍 Manual scan triggered...")
        run_main_analysis(triggered_by="MANUAL")

    # PHASE: R32 etc. (World Cup knockout tracking)
    elif cmd.startswith("PHASE:") or cmd.startswith("PHASE "):
        new_phase = text.split(":", 1)[-1].strip().upper() if ":" in text else text.split(" ", 1)[-1].strip().upper()
        valid = ("GROUP_STAGE", "R32", "R16", "QF", "SF", "FINAL")
        if new_phase in valid:
            update_phase(new_phase)
            send_status(f"✅ Phase updated to {new_phase}")
        else:
            send_error(f"Invalid phase '{new_phase}'. Valid: {', '.join(valid)}")

    # DRY: ON / DRY: OFF
    elif cmd.startswith("DRY:") or cmd.startswith("DRY "):
        mode = text.split(":", 1)[-1].strip().upper() if ":" in text else text.split(" ", 1)[-1].strip().upper()
        if mode == "OFF":
            set_dry_run(False)
            send_status("🔴 DRY RUN disabled — LIVE mode active. Real orders will be placed.")
        else:
            set_dry_run(True)
            send_status("⚪ DRY RUN enabled. No real orders will be placed.")

    # CLOSE: <market> <outcome> [pnl_pct]
    elif cmd.startswith("CLOSE:") or cmd.startswith("CLOSE "):
        parts = text.split(None, 3)
        if len(parts) >= 3:
            market  = parts[1]
            outcome = parts[2]
            pnl_pct = 0.0
            if len(parts) >= 4:
                try:
                    pnl_pct = float(parts[3].replace("%", "").replace("+", ""))
                except ValueError:
                    pass
            total_loss = record_trade_closed(market, outcome, pnl_pct)
            extra = f" | PnL {pnl_pct:+.1f}%" if pnl_pct else ""
            send_status(f"✅ Position closed: {market} / {outcome}{extra}")
            if total_loss >= KILL_SWITCH_DRAWDOWN_PCT:
                send_error(
                    f"🛑 KILL SWITCH ARMED — realized drawdown {total_loss:.1f}% ≥ "
                    f"{KILL_SWITCH_DRAWDOWN_PCT}%. All new trades are now blocked."
                )
        else:
            send_error("Usage: CLOSE <market> <outcome> [pnl_pct]")

    # RESET DRAWDOWN — disarm the kill switch after a reviewed drawdown
    elif cmd in ("RESET_DRAWDOWN", "RESET DRAWDOWN", "RESETDRAWDOWN"):
        reset_drawdown()
        send_status("♻️ Realized drawdown reset to 0%. Kill switch disarmed.")

    # LOG
    elif cmd in ("LOG", "/LOG"):
        rows = read_trade_log()
        if not rows:
            send_status("No trades logged yet.")
        else:
            recent = rows[-5:]
            lines  = "\n".join(
                f"  {r.get('timestamp','')[:16]} {r.get('edge','?')} {r.get('outcome','?')} "
                f"{'EXEC' if r.get('executed')=='True' else 'signal'}"
                for r in recent
            )
            send_status(f"Last {len(recent)} log entries:\n{lines}")

    # MARKETS: <query>
    elif cmd.startswith("MARKETS:") or cmd.startswith("MARKETS "):
        query = text.split(None, 1)[1] if " " in text else "world cup"
        results = search_markets(query, limit=5)
        lines   = "\n".join(
            f"  {r.get('slug','?')} — {r.get('title','')}"
            for r in results[:5] if isinstance(r, dict) and "error" not in r
        )
        send_status(f"Market search '{query}':\n{lines or 'No results'}")

    # SPORTS — list what's active
    elif cmd in ("SPORTS", "/SPORTS"):
        lines = "\n".join(
            f"  {c['emoji']} {c['label']} — {'ON ✅' if c.get('active') else 'off'}"
            for c in SPORT_CONFIGS.values()
        )
        send_status(f"SPORT COVERAGE\n{lines}")

    # HELP
    elif cmd in ("HELP", "/HELP", "/START"):
        send_status(
            "DÆMON-POLY MULTI-SPORT COMMANDS\n"
            "──────────────────────\n"
            "STATUS — current state + positions\n"
            "SPORTS — list active sports\n"
            "SCAN — force analysis now (all sports)\n"
            "PHASE: R32|R16|QF|SF|FINAL — World Cup phase\n"
            "DRY: ON|OFF — toggle dry run mode\n"
            "CLOSE: <market> <outcome> [pnl_pct] — mark position closed\n"
            "RESET_DRAWDOWN — disarm the 5% kill switch\n"
            "LOG — last 5 logged signals\n"
            "MARKETS: <query> — search Polymarket markets\n"
            "HELP — this menu"
        )

    else:
        print(f"[TG] Unrecognised command: {text!r}")


def telegram_listener():
    global _tg_offset
    print("[TG] Listener started")
    while True:
        try:
            updates = get_updates(offset=_tg_offset)
            for upd in updates:
                _tg_offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message") or {}
                text = msg.get("text", "").strip()
                if text:
                    print(f"[TG CMD] {text!r}")
                    handle_command(text)
        except Exception as e:
            print(f"[TG] Listener error: {e}")
        time.sleep(2)


# ── SHARED GATE + CHECKER PIPELINE ──────────────────────────────────────────

def _passes_gates_and_checker(signal: dict) -> bool:
    """Run a TRADE signal through gates, then the Opus CHECKER. Returns True only
    if both pass. Identical safety path for scheduled and in-play signals."""
    passed, violations = check_gates(signal)
    signal["gate_check"] = "PASS" if passed else "FAIL"
    signal["gate_notes"] = "; ".join(violations) if violations else "All gates passed"

    if not passed:
        print(f"  GATE FAIL: {signal['gate_notes']}")
        log_signal(signal, executed=False)
        if _should_send(f"gatefail:{signal.get('sport')}:{signal.get('market')}:{signal.get('edge')}"):
            send_error(
                f"Signal blocked by gates:\n{signal['gate_notes']}\n"
                f"Market: {signal.get('market')} | Edge: {signal.get('edge')}"
            )
        return False

    # CHECKER: independent Opus verification before any execution.
    signal = run_checker(signal)
    print(f"  Checker: {signal.get('checker_verdict')} — {signal.get('checker_reason')}")
    log_signal(signal, executed=False)

    if signal.get("checker_verdict") != "APPROVED":
        print(f"  CHECKER KILLED: {signal.get('checker_reason')}")
        if _should_send(f"killed:{signal.get('sport')}:{signal.get('market')}:{signal.get('edge')}"):
            send_error(
                f"⚡ CHECKER KILLED\n{signal.get('checker_reason')}\n"
                f"Market: {signal.get('market')} | Edge: {signal.get('edge')}"
            )
        return False

    return True


# ── MAIN ANALYSIS LOOP (per sport) ──────────────────────────────────────────

def analyze_sport(sport_cfg: dict, dry: bool):
    label = f"{sport_cfg['emoji']} {sport_cfg['label']}"
    try:
        matches      = get_all_matches(sport_cfg)
        futures_odds = get_sport_futures(sport_cfg)

        context = f"Games listed: {len(matches)} | {get_state_summary()}"

        # World Cup keeps its Edge LADDER scan; harmless no-op for other sports.
        if sport_cfg["key"] == "world_cup":
            ladder_anom = scan_stage_elimination_ladders()
            if ladder_anom:
                futures_odds["Stage-of-Elimination Anomalies"] = ladder_anom
                context += f"\nLadder anomalies: {', '.join(ladder_anom.keys())}"

        signal = run_signal(sport_cfg, matches, futures_odds, context)
        stype  = signal.get("signal_type")
        print(f"  [{label}] Signal: {stype} | Edge: {signal.get('edge', 'N/A')}")

        if stype == "TRADE":
            if _passes_gates_and_checker(signal):
                send_trade_signal(signal)
                record_signal_sent(signal)
                if dry:
                    print(f"  {dry_run_signal(signal)}")
                else:
                    state2   = load_state()
                    bankroll = state2.get("bankroll_usdc", 100)
                    size_usd = bankroll * float(signal.get("size_pct_bankroll", 5)) / 100
                    result   = place_order(signal, size_usd)
                    if "error" in result:
                        send_error(f"Order failed: {result['error']}")
                    else:
                        signal["order_id"] = result.get("orderID", "")
                        record_trade_opened(signal)
                        send_status(
                            f"✅ ORDER PLACED\n"
                            f"{signal.get('outcome')} {signal.get('direction')} @ "
                            f"{result.get('price', '?')} | {result.get('shares', '?')} shares\n"
                            f"Order ID: {result.get('orderID', '?')}"
                        )

        elif stype == "MONITOR":
            if _should_send(f"monitor:{signal.get('sport')}:{signal.get('watch')}"):
                send_monitor_signal(signal)
            log_signal(signal)

        elif stype == "NO_SIGNAL":
            print(f"  [{label}] No edge: {signal.get('reason')}")

        else:
            print(f"  [{label}] Unexpected signal: {signal}")

    except Exception as e:
        msg = f"Main loop error [{label}]: {e}"
        print(f"  ERROR: {msg}")
        send_error(msg)


def run_main_analysis(triggered_by: str = "SCHEDULED"):
    now = datetime.now(timezone.utc)
    print(f"\n[{now.strftime('%H:%M:%S UTC')}] Analysis [{triggered_by}]")
    state = load_state()
    dry   = state.get("dry_run", True)
    for sport_cfg in active_sports():
        analyze_sport(sport_cfg, dry)


# ── IN-PLAY EDGE CHECK (per sport) ──────────────────────────────────────────

def run_in_play_check():
    for sport_cfg in active_sports():
        try:
            live = get_live_matches(sport_cfg)
        except Exception as e:
            print(f"  in-play fetch error [{sport_cfg['key']}]: {e}")
            continue

        for match in live:
            if not is_in_play_window(sport_cfg, match):
                continue
            print(f"  ⚡ IN-PLAY [{sport_cfg['label']}]: "
                  f"{match.get('home_team')} vs {match.get('away_team')} @ {match.get('clock')}")
            detail       = get_match_detail(sport_cfg, match["id"])
            game         = find_game_market_odds(
                match.get("home_team", ""), match.get("away_team", ""), sport_cfg["label"]
            )
            current_odds = game if game else {}
            signal       = run_in_play_signal(sport_cfg, match, detail, current_odds)

            if signal.get("signal_type") == "TRADE":
                if _passes_gates_and_checker(signal):
                    send_trade_signal(signal)
                    record_signal_sent(signal)


# ── HEARTBEAT ───────────────────────────────────────────────────────────────

def send_heartbeat():
    send_status(
        f"⚙️ HEARTBEAT\n"
        f"Sports: {_active_label()}\n"
        f"{get_state_summary()}\n"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )


# ── ENTRY POINT ─────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("DÆMON-POLY // Multi-Sport Agent v3.0")
    state = load_state()
    dry   = state.get("dry_run", True)
    print(f"Mode: {'DRY RUN ⚪' if dry else 'LIVE 🔴'} | Poll: {POLL_INTERVAL_MIN} min")
    print(f"Sports: {_active_label()}")
    print("=" * 55)

    required = ["ANTHROPIC_API_KEY", "TELEGRAM_TOKEN"]
    missing  = [s for s in required if not os.environ.get(s)]
    if missing:
        print(f"FATAL: Missing secrets: {missing}")
        return

    # Fail fast if state can't be persisted (e.g. Railway volume not mounted) —
    # otherwise dry_run/positions would silently reset on every restart. We do
    # NOT create the dir: a custom path must pre-exist as a mounted volume.
    sd = os.path.dirname(STATE_FILE)
    if sd and not os.path.isdir(sd):
        msg = (f"🚨 CRITICAL: state dir '{sd}' does not exist — the Railway volume "
               f"for STATE_FILE='{STATE_FILE}' is not mounted. Halting.")
        print(f"FATAL: {msg}")
        try:
            send_error(msg)
        except Exception:
            pass
        raise SystemExit(1)
    try:
        _probe = os.path.join(sd or ".", ".write_test")
        with open(_probe, "w") as f:
            f.write("ok")
        os.remove(_probe)
    except Exception as e:
        msg = (f"🚨 CRITICAL: cannot write state to '{STATE_FILE}' ({e}). "
               f"State would NOT persist across restarts — halting.")
        print(f"FATAL: {msg}")
        try:
            send_error(msg)
        except Exception:
            pass
        raise SystemExit(1)
    print(f"Persistence OK -> {STATE_FILE}")

    threading.Thread(target=start_status_server, daemon=True).start()
    threading.Thread(target=telegram_listener,   daemon=True).start()

    send_status(
        f"🟢 Agent online — MULTI-SPORT\n"
        f"Sports: {_active_label()}\n"
        f"Mode: {'DRY RUN' if dry else 'LIVE'}\n"
        f"Poll: every {POLL_INTERVAL_MIN} min\n"
        f"Phase: {state.get('current_phase', 'GROUP_STAGE')}"
    )

    schedule.every(POLL_INTERVAL_MIN).minutes.do(run_main_analysis)
    schedule.every(IN_PLAY_INTERVAL_SEC).seconds.do(run_in_play_check)
    schedule.every().day.at("12:00").do(send_heartbeat)

    run_main_analysis(triggered_by="STARTUP")

    while True:
        schedule.run_pending()
        time.sleep(5)


if __name__ == "__main__":
    main()
