"""
DAEMON-POLY Trading Bot
US Open 2026 | Shinnecock Hills
Operator: LaDre Martin

Required Secrets (set in Replit Secrets panel):
  SLASH_GOLF_KEY          → RapidAPI key for live golf data
  TELEGRAM_TOKEN          → Telegram bot token from BotFather
  TELEGRAM_CHAT_ID        → Operator Telegram chat ID (default: 8486909237)
  POLYMARKET_API_KEY      → Polymarket API key
  POLYMARKET_PK           → Polymarket wallet private key (0x...)
  POLYMARKET_WALLET       → Polymarket wallet address (0x...)
  POLYMARKET_DEPOSIT_WALLET → Polymarket deposit/funder wallet (0x...)
  ANTHROPIC_API_KEY       → Claude API key
  SESSION_BANKROLL        → Starting bankroll in USD (e.g. 300)
"""

import os
import time
import threading
from datetime import datetime, timezone, date as dt

import requests

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

SLASH_GOLF_KEY            = os.environ.get("SLASH_GOLF_KEY", "")
TELEGRAM_TOKEN            = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID          = os.environ.get("TELEGRAM_CHAT_ID", "8486909237")
POLYMARKET_API_KEY        = os.environ.get("POLYMARKET_API_KEY", "")
POLYMARKET_PK             = os.environ.get("POLYMARKET_PK", "")
POLYMARKET_WALLET         = os.environ.get("POLYMARKET_WALLET", "")
POLYMARKET_DEPOSIT_WALLET = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "") or POLYMARKET_WALLET
ANTHROPIC_API_KEY         = os.environ.get("ANTHROPIC_API_KEY", "")
SESSION_BANKROLL          = float(os.environ.get("SESSION_BANKROLL", "300"))

TOURNAMENT_ID    = "401353268"
POLYMARKET_HOST  = "https://clob.polymarket.com"
SLASH_GOLF_HOST  = "https://live-golf-data.p.rapidapi.com"
TELEGRAM_API     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ANTHROPIC_API    = "https://api.anthropic.com/v1/messages"

# ─────────────────────────────────────────────
# POLYMARKET CLOB CLIENT
# ─────────────────────────────────────────────

_clob_client = None

