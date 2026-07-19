import schedule
import time
import os
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import pm_us
from sports_config import active_sports, SPORT_CONFIGS
from scout import get_all_matches, get_live_matches, get_match_detail, is_in_play_window
from market_reader import (
    scan_stage_elimination_ladders,
    find_game_market_odds, search_markets,
)
from signal_engine import run_signal, run_in_play_signal, run_checker
from telegram_ops import (
    send_trade_signal, send_monitor_signal, send_status, send_error, get_updates,
    CHAT_ID as AUTHORIZED_CHAT_ID,
)
from gates import (
    check_gates, record_signal_sent, record_trade_opened,
    record_trade_closed, update_phase, set_dry_run, dedupe_active_positions,
    get_state_summary, load_state, save_state, STATE_FILE,
    KILL_SWITCH_DRAWDOWN_PCT, reset_drawdown, MAX_TRADE_USD, get_trade_cap,
)
from executor import log_signal, dry_run_signal, place_order, read_trade_log, close_position
from learning import log_loss_and_learn, record_edge_result, learning_context

POLL_INTERVAL_MIN    = 3
IN_PLAY_INTERVAL_SEC = 45
STATUS_PORT          = int(os.environ.get("PORT", 8099))
VETO_WINDOW_SEC      = 60

_tg_offset = 0

