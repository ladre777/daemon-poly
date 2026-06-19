"""
DAEMON-POLY Trading Bot
US Open 2026 | Shinnecock Hills
Operator: LaDre Martin

SETUP (Replit Secrets — never hardcode real values):
  SLASH_GOLF_KEY         → your RapidAPI key for Slash Golf
  TELEGRAM_TOKEN         → your Telegram bot token (from BotFather)
  POLYMARKET_API_KEY     → your Polymarket API key
  POLYMARKET_PK          → your Polymarket wallet private key (0x...)
  POLYMARKET_WALLET      → your Polymarket wallet address (0x...)
  POLYMARKET_DEPOSIT_WALLET → your Polymarket deposit/funder wallet (0x...)
  ANTHROPIC_API_KEY      → your Claude API key
  SESSION_BANKROLL       → starting bankroll in USD (e.g. 300)
"""

import os
import time
import json
import requests
import threading
from datetime import datetime, timezone
from typing import Optional

# ─────────────────────────────────────────────
# CONFIGURATION — pulled from environment/Replit Secrets
# ─────────────────────────────────────────────

SLASH_GOLF_KEY        = os.environ.get("SLASH_GOLF_KEY", "")
TELEGRAM_TOKEN        = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "8486909237")
POLYMARKET_API_KEY    = os.environ.get("POLYMARKET_API_KEY", "")
POLYMARKET_PK         = os.environ.get("POLYMARKET_PK", "")
POLYMARKET_WALLET     = os.environ.get("POLYMARKET_WALLET", "")
POLYMARKET_DEPOSIT_WALLET = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "")
ANTHROPIC_API_KEY     = os.environ.get("ANTHROPIC_API_KEY", "")

SESSION_BANKROLL      = float(os.environ.get("SESSION_BANKROLL", "300"))

TOURNAMENT_ID         = "401353268"   # US Open 2026 — update if Slash Golf uses different ID
POLYMARKET_HOST       = "https://clob.polymarket.com"
SLASH_GOLF_HOST       = "https://live-golf-data.p.rapidapi.com"
TELEGRAM_API          = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ANTHROPIC_API         = "https://api.anthropic.com/v1/messages"

# ─────────────────────────────────────────────
# POLYMARKET CLIENT SETUP (py_clob_client_v2)
# ─────────────────────────────────────────────

_clob_client = None