def get_clob_client():
    """Lazy-init the CLOB client with L1→L2 auth derivation."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    try:
        from py_clob_client.client import ClobClient

        # Step 1 — L1 client to derive API creds
        l1 = ClobClient(
            host=POLYMARKET_HOST,
            chain_id=137,
            key=POLYMARKET_PK,
            signature_type=0,
            funder=POLYMARKET_DEPOSIT_WALLET,
        )
        api_creds = l1.create_or_derive_api_creds()

        # Step 2 — Full L2 client
        _clob_client = ClobClient(
            host=POLYMARKET_HOST,
            chain_id=137,
            key=POLYMARKET_PK,
            creds=api_creds,
            signature_type=0,
            funder=POLYMARKET_DEPOSIT_WALLET,
        )
        print("[POLYMARKET] CLOB client initialised (L2 auth)")
        return _clob_client
    except Exception as e:
        print(f"[POLYMARKET] Client init failed: {e} — will use HTTP fallback")
        return None

# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────

state = {
    "autopilot":            False,
    "bankroll":             SESSION_BANKROLL,
    "starting_bankroll":    SESSION_BANKROLL,
    "open_positions":       [],   # {player, token_id, side, size_usd, shares, entry_pct, order_id, round_entered}
    "closed_positions":     [],   # {player, pnl, ...}
    "banked_profit":        0.0,
    "cycle_pool":           0.0,
    "consecutive_losses":   0,
    "cooldown_active":      False,
    "cycling_active":       True,
    "cycle_no_growth_count":0,
    "current_round":        2,
    "last_leaderboard":     {},   # {name: {position, score, today, thru, cut}}
    "last_odds":            {},   # {name: float %}
    "market_token_map":     {},   # {name: token_id}
    "all_groups_finished":  False,
    "cut_players":          set(),
    "suspended":            False,
    "data_stale":           False,
    "total_wins":           0,
    "total_losses":         0,
    "scan_count_this_hour": 0,
    "scan_hour_reset":      datetime.now(timezone.utc).hour,
    "prev_leaderboard":     {},
    "prev_odds":            {},
}

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────

def tg(message: str):
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:
        print(f"[TG ERROR] {e}")

def get_telegram_updates(offset: int = 0):
    try:
        r = requests.get(
            f"{TELEGRAM_API}/getUpdates",
            params={"offset": offset, "timeout": 10},
            timeout=15,
        )
        return r.json().get("result", [])
    except:
        return []

# ─────────────────────────────────────────────
# SLASH GOLF — LEADERBOARD
# ─────────────────────────────────────────────

def fetch_leaderboard() -> dict:
    """Pull live leaderboard from Slash Golf API."""
    try:
        headers = {
            "X-RapidAPI-Key": SLASH_GOLF_KEY,
            "X-RapidAPI-Host": "live-golf-data.p.rapidapi.com",
        }
        r = requests.get(
            f"{SLASH_GOLF_HOST}/leaderboard",
            headers=headers,
            params={"orgId": "1", "tournId": TOURNAMENT_ID, "year": "2026"},
            timeout=10,
        )
        data = r.json()
        leaderboard = {}
        players = data.get("leaderboard", data.get("players", []))
        for p in players:
            name = p.get("playerName", p.get("name", "Unknown"))
            leaderboard[name] = {
                "position": p.get("position", p.get("pos", 0)),
                "score":    p.get("total", p.get("score", 0)),
                "today":    p.get("today", 0),
                "thru":     p.get("thru", "F"),
                "cut":      p.get("status", "") in ["CUT", "WD", "DQ"],
            }
        state["last_leaderboard"] = leaderboard
        state["all_groups_finished"] = all(
            str(p.get("thru", "")) == "F" for p in players
        )
        state["data_stale"] = False
        return leaderboard
    except Exception as e:
        tg(f"SLASH GOLF ERROR: {e}")
        state["data_stale"] = True
        return state["last_leaderboard"]

# ─────────────────────────────────────────────
# POLYMARKET — ODDS & MARKET LOOKUP
# ─────────────────────────────────────────────

def _fetch_markets_http():
    """Fetch US Open 2026 markets via raw HTTP."""
    headers = {"Authorization": f"Bearer {POLYMARKET_API_KEY}"}
    r = requests.get(
        f"{POLYMARKET_HOST}/markets",
        headers=headers,
        params={"keyword": "US Open 2026", "active": "true"},
        timeout=10,
    )
    return r.json().get("data", [])

def fetch_polymarket_odds(player_names: list) -> dict:
    """Pull current win probabilities and cache token IDs."""
    try:
        client = get_clob_client()
        odds = {}

        if client:
            resp = client.get_markets()
            markets = resp.get("data", []) if isinstance(resp, dict) else []
        else:
            markets = _fetch_markets_http()

        for market in markets:
            for token in market.get("tokens", []):
                outcome  = token.get("outcome", "")
                price    = float(token.get("price", 0))
                token_id = token.get("token_id", "")
                for name in player_names:
                    if name.lower() in outcome.lower():
                        odds[name] = round(price * 100, 2)
                        if token_id:
                            state["market_token_map"][name] = token_id

        state["last_odds"] = odds
        return odds
    except Exception as e:
        tg(f"POLYMARKET ODDS ERROR: {e}")
        return state["last_odds"]

# ─────────────────────────────────────────────
# POLYMARKET — ORDER EXECUTION
# ─────────────────────────────────────────────

def _usd_to_shares(size_usd: float, price_pct: float) -> float:
    """Convert USD budget → share count (Polymarket sizes in shares, not USD)."""
    price = max(price_pct / 100, 0.01)
    return round(size_usd / price, 2)

def place_polymarket_order(token_id: str, side: str, size_usd: float, player_name: str) -> dict:
    """Place a YES/NO order. side = 'YES' or 'NO'."""
    try:
        client = get_clob_client()
        price_pct = state["last_odds"].get(player_name, 50)
        price     = round(price_pct / 100, 2)
        shares    = _usd_to_shares(size_usd, price_pct)

        if client:
            from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
            from py_clob_client.order_builder.constants import BUY, SELL
            order_side = BUY if side == "YES" else SELL
            result = client.create_and_post_order(
                OrderArgs(token_id=token_id, price=price, size=shares, side=order_side),
                options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
            )
            return result if isinstance(result, dict) else {"orderID": str(result), "shares": shares}
        else:
            headers = {"Authorization": f"Bearer {POLYMARKET_API_KEY}", "Content-Type": "application/json"}
            r = requests.post(
                f"{POLYMARKET_HOST}/order",
                headers=headers,
                json={"market": token_id, "side": side, "size": str(shares), "type": "MARKET",
                      "funder": POLYMARKET_DEPOSIT_WALLET},
                timeout=15,
            )
            return r.json()
    except Exception as e:
        tg(f"POLYMARKET ORDER ERROR: {e}")
        return {"error": str(e)}

def exit_polymarket_position(token_id: str, size_usd: float, player_name: str) -> dict:
    """Sell (exit) an open position."""
    try:
        client = get_clob_client()
        price_pct = state["last_odds"].get(player_name, 50)
        price     = round(price_pct / 100, 2)
        shares    = _usd_to_shares(size_usd, price_pct)

        if client:
            from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions
            from py_clob_client.order_builder.constants import SELL
            result = client.create_and_post_order(
                OrderArgs(token_id=token_id, price=price, size=shares, side=SELL),
                options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
            )
            return result if isinstance(result, dict) else {"orderID": str(result)}
        else:
            headers = {"Authorization": f"Bearer {POLYMARKET_API_KEY}", "Content-Type": "application/json"}
            r = requests.post(
                f"{POLYMARKET_HOST}/order",
                headers=headers,
                json={"market": token_id, "side": "SELL", "size": str(shares), "type": "MARKET",
                      "funder": POLYMARKET_DEPOSIT_WALLET},
                timeout=15,
            )
            return r.json()
    except Exception as e:
        tg(f"EXIT ORDER ERROR: {e}")
        return {"error": str(e)}

# ─────────────────────────────────────────────
# CLAUDE SIGNAL ENGINE
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are a golf prediction market trading analyst. Evaluate live tournament data and generate trade signals for Polymarket golf markets. Output must be concise and Telegram-readable — plain text, no markdown.

CORE STRATEGY:
Entry mid-tournament only (R2-R3). Never pre-tournament. Target players 3-6 shots off lead with ascending trajectory. YES positions where market probability lags performance. Avoid leaders priced >40%. Max 3-4 active positions. Never stack >2 on same player.

SHINNECOCK HILLS EDGE:
Fade bombers, reward accuracy and low ball-flight. SG:APP is #1 differentiator. Public over-bets Scheffler. Find value in top-30 OWGR players priced under 8%. R2 movers gaining 3+ shots still outside top 10 are primary targets. R4 field historically goes over par.

FOR EACH PLAYER OUTPUT EXACTLY:
PLAYER: [Name]
MARKET: [%]
FAIR VALUE: [%]
EDGE: [+/- %]
SIGNAL: [ENTER / HOLD / AVOID]
CONFIDENCE: [High / Medium / Low]
WHY: [1 sentence]

Rank by edge descending. Mark top pick with ★
If no edge >8%: reply NO TRADE — WAIT
R4 rule: only signal ENTER if edge >15% and player within 4 shots of lead."""

