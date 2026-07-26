import schedule
import time
import os
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import pm_us
from sports_config import active_sports, SPORT_CONFIGS
from scout import get_all_matches, get_live_matches, get_match_detail, is_in_play_window, get_standings
from market_reader import (
    scan_stage_elimination_ladders,
    find_game_market_odds, search_markets,
)
from signal_engine import run_signal, run_in_play_signal, run_checker, run_discovery_signal
from telegram_ops import (
    send_trade_signal, send_monitor_signal, send_status, send_error, get_updates,
    CHAT_ID as AUTHORIZED_CHAT_ID,
)
from gates import (
    check_gates, record_signal_sent, record_trade_opened,
    record_trade_closed, update_phase, set_dry_run, dedupe_active_positions,
    get_state_summary, load_state, save_state, STATE_FILE,
    KILL_SWITCH_DRAWDOWN_PCT, reset_drawdown, reset_positions,
    MAX_TRADE_USD, get_trade_cap, auto_close_resolved_positions,
    reconcile_positions,
)
from executor import log_signal, dry_run_signal, place_order, read_trade_log, close_position
from learning import log_loss_and_learn, record_edge_result, learning_context

POLL_INTERVAL_MIN    = 3
IN_PLAY_INTERVAL_SEC = 45
STATUS_PORT          = int(os.environ.get("PORT", 8099))
_tg_offset = 0

# In-memory alert throttle: looping 4 sports every few minutes can flood Telegram
# with repeat gate-fail / checker-kill / monitor notices. Approved TRADE signals
# and the kill-switch alert are NEVER throttled — only routine repeats are deduped.
_alert_history     = {}
ALERT_COOLDOWN_SEC = 1800