def get_clob_client():
    """Lazy-initialize the Polymarket CLOB client with L2 auth."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        funder = POLYMARKET_DEPOSIT_WALLET or POLYMARKET_WALLET

        # Step 1: L1 client to derive API creds
        l1_client = ClobClient(
            host=POLYMARKET_HOST,
            chain_id=137,
            key=POLYMARKET_PK,
            signature_type=0,
            funder=funder,
        )
        api_creds = l1_client.create_or_derive_api_creds()

        # Step 2: Full L2 client
        _clob_client = ClobClient(
            host=POLYMARKET_HOST,
            chain_id=137,
            key=POLYMARKET_PK,
            creds=api_creds,
            signature_type=0,
            funder=funder,
        )
        print("[POLYMARKET] CLOB client initialized (L2 auth)")
        return _clob_client
    except ImportError:
        print("[POLYMARKET] py_clob_client not installed — falling back to HTTP")
        return None
    except Exception as e:
        print(f"[POLYMARKET] Client init error: {e}")
        return None

# ─────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────

state = {
    "autopilot": False,
    "bankroll": SESSION_BANKROLL,
    "starting_bankroll": SESSION_BANKROLL,
    "open_positions": [],       # list of dicts: {player, market_id, token_id, side, size, entry_pct, order_id}
    "closed_positions": [],     # settled trades
    "banked_profit": 0.0,       # locked profit from cycling
    "cycle_pool": 0.0,          # available for cycling this round
    "consecutive_losses": 0,
    "cooldown_active": False,
    "cycling_active": True,
    "cycle_no_growth_count": 0,
    "current_round": 1,
    "last_leaderboard": {},     # {player_name: {position, score, today, thru}}
    "last_odds": {},            # {player_name: float probability}
    "all_groups_finished": False,
    "cut_players": set(),
    "suspended": False,
    "total_wins": 0,
    "total_losses": 0,
    "best_trade": None,
    "worst_trade": None,
    "scan_count_this_hour": 0,
    "scan_hour_reset": datetime.now(timezone.utc).hour,
    "market_token_map": {},     # {player_name: token_id} for CLOB orders
}

# ─────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────

def tg(message: str):
    """Send plain text message to operator Telegram."""
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10
        )
    except Exception as e:
        print(f"[TG ERROR] {e}")

def get_telegram_updates(offset: int = 0):
    try:
        r = requests.get(
            f"{TELEGRAM_API}/getUpdates",
            params={"offset": offset, "timeout": 10},
            timeout=15
        )
        return r.json().get("result", [])
    except:
        return []

# ─────────────────────────────────────────────
# SLASH GOLF
# ─────────────────────────────────────────────

def fetch_leaderboard() -> dict:
    """Pull live leaderboard from Slash Golf. Returns dict keyed by player name."""
    try:
        headers = {
            "X-RapidAPI-Key": SLASH_GOLF_KEY,
            "X-RapidAPI-Host": "live-golf-data.p.rapidapi.com"
        }
        r = requests.get(
            f"{SLASH_GOLF_HOST}/leaderboard",
            headers=headers,
            params={"orgId": "1", "tournId": TOURNAMENT_ID, "year": "2026"},
            timeout=10
        )
        data = r.json()
        leaderboard = {}
        players = data.get("leaderboard", data.get("players", []))
        for p in players:
            name = p.get("playerName", p.get("name", "Unknown"))
            leaderboard[name] = {
                "position": p.get("position", p.get("pos", 0)),
                "score": p.get("total", p.get("score", 0)),
                "today": p.get("today", 0),
                "thru": p.get("thru", "F"),
                "cut": p.get("status", "") in ["CUT", "WD", "DQ"]
            }
        state["last_leaderboard"] = leaderboard
        state["all_groups_finished"] = all(
            str(p.get("thru", "")) == "F" for p in players
        )
        return leaderboard
    except Exception as e:
        tg(f"SLASH GOLF ERROR: {e}")
        return state["last_leaderboard"]

def fetch_scorecard(player_name: str) -> str:
    """Pull hole-by-hole scorecard for a specific player."""
    try:
        headers = {
            "X-RapidAPI-Key": SLASH_GOLF_KEY,
            "X-RapidAPI-Host": "live-golf-data.p.rapidapi.com"
        }
        r = requests.get(
            f"{SLASH_GOLF_HOST}/scorecard",
            headers=headers,
            params={"orgId": "1", "tournId": TOURNAMENT_ID, "year": "2026"},
            timeout=10
        )
        return r.text[:500]
    except:
        return "Scorecard unavailable"

# ─────────────────────────────────────────────
# POLYMARKET — ODDS & ORDERS
# ─────────────────────────────────────────────

def fetch_polymarket_odds(player_names: list) -> dict:
    """
    Pull current win probabilities from Polymarket for listed players.
    Returns {player_name: probability_float}
    """
    try:
        client = get_clob_client()
        odds = {}

        if client:
            # Use CLOB client to get markets
            markets = client.get_markets()
            market_list = markets.get("data", []) if isinstance(markets, dict) else []
        else:
            # Fallback: raw HTTP
            headers = {"Authorization": f"Bearer {POLYMARKET_API_KEY}"}
            r = requests.get(
                f"{POLYMARKET_HOST}/markets",
                headers=headers,
                params={"keyword": "US Open 2026", "active": "true"},
                timeout=10
            )
            market_list = r.json().get("data", [])

        for market in market_list:
            for token in market.get("tokens", []):
                outcome = token.get("outcome", "")
                price = float(token.get("price", 0))
                token_id = token.get("token_id", "")
                for name in player_names:
                    if name.lower() in outcome.lower():
                        odds[name] = round(price * 100, 2)
                        state["market_token_map"][name] = token_id

        state["last_odds"] = odds
        return odds
    except Exception as e:
        tg(f"POLYMARKET ODDS ERROR: {e}")
        return state["last_odds"]

def place_polymarket_order(token_id: str, side: str, size_usd: float, player_name: str) -> dict:
    """
    Place a trade on Polymarket CLOB using py_clob_client.
    side: "YES" or "NO" → mapped to BUY/SELL
    Returns order result dict.
    """
    try:
        client = get_clob_client()

        if client:
            from py_clob_client.order_builder.constants import BUY, SELL
            from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions

            # Get current price to set limit
            current_pct = state["last_odds"].get(player_name, 50) / 100
            price = round(current_pct, 2)

            order_side = BUY if side == "YES" else SELL
            result = client.create_and_post_order(
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size_usd,
                    side=order_side,
                ),
                options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
            )
            return result if isinstance(result, dict) else {"orderID": str(result)}
        else:
            # Fallback: raw HTTP
            headers = {
                "Authorization": f"Bearer {POLYMARKET_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "market": token_id,
                "side": side,
                "size": str(size_usd),
                "type": "MARKET",
                "funder": POLYMARKET_DEPOSIT_WALLET or POLYMARKET_WALLET,
            }
            r = requests.post(
                f"{POLYMARKET_HOST}/order",
                headers=headers,
                json=payload,
                timeout=15
            )
            return r.json()

    except Exception as e:
        tg(f"POLYMARKET ORDER ERROR: {e}")
        return {"error": str(e)}

def exit_polymarket_position(token_id: str, size_usd: float, player_name: str) -> dict:
    """Exit (sell) an open position."""
    try:
        client = get_clob_client()

        if client:
            from py_clob_client.order_builder.constants import SELL
            from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions

            current_pct = state["last_odds"].get(player_name, 50) / 100
            price = round(current_pct, 2)

            result = client.create_and_post_order(
                OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size_usd,
                    side=SELL,
                ),
                options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
            )
            return result if isinstance(result, dict) else {"orderID": str(result)}
        else:
            headers = {
                "Authorization": f"Bearer {POLYMARKET_API_KEY}",
                "Content-Type": "application/json"
            }
            payload = {
                "market": token_id,
                "side": "SELL",
                "size": str(size_usd),
                "type": "MARKET",
                "funder": POLYMARKET_DEPOSIT_WALLET or POLYMARKET_WALLET,
            }
            r = requests.post(
                f"{POLYMARKET_HOST}/order",
                headers=headers,
                json=payload,
                timeout=15
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

Rank by edge descending. Mark top pick with star.
If no edge >8%: reply NO TRADE — WAIT
R4 rule: only signal ENTER if edge >15% and player within 4 shots of lead."""