def claude_signal_scan(leaderboard: dict, odds: dict, active_positions: list, round_num: int) -> str:
    try:
        lb_text = "\n".join([
            f"{v['position']} | {name} | {v['score']:+d} | Today: {v['today']:+d} | Thru: {v['thru']}"
            for name, v in sorted(leaderboard.items(), key=lambda x: x[1].get("position", 999))[:20]
        ])
        odds_text  = "\n".join([f"{name}: {pct}%" for name, pct in sorted(odds.items(), key=lambda x: -x[1])[:20]])
        pos_text   = "\n".join([
            f"  {p['player']} YES @ {p['entry_pct']}% — now {odds.get(p['player'], '?')}%"
            for p in active_positions
        ]) or "  None"

        user_msg = (
            f"SCAN | Round: R{round_num} | {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n\n"
            f"LEADERBOARD:\n{lb_text}\n\n"
            f"POLYMARKET ODDS:\n{odds_text}\n\n"
            f"ACTIVE POSITIONS:\n{pos_text}\n\n"
            f"Run full signal scan. Top 3 edges. Flag HOLD or EXIT on active positions. Apply R{round_num} rules."
        )
        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": "claude-sonnet-4-5",
            "max_tokens": 700,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_msg}],
        }
        r = requests.post(ANTHROPIC_API, headers=headers, json=payload, timeout=30)
        return r.json()["content"][0]["text"]
    except Exception as e:
        return f"CLAUDE ERROR: {e}"

# ─────────────────────────────────────────────
# PRE-FLIGHT
# ─────────────────────────────────────────────

def run_preflight(player: str, edge: float, confidence: str, round_num: int) -> tuple:
    lb          = state["last_leaderboard"]
    player_data = lb.get(player, {})
    position    = player_data.get("position", 999)
    shots_back  = max(position - 1, 0)

    if round_num < 2:
        return False, "PF-01: R1 blackout"
    if round_num == 4 and edge < 15:
        return False, f"PF-01: R4 needs edge >15% (got {edge:.1f}%)"
    if edge < 8:
        return False, f"PF-02: Edge {edge:.1f}% below 8% minimum"
    if len(state["open_positions"]) >= 4:
        return False, "PF-03: Max 4 open positions"
    if sum(1 for p in state["open_positions"] if p["player"] == player) >= 2:
        return False, f"PF-04: Already 2 positions on {player}"
    shot_gate = 4 if round_num == 4 else 6
    if shots_back > shot_gate:
        return False, f"PF-05: {player} {shots_back} shots back (gate: {shot_gate})"
    if state["cooldown_active"]:
        return False, "PF-06: Cooldown — send COOLDOWN: RESET"
    if state["data_stale"]:
        return False, "PF-07: Stale data — leaderboard unavailable"
    if confidence not in ("High", "Medium"):
        return False, f"PF-08: Confidence '{confidence}' too low"
    if player not in state["market_token_map"]:
        return False, f"PF-09: No Polymarket token found for {player}"
    return True, "PASS"

# ─────────────────────────────────────────────
# SIZING
# ─────────────────────────────────────────────

def calculate_size(edge: float, confidence: str) -> float:
    bankroll = state["bankroll"]
    if confidence == "High" and edge > 15:
        pct = 0.05
    elif confidence == "High":
        pct = 0.03
    elif confidence == "Medium" and edge > 12:
        pct = 0.02
    else:
        pct = 0.01
    size = round(bankroll * pct, 2)
    current_exposure = sum(p["size_usd"] for p in state["open_positions"])
    max_exposure     = bankroll * 0.10
    if current_exposure + size > max_exposure:
        size = max(0.0, max_exposure - current_exposure)
    return size

# ─────────────────────────────────────────────
# EXECUTE TRADE
# ─────────────────────────────────────────────

