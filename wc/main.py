import schedule
import time
import os
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

from scout import get_live_scoreboard, get_live_matches, get_match_detail, is_hydration_break_window
from market_reader import get_winner_odds, get_golden_boot_odds, scan_stage_elimination_ladders, get_market_by_slug, search_markets
from signal_engine import run_signal, run_in_play_signal
from telegram_ops import send_trade_signal, send_monitor_signal, send_status, send_error, get_updates
from gates import (
    check_gates, record_signal_sent, record_trade_opened,
    record_trade_closed, update_phase, set_dry_run,
    get_state_summary, load_state, save_state,
)
from executor import log_signal, dry_run_signal, place_order, read_trade_log

POLL_INTERVAL_MIN    = 3
IN_PLAY_INTERVAL_SEC = 45
STATUS_PORT          = int(os.environ.get("PORT", 8099))

_tg_offset = 0


# ── STATUS SERVER (keep-alive ping target) ──────────────────────────────────

class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = load_state()
        body  = (
            f"DÆMON-POLY ⚽ ONLINE\n"
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
        send_status(f"{summary}\n\nOPEN POSITIONS:\n{pos_lines}")

    # SCAN
    elif cmd in ("SCAN", "/SCAN"):
        send_status("🔍 Manual scan triggered...")
        run_main_analysis(triggered_by="MANUAL")

    # PHASE: R32 etc.
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

    # CLOSE: <market> <outcome>
    elif cmd.startswith("CLOSE:") or cmd.startswith("CLOSE "):
        parts = text.split(None, 2)
        if len(parts) >= 3:
            market  = parts[1]
            outcome = parts[2]
            record_trade_closed(market, outcome)
            send_status(f"✅ Position closed: {market} / {outcome}")
        else:
            send_error("Usage: CLOSE <market> <outcome>")

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
        lines   = "\n".join(r.get("slug", r.get("title", str(r))) for r in results[:5] if isinstance(r, dict))
        send_status(f"Market search '{query}':\n{lines or 'No results'}")

    # HELP
    elif cmd in ("HELP", "/HELP", "/START"):
        send_status(
            "DÆMON-POLY ⚽ COMMANDS\n"
            "──────────────────────\n"
            "STATUS — current state\n"
            "SCAN — force analysis now\n"
            "PHASE: R32|R16|QF|SF|FINAL — update phase\n"
            "DRY: ON|OFF — toggle dry run mode\n"
            "CLOSE: <market> <outcome> — mark position closed\n"
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


# ── MAIN ANALYSIS LOOP ──────────────────────────────────────────────────────

def run_main_analysis(triggered_by: str = "SCHEDULED"):
    now = datetime.now(timezone.utc)
    print(f"\n[{now.strftime('%H:%M:%S UTC')}] Analysis [{triggered_by}]")

    state = load_state()
    dry   = state.get("dry_run", True)

    try:
        live_matches   = get_live_matches()
        winner_odds    = get_winner_odds()
        golden_boot    = get_golden_boot_odds()
        ladder_anom    = scan_stage_elimination_ladders()

        context = f"Live matches in progress: {len(live_matches)}"
        if ladder_anom:
            context += f"\nLadder anomalies: {', '.join(ladder_anom.keys())}"
        context += f"\n{get_state_summary()}"

        signal = run_signal(
            live_matches=live_matches,
            winner_odds=winner_odds,
            golden_boot_odds=golden_boot,
            ladder_anomalies=ladder_anom,
            additional_context=context,
        )

        stype = signal.get("signal_type")
        print(f"  Signal: {stype} | Edge: {signal.get('edge', 'N/A')}")

        if stype == "TRADE":
            passed, violations = check_gates(signal)
            signal["gate_check"] = "PASS" if passed else "FAIL"
            signal["gate_notes"] = "; ".join(violations) if violations else "All gates passed"

            log_signal(signal, executed=False)

            if passed:
                send_trade_signal(signal)
                record_signal_sent(signal)

                if dry:
                    result = dry_run_signal(signal)
                    print(f"  {result}")
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
            else:
                print(f"  GATE FAIL: {signal['gate_notes']}")
                send_error(
                    f"Signal blocked by gates:\n{signal['gate_notes']}\n"
                    f"Market: {signal.get('market')} | Edge: {signal.get('edge')}"
                )

        elif stype == "MONITOR":
            send_monitor_signal(signal)
            log_signal(signal)

        elif stype == "NO_SIGNAL":
            print(f"  No edge: {signal.get('reason')}")

        else:
            print(f"  Unexpected signal: {signal}")

    except Exception as e:
        msg = f"Main loop error [{triggered_by}]: {e}"
        print(f"  ERROR: {msg}")
        send_error(msg)


# ── IN-PLAY EDGE 3 CHECK ────────────────────────────────────────────────────

def run_in_play_check():
    live_matches = get_live_matches()
    for match in live_matches:
        if is_hydration_break_window(match):
            print(f"  ⚡ HYDRATION BREAK: {match['home_team']} vs {match['away_team']} @ {match['clock']}")
            detail       = get_match_detail(match["id"])
            home         = match["home_team"].lower().replace(" ", "-")
            away         = match["away_team"].lower().replace(" ", "-")
            current_odds = get_market_by_slug(f"{home}-vs-{away}") or {}
            signal       = run_in_play_signal(match, detail, current_odds)

            if signal.get("signal_type") == "TRADE":
                passed, violations = check_gates(signal)
                signal["gate_check"] = "PASS" if passed else "FAIL"
                signal["gate_notes"] = "; ".join(violations) if violations else ""
                log_signal(signal)
                if passed:
                    send_trade_signal(signal)
                    record_signal_sent(signal)


# ── HEARTBEAT ───────────────────────────────────────────────────────────────

def send_heartbeat():
    send_status(
        f"⚙️ HEARTBEAT\n"
        f"{get_state_summary()}\n"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )


# ── ENTRY POINT ─────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("DÆMON-POLY // World Cup 2026 Agent v2.0")
    state = load_state()
    dry   = state.get("dry_run", True)
    print(f"Mode: {'DRY RUN ⚪' if dry else 'LIVE 🔴'} | Poll: {POLL_INTERVAL_MIN} min")
    print("=" * 55)

    required = ["ANTHROPIC_API_KEY", "TELEGRAM_TOKEN"]
    missing  = [s for s in required if not os.environ.get(s)]
    if missing:
        print(f"FATAL: Missing secrets: {missing}")
        return

    threading.Thread(target=start_status_server, daemon=True).start()
    threading.Thread(target=telegram_listener,   daemon=True).start()

    send_status(
        f"🟢 Agent online\n"
        f"Mode: {'DRY RUN' if dry else 'LIVE'}\n"
        f"Poll: every {POLL_INTERVAL_MIN} min\n"
        f"Phase: {state.get('current_phase', 'GROUP_STAGE')}\n"
        f"World Cup 2026 — Knockout begins June 28"
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