def claude_signal_scan(leaderboard: dict, odds: dict, active_positions: list, round_num: int) -> str:
    """Send data to Claude for signal analysis. Returns signal text."""
    try:
        lb_text = "\n".join([
            f"{v['position']} | {name} | {v['score']} | {v['today']} | {v['thru']}"
            for name, v in sorted(leaderboard.items(), key=lambda x: x[1].get('position', 999))[:20]
        ])
        odds_text = "\n".join([f"{name} {pct}%" for name, pct in odds.items()])
        pos_text = "\n".join([
            f"{p['player']} YES @ {p['entry_pct']}% — now {odds.get(p['player'], '?')}%"
            for p in active_positions
        ]) or "None"

        user_msg = f"""SCAN
Round: R{round_num}
Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}

LEADERBOARD:
{lb_text}

POLYMARKET ODDS:
{odds_text}

ACTIVE POSITIONS:
{pos_text}

Run full signal scan. Top 3 edges. Flag HOLD or EXIT on active positions. Apply R{round_num} rules."""

        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        payload = {
            "model": "claude-sonnet-4-5",
            "max_tokens": 600,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_msg}]
        }
        r = requests.post(ANTHROPIC_API, headers=headers, json=payload, timeout=30)
        return r.json()["content"][0]["text"]
    except Exception as e:
        return f"CLAUDE ERROR: {e}"

# ─────────────────────────────────────────────
# PRE-FLIGHT CHECKS
# ─────────────────────────────────────────────

def run_preflight(player: str, edge: float, confidence: str, round_num: int) -> tuple:
    """Run all 8 pre-flight checks. Returns (pass: bool, reason: str)."""
    lb = state["last_leaderboard"]
    player_data = lb.get(player, {})
    position = player_data.get("position", 999)
    shots_back = position - 1  # rough approximation from position

    if round_num < 2:
        return False, "PF-01: R1 blackout"
    if round_num == 4 and edge < 15:
        return False, f"PF-01: R4 requires edge >15% (got {edge}%)"
    if edge < 8:
        return False, f"PF-02: Edge {edge}% below 8% minimum"
    if len(state["open_positions"]) >= 4:
        return False, "PF-03: Max 4 open positions reached"
    player_positions = [p for p in state["open_positions"] if p["player"] == player]
    if len(player_positions) >= 2:
        return False, f"PF-04: Already have 2 positions on {player}"
    shot_gate = 4 if round_num == 4 else 6
    if shots_back > shot_gate:
        return False, f"PF-05: {player} is {shots_back} shots back (gate: {shot_gate})"
    if state["cooldown_active"]:
        return False, "PF-06: Cooldown active — send COOLDOWN: RESET to resume"
    if state.get("data_stale", False):
        return False, "PF-07: Stale data — leaderboard >2 hours old"
    if confidence not in ["High", "Medium"]:
        return False, f"PF-08: Confidence {confidence} below minimum (need High or Medium)"
    return True, "PASS"