def execute_trade(player: str, edge: float, confidence: str,
                  entry_pct: float, round_num: int, from_cycle_pool: bool = False) -> bool:
    ok, reason = run_preflight(player, edge, confidence, round_num)
    if not ok:
        tg(f"BLOCKED: {player} — {reason}")
        return False

    size = calculate_size(edge, confidence)
    if from_cycle_pool:
        size = min(state["cycle_pool"], size)
    if size <= 0:
        tg(f"BLOCKED: {player} — exposure cap reached")
        return False

    token_id = state["market_token_map"][player]
    tg(f"EXECUTING: {player} YES ${size:.2f} @ {entry_pct}% | Edge: {edge:.1f}% | {confidence}")

    result = place_polymarket_order(token_id, "YES", size, player)

    if "error" in result:
        tg(f"ORDER FAILED: {player} — {result['error']}")
        state["consecutive_losses"] += 1
        if state["consecutive_losses"] >= 3:
            state["cooldown_active"] = True
            tg("COOLDOWN ACTIVATED — 3 consecutive failures. Send COOLDOWN: RESET to resume.")
        return False

    order_id = result.get("orderID", result.get("id", "UNKNOWN"))
    shares   = result.get("shares", _usd_to_shares(size, entry_pct))

    state["open_positions"].append({
        "player":        player,
        "token_id":      token_id,
        "side":          "YES",
        "size_usd":      size,
        "shares":        shares,
        "entry_pct":     entry_pct,
        "order_id":      order_id,
        "round_entered": round_num,
        "from_cycle":    from_cycle_pool,
    })
    if from_cycle_pool:
        state["cycle_pool"] -= size

    state["consecutive_losses"] = 0
    tg(f"✅ FILLED: {player} YES | {shares:.2f} shares @ ${entry_pct/100:.2f} | Order: {order_id}")
    return True

# ─────────────────────────────────────────────
# PROFIT CYCLING
# ─────────────────────────────────────────────

def run_profit_cycle(round_num: int):
    if not state["cycling_active"]:
        tg("CYCLING: Disabled")
        return

    realized = sum(p.get("pnl", 0) for p in state["closed_positions"])
    if realized <= 0:
        tg("NO PROFIT TO CYCLE — standard rules apply")
        return

    lock   = round(realized * 0.5, 2)
    cycle  = round(realized * 0.5, 2)
    prev   = state["banked_profit"]
    state["banked_profit"] += lock
    state["cycle_pool"]     = cycle

    tg(f"--- PROFIT CYCLE ---\nR{round_num}→R{round_num+1}\nRealized: ${realized:.2f}\n"
       f"Locked:   ${lock:.2f}\nCycle:    ${cycle:.2f}\nBanked:   ${state['banked_profit']:.2f}\n"
       f"--------------------")

    if state["banked_profit"] <= prev:
        state["cycle_no_growth_count"] += 1
    else:
        state["cycle_no_growth_count"] = 0

    if state["cycle_no_growth_count"] >= 2:
        state["cycling_active"] = False
        tg("CYCLING PAUSED — reverting to standard sizing")

# ─────────────────────────────────────────────
# SIGNAL SCAN
# ─────────────────────────────────────────────

def run_scan(round_num: int, trigger: str = "SCHEDULED", from_cycle: bool = False):
    now = datetime.now(timezone.utc)
    cur_hour = now.hour
    if cur_hour != state["scan_hour_reset"]:
        state["scan_count_this_hour"] = 0
        state["scan_hour_reset"]      = cur_hour
    if trigger != "SCHEDULED":
        if state["scan_count_this_hour"] >= 3:
            tg("SCAN QUEUED: rate limit 3/hr. Will run at next 10-min mark.")
            return
        state["scan_count_this_hour"] += 1

    if state["suspended"]:
        tg("BOT SUSPENDED — send AUTOPILOT: RESUME")
        return

    tg(f"🔍 SCAN [{trigger}] R{round_num} — {now.strftime('%H:%M UTC')}")

    leaderboard  = fetch_leaderboard()
    player_names = list(leaderboard.keys())
    odds         = fetch_polymarket_odds(player_names)

    signal_text  = claude_signal_scan(leaderboard, odds, state["open_positions"], round_num)
    tg(f"--- SIGNAL SCAN R{round_num} ---\n{signal_text}\n-----------------------")

    if not state["autopilot"]:
        return

    # Parse Claude's structured output and execute ENTER signals
    lines          = signal_text.split("\n")
    current_player = {}
    for line in lines:
        line = line.strip()
        if line.startswith("PLAYER:"):
            current_player = {"player": line.split(":", 1)[1].strip().lstrip("★ ")}
        elif line.startswith("MARKET:"):
            try:
                current_player["market_pct"] = float(line.split(":", 1)[1].strip().replace("%", ""))
            except:
                pass
        elif line.startswith("EDGE:"):
            try:
                current_player["edge"] = float(line.split(":", 1)[1].strip().replace("%", "").replace("+", ""))
            except:
                pass
        elif line.startswith("SIGNAL:"):
            current_player["signal"] = line.split(":", 1)[1].strip()
        elif line.startswith("CONFIDENCE:"):
            current_player["confidence"] = line.split(":", 1)[1].strip()
            # All fields collected — attempt ENTER
            if current_player.get("signal") == "ENTER":
                player = current_player.get("player", "")
                if player and player not in state["cut_players"]:
                    execute_trade(
                        player,
                        current_player.get("edge", 0),
                        current_player.get("confidence", "Low"),
                        current_player.get("market_pct", 0),
                        round_num,
                        from_cycle,
                    )
            current_player = {}

    send_cycle_report(round_num)