# Track how many consecutive main-loop cycles each sport's catalog came back empty.
# Used by auto_close_resolved_positions to distinguish transient API failures from a
# genuinely ended season/tournament.  Resets to 0 whenever a non-empty catalog is
# fetched; a restart also resets all counters (intentional — a fresh start should
# re-verify before acting).
_empty_catalog_streak: dict = {}
EMPTY_CATALOG_THRESHOLD = 3   # consecutive empty fetches required before force-close


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

    # TEST — dry-run the full pipeline (gates → checker → order sim) to verify wiring
    elif cmd in ("TEST", "/TEST"):
        send_status("🔬 Running pipeline test — this takes ~10s…")
        try:
            mock = {
                "signal_type":      "TRADE",
                "sport":            "mlb",
                "edge":             "IN_PLAY",
                "market":           "TEST: Pipeline Check",
                "market_slug":      "__test__",
                "direction":        "YES",
                "outcome":          "Test Team",
                "entry_price_pct":  15.0,
                "target_exit_pct":  30.0,
                "size_pct_bankroll": 3.0,
                "confidence":       "HIGH",
                "rationale":        "Operator-triggered pipeline test.",
            }
            results = []
            # 1. Gates
            passed, viols = check_gates(mock)
            # filter out the expected "__test__" slug violation — it's not a real gate
            real_viols = [v for v in viols if "no market_slug" not in v and "stale" not in v]
            gate_note = "✅ PASS" if (passed or not real_viols) else f"⚠️ {'; '.join(real_viols)}"
            results.append(f"Gates:    {gate_note}")
            # 2. PM credentials + buying power
            bp = pm_us.get_buying_power()
            pm_note = f"✅ ${bp:.2f} buying power" if bp > 0 else "❌ $0 — check POLYMARKET_KEY_ID / SECRET"
            results.append(f"PM auth:  {pm_note}")
            # 3. Telegram (we're already here — it works)
            results.append("Telegram: ✅ (you received this message)")
            # 4. Executor path (dry-run only — does NOT place a real order)
            dry_run_signal(mock)
            results.append("Executor: ✅ order-sizing path OK (dry-run, no real order)")
            # 5. Checker — NOT called in TEST to avoid unnecessary LLM cost.
            #    Verify checker independently: check Railway logs for "CHECKER:" lines
            #    after a real TRADE signal is generated.
            results.append("Checker:  ⏭ skipped (LLM call — verify in Railway logs)")
            state_s = get_state_summary()
            send_status(
                "🔬 PIPELINE TEST RESULTS\n"
                "──────────────────────\n"
                + "\n".join(results) + "\n\n"
                + state_s + "\n\n"
                "Tests cover: gates · PM credentials · Telegram · executor sizing.\n"
                "Checker is not called here — watch Railway logs for CHECKER output on the next real signal."
            )
        except Exception as ex:
            send_error(f"Pipeline test error: {ex}")

    # RECONCILE — diff state vs real Polymarket holdings, drop ghost positions
    elif cmd in ("RECONCILE", "/RECONCILE"):
        send_status("🔄 Reconciling state against live Polymarket portfolio…")
        try:
            removed, kept = reconcile_positions()
            if removed:
                lines = "\n".join(
                    f"  • {p.get('sport','?')} | {p.get('outcome','?')} "
                    f"({p.get('market_slug','?')})"
                    for p in removed
                )
                send_status(
                    f"🧹 RECONCILE: removed {len(removed)} ghost position(s) not backed by real PM holdings:\n"
                    f"{lines}\n\n"
                    f"{len(kept)} position(s) confirmed real and kept."
                )
            else:
                send_status(
                    f"✅ RECONCILE: all {len(kept)} tracked position(s) confirmed against Polymarket. "
                    f"No ghosts found."
                )
        except Exception as ex:
            send_error(f"RECONCILE error: {ex}")

    # RESET_POSITIONS — nuclear option: wipe all positions from state
    elif cmd in ("RESET_POSITIONS", "/RESET_POSITIONS", "CLEAR_POSITIONS", "/CLEAR_POSITIONS"):
        n = reset_positions()
        send_status(
            f"🧹 Cleared {n} active position(s) from state.\n"
            f"Bankroll-deployed counter reset to 0%.\n"
            f"⚠️ If any real shares were held, sell them manually in the Polymarket app."
        )

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
            "CLOSE: <market> <outcome> [pnl_pct] — mark position closed\n"
            "TEST — verify full pipeline (gates · PM auth · executor)\n"
            "RECONCILE — diff state vs real Polymarket holdings, drop ghost positions\n"
            "RESET_POSITIONS — nuclear wipe of all positions from state\n"
            "RESET_DRAWDOWN — disarm the 5% kill switch\n"
            "LOG — last 5 logged signals\n"
            "MARKETS: <query> — search Polymarket markets\n"
            "HELP — this menu"
        )

    else:
        print(f"[TG] Unrecognised command: {text!r}")
        send_status(
            f"🤖 Unknown command: '{text[:40]}'\n"
            "Try: STATUS · SCAN · SPORTS · LOG · HELP\n"
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


# ── LIVE TRADE EXECUTOR ──────────────────────────────────────────────────────

def _fire_trade(signal: dict, size_usd: float, catalog: dict):
    """Execute a live trade immediately after gates + checker pass.
    Gates are re-checked here as a final safety net (market state can shift
    between signal generation and order placement)."""
    if load_state().get("dry_run", True):
        return  # Should never reach here — dry mode is checked upstream

    passed, violations = check_gates(signal)
    if not passed:
        send_error(f"Signal blocked by gates:\n{'; '.join(violations)}\n"
                   f"Market: {signal.get('market','?')} | Edge: {signal.get('edge','?')}")
        log_signal(signal, executed=False, error="gate fail at fire time")
        return

    slug        = (signal.get("market_slug") or "").strip()
    cat_outcome = catalog.get(slug, {}).get("outcome") or signal.get("outcome", "?")
    send_status(
        f"🚀 FIRING — executing on Polymarket US…\n"
        f"{signal.get('sport','')} | {signal.get('market','?')}\n"
        f"BUY {cat_outcome} @ ~{signal.get('entry_price_pct','?')}¢ · ${size_usd:.2f}"
    )
    result = place_order(signal, size_usd, catalog)
    if "error" in result:
        err = result["error"]
        if "price moved" in err:
            hint = "Price drifted >3pp from signal — edge may have closed. No loss."
        elif "no live ask" in err:
            hint = "Market illiquid right now. No loss."
        elif "below minimum" in err or "whole share" in err:
            hint = "Position size too small — top up buying power to trade."
        elif "outcome mismatch" in err:
            hint = "Outcome label mismatch vs catalog — check sports_config slug."
        elif "client unavailable" in err:
            hint = "Polymarket credentials error — check POLYMARKET_KEY_ID/SECRET."
        else:
            hint = "Check Railway logs for full traceback."
        send_error(
            f"❌ Order FAILED\n"
            f"{signal.get('sport','')} | {signal.get('outcome','?')} @ "
            f"{signal.get('entry_price_pct','?')}¢\n"
            f"Reason: {err}\n"
            f"→ {hint}"
        )
        log_signal(signal, executed=False, error=err)
    else:
        order_id = result.get("orderID", "?")
        shares   = result.get("shares", "?")
        price    = result.get("price", "?")
        notional = result.get("notional", "?")
        signal["order_id"] = order_id
        record_trade_opened(signal)
        send_status(
            f"✅ ORDER PLACED\n"
            f"{signal.get('sport','')} | {signal.get('market','?')}\n"
            f"BUY {signal.get('outcome','?')} @ {price} ({signal.get('entry_price_pct','?')}¢ signal)\n"
            f"{shares} shares · ${notional} notional\n"
            f"Order ID: {order_id}"
        )


# ── MAIN ANALYSIS LOOP (per sport) ──────────────────────────────────────────

def analyze_sport(sport_cfg: dict, dry: bool):
    label = f"{sport_cfg['emoji']} {sport_cfg['label']}"
    try:
        matches      = get_all_matches(sport_cfg)
        # Polymarket US futures catalog — the SOLE auto-execution whitelist.
        futures_odds = pm_us.get_sport_futures_us(sport_cfg)
        idx          = pm_us.catalog_index(futures_odds)
        valid_slugs  = set(idx.keys())

        # Auto-close positions whose market resolved (slug gone from live catalog).
        # Positions are matched by sport key + outcome-level market_slug (the slug
        # stored at trade-open time, sourced from catalog_index).
        #
        # Empty-catalog safety: a single empty catalog fetch is ambiguous (transient
        # API failure vs. tournament over). We track consecutive empty fetches and
        # only force-close after EMPTY_CATALOG_THRESHOLD consecutive misses.
        sport_key = sport_cfg["key"]
        if valid_slugs:
            _empty_catalog_streak[sport_key] = 0
        else:
            _empty_catalog_streak[sport_key] = _empty_catalog_streak.get(sport_key, 0) + 1
            streak = _empty_catalog_streak[sport_key]
            if streak < EMPTY_CATALOG_THRESHOLD:
                print(f"  [{label}] Catalog empty (streak {streak}/{EMPTY_CATALOG_THRESHOLD}) "
                      f"— skipping stale-position check until threshold reached")

        streak = _empty_catalog_streak.get(sport_key, 0)
        resolved_pos = auto_close_resolved_positions(
            sport_key, valid_slugs, streak, EMPTY_CATALOG_THRESHOLD
        )
        for pos in resolved_pos:
            slug_gone = pos.get("market_slug", "")
            size_pct  = pos.get("size_pct", 0)
            reason    = "slug absent from live catalog" if valid_slugs else f"catalog empty {streak} cycles"
            print(f"  [{label}] 🏁 Auto-closed resolved position: "
                  f"{pos.get('outcome')} ({slug_gone}) — {reason}")
            send_status(
                f"🏁 Position auto-closed: {pos.get('outcome')} — market resolved on Polymarket\n"
                f"Market: {pos.get('market')} | Slug: {slug_gone}\n"
                f"Bankroll deployed reduced by {size_pct:.1f}%\n"
                f"⚠️ If real shares were held, verify settlement in the Polymarket app."
            )

        context = f"Games listed: {len(matches)} | {get_state_summary()}"

        # Standings give the AI win%, run/point differential, and playoff odds —
        # the context it needs to spot futures mispricings for MLB and WNBA.
        standings_ctx = get_standings(sport_cfg)
        if standings_ctx:
            context += f"\n\n{standings_ctx}"

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

            # ── SLUG FALLBACK: if model gave no slug (or a wrong one), try to fill
            # it from sports_config futures dict.  Fails closed on any ambiguity:
            #   • market name must be non-trivial (≥5 chars) for substring matching
            #   • single-slug shortcut uses config count (not catalog count) so a
            #     multi-futures sport (MLB, WNBA) never gets silently routed to the
            #     one slug that happens to be open right now
            if not executable:
                if slug and slug not in valid_slugs:
                    print(f"  [{label}] model slug '{slug}' not in US catalog — trying futures fallback")
                futures = sport_cfg.get("futures", {})
                # 1. Market-name substring match — only when market is unambiguous
                signal_market = (signal.get("market") or "").lower().strip()
                if len(signal_market) >= 5:  # guard against empty / trivially-short strings
                    for f_label, f_slug in futures.items():
                        if f_slug in valid_slugs and (
                            f_label.lower() in signal_market or signal_market in f_label.lower()
                        ):
                            slug = f_slug
                            signal["market_slug"] = slug
                            executable = True
                            print(f"  [{label}] slug auto-filled (market match '{f_label}'): {slug}")
                            break
                # 2. Single-futures-config sport — safe to fill unconditionally only
                #    when the sport's config defines exactly ONE futures entry.
                #    Multi-entry sports (MLB: WS + AL + NL) are left as alert-only.
                if not executable and len(futures) == 1:
                    sole_slug = next(iter(futures.values()))
                    if sole_slug in valid_slugs:
                        slug = sole_slug
                        signal["market_slug"] = slug
                        executable = True
                        print(f"  [{label}] slug auto-filled (sole config futures entry): {slug}")

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
                        f"ℹ️ {signal.get('sport')} TRADE signal — no auto-executable US slug found.\n"
                        f"Market: {signal.get('market')} | Outcome: {signal.get('outcome')}\n"
                        f"Signal slug: '{signal.get('market_slug','')}' | Catalog has {len(valid_slugs)} slug(s)\n"
                        f"No order placed. Check futures slugs in sports_config.py."
                    )
                    log_signal(signal, executed=False, error="no US slug (alert-only)")
                else:
                    bankroll = pm_us.get_buying_power()
                    cap      = get_trade_cap(bankroll)
                    size_usd = min(
                        bankroll * float(signal.get("size_pct_bankroll", 5)) / 100,
                        cap,
                    )
                    _fire_trade(signal, size_usd, idx)

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