# ─────────────────────────────────────────────
# TRADE SIZING
# ─────────────────────────────────────────────

def calculate_size(edge: float, confidence: str) -> float:
    """Return trade size in USD based on edge and confidence."""
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
    # Check exposure cap
    current_exposure = sum(p["size"] for p in state["open_positions"])
    max_exposure = bankroll * 0.10
    if current_exposure + size > max_exposure:
        size = max(0, max_exposure - current_exposure)
    return size

# ─────────────────────────────────────────────
# EXECUTE TRADE
# ─────────────────────────────────────────────

def execute_trade(player: str, edge: float, confidence: str,
                  entry_pct: float, round_num: int, from_cycle_pool: bool = False) -> bool:
    """Full trade execution with pre-flight, sizing, API call, logging."""
    pass_check, reason = run_preflight(player, edge, confidence, round_num)
    if not pass_check:
        tg(f"BLOCKED: {player} — {reason}")
        return False

    size = calculate_size(edge, confidence)
    if from_cycle_pool:
        size = min(state["cycle_pool"], size)
    if size <= 0:
        tg(f"BLOCKED: {player} — exposure cap reached, no room for trade")
        return False

    token_id = state["market_token_map"].get(player, f"USOPEN2026_{player.replace(' ', '_').upper()}")

    tg(f"EXECUTING: {player} YES ${size} @ {entry_pct}% | Edge: {edge}% | Conf: {confidence}")
    result = place_polymarket_order(token_id, "YES", size, player)

    if "error" in result:
        tg(f"ORDER FAILED: {player} — {result['error']}")
        return False

    order_id = result.get("orderID", result.get("id", "UNKNOWN"))
    position = {
        "player": player,
        "token_id": token_id,
        "side": "YES",
        "size": size,
        "entry_pct": entry_pct,
        "order_id": order_id,
        "round_entered": round_num,
        "from_cycle": from_cycle_pool
    }
    state["open_positions"].append(position)
    if from_cycle_pool:
        state["cycle_pool"] -= size

    tg(f"FILLED: {player} YES ${size} @ {entry_pct}% | Order ID: {order_id}")
    return True

# ─────────────────────────────────────────────
# PROFIT CYCLING
# ─────────────────────────────────────────────

def run_profit_cycle(round_num: int):
    """Run between-round profit cycling logic."""
    if not state["cycling_active"]:
        tg("CYCLING: Disabled for remainder of session")
        return

    realized = sum(p.get("pnl", 0) for p in state["closed_positions"])
    if realized <= 0:
        tg("NO PROFIT TO CYCLE — STANDARD RULES APPLY")
        return

    lock_amount = round(realized * 0.5, 2)
    cycle_amount = round(realized * 0.5, 2)

    prev_banked = state["banked_profit"]
    state["banked_profit"] += lock_amount
    state["cycle_pool"] = cycle_amount

    tg(f"""--- PROFIT CYCLE ---
Round Break: R{round_num}->R{round_num+1}
Realized Profit: ${realized}
Locked (banked): ${lock_amount}
Cycle Pool: ${cycle_amount}
Running Banked Total: ${state['banked_profit']}
--------------------""")

    if state["banked_profit"] <= prev_banked:
        state["cycle_no_growth_count"] += 1
    else:
        state["cycle_no_growth_count"] = 0

    if state["cycle_no_growth_count"] >= 2:
        state["cycling_active"] = False
        tg("CYCLING PAUSED — REVERTING TO STANDARD SIZING")

# ─────────────────────────────────────────────
# FULL SCAN
# ─────────────────────────────────────────────