# ─────────────────────────────────────────────
# MOVEMENT TRIGGERS
# ─────────────────────────────────────────────

def check_movement_triggers(round_num: int):
    if round_num < 2 or state["suspended"]:
        return

    new_lb   = fetch_leaderboard()
    new_odds = fetch_polymarket_odds(list(new_lb.keys()))
    old_lb   = state.get("prev_leaderboard") or new_lb
    old_odds = state.get("prev_odds") or new_odds

    # T1: Odds collapse on open position
    for pos in state["open_positions"]:
        player   = pos["player"]
        old_pct  = old_odds.get(player, 0)
        new_pct  = new_odds.get(player, old_pct)
        drop     = old_pct - new_pct
        if drop >= 8:
            tg(f"⚠️ ODDS COLLAPSE — {player}\n"
               f"Entered @ {pos['entry_pct']}% | Now {new_pct}% (−{drop:.1f}pp)\n"
               f"Evaluating early exit...")
            run_scan(round_num, trigger="T1_ODDS_COLLAPSE")

    # T2: Mispricing — leaderboard moved, market hasn't caught up
    for player, new_data in new_lb.items():
        if player in state["cut_players"]:
            continue
        old_data   = old_lb.get(player, new_data)
        pos_change = old_data.get("position", 99) - new_data.get("position", 99)
        old_pct    = old_odds.get(player, 0)
        new_pct    = new_odds.get(player, old_pct)
        odds_move  = new_pct - old_pct
        if pos_change >= 2 and odds_move <= 3:
            tg(f"⚡ MISPRICING — {player}\n"
               f"Pos: {old_data.get('position')}→{new_data.get('position')} (+{pos_change})\n"
               f"Odds barely moved: {old_pct}%→{new_pct}% (+{odds_move:.1f}pp)\nScanning...")
            run_scan(round_num, trigger="T2_MISPRICING")

    # T3: Leader pulling away, possibly underpriced
    sorted_lb = sorted(new_lb.items(), key=lambda x: x[1].get("position", 999))
    if len(sorted_lb) >= 2:
        ldr_name = sorted_lb[0][0]
        gap      = sorted_lb[0][1].get("score", 0) - sorted_lb[1][1].get("score", 0)
        ldr_pct  = new_odds.get(ldr_name, 100)
        if gap >= 3 and ldr_pct < 35:
            tg(f"⚡ LEADER SEPARATION — {ldr_name} ({gap} shots clear, {ldr_pct}%)\nScanning...")
            run_scan(round_num, trigger="T3_LEADER_SEP")

    # T4: Profit target hit (50% gain)
    for pos in state["open_positions"]:
        player  = pos["player"]
        entry   = pos["entry_pct"]
        current = new_odds.get(player, entry)
        if entry > 0 and current >= entry * 1.5:
            gain = ((current - entry) / entry) * 100
            tg(f"🎯 PROFIT TARGET HIT — {player}\n"
               f"Entry {entry}% → Now {current}% (+{gain:.0f}%)\n"
               f"Reply: EXIT HALF {player}  or  EXIT FULL {player}")

    # T5: Scoring conditions shift
    live_scores = [v.get("today", 0) for v in new_lb.values() if str(v.get("thru")) != "F"]
    old_scores  = [v.get("today", 0) for v in old_lb.values()  if str(v.get("thru")) != "F"]
    if live_scores and old_scores:
        delta = sum(live_scores) / len(live_scores) - sum(old_scores) / len(old_scores)
        if abs(delta) >= 1.5:
            tg(f"🌬️ CONDITIONS SHIFT — scoring avg {delta:+.1f} strokes\n"
               f"Pausing unscheduled entries for 30 min...")
            state["suspended"] = True
            threading.Timer(1800, lambda: state.update({"suspended": False})).start()

    state["prev_leaderboard"] = new_lb
    state["prev_odds"]        = new_odds

# ─────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────

def send_cycle_report(round_num: int):
    pos_lines = "\n".join([
        f"  {p['player']} YES @ {p['entry_pct']}% | ${p['size_usd']:.2f} | #{p['order_id']}"
        for p in state["open_positions"]
    ]) or "  None"
    exposure = sum(p["size_usd"] for p in state["open_positions"])
    tg(
        f"--- STATUS | R{round_num} | {datetime.now(timezone.utc).strftime('%H:%M UTC')} ---\n"
        f"Bankroll: ${state['bankroll']:.2f} | Banked: ${state['banked_profit']:.2f}\n\n"
        f"OPEN ({len(state['open_positions'])}/4):\n{pos_lines}\n\n"
        f"Exposure: ${exposure:.2f} ({exposure/state['bankroll']*100:.1f}%)\n"
        f"Autopilot: {'ON 🟢' if state['autopilot'] else 'OFF 🔴'}\n"
        f"-----------------------------"
    )