# In-memory live-trade veto queue. Pending trades are NEVER persisted: a restart
# must force a fresh scan rather than firing a stale approval. Each entry:
#   {"id", "signal", "size_usd", "valid_slugs", "queued_at", "vetoed"}
_pending_lock   = threading.Lock()
_pending_trades = []
_pending_seq    = 0

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
            # Actually sell on Polymarket in LIVE mode (state-only before).
            # FAIL-CLOSED: if any real sell fails, keep the position in state so
            # exposure is never hidden — operator must retry or sell manually.
            sell_note  = ""
            sell_error = False
            if not load_state().get("dry_run", True):
                slugs = {
                    p.get("market_slug") for p in load_state().get("active_positions", [])
                    if p.get("market") == market and p.get("outcome") == outcome
                    and p.get("market_slug")
                }
                if slugs:
                    results = []
                    for slug in slugs:
                        r = close_position(slug)
                        if r.get("ok"):
                            results.append(f"{slug}: sold ✅")
                        else:
                            sell_error = True
                            results.append(f"{slug}: SELL FAILED — {r.get('error')}")
                    sell_note = "\n" + "\n".join(results)
                else:
                    sell_note = ("\n⚠️ No market slug stored for this position — "
                                 "state cleared, but sell the shares manually in the Polymarket app if you don't want to hold to resolution.")
            if sell_error:
                send_error(
                    f"❌ CLOSE aborted: Polymarket sell failed — position kept in state "
                    f"so exposure stays visible.{sell_note}\n"
                    f"Retry CLOSE, or sell manually in the Polymarket app and then re-run CLOSE."
                )
                return
            total_loss = record_trade_closed(market, outcome, pnl_pct)
            extra = f" | PnL {pnl_pct:+.1f}%" if pnl_pct else ""
            send_status(f"✅ Position closed: {market} / {outcome}{extra}{sell_note}")

            # SELF-IMPROVEMENT: update per-edge stats and, on a loss, have
            # Claude extract one preventive RULE. Runs in a background thread
            # so a slow/failed API call never blocks the Telegram listener.
            closed_pos = None
            for p in reversed(load_state().get("closed_positions", [])):
                if p.get("market") == market and p.get("outcome") == outcome:
                    closed_pos = p
                    break
            if closed_pos is None:
                closed_pos = {"market": market, "outcome": outcome}
            record_edge_result(closed_pos.get("edge", ""), closed_pos.get("sport", ""), pnl_pct)
            if pnl_pct < 0:
                def _learn(pos=closed_pos, pnl=pnl_pct):
                    lesson = log_loss_and_learn(pos, pnl)
                    if lesson and lesson.get("new_rule"):
                        send_status(
                            f"📚 LESSON LEARNED\n{lesson.get('root_cause', '')}\n"
                            f"{lesson['new_rule']}\n"
                            f"(now enforced in every future signal)"
                        )
                threading.Thread(target=_learn, daemon=True).start()
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

    # VETO — cancel a pending live trade inside its 60s window
    elif cmd == "VETO" or cmd.startswith("VETO "):
        arg = text.split(None, 1)[1].strip().upper() if " " in text else ""
        with _pending_lock:
            pending = [t for t in _pending_trades if not t["vetoed"]]
            if arg == "ALL":
                for t in pending:
                    t["vetoed"] = True
                send_status(f"🛑 Vetoed {len(pending)} pending trade(s).")
            elif arg == "":
                if not pending:
                    send_status("No pending trades.")
                else:
                    lines = "\n".join(
                        f"  #{t['id']} {t['signal'].get('sport','')} "
                        f"{t['signal'].get('outcome','?')} ${t['size_usd']:.2f} "
                        f"({max(0, VETO_WINDOW_SEC - int(time.time() - t['queued_at']))}s left)"
                        for t in pending
                    )
                    send_status(f"PENDING TRADES:\n{lines}\nReply VETO <id> or VETO ALL.")
            else:
                try:
                    pid = int(arg)
                except ValueError:
                    send_error("Usage: VETO <id> | VETO ALL")
                    return
                hit = next((t for t in pending if t["id"] == pid), None)
                if hit:
                    hit["vetoed"] = True
                    send_status(f"🛑 Vetoed pending trade #{pid}.")
                else:
                    send_error(f"No active pending trade #{pid}.")

    # HELP
    elif cmd in ("HELP", "/HELP", "/START"):
        send_status(
            "DÆMON-POLY MULTI-SPORT COMMANDS\n"
            "──────────────────────\n"
            "STATUS — current state + positions\n"
            "SPORTS — list active sports\n"
            "SCAN — force analysis now (all sports)\n"
            "PHASE: R32|R16|QF|SF|FINAL — World Cup phase\n"
            "DRY: ON|OFF — toggle live trading (OFF = LIVE)\n"
            "VETO — list pending trades\n"
            "VETO <id> | VETO ALL — cancel a pending live trade\n"
            "CLOSE: <market> <outcome> [pnl_pct] — mark position closed\n"
            "RESET_DRAWDOWN — disarm the 5% kill switch\n"
            "LOG — last 5 logged signals\n"
            "MARKETS: <query> — search Polymarket markets\n"
            "HELP — this menu"
        )

    else:
        print(f"[TG] Unrecognised command: {text!r}")
        send_status(
            f"🤖 Unknown command: '{text[:40]}'\n"
            "Try: STATUS · SCAN · SPORTS · LOG · VETO · HELP\n"
            "(Old-bot commands like CYCLE/AUTOPILOT no longer exist.)"
        )


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
                if not text:
                    continue
                # Only the configured operator may issue commands (DRY: OFF, VETO,
                # SCAN, etc.). Silently ignore everyone else.
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                if AUTHORIZED_CHAT_ID and chat_id != AUTHORIZED_CHAT_ID:
                    print(f"[TG] ignored command from unauthorized chat {chat_id!r}: {text!r}")
                    continue
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


# ── LIVE-TRADE VETO QUEUE ───────────────────────────────────────────────────