def run_scan(round_num: int, trigger: str = "SCHEDULED", from_cycle: bool = False):
    """Pull data, run Claude analysis, execute trades if autopilot on."""
    current_hour = datetime.now(timezone.utc).hour
    if current_hour != state["scan_hour_reset"]:
        state["scan_count_this_hour"] = 0
        state["scan_hour_reset"] = current_hour
    if trigger != "SCHEDULED":
        if state["scan_count_this_hour"] >= 3:
            tg(f"SCAN QUEUED: Rate limit (3/hr). Will run at next 20-min mark.")
            return
        state["scan_count_this_hour"] += 1

    if state["suspended"]:
        tg("BOT SUSPENDED — send AUTOPILOT: RESUME to restart")
        return

    tg(f"SCAN TRIGGERED [{trigger}] R{round_num} — {datetime.now(timezone.utc).strftime('%H:%M UTC')}")

    leaderboard = fetch_leaderboard()
    player_names = list(leaderboard.keys())
    odds = fetch_polymarket_odds(player_names)

    signal_text = claude_signal_scan(leaderboard, odds, state["open_positions"], round_num)
    tg(f"--- SIGNAL SCAN R{round_num} ---\n{signal_text}\n-----------------------")

    if not state["autopilot"]:
        return

    lines = signal_text.split("\n")
    current_player = {}
    for line in lines:
        if line.startswith("PLAYER:"):
            current_player = {"player": line.split(":", 1)[1].strip()}
        elif line.startswith("MARKET:"):
            try:
                current_player["market_pct"] = float(line.split(":", 1)[1].strip().replace("%", ""))
            except:
                pass
        elif line.startswith("FAIR VALUE:"):
            try:
                current_player["fair_value"] = float(line.split(":", 1)[1].strip().replace("%", ""))
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
            if current_player.get("signal") == "ENTER":
                player = current_player.get("player", "")
                if player and player not in state["cut_players"]:
                    edge = current_player.get("edge", 0)
                    confidence = current_player.get("confidence", "Low")
                    entry_pct = current_player.get("market_pct", 0)
                    execute_trade(player, edge, confidence, entry_pct, round_num, from_cycle)
            current_player = {}

    send_cycle_report(round_num)

# ─────────────────────────────────────────────
# MOVEMENT TRIGGERS
# ─────────────────────────────────────────────

def check_movement_triggers(round_num: int):
    """Poll for movement and fire triggers when conditions met."""
    if round_num < 2 or state["suspended"]:
        return

    new_leaderboard = fetch_leaderboard()
    new_odds = fetch_polymarket_odds(list(new_leaderboard.keys()))
    old_leaderboard = state.get("prev_leaderboard", new_leaderboard)
    old_odds = state.get("prev_odds", new_odds)

    # T1: Odds Collapse on open position
    for pos in state["open_positions"]:
        player = pos["player"]
        old_pct = old_odds.get(player, 0)
        new_pct = new_odds.get(player, old_pct)
        drop = old_pct - new_pct
        if drop >= 8:
            tg(f"""--- MOVEMENT ALERT: ODDS COLLAPSE ---
{datetime.now(timezone.utc).strftime('%H:%M UTC')}
Player: {player}
Position: YES entered @ {pos['entry_pct']}%
Current market: {new_pct}% (dropped {drop:.1f}pp)
Action: Evaluating early exit
------------------------------------""")
            run_scan(round_num, trigger="T1_ODDS_COLLAPSE")

    # T2: Mispricing — leaderboard moved, odds haven't
    for player, new_data in new_leaderboard.items():
        if player in state["cut_players"]:
            continue
        old_data = old_leaderboard.get(player, new_data)
        pos_change = old_data.get("position", 99) - new_data.get("position", 99)
        old_pct = old_odds.get(player, 0)
        new_pct = new_odds.get(player, old_pct)
        odds_move = new_pct - old_pct
        if pos_change >= 2 and odds_move <= 3:
            tg(f"""--- MOVEMENT ALERT: MISPRICING DETECTED ---
{datetime.now(timezone.utc).strftime('%H:%M UTC')}
Player: {player}
Leaderboard: pos {old_data.get('position')} -> {new_data.get('position')} (+{pos_change})
Odds lagging: {old_pct}% -> {new_pct}% (only +{odds_move:.1f}pp)
Running scan...
--------------------------------------------""")
            run_scan(round_num, trigger="T2_MISPRICING")

    # T3: Leader Separation
    sorted_lb = sorted(new_leaderboard.items(), key=lambda x: x[1].get("position", 999))
    if len(sorted_lb) >= 2:
        leader_name = sorted_lb[0][0]
        leader_pos = sorted_lb[0][1].get("score", 0)
        second_pos = sorted_lb[1][1].get("score", 0)
        gap = leader_pos - second_pos
        leader_pct = new_odds.get(leader_name, 100)
        if gap >= 3 and leader_pct < 35:
            tg(f"""--- MOVEMENT ALERT: LEADER SEPARATION ---
{datetime.now(timezone.utc).strftime('%H:%M UTC')}
Leader: {leader_name} ({gap} shots clear)
Market price: {leader_pct}% (potentially underpriced)
Running scan...
-----------------------------------------""")
            run_scan(round_num, trigger="T3_LEADER_SEP")

    # T4: Profit Target Hit
    for pos in state["open_positions"]:
        player = pos["player"]
        entry = pos["entry_pct"]
        current = new_odds.get(player, entry)
        if entry > 0 and current >= entry * 1.5:
            gain_pct = ((current - entry) / entry) * 100
            tg(f"""--- MOVEMENT ALERT: PROFIT TARGET HIT ---
{datetime.now(timezone.utc).strftime('%H:%M UTC')}
Player: {player}
Entry: {entry}% | Current: {current}% | Gain: +{gain_pct:.0f}%
Options: EXIT FULL / EXIT HALF / HOLD
Reply with EXIT HALF {player} or EXIT FULL {player}
-----------------------------------------""")

    # T5: Conditions Shift (scoring volatility)
    scores = [v.get("today", 0) for v in new_leaderboard.values() if v.get("thru") != "F"]
    old_scores = [v.get("today", 0) for v in old_leaderboard.values() if v.get("thru") != "F"]
    if scores and old_scores:
        avg_new = sum(scores) / len(scores)
        avg_old = sum(old_scores) / len(old_scores)
        if abs(avg_new - avg_old) >= 1.5:
            tg(f"""--- CONDITIONS ALERT ---
{datetime.now(timezone.utc).strftime('%H:%M UTC')}
Scoring avg shifted {avg_new - avg_old:+.1f} strokes
Possible wind/conditions change
Pausing unscheduled entries until next scheduled scan
-----------------------""")
            state["suspended"] = True
            threading.Timer(1800, lambda: state.update({"suspended": False})).start()

    state["prev_leaderboard"] = new_leaderboard
    state["prev_odds"] = new_odds