def send_pnl_report():
    odds    = state["last_odds"]
    now_str = datetime.now(timezone.utc).strftime('%H:%M UTC')
    rnd     = state["current_round"]

    if not state["open_positions"]:
        realized = sum(p.get("pnl", 0) for p in state["closed_positions"])
        tg(f"--- P&L REPORT | R{rnd} | {now_str} ---\n"
           f"No open positions.\n"
           f"Realized P&L: {'+' if realized>=0 else ''}${realized:.2f}\n"
           f"Bankroll: ${state['bankroll']:.2f} | Banked: ${state['banked_profit']:.2f}\n"
           f"-----------------------------")
        return

    lines            = []
    total_unrealized = 0.0
    total_exposure   = 0.0

    for pos in state["open_positions"]:
        player  = pos["player"]
        entry   = pos["entry_pct"]
        size    = pos["size_usd"]
        current = odds.get(player, entry)

        if entry > 0 and current > 0:
            pnl_pct = (current - entry) / entry * 100
            pnl_usd = round(size * (current / entry - 1), 2)
        else:
            pnl_pct = pnl_usd = 0.0

        total_unrealized += pnl_usd
        total_exposure   += size
        arrow = "▲" if pnl_usd >= 0 else "▼"
        sign  = "+" if pnl_usd >= 0 else ""
        lines.append(
            f"{arrow} {player}\n"
            f"   Entry {entry:.1f}% → Now {current:.1f}% ({sign}{pnl_pct:.0f}%)\n"
            f"   Size ${size:.2f} | Est P&L: {sign}${pnl_usd:.2f}"
        )

    realized  = sum(p.get("pnl", 0) for p in state["closed_positions"])
    total_pnl = realized + total_unrealized

    tg(
        f"--- P&L REPORT | R{rnd} | {now_str} ---\n"
        + "\n".join(lines)
        + f"\n\nPositions:      {len(state['open_positions'])}/4"
        + f"\nExposure:       ${total_exposure:.2f}"
        + f"\nUnrealized P&L: {'+' if total_unrealized>=0 else ''}${total_unrealized:.2f}"
        + f"\nRealized P&L:   {'+' if realized>=0 else ''}${realized:.2f}"
        + f"\nNet P&L:        {'+' if total_pnl>=0 else ''}${total_pnl:.2f}"
        + f"\nBankroll: ${state['bankroll']:.2f} | Banked: ${state['banked_profit']:.2f}"
        + "\n-----------------------------"
    )

def send_leaderboard():
    lb  = fetch_leaderboard()
    rnd = state["current_round"]
    if not lb:
        tg("Leaderboard unavailable — Slash Golf API error")
        return
    sorted_lb = sorted(lb.items(), key=lambda x: x[1].get("position", 999))[:15]
    odds      = state["last_odds"]
    lines     = []
    for name, d in sorted_lb:
        score = f"{d['score']:+d}" if isinstance(d["score"], int) else str(d["score"])
        today = f"{d['today']:+d}" if isinstance(d["today"], int) else str(d["today"])
        thru  = d.get("thru", "-")
        pct   = odds.get(name)
        mkt   = f" [{pct:.1f}%]" if pct else ""
        lines.append(f"{str(d['position']).rjust(2)}. {name} | {score} ({today} thru {thru}){mkt}")
    tg(
        f"--- LEADERBOARD | R{rnd} | {datetime.now(timezone.utc).strftime('%H:%M UTC')} ---\n"
        + "\n".join(lines)
        + "\n-----------------------------"
    )

def send_session_summary():
    total    = state["total_wins"] + state["total_losses"]
    win_rate = (state["total_wins"] / total * 100) if total else 0
    pnl      = state["bankroll"] - state["starting_bankroll"] + state["banked_profit"]
    tg(
        f"--- FINAL SESSION REPORT ---\n"
        f"US Open 2026 | Shinnecock Hills\n"
        f"Starting: ${state['starting_bankroll']:.2f}\n"
        f"Ending:   ${state['bankroll']:.2f}\n"
        f"Banked:   ${state['banked_profit']:.2f}\n"
        f"Net P&L:  {'+' if pnl>=0 else ''}${pnl:.2f}\n"
        f"Win Rate: {win_rate:.0f}% ({state['total_wins']}W / {state['total_losses']}L)\n"
        f"Trades:   {total}\n"
        f"----------------------------"
    )

# ─────────────────────────────────────────────
# COMMAND HANDLER
# ─────────────────────────────────────────────

HELP_TEXT = """DAEMON-POLY Commands:
AUTOPILOT: ON       — activate live trading
AUTOPILOT: OFF      — signals only, no trades
AUTOPILOT: RESUME   — resume after suspension
STATUS              — open positions + exposure
REPORT              — P&L per position vs entry
LEADERBOARD         — live top-15 + Polymarket odds
SCAN                — manual Claude signal scan
BANKROLL: $[X]      — update bankroll
COOLDOWN: RESET     — clear cooldown
CYCLE PROFIT        — trigger profit cycling
CYCLE: OFF          — disable cycling
EXIT HALF [player]  — sell half a position
EXIT FULL [player]  — close full position
HELP                — show this message"""