def run_discovery_scan(dry: bool):
    """Broad Polymarket US market discovery — finds edges across all sports,
    not just the 5 pre-configured ones. Runs after the per-sport loop."""
    try:
        catalog = pm_us.discover_us_markets()
        if not catalog:
            print("  [🔭 Discovery] No markets found")
            return
        n_markets = sum(len(v) for v in catalog.values())
        print(f"  [🔭 Discovery] Scanning {len(catalog)} events / {n_markets} outcomes…")
        signal = run_discovery_signal(catalog)
        stype  = signal.get("signal_type", "NO_SIGNAL")
        print(f"  [🔭 Discovery] Signal: {stype} | Edge: {signal.get('edge', 'N/A')}")
        if stype == "NO_SIGNAL":
            print(f"  [🔭 Discovery] {signal.get('reason', '')[:180]}")
        elif stype == "TRADE":
            idx = pm_us.catalog_index(catalog)
            slug = (signal.get("market_slug") or "").strip()
            executable = bool(slug) and slug in idx
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
                        f"ℹ️ Discovery signal is ALERT-ONLY — no auto-executable slug.\n"
                        f"{signal.get('market','?')} / {signal.get('outcome','?')}"
                    )
                    log_signal(signal, executed=False, error="discovery: no US slug")
                else:
                    bankroll = pm_us.get_buying_power()
                    cap      = get_trade_cap(bankroll)
                    size_usd = min(
                        bankroll * float(signal.get("size_pct_bankroll", 5)) / 100,
                        cap,
                    )
                    _fire_trade(signal, size_usd, idx)
        elif stype == "ERROR":
            print(f"  [🔭 Discovery] ERROR: {signal.get('reason', '')[:180]}")
    except Exception as e:
        print(f"  [🔭 Discovery] scan error: {e}")