# ─────────────────────────────────────────────
# REPORTING
# ─────────────────────────────────────────────

def send_cycle_report(round_num: int):
    """Send position summary to Telegram."""
    positions_text = "\n".join([
        f"{p['player']} in @ {p['entry_pct']}% | Order: {p['order_id']}"
        for p in state["open_positions"]
    ]) or "None"
    exposure = sum(p["size"] for p in state["open_positions"])
    tg(f"""--- CYCLE REPORT ---
{datetime.now(timezone.utc).strftime('%H:%M UTC')} | R{round_num} | Bankroll: ${state['bankroll']:.2f}

OPEN ({len(state['open_positions'])}/4):
{positions_text}

EXPOSURE: ${exposure:.2f} ({(exposure/state['bankroll']*100):.1f}% of bankroll)
Banked profit: ${state['banked_profit']:.2f}
--------------------""")

def send_session_summary():
    """Send end of tournament summary."""
    total = state["total_wins"] + state["total_losses"]
    win_rate = (state["total_wins"] / total * 100) if total > 0 else 0
    pnl = state["bankroll"] - state["starting_bankroll"] + state["banked_profit"]
    tg(f"""--- SESSION FINAL REPORT ---
US Open 2026 | Shinnecock Hills
Starting Bankroll: ${state['starting_bankroll']:.2f}
Ending Bankroll: ${state['bankroll']:.2f}
Banked (locked): ${state['banked_profit']:.2f}
Total P&L: ${pnl:+.2f}
Win Rate: {win_rate:.0f}% ({state['total_wins']}W / {state['total_losses']}L)
Trades Placed: {total}
----------------------------""")

# ─────────────────────────────────────────────
# COMMAND HANDLER
# ─────────────────────────────────────────────