def handle_command(text: str):
    text = text.strip()
    cmd  = text.upper()

    if cmd == "AUTOPILOT: ON":
        state["autopilot"] = True
        tg("🟢 AUTOPILOT: ON — live trading active")

    elif cmd == "AUTOPILOT: OFF":
        state["autopilot"] = False
        tg("🔴 AUTOPILOT: OFF — signal mode only")

    elif cmd == "AUTOPILOT: RESUME":
        state["autopilot"]      = True
        state["suspended"]      = False
        state["cooldown_active"] = False
        tg("🟢 AUTOPILOT: RESUMED — all systems active")

    elif cmd in ("STATUS", "POSITIONS"):
        send_cycle_report(state["current_round"])

    elif cmd == "REPORT":
        send_pnl_report()

    elif cmd == "LEADERBOARD":
        send_leaderboard()

    elif cmd == "SCAN":
        run_scan(state["current_round"], trigger="MANUAL")

    elif cmd == "COOLDOWN: RESET":
        state["consecutive_losses"] = 0
        state["cooldown_active"]    = False
        tg("COOLDOWN: RESET — trading resumed")

    elif cmd == "CYCLE PROFIT":
        run_profit_cycle(state["current_round"])

    elif cmd == "CYCLE: OFF":
        state["cycling_active"] = False
        tg("CYCLING: disabled for remainder of session")

    elif cmd == "HELP":
        tg(HELP_TEXT)

    elif cmd.startswith("BANKROLL:"):
        try:
            amount = float(text.split(":", 1)[1].strip().replace("$", "").replace(",", ""))
            state["bankroll"]          = amount
            state["starting_bankroll"] = amount
            tg(f"BANKROLL SET: ${amount:.2f}")
        except:
            tg("Format: BANKROLL: $300")

    elif text.upper().startswith("EXIT HALF "):
        player = text[10:].strip()
        pos    = next((p for p in state["open_positions"] if p["player"].lower() == player.lower()), None)
        if pos:
            half = pos["size_usd"] / 2
            exit_polymarket_position(pos["token_id"], half, player)
            tg(f"EXIT HALF: {player} — sold ${half:.2f}")
        else:
            tg(f"No open position for: {player}")

    elif text.upper().startswith("EXIT FULL "):
        player = text[10:].strip()
        pos    = next((p for p in state["open_positions"] if p["player"].lower() == player.lower()), None)
        if pos:
            exit_polymarket_position(pos["token_id"], pos["size_usd"], player)
            state["open_positions"].remove(pos)
            tg(f"EXIT FULL: {player} — closed ${pos['size_usd']:.2f}")
        else:
            tg(f"No open position for: {player}")

    else:
        tg(f"Unknown command: {text}\nSend HELP for full command list.")

# ─────────────────────────────────────────────
# ROUND DETECTION
# ─────────────────────────────────────────────

def get_current_round() -> int:
    today = datetime.now(timezone.utc).date()
    if today == dt(2026, 6, 18): return 1
    if today == dt(2026, 6, 19): return 2
    if today == dt(2026, 6, 20): return 3
    if today == dt(2026, 6, 21): return 4
    return 0

# ─────────────────────────────────────────────
# SCHEDULER LOOP
# ─────────────────────────────────────────────