def run_main_analysis(triggered_by: str = "SCHEDULED"):
    now = datetime.now(timezone.utc)
    print(f"\n[{now.strftime('%H:%M:%S UTC')}] Analysis [{triggered_by}]")
    state = load_state()
    dry   = state.get("dry_run", True)
    for sport_cfg in active_sports():
        analyze_sport(sport_cfg, dry)
    run_discovery_scan(dry)


# ── IN-PLAY EDGE CHECK (per sport) ──────────────────────────────────────────

def run_in_play_check():
    dry = load_state().get("dry_run", True)
    for sport_cfg in active_sports():
        try:
            live = get_live_matches(sport_cfg)
        except Exception as e:
            print(f"  in-play fetch error [{sport_cfg['key']}]: {e}")
            continue

        for match in live:
            if not is_in_play_window(sport_cfg, match):
                continue
            game         = find_game_market_odds(
                match.get("home_team", ""), match.get("away_team", ""), sport_cfg["label"]
            )
            current_odds = game if game else {}
            # Skip AI call if Polymarket has no live market for this game.
            if not current_odds:
                continue
            print(f"  ⚡ IN-PLAY [{sport_cfg['label']}]: "
                  f"{match.get('home_team')} vs {match.get('away_team')} @ {match.get('clock')}")
            detail = get_match_detail(sport_cfg, match["id"])
            signal = run_in_play_signal(sport_cfg, match, detail, current_odds)

            if signal.get("signal_type") == "TRADE":
                # Try to resolve a US-executable per-game slug.
                us_game = pm_us.find_us_game_market(
                    match.get("home_team", ""), match.get("away_team", "")
                )
                slug       = (us_game.get("slug") or "") if us_game else ""
                executable = bool(slug)
                if executable:
                    signal["market_slug"] = slug
                    signal["entry_price_pct"] = us_game.get("implied_pct",
                                                             signal.get("entry_price_pct", 0))
                    signal["_tick"] = "0.001"
                if _passes_gates_and_checker(signal):
                    send_trade_signal(signal)
                    record_signal_sent(signal)
                    if dry:
                        tag = "" if executable else " (alert-only: no US per-game slug)"
                        print(f"  ⚡ {dry_run_signal(signal)}{tag}")
                    elif not executable:
                        send_status(
                            f"ℹ️ In-play signal is ALERT-ONLY — no US per-game market.\n"
                            f"{signal.get('market','?')} / {signal.get('outcome','?')}"
                        )
                        log_signal(signal, executed=False, error="in-play: no US slug")
                    else:
                        bankroll = pm_us.get_buying_power()
                        cap      = get_trade_cap(bankroll)
                        size_usd = min(
                            bankroll * float(signal.get("size_pct_bankroll", 5)) / 100,
                            cap,
                        )
                        _fire_trade(signal, size_usd, {slug: {
                            "tick": "0.001", "outcome": signal.get("outcome"),
                            "market": signal.get("market"), "implied_pct": signal.get("entry_price_pct"),
                        }})


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

    # Reconcile state against real Polymarket holdings on every startup.
    # Removes ghost positions (recorded in state but not backed by a real PM
    # holding) so PF-03 never blocks trades due to phantom concurrency count.
    try:
        rec_removed, rec_kept = reconcile_positions()
        if rec_removed:
            lines = ", ".join(
                f"{p.get('sport','?')}/{p.get('outcome','?')}"
                for p in rec_removed
            )
            print(f"Startup reconcile: removed {len(rec_removed)} ghost position(s): {lines}")
            try:
                send_status(
                    f"🧹 Startup reconcile: removed {len(rec_removed)} ghost position(s) "
                    f"not backed by real Polymarket holdings ({lines}). "
                    f"{len(rec_kept)} real position(s) kept."
                )
            except Exception:
                pass
        else:
            print(f"Startup reconcile: {len(rec_kept)} position(s) confirmed real.")
    except Exception as e:
        print(f"Startup reconcile error (non-fatal): {e}")

    # Auto-reset phase and orphaned positions when WC is no longer active.
    # Any positions remaining in state from a concluded WC are stale — the markets
    # have resolved on Polymarket and those slots permanently block new trades.
    wc_active = SPORT_CONFIGS.get("world_cup", {}).get("active", False)
    state = load_state()
    if not wc_active:
        # Reset WC-era phase so PF-08 / PF-10 gates no longer fire at FINAL limits.
        if state.get("current_phase") in ("GROUP_STAGE", "R32", "R16", "QF", "SF", "FINAL"):
            update_phase("SEASON")
            print("WC inactive — phase reset to SEASON")
        # Clear any positions that don't belong to a currently-active sport.
        active_keys = {c["key"] for c in active_sports()}
        stale = [p for p in state.get("active_positions", [])
                 if p.get("sport", p.get("edge", "")) not in active_keys
                 and p.get("sport", None) not in (None, "") or
                 # also clear if market_slug matches a known resolved WC slug
                 any(kw in (p.get("market_slug") or "").lower()
                     for kw in ("world-cup", "golden-boot", "wc-winner"))]
        if stale:
            n_stale = len(stale)
            for p in stale:
                state["active_positions"].remove(p)
                state["total_bankroll_deployed_pct"] = max(
                    0, state.get("total_bankroll_deployed_pct", 0) - float(p.get("size_pct", 0) or 0))
            save_state(state)
            print(f"Startup: removed {n_stale} stale WC position(s) from state")
            try:
                send_status(
                    f"🧹 Cleared {n_stale} stale World Cup position(s) — "
                    f"WC is over, those markets resolved. Slots are free for new trades.\n"
                    f"⚠️ If any real WC shares are still held, sell them manually in Polymarket."
                )
            except Exception:
                pass

    # Honour DRY_RUN env var set on Railway — overrides whatever is in state so
    # the operator can flip live/dry without needing a Telegram command.
    # Default is LIVE unless DRY_RUN is explicitly set to a truthy value.
    env_dry = os.environ.get("DRY_RUN", "false").strip().lower()
    if env_dry in ("true", "1", "yes", "on"):
        set_dry_run(True)
        print("DRY_RUN env=true → DRY RUN mode")
    else:
        set_dry_run(False)
        print("DRY_RUN env=false → LIVE mode")
    # Re-read after potential env override so banner + Telegram reflect true mode.
    dry = load_state().get("dry_run", False)

    # ── STARTUP HEALTH CHECK ─────────────────────────────────────────────────
    # Probe every subsystem before the first scan so any misconfiguration is
    # surfaced immediately in Telegram rather than silently blocking the first trade.
    buying_power = pm_us.get_buying_power()
    pm_ok   = buying_power > 0
    pm_note = f"✅ ${buying_power:.2f} buying power" if pm_ok else "❌ $0 — check POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY"
    print(f"Polymarket US: {pm_note} | Per-trade cap: ${get_trade_cap(buying_power):.2f}")

    threading.Thread(target=start_status_server, daemon=True).start()
    threading.Thread(target=telegram_listener,   daemon=True).start()

    # Telegram health: if this send_status succeeds, Telegram is confirmed working.
    tg_resp = send_status(
        f"🟢 Agent online — MULTI-SPORT\n"
        f"Sports: {_active_label()}\n"
        f"Mode: {'DRY RUN ⚪' if dry else '🔴 LIVE'}\n"
        f"──────────────────────\n"
        f"Telegram: ✅ (message delivered)\n"
        f"Polymarket: {pm_note}\n"
        f"Per-trade cap: ${get_trade_cap(buying_power):.0f} | Instant execution\n"
        f"Poll: every {POLL_INTERVAL_MIN} min\n"
        f"Phase: {load_state().get('current_phase', 'SEASON')}\n"
        f"──────────────────────\n"
        f"{'⚠️ PM credentials invalid — no real trades until fixed.' if not pm_ok else 'Ready to trade autonomously.'}"
    )
    if not (isinstance(tg_resp, dict) and tg_resp.get("ok")):
        print(f"WARNING: startup Telegram health check failed: {tg_resp}")
    if not pm_ok:
        # Don't halt — operator may fix credentials via env var without restart —
        # but log loudly so the problem is visible in Railway logs.
        print("WARNING: Polymarket buying power is $0. Check POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY.")

    schedule.every(POLL_INTERVAL_MIN).minutes.do(run_main_analysis)
    schedule.every(IN_PLAY_INTERVAL_SEC).seconds.do(run_in_play_check)
    schedule.every().day.at("12:00").do(send_heartbeat)

    run_main_analysis(triggered_by="STARTUP")

    while True:
        schedule.run_pending()
        time.sleep(5)


if __name__ == "__main__":
    main()