def _queue_live_trade(signal: dict, size_usd: float, catalog: dict):
    """Queue an approved live trade with a VETO_WINDOW_SEC operator veto window.
    The veto_worker fires it automatically once the window elapses, unless vetoed.

    FAILS CLOSED: the trade is only armed if the Telegram veto countdown is
    CONFIRMED delivered. If Telegram is down / misconfigured, the operator never
    saw the veto window, so we must NOT auto-fire — the trade is dropped."""
    global _pending_seq
    slug        = (signal.get("market_slug") or "").strip()
    cat_outcome = catalog.get(slug, {}).get("outcome") or signal.get("outcome", "?")

    with _pending_lock:
        _pending_seq += 1
        pid = _pending_seq

    resp = send_status(
        f"⏳ PENDING LIVE TRADE #{pid} — {VETO_WINDOW_SEC}s to veto\n"
        f"{signal.get('sport','')} | {signal.get('market','?')}\n"
        f"BUY {cat_outcome} @ ~{signal.get('entry_price_pct','?')}¢ "
        f"| ${size_usd:.2f} (cap ${get_trade_cap(pm_us.get_buying_power()):.0f})\n"
        f"slug: {slug}\n"
        f"Reply  VETO {pid}  or  VETO ALL  to cancel.\n"
        f"Otherwise fires automatically in {VETO_WINDOW_SEC}s."
    )
    if not (isinstance(resp, dict) and resp.get("ok")):
        # Operator never got a veto chance — do not arm an auto-trade.
        print(f"[VETO] queue #{pid} ABORTED — Telegram countdown not delivered: {resp}")
        log_signal(signal, executed=False, error="veto countdown undeliverable — trade dropped")
        return

    with _pending_lock:
        _pending_trades.append({
            "id":        pid,
            "signal":    signal,
            "size_usd":  size_usd,
            "catalog":   dict(catalog),
            "queued_at": time.time(),
            "vetoed":    False,
        })


def _fire_trade(t: dict):
    """Execute a pending trade after its veto window, re-validating everything."""
    signal, size_usd, catalog = t["signal"], t["size_usd"], t["catalog"]
    pid = t["id"]

    if load_state().get("dry_run", True):
        send_status(f"⚪ Pending trade #{pid} skipped — DRY RUN is now ON.")
        return

    passed, violations = check_gates(signal)
    if not passed:
        send_error(f"Pending trade #{pid} blocked at fire time:\n{'; '.join(violations)}")
        log_signal(signal, executed=False, error="gate fail at fire time")
        return

    send_status(f"🚀 FIRING #{pid} — veto window expired, executing on Polymarket US…")
    result = place_order(signal, size_usd, catalog)
    if "error" in result:
        send_error(f"Order #{pid} failed: {result['error']}")
    else:
        signal["order_id"] = result.get("orderID", "")
        record_trade_opened(signal)
        send_status(
            f"✅ ORDER PLACED #{pid}\n"
            f"{signal.get('outcome')} @ {result.get('price','?')} | "
            f"{result.get('shares','?')} shares (${result.get('notional','?')})\n"
            f"Order ID: {result.get('orderID','?')}"
        )


def veto_worker():
    """Background loop that fires due (un-vetoed) pending trades and purges vetoed ones."""
    print("[VETO] worker started")
    while True:
        try:
            now = time.time()
            with _pending_lock:
                due = [t for t in _pending_trades
                       if not t["vetoed"] and now - t["queued_at"] >= VETO_WINDOW_SEC]
                _pending_trades[:] = [
                    t for t in _pending_trades
                    if not t["vetoed"] and t not in due
                ]
            for t in due:
                _fire_trade(t)
        except Exception as e:
            print(f"[VETO] worker error: {e}")
        time.sleep(2)


# ── MAIN ANALYSIS LOOP (per sport) ──────────────────────────────────────────