def handle_command(text: str):
    """Process operator Telegram commands."""
    text = text.strip()
    cmd = text.upper()

    if cmd == "AUTOPILOT: ON":
        state["autopilot"] = True
        tg("AUTOPILOT: ON — trade execution active")

    elif cmd == "AUTOPILOT: OFF":
        state["autopilot"] = False
        tg("AUTOPILOT: OFF — signal mode only")

    elif cmd == "AUTOPILOT: RESUME":
        state["autopilot"] = True
        state["suspended"] = False
        state["cooldown_active"] = False
        tg("AUTOPILOT: RESUMED — all systems active")

    elif cmd == "STATUS":
        send_cycle_report(state["current_round"])

    elif cmd == "COOLDOWN: RESET":
        state["consecutive_losses"] = 0
        state["cooldown_active"] = False
        tg("COOLDOWN: RESET — trading resumed")

    elif cmd == "CYCLE PROFIT":
        run_profit_cycle(state["current_round"])

    elif cmd == "CYCLE: OFF":
        state["cycling_active"] = False
        tg("CYCLING: Disabled for remainder of session")

    elif cmd == "SCAN":
        run_scan(state["current_round"], trigger="MANUAL")

    elif cmd.startswith("BANKROLL:"):
        try:
            amount = float(text.split(":", 1)[1].strip().replace("$", ""))
            state["bankroll"] = amount
            state["starting_bankroll"] = amount
            tg(f"BANKROLL SET: ${amount:.2f}")
        except:
            tg("BANKROLL FORMAT: BANKROLL: $300")

    elif text.upper().startswith("EXIT HALF "):
        player = text[10:].strip()
        pos = next((p for p in state["open_positions"] if p["player"].lower() == player.lower()), None)
        if pos:
            half_size = pos["size"] / 2
            result = exit_polymarket_position(pos["token_id"], half_size, player)
            tg(f"EXIT HALF: {player} — sold ${half_size:.2f} at market price")
        else:
            tg(f"No open position found for {player}")

    elif text.upper().startswith("EXIT FULL "):
        player = text[10:].strip()
        pos = next((p for p in state["open_positions"] if p["player"].lower() == player.lower()), None)
        if pos:
            result = exit_polymarket_position(pos["token_id"], pos["size"], player)
            state["open_positions"].remove(pos)
            tg(f"EXIT FULL: {player} — closed ${pos['size']:.2f} position")
        else:
            tg(f"No open position found for {player}")

    else:
        tg(f"Unknown command: {text}\nCommands: AUTOPILOT: ON/OFF/RESUME, STATUS, SCAN, BANKROLL: $X, COOLDOWN: RESET, CYCLE PROFIT, CYCLE: OFF, EXIT HALF/FULL [player]")

# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────

def get_current_round() -> int:
    """Determine current round based on date."""
    now = datetime.now(timezone.utc)
    date = now.date()
    from datetime import date as dt
    if date == dt(2026, 6, 18): return 1
    if date == dt(2026, 6, 19): return 2
    if date == dt(2026, 6, 20): return 3
    if date == dt(2026, 6, 21): return 4
    return 0