def schedule_loop():
    flags = {k: False for k in [
        "r1_monitor", "r2_scan1", "r2_scan2",
        "r3_morning", "r3_scan1", "r3_scan2", "r3_night",
        "r4_morning", "r4_11", "r4_14", "r4_17", "r4_18",
    ]}
    r2_scan1_time = r3_scan1_time = None

    tg(
        "🤖 DAEMON-POLY ONLINE\n"
        "US Open 2026 | Shinnecock Hills\n"
        f"Bankroll: ${SESSION_BANKROLL} | Autopilot: OFF\n"
        "Send BANKROLL: $[amount] then AUTOPILOT: ON to begin\n"
        "Send HELP for all commands."
    )

    while True:
        now       = datetime.now(timezone.utc)
        hour      = now.hour
        minute    = now.minute
        rnd       = get_current_round()
        state["current_round"] = rnd

        # ── R1 THURSDAY ──
        if rnd == 1:
            fetch_leaderboard()
            if state["all_groups_finished"] and not flags["r1_monitor"]:
                lb = state["last_leaderboard"]
                top10 = "\n".join([
                    f"{v['position']}. {name} {v['score']:+d} | {v['today']:+d} thru {v['thru']}"
                    for name, v in sorted(lb.items(), key=lambda x: x[1].get("position", 999))[:10]
                ])
                tg(f"📋 R1 COMPLETE — observation only\n{top10}")
                flags["r1_monitor"] = True

        # ── R2 FRIDAY ──
        elif rnd == 2:
            fetch_leaderboard()
            if state["all_groups_finished"] and not flags["r2_scan1"]:
                tg("R2 COMPLETE — running Scan 1...")
                run_scan(2, "SCHEDULED_R2_S1")
                flags["r2_scan1"] = True
                r2_scan1_time     = now

            if flags["r2_scan1"] and r2_scan1_time and not flags["r2_scan2"]:
                if (now - r2_scan1_time).total_seconds() >= 2100:
                    tg("R2 Scan 2 — updated odds...")
                    run_scan(2, "SCHEDULED_R2_S2")
                    if state["cycling_active"]:
                        run_profit_cycle(2)
                    # Purge cut players
                    for name, d in state["last_leaderboard"].items():
                        if d.get("cut"):
                            state["cut_players"].add(name)
                    if state["cut_players"]:
                        tg(f"CUT: {', '.join(state['cut_players'])}")
                    flags["r2_scan2"] = True

            check_movement_triggers(2)

        # ── R3 SATURDAY ──
        elif rnd == 3:
            if hour == 12 and minute < 10 and not flags["r3_morning"]:
                tg("R3 PRE-ROUND CHECK")
                send_cycle_report(3)
                flags["r3_morning"] = True

            fetch_leaderboard()
            if state["all_groups_finished"] and not flags["r3_scan1"]:
                tg("R3 COMPLETE — running Scan 1...")
                run_scan(3, "SCHEDULED_R3_S1")
                flags["r3_scan1"] = True
                r3_scan1_time     = now

            if flags["r3_scan1"] and r3_scan1_time and not flags["r3_scan2"]:
                try:
                    if (now - r3_scan1_time).total_seconds() >= 2100:
                        run_scan(3, "SCHEDULED_R3_S2")
                        if state["cycling_active"]:
                            run_profit_cycle(3)
                        flags["r3_scan2"] = True
                except:
                    pass

            if hour == 1 and minute < 10 and flags["r3_scan2"] and not flags["r3_night"]:
                tg("SATURDAY NIGHT — R4 preview & position review")
                lb     = state["last_leaderboard"]
                sorted_lb = sorted(lb.items(), key=lambda x: x[1].get("position", 999))
                lead_score = sorted_lb[0][1].get("score", 0) if sorted_lb else 0
                exited = []
                for pos in state["open_positions"][:]:
                    shots_back = lb.get(pos["player"], {}).get("score", 0) - lead_score
                    if shots_back > 4:
                        exit_polymarket_position(pos["token_id"], pos["size_usd"], pos["player"])
                        state["open_positions"].remove(pos)
                        exited.append(pos["player"])
                tg(f"R4 PREVIEW\nExited (>4 back): {', '.join(exited) or 'None'}\n"
                   f"Remaining: {len(state['open_positions'])}/4 | Banked: ${state['banked_profit']:.2f}\n"
                   f"R4 rules: 15% edge min, 4-shot gate")
                flags["r3_night"] = True

            check_movement_triggers(3)

        # ── R4 SUNDAY ──
        elif rnd == 4:
            if hour == 12 and minute < 10 and not flags["r4_morning"]:
                tg("R4 ACTIVE — 15% edge min, 4-shot gate")
                send_cycle_report(4)
                flags["r4_morning"] = True

            scheduled = [(15, "r4_11", "R4_11AM"), (18, "r4_14", "R4_2PM"),
                         (21, "r4_17", "R4_5PM"),  (22, "r4_18", "R4_6PM_FINAL")]
            for scan_hour, flag_key, label in scheduled:
                if hour == scan_hour and minute < 10 and not flags[flag_key]:
                    run_scan(4, f"SCHEDULED_{label}")
                    flags[flag_key] = True

            if hour == 23 and minute < 10 and flags["r4_18"]:
                send_session_summary()

            check_movement_triggers(4)

        time.sleep(600)  # poll every 10 minutes

# ─────────────────────────────────────────────
# TELEGRAM LISTENER
# ─────────────────────────────────────────────

def telegram_listener():
    offset = 0
    while True:
        try:
            for update in get_telegram_updates(offset):
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                text   = msg.get("text", "")
                cid    = str(msg.get("chat", {}).get("id", ""))
                if cid == str(TELEGRAM_CHAT_ID) and text:
                    handle_command(text)
        except Exception as e:
            print(f"[LISTENER ERROR] {e}")
        time.sleep(3)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 52)
    print("  DAEMON-POLY | US Open 2026 | Shinnecock Hills")
    print("=" * 52)
    print(f"  Chat ID  : {TELEGRAM_CHAT_ID}")
    print(f"  Bankroll : ${SESSION_BANKROLL}")
    print(f"  Round    : R{get_current_round() or '?'}")
    print()
    print("  Connecting to Polymarket CLOB...")
    get_clob_client()
    print("  Pre-loading leaderboard + market tokens...")
    lb = fetch_leaderboard()
    if lb:
        fetch_polymarket_odds(list(lb.keys()))
        print(f"  Leaderboard: {len(lb)} players loaded")
        print(f"  Token map  : {len(state['market_token_map'])} markets mapped")
    print()
    print("  Starting Telegram listener + scheduler...")
    threading.Thread(target=telegram_listener, daemon=True).start()
    schedule_loop()