def analyze_sport(sport_cfg: dict, dry: bool):
    label = f"{sport_cfg['emoji']} {sport_cfg['label']}"
    try:
        matches      = get_all_matches(sport_cfg)
        # Polymarket US futures catalog — the SOLE auto-execution whitelist.
        futures_odds = pm_us.get_sport_futures_us(sport_cfg)
        idx          = pm_us.catalog_index(futures_odds)
        valid_slugs  = set(idx.keys())

        context = f"Games listed: {len(matches)} | {get_state_summary()}"

        # SELF-IMPROVEMENT: inject learned rules + real per-edge performance
        # so SIGNAL weights edges by actual results, not just theory.
        learn_ctx = learning_context()
        if learn_ctx:
            context += f"\n\n{learn_ctx}"

        # World Cup keeps its Edge LADDER scan; alert-only context (no US slug).
        if sport_cfg["key"] == "world_cup":
            ladder_anom = scan_stage_elimination_ladders()
            if ladder_anom:
                futures_odds["Stage-of-Elimination Anomalies (alert-only)"] = ladder_anom
                context += f"\nLadder anomalies: {', '.join(ladder_anom.keys())}"

        signal = run_signal(sport_cfg, matches, futures_odds, context)
        stype  = signal.get("signal_type")
        print(f"  [{label}] Signal: {stype} | Edge: {signal.get('edge', 'N/A')}")

        if stype == "TRADE":
            slug       = (signal.get("market_slug") or "").strip()
            executable = bool(slug) and slug in valid_slugs
            if executable:
                signal["_tick"] = idx[slug].get("tick", "0.001")
            if _passes_gates_and_checker(signal):
                send_trade_signal(signal)
                record_signal_sent(signal)
                if dry:
                    tag = "" if executable else " (alert-only: no US slug)"
                    print(f"  {dry_run_signal(signal)}{tag}")
                elif not executable:
                    send_status(
                        f"ℹ️ {signal.get('sport')} signal is ALERT-ONLY — no auto-executable "
                        f"US futures market. No order placed."
                    )
                    log_signal(signal, executed=False, error="no US slug (alert-only)")
                else:
                    bankroll = pm_us.get_buying_power()
                    cap      = get_trade_cap(bankroll)
                    size_usd = min(
                        bankroll * float(signal.get("size_pct_bankroll", 5)) / 100,
                        cap,
                    )
                    _queue_live_trade(signal, size_usd, idx)

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

    # Clean up any duplicate positions left from before the PF-04 gate existed.
    removed = dedupe_active_positions()
    if removed:
        print(f"Startup dedupe: removed {removed} duplicate position(s)")
        try:
            send_status(
                f"🧹 Cleaned up {removed} duplicate position(s) from state — "
                f"slots freed. (Note: any real shares from those duplicate buys "
                f"remain in your Polymarket account until sold or resolved.)"
            )
        except Exception:
            pass

    # Honour DRY_RUN env var set on Railway — overrides whatever is in state so
    # the operator can flip live/dry without needing a Telegram command.
    env_dry = os.environ.get("DRY_RUN", "").strip().lower()
    if env_dry in ("false", "0", "no", "off"):
        set_dry_run(False)
        print("DRY_RUN env=false → LIVE mode")
    elif env_dry in ("true", "1", "yes", "on"):
        set_dry_run(True)
        print("DRY_RUN env=true → DRY RUN mode")
    # Re-read after potential env override so banner + Telegram reflect true mode.
    dry = load_state().get("dry_run", True)

    # Probe the live execution venue so startup surfaces any auth/funding issue.
    buying_power = pm_us.get_buying_power()
    print(f"Polymarket US buying power: ${buying_power:.2f} | Per-trade cap: ${get_trade_cap(buying_power):.2f}")

    threading.Thread(target=start_status_server, daemon=True).start()
    threading.Thread(target=telegram_listener,   daemon=True).start()
    threading.Thread(target=veto_worker,         daemon=True).start()

    send_status(
        f"🟢 Agent online — MULTI-SPORT\n"
        f"Sports: {_active_label()}\n"
        f"Mode: {'DRY RUN' if dry else '🔴 LIVE'}\n"
        f"Buying power: ${buying_power:.2f} | Cap: ${get_trade_cap(buying_power):.0f}/trade | Veto: {VETO_WINDOW_SEC}s\n"
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