def schedule_loop():
    """Main scheduling loop — runs continuously."""
    last_r1_monitor = False
    last_r2_scan1 = False
    last_r2_scan2 = False
    last_r3_morning = False
    last_r3_scan1 = False
    last_r3_scan2 = False
    last_r3_night = False
    last_r4_morning = False
    last_r4_11 = False
    last_r4_14 = False
    last_r4_17 = False
    last_r4_18 = False
    r2_scan1_time = None
    r3_scan1_time = None

    tg("DAEMON-POLY BOT ONLINE\nUS Open 2026 | Shinnecock Hills\nSend BANKROLL: $[amount] then AUTOPILOT: ON to begin")

    while True:
        now = datetime.now(timezone.utc)
        hour = now.hour
        minute = now.minute
        round_num = get_current_round()
        state["current_round"] = round_num

        # ── THURSDAY R1 ──
        if round_num == 1:
            lb = fetch_leaderboard()
            if state["all_groups_finished"] and not last_r1_monitor:
                tg("R1 COMPLETE — Running monitor report...")
                lb_text = "\n".join([
                    f"{v['position']} | {name} | {v['score']} | {v['today']} | {v['thru']}"
                    for name, v in sorted(lb.items(), key=lambda x: x[1].get('position', 999))[:10]
                ])
                tg(f"""--- R1 MONITOR REPORT ---
{now.strftime('%H:%M UTC')}
TOP 10:
{lb_text}
NO TRADES — R1 OBSERVATION ONLY
-------------------------""")
                last_r1_monitor = True

        # ── FRIDAY R2 ──
        elif round_num == 2:
            if hour == 18 and minute < 5 and not state["bankroll"]:
                tg("BANKROLL CONFIRMATION NEEDED before R2 trading begins.\nReply: BANKROLL: $[amount]")

            lb = fetch_leaderboard()
            r1_done = state["all_groups_finished"] or last_r1_monitor

            if r1_done and state["all_groups_finished"] and not last_r2_scan1:
                tg("R2 COMPLETE — Running Scan 1...")
                run_scan(2, trigger="SCHEDULED_R2_S1")
                last_r2_scan1 = True
                r2_scan1_time = now

            if last_r2_scan1 and r2_scan1_time and not last_r2_scan2:
                elapsed = (now - r2_scan1_time).total_seconds()
                if elapsed >= 2100:
                    tg("R2 Scan 2 — running updated odds scan...")
                    run_scan(2, trigger="SCHEDULED_R2_S2")
                    if state["cycling_active"]:
                        run_profit_cycle(2)
                    last_r2_scan2 = True
                    fresh_lb = fetch_leaderboard()
                    for name, data in fresh_lb.items():
                        if data.get("cut"):
                            state["cut_players"].add(name)
                    if state["cut_players"]:
                        tg(f"CUT PLAYERS REMOVED: {', '.join(state['cut_players'])}")

            check_movement_triggers(2)

        # ── SATURDAY R3 ──
        elif round_num == 3:
            if hour == 12 and minute < 5 and not last_r3_morning:
                tg("R3 PRE-ROUND CHECK — reviewing overnight positions...")
                send_cycle_report(3)
                last_r3_morning = True

            lb = fetch_leaderboard()
            if state["all_groups_finished"] and not last_r3_scan1:
                tg("R3 COMPLETE — Running Scan 1...")
                run_scan(3, trigger="SCHEDULED_R3_S1")
                last_r3_scan1 = True
                r3_scan1_time = now

            if last_r3_scan1 and not last_r3_scan2:
                try:
                    elapsed = (now - r3_scan1_time).total_seconds()
                    if elapsed >= 2100:
                        tg("R3 Scan 2 — running updated odds scan...")
                        run_scan(3, trigger="SCHEDULED_R3_S2")
                        if state["cycling_active"]:
                            run_profit_cycle(3)
                        last_r3_scan2 = True
                except:
                    pass

            if hour == 1 and minute < 5 and last_r3_scan2 and not last_r3_night:
                tg("SATURDAY NIGHT R4 PREVIEW — checking positions against R4 rules...")
                lb = fetch_leaderboard()
                sorted_lb = sorted(lb.items(), key=lambda x: x[1].get("position", 999))
                leader_score = sorted_lb[0][1].get("score", 0) if sorted_lb else 0
                exited = []
                for pos in state["open_positions"][:]:
                    player_data = lb.get(pos["player"], {})
                    player_score = player_data.get("score", 0)
                    shots_back = player_score - leader_score
                    if shots_back > 4:
                        exit_polymarket_position(pos["token_id"], pos["size"], pos["player"])
                        state["open_positions"].remove(pos)
                        exited.append(pos["player"])
                tg(f"""--- R4 PREVIEW ---
Positions exited (>4 shots back): {', '.join(exited) or 'None'}
Remaining open: {len(state['open_positions'])}/4
Banked: ${state['banked_profit']:.2f}
R4 Rules: 15% edge min, within 4 shots only
------------------""")
                last_r3_night = True

            check_movement_triggers(3)

        # ── SUNDAY R4 ──
        elif round_num == 4:
            if hour == 12 and minute < 5 and not last_r4_morning:
                tg("R4 PRE-ROUND CHECK — R4 rules active (15% edge min, 4-shot gate)")
                send_cycle_report(4)
                last_r4_morning = True

            if hour == 15 and minute < 5 and not last_r4_11:
                run_scan(4, trigger="SCHEDULED_R4_11AM")
                last_r4_11 = True
            if hour == 18 and minute < 5 and not last_r4_14:
                run_scan(4, trigger="SCHEDULED_R4_2PM")
                last_r4_14 = True
            if hour == 21 and minute < 5 and not last_r4_17:
                run_scan(4, trigger="SCHEDULED_R4_5PM")
                last_r4_17 = True
            if hour == 22 and minute < 5 and not last_r4_18:
                run_scan(4, trigger="SCHEDULED_R4_6PM_FINAL")
                last_r4_18 = True

            if hour == 23 and minute < 5 and last_r4_18:
                send_session_summary()

            check_movement_triggers(4)

        time.sleep(600)  # Poll every 10 minutes

# ─────────────────────────────────────────────
# TELEGRAM LISTENER
# ─────────────────────────────────────────────

def telegram_listener():
    """Listen for operator commands via Telegram long polling."""
    offset = 0
    tg("TELEGRAM LISTENER ACTIVE — waiting for commands")
    while True:
        try:
            updates = get_telegram_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = msg.get("chat", {}).get("id", "")
                if str(chat_id) == str(TELEGRAM_CHAT_ID) and text:
                    handle_command(text)
        except Exception as e:
            print(f"[LISTENER ERROR] {e}")
        time.sleep(3)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 50)
    print("DAEMON-POLY | US Open 2026 | Shinnecock Hills")
    print("=" * 50)
    print(f"Chat ID : {TELEGRAM_CHAT_ID}")
    print(f"Bankroll: ${SESSION_BANKROLL}")
    print(f"Autopilot: OFF (send AUTOPILOT: ON via Telegram)")
    print("Initializing Polymarket client...")
    get_clob_client()  # Warm up client on startup
    print("Launching scheduler and Telegram listener...")

    listener_thread = threading.Thread(target=telegram_listener, daemon=True)
    listener_thread.start()

    schedule_loop()
