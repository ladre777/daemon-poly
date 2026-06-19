"""
DAEMON-POLY Trading Bot
US Open 2026 | Shinnecock Hills
Operator: LaDre Martin

Required Secrets (set in Replit Secrets panel):
  SLASH_GOLF_KEY          → RapidAPI key for live golf data
  TELEGRAM_TOKEN          → Telegram bot token from BotFather
  TELEGRAM_CHAT_ID        → Operator Telegram chat ID (default: 8486909237)
  POLYMARKET_KEY_ID       → Polymarket US API key id (UUID)
  POLYMARKET_SECRET_KEY   → Polymarket US API secret (base64 Ed25519)
  ANTHROPIC_API_KEY       → Claude API key
  SESSION_BANKROLL        → Starting bankroll in USD (e.g. 300)
"""

import os
import re
import json
import math
import time
import threading
import unicodedata
from datetime import datetime, timezone, date as dt

import requests

STATE_FILE = "daemon_state.json"

# Keys persisted to disk — excludes ephemeral runtime fields
PERSIST_KEYS = [
    "autopilot", "bankroll", "starting_bankroll",
    "open_positions", "closed_positions",
    "banked_profit", "cycle_pool",
    "consecutive_losses", "cooldown_active",
    "cycling_active", "cycle_no_growth_count",
    "current_round", "last_leaderboard", "last_odds",
    "market_token_map", "cut_players",
    "total_wins", "total_losses",
]

def save_state():
    """Persist bot state to disk. Called after every meaningful mutation."""
    try:
        snapshot = {}
        for k in PERSIST_KEYS:
            v = state.get(k)
            snapshot[k] = list(v) if isinstance(v, set) else v
        with open(STATE_FILE, "w") as f:
            json.dump(snapshot, f, indent=2)
    except Exception as e:
        print(f"[STATE] Save error: {e}")

def load_state():
    """Restore bot state from disk on startup."""
    if not os.path.exists(STATE_FILE):
        print("[STATE] No saved state found — starting fresh")
        return
    try:
        with open(STATE_FILE) as f:
            snapshot = json.load(f)
        for k in PERSIST_KEYS:
            if k not in snapshot:
                continue
            v = snapshot[k]
            if k == "cut_players":
                state[k] = set(v)
            else:
                state[k] = v
        pos_count  = len(state["open_positions"])
        closed_count = len(state["closed_positions"])
        print(f"[STATE] Restored — bankroll ${state['bankroll']:.2f} | "
              f"{pos_count} open | {closed_count} closed | "
              f"autopilot {'ON' if state['autopilot'] else 'OFF'}")
        tg(
            f"♻️ STATE RESTORED after restart\n"
            f"Bankroll:  ${state['bankroll']:.2f}\n"
            f"Open:      {pos_count} position(s)\n"
            f"Autopilot: {'ON 🟢' if state['autopilot'] else 'OFF 🔴'}\n"
            + (f"Positions: " + ", ".join(p['player'] for p in state['open_positions'])
               if pos_count else "")
        )
    except Exception as e:
        print(f"[STATE] Load error: {e} — starting fresh")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

SLASH_GOLF_KEY            = os.environ.get("SLASH_GOLF_KEY", "")
TELEGRAM_TOKEN            = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID          = os.environ.get("TELEGRAM_CHAT_ID", "8486909237")
POLYMARKET_KEY_ID         = os.environ.get("POLYMARKET_KEY_ID", "")
POLYMARKET_SECRET_KEY     = os.environ.get("POLYMARKET_SECRET_KEY", "")
ANTHROPIC_API_KEY         = os.environ.get("ANTHROPIC_API_KEY", "")
SESSION_BANKROLL          = float(os.environ.get("SESSION_BANKROLL", "300"))

TOURNAMENT_ID    = "026"
SLASH_GOLF_HOST  = "https://live-golf-data.p.rapidapi.com"
TELEGRAM_API     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
ANTHROPIC_API    = "https://api.anthropic.com/v1/messages"

VETO_WINDOW_SECS = 60   # seconds before a queued trade auto-fires

# ─────────────────────────────────────────────
# POLYMARKET US CLIENT
# ─────────────────────────────────────────────

_pm_client = None

def get_pm_client():
    """Lazy-init the Polymarket US API client (Ed25519 key auth)."""
    global _pm_client
    if _pm_client is not None:
        return _pm_client
    try:
        from polymarket_us import PolymarketUS
        _pm_client = PolymarketUS(
            key_id=POLYMARKET_KEY_ID,
            secret_key=POLYMARKET_SECRET_KEY,
        )
        print("[POLYMARKET] US client initialised (Ed25519 auth)")
        return _pm_client
    except Exception as e:
        print(f"[POLYMARKET] Client init failed: {e}")
        _pm_client = None
        return None

def get_buying_power() -> float:
    """Return current Polymarket US buying power in USD (0.0 on failure)."""
    try:
        client = get_pm_client()
        if not client:
            return 0.0
        bal = client.account.balances()
        for b in bal.get("balances", []):
            if b.get("currency", "USD") == "USD":
                return float(b.get("buyingPower", b.get("currentBalance", 0)) or 0)
    except Exception as e:
        print(f"[POLYMARKET] balance error: {e}")
    return 0.0

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
    "pending_trades":       [],   # [{player, edge, confidence, entry_pct, round, from_cycle, queued_at}]
    "veto_lock":            threading.Lock(),
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

def _parse_pos(v) -> int:
    """Parse a leaderboard position ('1', 'T5', 'CUT') into an int (999 = out)."""
    s = str(v).strip().upper().lstrip("T")
    try:
        return int(s)
    except (ValueError, TypeError):
        return 999

def _parse_score(v) -> int:
    """Parse a golf score ('-7', 'E', '+4') into an int relative to par."""
    s = str(v).strip().upper()
    if s in ("E", "EVEN", "", "-"):
        return 0
    try:
        return int(s.replace("+", ""))
    except (ValueError, TypeError):
        return 0

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
        players = data.get("leaderboardRows", data.get("leaderboard", data.get("players", [])))
        for p in players:
            first = str(p.get("firstName", "")).strip()
            last  = str(p.get("lastName", "")).strip()
            name  = (first + " " + last).strip() or p.get("playerName", p.get("name", "Unknown"))
            status = str(p.get("status", "")).upper()
            leaderboard[name] = {
                "position": _parse_pos(p.get("position", 999)),
                "score":    _parse_score(p.get("total", p.get("score", 0))),
                "today":    _parse_score(p.get("currentRoundScore", p.get("today", 0))),
                "thru":     p.get("thru", "F"),
                "cut":      status in ("CUT", "WD", "DQ", "WITHDRAWN"),
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

US_OPEN_EVENT_SLUG  = "pga-us-2026-06-21-w"
US_OPEN_SEARCH      = "us open winner"
_US_OPEN_DESC_RE    = re.compile(r"Will (.+?) win the 2026 U\.S\. Open")
_MARKET_TICK        = {}   # marketSlug -> tick size string (e.g. "0.001")

def _norm_name(n: str) -> str:
    """Normalize a player name for matching (strip accents, punctuation, case)."""
    n = unicodedata.normalize("NFKD", str(n)).encode("ascii", "ignore").decode()
    return "".join(c for c in n.lower() if c.isalnum())

def _fetch_us_open_markets() -> list:
    """
    Fetch the live 2026 U.S. Open Winner event from Polymarket US.
    Returns [{name, slug, price_pct, tick}] for each named player.
    The player name lives in each market's `description`
    ('Will <Name> win the 2026 U.S. Open...'); the YES price is the
    first entry of `outcomePrices`.
    """
    client = get_pm_client()
    if client is None:
        from polymarket_us import PolymarketUS
        client = PolymarketUS()          # search is a public endpoint
    events = client.search.query({"query": US_OPEN_SEARCH}).get("events", [])
    ev = next((e for e in events if e.get("slug") == US_OPEN_EVENT_SLUG), None)
    if not ev:
        return []
    out = []
    for m in ev.get("markets", []):
        if m.get("closed"):
            continue
        match = _US_OPEN_DESC_RE.search(m.get("description", "") or "")
        if not match:
            continue
        name = match.group(1).strip()
        try:
            yes_price = float(json.loads(m.get("outcomePrices", '["0"]'))[0])
        except (ValueError, TypeError, IndexError):
            yes_price = 0.0
        out.append({
            "name":      name,
            "slug":      m["slug"],
            "price_pct": round(yes_price * 100, 2),
            "tick":      str(m.get("orderPriceMinTickSize", "0.001")),
        })
    return out

def fetch_polymarket_odds(player_names: list) -> dict:
    """Pull current win probabilities for the live US Open market and cache market slugs."""
    try:
        markets = _fetch_us_open_markets()
        if not markets:
            return state["last_odds"]

        norm_lookup = {_norm_name(p): p for p in player_names}
        odds = {}
        for mk in markets:
            # Prefer the leaderboard's spelling of the name when it matches.
            player = norm_lookup.get(_norm_name(mk["name"]), mk["name"])
            odds[player] = mk["price_pct"]
            state["market_token_map"][player] = mk["slug"]
            _MARKET_TICK[mk["slug"]] = mk["tick"]

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

def _round_to_tick(price: float, tick: str) -> float:
    """Round a probability price (0-1) to the market's tick size, clamped to (0,1)."""
    try:
        t = float(tick)
    except (ValueError, TypeError):
        t = 0.01
    if t <= 0:
        t = 0.01
    decimals = max(0, len(tick.split(".")[1]) if "." in str(tick) else 0)
    p = round(round(price / t) * t, decimals)
    return min(max(p, t), 1 - t)

def _live_price(market_slug: str, side: str) -> float:
    """Live best ask (for buys) or best bid (for sells) as a 0-1 probability."""
    try:
        client = get_pm_client()
        if not client:
            return 0.0
        md = client.markets.bbo(market_slug)
        md = md.get("marketData", md)
        quote = md.get("bestAsk" if side == "BUY" else "bestBid") or {}
        return float(quote.get("value", 0) or 0)
    except Exception:
        return 0.0

def place_polymarket_order(market_slug: str, side: str, size_usd: float, player_name: str) -> dict:
    """Buy a YES position on Polymarket US (side='YES'). Marketable limit at the live ask."""
    try:
        client = get_pm_client()
        if not client:
            return {"error": "Polymarket US client unavailable — check POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY"}

        tick  = _MARKET_TICK.get(market_slug, "0.001")
        ask   = _live_price(market_slug, "BUY") or (state["last_odds"].get(player_name, 50) / 100)
        price = _round_to_tick(ask, tick)
        shares = int(size_usd / max(price, 0.001))
        if shares < 1:
            return {"error": f"size ${size_usd:.2f} too small at price {price}"}

        result = client.orders.create({
            "marketSlug": market_slug,
            "intent":     "ORDER_INTENT_BUY_LONG",
            "type":       "ORDER_TYPE_LIMIT",
            "price":      {"value": f"{price:.4f}", "currency": "USD"},
            "quantity":   shares,
            "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        })
        order = result.get("order", result)
        return {
            "orderID": order.get("id", ""),
            "shares":  shares,
            "price":   price,
            "raw":     result,
        }
    except Exception as e:
        tg(f"POLYMARKET ORDER ERROR: {e}")
        return {"error": str(e)}

def exit_polymarket_position(market_slug: str, size_usd: float, player_name: str) -> dict:
    """Sell (exit) an open position. Full exit uses close_position; partial sells a share slice."""
    try:
        client = get_pm_client()
        if not client:
            return {"error": "Polymarket US client unavailable"}

        pos = next((p for p in state["open_positions"]
                    if p["player"] == player_name and p.get("token_id") == market_slug), None)

        full          = True
        shares_to_sell = None
        if pos and pos.get("shares"):
            total = float(pos["shares"])
            frac  = 1.0 if pos.get("size_usd", 0) <= 0 else min(1.0, size_usd / pos["size_usd"])
            full  = frac >= 0.999
            shares_to_sell = max(1, int(round(total * frac)))

        if full or not shares_to_sell:
            result = client.orders.close_position({"marketSlug": market_slug})
        else:
            tick  = _MARKET_TICK.get(market_slug, "0.001")
            bid   = _live_price(market_slug, "SELL") or (state["last_odds"].get(player_name, 50) / 100)
            price = _round_to_tick(bid, tick)
            result = client.orders.create({
                "marketSlug": market_slug,
                "intent":     "ORDER_INTENT_SELL_LONG",
                "type":       "ORDER_TYPE_LIMIT",
                "price":      {"value": f"{price:.4f}", "currency": "USD"},
                "quantity":   shares_to_sell,
                "tif":        "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            })
        order = result.get("order", result) if isinstance(result, dict) else {}
        return {"orderID": order.get("id", ""), "raw": result}
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

def claude_morning_briefing(leaderboard: dict, odds: dict, round_num: int) -> str:
    """Ask Claude for a structured morning-of-round briefing."""
    try:
        lb_text   = "\n".join([
            f"{v['position']} | {name} | {v['score']:+d} | R{round_num-1} today: {v['today']:+d}"
            for name, v in sorted(leaderboard.items(), key=lambda x: x[1].get("position", 999))[:15]
        ])
        odds_text = "\n".join([
            f"{name}: {pct:.1f}%"
            for name, pct in sorted(odds.items(), key=lambda x: -x[1])[:15]
        ])
        user_msg = (
            f"MORNING BRIEFING | Round R{round_num} | US Open 2026 | Shinnecock Hills\n\n"
            f"TOP 15 ENTERING R{round_num}:\n{lb_text}\n\n"
            f"POLYMARKET WIN ODDS:\n{odds_text}\n\n"
            f"Give me a tight morning briefing for R{round_num} covering:\n"
            f"1. KEY STORYLINES (2-3 bullet points — who has momentum, weather, course conditions)\n"
            f"2. PLAYERS TO WATCH (top 3 names + one-line reason each)\n"
            f"3. MARKET EDGES TODAY (where Polymarket odds look mispriced vs true win probability)\n"
            f"4. RISK FLAGS (any contenders to fade or avoid)\n"
            f"5. TODAY'S EDGE THRESHOLD (recommend min edge % for R{round_num} given volatility)\n\n"
            f"Keep it sharp — 5 sections, no waffle. This goes straight to my phone."
        )
        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model":      "claude-sonnet-4-5",
            "max_tokens": 600,
            "system":     SYSTEM_PROMPT,
            "messages":   [{"role": "user", "content": user_msg}],
        }
        r = requests.post(ANTHROPIC_API, headers=headers, json=payload, timeout=30)
        return r.json()["content"][0]["text"]
    except Exception as e:
        return f"BRIEFING ERROR: {e}"


def send_morning_briefing(round_num: int):
    """Fetch leaderboard + odds and send Claude's morning briefing to Telegram."""
    tg(f"☀️ R{round_num} MORNING BRIEFING — pulling data...")
    lb   = fetch_leaderboard()
    odds = fetch_polymarket_odds(list(lb.keys())) if lb else {}
    brief = claude_morning_briefing(lb, odds, round_num)
    now_str = datetime.now(timezone.utc).strftime('%H:%M UTC')
    tg(
        f"--- R{round_num} MORNING BRIEF | {now_str} ---\n"
        f"{brief}\n"
        f"----------------------------------------\n"
        f"Bankroll: ${state['bankroll']:.2f} | "
        f"Open: {len(state['open_positions'])} | "
        f"Banked: ${state['banked_profit']:.2f}"
    )


# ─────────────────────────────────────────────
# HALFTIME
# ─────────────────────────────────────────────

def _field_half_through(lb: dict) -> bool:
    """Return True when ≥50% of active players are through hole 9 or more."""
    if not lb:
        return False
    thru_vals = []
    for d in lb.values():
        t = d.get("thru", 0)
        try:
            thru_vals.append(int(t))
        except (ValueError, TypeError):
            pass  # "F" or "-" — skip
    if not thru_vals:
        return False
    half_through = sum(1 for t in thru_vals if t >= 9)
    return half_through >= len(thru_vals) * 0.5


def claude_halftime_analysis(lb: dict, old_lb: dict, odds: dict,
                              positions: list, round_num: int) -> str:
    """Ask Claude for a mid-round analysis: movers, faders, position advice."""
    try:
        # Build sorted current leaderboard
        sorted_lb = sorted(lb.items(), key=lambda x: x[1].get("position", 999))[:15]
        lb_text = "\n".join([
            f"{v['position']} | {name} | {v['score']:+d} | Today: {v['today']:+d} | Thru: {v['thru']}"
            for name, v in sorted_lb
        ])

        # Detect movers: compare today's score vs old leaderboard
        movers = []
        for name, d in sorted_lb:
            old = old_lb.get(name, {})
            old_pos = old.get("position", 999)
            new_pos = d.get("position", 999)
            delta   = old_pos - new_pos  # positive = moved up
            if abs(delta) >= 3:
                direction = "▲" if delta > 0 else "▼"
                movers.append(f"{direction} {name}: pos {old_pos}→{new_pos} ({delta:+d})")
        movers_text = "\n".join(movers) if movers else "No major moves yet"

        # Active positions context
        pos_text = "\n".join([
            f"  {p['player']} YES @ {p['entry_pct']}% | Now {odds.get(p['player'], '?')}%"
            for p in positions
        ]) or "  None"

        user_msg = (
            f"HALFTIME ANALYSIS | R{round_num} | {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n\n"
            f"MID-ROUND LEADERBOARD:\n{lb_text}\n\n"
            f"POSITION MOVERS:\n{movers_text}\n\n"
            f"MY OPEN POSITIONS:\n{pos_text}\n\n"
            f"Provide a tight halftime report:\n"
            f"1. SURGING (top 2 players gaining ground — are they worth entering?)\n"
            f"2. COLLAPSING (who is fading — should I exit if I hold them?)\n"
            f"3. POSITION ADVICE (for each of my open positions: HOLD, EXIT, or ADD)\n"
            f"4. WATCH LIST (1-2 players not yet entered who may spike late)\n"
            f"Keep it concise — one line per point. Goes straight to my phone."
        )
        headers = {
            "x-api-key":          ANTHROPIC_API_KEY,
            "anthropic-version":  "2023-06-01",
            "content-type":       "application/json",
        }
        payload = {
            "model":    "claude-sonnet-4-5",
            "max_tokens": 500,
            "system":   SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user_msg}],
        }
        r = requests.post(ANTHROPIC_API, headers=headers, json=payload, timeout=30)
        return r.json()["content"][0]["text"]
    except Exception as e:
        return f"HALFTIME ERROR: {e}"


def send_halftime_report(round_num: int):
    """Fetch fresh data, compare to prev leaderboard, send halftime Telegram report."""
    tg(f"⏱️ R{round_num} HALFTIME — pulling mid-round data...")
    old_lb = dict(state.get("last_leaderboard", {}))
    lb     = fetch_leaderboard()
    odds   = fetch_polymarket_odds(list(lb.keys())) if lb else {}
    analysis = claude_halftime_analysis(lb, old_lb, odds, state["open_positions"], round_num)
    now_str  = datetime.now(timezone.utc).strftime('%H:%M UTC')

    # Quick stats
    in_progress = [
        (name, d) for name, d in lb.items()
        if str(d.get("thru", "")).isdigit()
    ]
    finished = [d for d in lb.values() if str(d.get("thru", "")) == "F"]

    tg(
        f"--- R{round_num} HALFTIME | {now_str} ---\n"
        f"Field: {len(finished)} finished | {len(in_progress)} in progress\n\n"
        f"{analysis}\n"
        f"------------------------------\n"
        f"Bankroll: ${state['bankroll']:.2f} | "
        f"Open: {len(state['open_positions'])} | "
        f"Banked: ${state['banked_profit']:.2f}"
    )


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
    # Use the actual order price (live ask) as the recorded entry, not the signal price.
    fill_pct = round(result["price"] * 100, 2) if result.get("price") else entry_pct

    state["open_positions"].append({
        "player":        player,
        "token_id":      token_id,
        "side":          "YES",
        "size_usd":      size,
        "shares":        shares,
        "entry_pct":     fill_pct,
        "signal_pct":    entry_pct,
        "order_id":      order_id,
        "round_entered": round_num,
        "from_cycle":    from_cycle_pool,
        "entered_at":    time.time(),
    })
    if from_cycle_pool:
        state["cycle_pool"] -= size

    state["consecutive_losses"] = 0
    tg(f"✅ FILLED: {player} YES | {shares} shares @ ${fill_pct/100:.2f} | Order: {order_id}")
    save_state()
    return True

# ─────────────────────────────────────────────
# VETO QUEUE
# ─────────────────────────────────────────────

def queue_trade(player: str, edge: float, confidence: str,
                entry_pct: float, round_num: int, from_cycle_pool: bool = False):
    """
    Queue a trade for deferred execution.
    Sends a Telegram alert with a VETO_WINDOW_SECS countdown.
    If no VETO is received the veto_worker fires the order automatically.
    """
    trade = {
        "player":       player,
        "edge":         edge,
        "confidence":   confidence,
        "entry_pct":    entry_pct,
        "round":        round_num,
        "from_cycle":   from_cycle_pool,
        "queued_at":    time.time(),
        "vetoed":       False,
    }
    with state["veto_lock"]:
        state["pending_trades"].append(trade)

    size = calculate_size(edge, confidence)
    tg(
        f"⏳ PENDING TRADE — {VETO_WINDOW_SECS}s to veto\n"
        f"Player:     {player}\n"
        f"Signal:     YES @ {entry_pct:.1f}%\n"
        f"Edge:       +{edge:.1f}% | {confidence} confidence\n"
        f"Size:       ${size:.2f}\n"
        f"Round:      R{round_num}\n"
        f"\nReply VETO {player}  or  VETO ALL  to cancel.\n"
        f"Otherwise fires in {VETO_WINDOW_SECS}s automatically."
    )


def veto_worker():
    """
    Background thread. Checks pending_trades every 3 seconds.
    Fires each trade once VETO_WINDOW_SECS has elapsed and it hasn't been vetoed.
    """
    while True:
        time.sleep(3)
        now = time.time()
        with state["veto_lock"]:
            ready = [t for t in state["pending_trades"]
                     if not t["vetoed"] and now - t["queued_at"] >= VETO_WINDOW_SECS]
            for trade in ready:
                state["pending_trades"].remove(trade)
            # Also remove any already-vetoed entries
            state["pending_trades"] = [t for t in state["pending_trades"] if not t["vetoed"]]

        for trade in ready:
            tg(f"🚀 FIRING: {trade['player']} — veto window expired, executing now...")
            execute_trade(
                trade["player"],
                trade["edge"],
                trade["confidence"],
                trade["entry_pct"],
                trade["round"],
                trade["from_cycle"],
            )

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
    save_state()

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
                    queue_trade(
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

def _estimate_true_probs(lb: dict) -> dict:
    """
    Estimate true win probability for each player using an exponential
    decay model keyed on shots back from the lead.
    k=0.28 calibrated for US Open tight scoring distributions.
    Returns {name: pct} where pct is 0-100.
    """
    if not lb:
        return {}
    sorted_lb = sorted(lb.items(), key=lambda x: x[1].get("score", 999))
    lead_score = sorted_lb[0][1].get("score", 0)
    k = 0.28
    raw = {}
    for name, d in sorted_lb:
        shots_back = max(0, d.get("score", 0) - lead_score)
        # Cut players get near-zero weight
        if d.get("cut"):
            raw[name] = 0.001
        else:
            raw[name] = math.exp(-k * shots_back)
    total = sum(raw.values())
    if not total:
        return {}
    return {name: round(v / total * 100, 2) for name, v in raw.items()}


def send_ladder():
    """
    Rank every player in the field by estimated edge vs current Polymarket odds.
    Shows full edge ladder sorted best→worst.
    """
    tg("📊 LADDER — fetching live data...")
    lb   = fetch_leaderboard()
    odds = fetch_polymarket_odds(list(lb.keys())) if lb else {}
    if not lb:
        tg("LADDER: Leaderboard unavailable")
        return

    true_probs = _estimate_true_probs(lb)
    rnd        = state["current_round"]
    now_str    = datetime.now(timezone.utc).strftime('%H:%M UTC')
    held       = {p["player"] for p in state["open_positions"]}

    has_market = any(v and v > 0 for v in odds.values())

    rows = []
    for name, true_pct in true_probs.items():
        mkt_pct = odds.get(name)
        lb_d  = lb.get(name, {})
        pos   = lb_d.get("position", 999)
        score = lb_d.get("score", 0)
        if mkt_pct is None or mkt_pct <= 0:
            # No market odds — rank by model probability instead of edge
            rows.append((None, name, true_pct, None, pos, score))
        else:
            edge = round(true_pct - mkt_pct, 1)
            rows.append((edge, name, true_pct, mkt_pct, pos, score))

    # Sort: by edge when market exists, else by model probability
    if has_market:
        rows.sort(key=lambda x: (-(x[0] if x[0] is not None else -999)))
    else:
        rows.sort(key=lambda x: -x[2])

    edge_threshold = state["min_edge_pct"]

    lines = []
    for edge, name, true_pct, mkt_pct, pos, score in rows[:25]:
        tag = ""
        if name in held:
            tag = " ✅"
        elif edge is not None and edge >= edge_threshold and pos <= 20:
            tag = " 🎯"
        score_str = f"{score:+d}" if isinstance(score, int) else str(score)
        if mkt_pct is None:
            lines.append(
                f"#{pos} {name}{tag}\n"
                f"   Est {true_pct:.1f}% | Mkt n/a (no market)\n"
                f"   Score: {score_str}"
            )
        else:
            sign = "+" if edge >= 0 else ""
            lines.append(
                f"#{pos} {name}{tag}\n"
                f"   Est {true_pct:.1f}% | Mkt {mkt_pct:.1f}% | Edge {sign}{edge:.1f}%\n"
                f"   Score: {score_str}"
            )

    if has_market:
        pos_edges = [r for r in rows if r[0] is not None and r[0] >= edge_threshold
                     and lb.get(r[1], {}).get("position", 999) <= 30]
        footer = (
            f"Min edge threshold: +{edge_threshold:.0f}%\n"
            f"🎯 = actionable  ✅ = held\n\n"
            + "\n".join(lines)
            + f"\n\nActionable: {len(pos_edges)} players above threshold"
        )
    else:
        footer = (
            f"⚠️ No Polymarket market for this tournament — "
            f"showing model win-probability ranking only.\n"
            f"✅ = held\n\n"
            + "\n".join(lines)
        )

    tg(
        f"--- EDGE LADDER | R{rnd} | {now_str} ---\n"
        + footer
        + "\n-----------------------------"
    )


def claude_targets_analysis(lb: dict, odds: dict, rows: list,
                             round_num: int) -> str:
    """
    Ask Claude to select top 3 specific buy targets with exact position sizes.
    rows = sorted (edge, name, true_pct, mkt_pct, pos, score) list from LADDER.
    """
    try:
        bankroll   = state["bankroll"]
        held       = [p["player"] for p in state["open_positions"]]
        open_slots = max(0, 4 - len(held))
        avail_cap  = round(bankroll * 0.30, 2)  # max 30% of bankroll per session

        # Top 15 edge candidates for Claude to review
        candidates = "\n".join([
            f"{name} | #{pos} | Score {score:+d} | Est {true_pct:.1f}% | Mkt {mkt_pct:.1f}% | Edge +{edge:.1f}%"
            for edge, name, true_pct, mkt_pct, pos, score in rows[:15]
            if edge > 0
        ]) or "No positive-edge candidates found"

        held_text = ", ".join(held) if held else "None"

        user_msg = (
            f"TARGETS REQUEST | R{round_num} | {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n\n"
            f"BANKROLL: ${bankroll:.2f}\n"
            f"AVAILABLE CAPITAL (30% cap): ${avail_cap:.2f}\n"
            f"OPEN SLOTS: {open_slots}/4\n"
            f"CURRENTLY HELD: {held_text}\n\n"
            f"TOP EDGE CANDIDATES:\n{candidates}\n\n"
            f"Select exactly {min(3, open_slots)} specific buy targets from the candidates above. "
            f"For each, provide:\n"
            f"TARGET: [player name]\n"
            f"ENTRY: [Polymarket % price]\n"
            f"SIZE: $[exact dollar amount — max ${avail_cap/max(1,min(3,open_slots)):.0f} per trade]\n"
            f"EDGE: [true% - market%]\n"
            f"REASON: [one tight sentence — why now, R{round_num} specific]\n\n"
            f"Rules: only top-30 leaderboard players, no players already held, "
            f"min +10% edge, total sizes must not exceed ${avail_cap:.2f}. "
            f"If fewer than {min(3, open_slots)} qualify, return only those that do. "
            f"Be decisive — this goes straight to execution."
        )
        headers = {
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        }
        payload = {
            "model":      "claude-sonnet-4-5",
            "max_tokens": 500,
            "system":     SYSTEM_PROMPT,
            "messages":   [{"role": "user", "content": user_msg}],
        }
        r = requests.post(ANTHROPIC_API, headers=headers, json=payload, timeout=30)
        return r.json()["content"][0]["text"]
    except Exception as e:
        return f"TARGETS ERROR: {e}"


def send_targets():
    """
    Pull live ladder, send to Claude for target selection, format and deliver
    specific buy recommendations with sizes to Telegram.
    """
    tg("🎯 TARGETS — building edge ladder + asking Claude for picks...")
    lb   = fetch_leaderboard()
    odds = fetch_polymarket_odds(list(lb.keys())) if lb else {}
    if not lb:
        tg("TARGETS: Leaderboard unavailable")
        return

    true_probs = _estimate_true_probs(lb)
    rnd        = state["current_round"]
    now_str    = datetime.now(timezone.utc).strftime('%H:%M UTC')
    held       = {p["player"] for p in state["open_positions"]}

    # Build rows same as LADDER
    rows = []
    for name, true_pct in true_probs.items():
        mkt_pct = odds.get(name)
        if mkt_pct is None or mkt_pct <= 0:
            continue
        if name in held:
            continue  # already holding
        if lb.get(name, {}).get("cut"):
            continue  # cut players excluded
        edge = round(true_pct - mkt_pct, 1)
        lb_d = lb.get(name, {})
        pos   = lb_d.get("position", 999)
        score = lb_d.get("score", 0)
        rows.append((edge, name, true_pct, mkt_pct, pos, score))

    rows.sort(key=lambda x: -x[0])

    open_slots = max(0, 4 - len(held))
    if open_slots == 0:
        tg("TARGETS: All 4 position slots are full. Exit a position first.")
        return

    analysis = claude_targets_analysis(lb, odds, rows, rnd)

    tg(
        f"--- TARGETS | R{rnd} | {now_str} ---\n"
        f"Bankroll: ${state['bankroll']:.2f} | Slots: {open_slots}/4\n\n"
        f"{analysis}\n"
        f"----------------------------------------\n"
        f"Send AUTOPILOT: ON to let Claude auto-execute,\n"
        f"or VETO [player] within 60s of any queued trade."
    )


def settle_positions():
    """
    Settle all open positions against the final R4 leaderboard.
    YES on the winner pays $1/share → profit.
    YES on any other player resolves $0 → full loss.
    Updates bankroll, win/loss counters, and closed_positions.
    """
    if not state["open_positions"]:
        tg("SETTLE: No open positions to settle.")
        return

    lb = fetch_leaderboard()
    if not lb:
        tg("SETTLEMENT ERROR: Cannot fetch final leaderboard — try again in a moment.")
        return

    sorted_lb = sorted(lb.items(), key=lambda x: x[1].get("position", 999))
    winner    = sorted_lb[0][0]
    winner_score = sorted_lb[0][1].get("score", 0)

    tg(
        f"🏆 FINAL RESULT: {winner} wins US Open 2026\n"
        f"Score: {winner_score:+d}\n"
        f"Settling {len(state['open_positions'])} open position(s)..."
    )

    lines = []
    for pos in state["open_positions"][:]:
        player   = pos["player"]
        size_usd = pos["size_usd"]
        entry    = pos["entry_pct"]
        shares   = pos.get("shares", _usd_to_shares(size_usd, entry))

        if player.lower() == winner.lower():
            # YES resolves to $1/share — receive full share value back
            proceeds = round(shares, 2)
            pnl      = round(proceeds - size_usd, 2)
            state["total_wins"] += 1
            result = "WIN"
            state["bankroll"] = round(state["bankroll"] + proceeds, 2)
        else:
            # YES resolves to $0 — lose stake
            pnl    = round(-size_usd, 2)
            result = "LOSS"
            state["total_losses"] += 1

        pos["pnl"]    = pnl
        pos["result"] = result
        state["closed_positions"].append(pos)
        state["open_positions"].remove(pos)

        icon = "🟢" if result == "WIN" else "🔴"
        sign = "+" if pnl >= 0 else ""
        lines.append(
            f"{icon} {player}\n"
            f"   Result: {result} | Shares: {shares:.2f}\n"
            f"   Cost: ${size_usd:.2f} | P&L: {sign}${pnl:.2f}"
        )

    total_realized = sum(p.get("pnl", 0) for p in state["closed_positions"])
    banked         = state["banked_profit"]
    grand_total    = total_realized + banked
    total_trades   = state["total_wins"] + state["total_losses"]
    win_rate       = (state["total_wins"] / total_trades * 100) if total_trades else 0

    tg(
        f"--- SETTLEMENT COMPLETE ---\n"
        + "\n".join(lines)
        + f"\n\nRealized P&L:  {'+' if total_realized>=0 else ''}${total_realized:.2f}"
        + f"\nBanked profit: ${banked:.2f}"
        + f"\nGrand total:   {'+' if grand_total>=0 else ''}${grand_total:.2f}"
        + f"\nWin rate:      {win_rate:.0f}% ({state['total_wins']}W / {state['total_losses']}L)"
        + f"\nTrades:        {total_trades}"
        + "\n---------------------------"
    )
    save_state()


def send_snapshot():
    """Full state audit snapshot sent to Telegram."""
    now      = time.time()
    now_str  = datetime.now(timezone.utc).strftime('%H:%M UTC')
    rnd      = state["current_round"]
    odds     = state["last_odds"]
    bankroll = state["bankroll"]
    start    = state["starting_bankroll"]
    banked   = state["banked_profit"]
    pool     = state["cycle_pool"]
    wins     = state["total_wins"]
    losses   = state["total_losses"]
    total_t  = wins + losses
    win_rate = (wins / total_t * 100) if total_t else 0
    ap_icon  = "🟢 ON" if state["autopilot"] else "🔴 OFF"

    # ── Header ─────────────────────────────────────
    header = (
        f"--- SNAPSHOT | R{rnd} | {now_str} ---\n"
        f"Autopilot:  {ap_icon}\n"
        f"Bankroll:   ${bankroll:.2f}  (started ${start:.2f})\n"
        f"Banked:     ${banked:.2f}\n"
        f"Cycle pool: ${pool:.2f}\n"
        f"Record:     {wins}W / {losses}L "
        + (f"({win_rate:.0f}%)" if total_t else "(no trades yet)")
    )

    # ── Open Positions ──────────────────────────────
    pos_lines = []
    total_exposure   = 0.0
    total_unrealized = 0.0

    for pos in state["open_positions"]:
        player  = pos["player"]
        entry   = pos["entry_pct"]
        size    = pos["size_usd"]
        shares  = pos.get("shares", _usd_to_shares(size, entry))
        current = odds.get(player, entry)
        cycle   = " [CYCLE]" if pos.get("from_cycle") else ""
        rnd_in  = pos.get("round_entered", "?")

        # Unrealized P&L: current share value vs cost
        cur_val  = round(shares * current / 100, 2)
        pnl_usd  = round(cur_val - size, 2)
        pnl_sign = "+" if pnl_usd >= 0 else ""
        arrow    = "▲" if pnl_usd >= 0 else "▼"

        # Position age
        entered_at = pos.get("entered_at")
        if entered_at:
            age_secs = int(now - entered_at)
            age_h    = age_secs // 3600
            age_m    = (age_secs % 3600) // 60
            age_str  = f"{age_h}h {age_m}m" if age_h else f"{age_m}m"
        else:
            age_str = "?"

        total_exposure   += size
        total_unrealized += pnl_usd

        pos_lines.append(
            f"{arrow} {player}{cycle}\n"
            f"   R{rnd_in} | Entry {entry:.1f}% → Now {current:.1f}%\n"
            f"   Shares {shares:.2f} | Cost ${size:.2f} | Age {age_str}\n"
            f"   Unreal P&L: {pnl_sign}${pnl_usd:.2f}"
        )

    # ── Closed Positions ────────────────────────────
    realized = sum(p.get("pnl", 0) for p in state["closed_positions"])
    net_pnl  = realized + total_unrealized + banked

    if state["open_positions"]:
        pos_section = f"\nOPEN ({len(state['open_positions'])}):\n" + "\n".join(pos_lines)
        pos_section += f"\nExposure: ${total_exposure:.2f} | Unreal: {'+' if total_unrealized>=0 else ''}${total_unrealized:.2f}"
    else:
        pos_section = "\nOPEN: none"

    closed_section = (
        f"\nCLOSED: {len(state['closed_positions'])} | "
        f"Realized: {'+' if realized>=0 else ''}${realized:.2f}"
    )

    footer = (
        f"\nGrand total: {'+' if net_pnl>=0 else ''}${net_pnl:.2f}"
        f"\n------------------------------"
    )

    tg(header + pos_section + closed_section + footer)


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
SNAPSHOT            — full state audit (bankroll, positions, age, P&L)
BRIEFING            — Claude morning briefing for today's round
HALFTIME            — mid-round analysis: movers, faders, position advice
LADDER              — full field ranked by edge (Est% vs Polymarket%)
TARGETS             — Claude picks top 3 buys with exact sizes right now
LEADERBOARD         — live top-15 + Polymarket odds
SCAN                — manual Claude signal scan
PENDING             — queued trades + countdown
VETO [player]       — cancel one pending trade
VETO ALL            — cancel all pending trades
SETTLE              — settle positions vs final result
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
        save_state()

    elif cmd == "AUTOPILOT: OFF":
        state["autopilot"] = False
        tg("🔴 AUTOPILOT: OFF — signal mode only")
        save_state()

    elif cmd == "AUTOPILOT: RESUME":
        state["autopilot"]       = True
        state["suspended"]       = False
        state["cooldown_active"] = False
        tg("🟢 AUTOPILOT: RESUMED — all systems active")
        save_state()

    elif cmd in ("STATUS", "POSITIONS"):
        send_cycle_report(state["current_round"])

    elif cmd == "REPORT":
        send_pnl_report()

    elif cmd == "SNAPSHOT":
        send_snapshot()

    elif cmd == "BRIEFING":
        send_morning_briefing(state["current_round"])

    elif cmd == "HALFTIME":
        send_halftime_report(state["current_round"])

    elif cmd == "LADDER":
        send_ladder()

    elif cmd == "TARGETS":
        send_targets()

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

    elif cmd == "SETTLE":
        tg("SETTLE: pulling final standings and settling all open positions...")
        settle_positions()
        send_session_summary()

    elif cmd == "HELP":
        tg(HELP_TEXT)

    elif cmd == "VETO ALL":
        with state["veto_lock"]:
            count = sum(1 for t in state["pending_trades"] if not t["vetoed"])
            for t in state["pending_trades"]:
                t["vetoed"] = True
        tg(f"🚫 VETO ALL — {count} pending trade(s) cancelled.")

    elif cmd.startswith("VETO "):
        player = text[5:].strip()
        with state["veto_lock"]:
            matches = [t for t in state["pending_trades"]
                       if not t["vetoed"] and t["player"].lower() == player.lower()]
            for t in matches:
                t["vetoed"] = True
        if matches:
            tg(f"🚫 VETOED: {matches[0]['player']} — trade cancelled.")
        else:
            tg(f"No pending trade found for: {player}")

    elif cmd == "PENDING":
        with state["veto_lock"]:
            pending = [t for t in state["pending_trades"] if not t["vetoed"]]
        if not pending:
            tg("No pending trades in queue.")
        else:
            lines = []
            for t in pending:
                secs_left = max(0, VETO_WINDOW_SECS - int(time.time() - t["queued_at"]))
                lines.append(f"• {t['player']} YES @ {t['entry_pct']:.1f}% | Edge +{t['edge']:.1f}% | fires in {secs_left}s")
            tg("⏳ PENDING TRADES:\n" + "\n".join(lines) + "\nReply VETO [player] or VETO ALL to cancel.")

    elif cmd.startswith("BANKROLL:"):
        try:
            amount = float(text.split(":", 1)[1].strip().replace("$", "").replace(",", ""))
            state["bankroll"]          = amount
            state["starting_bankroll"] = amount
            tg(f"BANKROLL SET: ${amount:.2f}")
            save_state()
        except:
            tg("Format: BANKROLL: $300")

    elif text.upper().startswith("EXIT HALF "):
        player = text[10:].strip()
        pos    = next((p for p in state["open_positions"] if p["player"].lower() == player.lower()), None)
        if pos:
            half = pos["size_usd"] / 2
            res  = exit_polymarket_position(pos["token_id"], half, player)
            if "error" in res:
                tg(f"EXIT HALF FAILED: {player} — {res['error']}")
            else:
                pos["size_usd"] -= half
                pos["shares"]    = round(pos.get("shares", 0) / 2, 2)
                tg(f"EXIT HALF: {player} — sold ${half:.2f}")
                save_state()
        else:
            tg(f"No open position for: {player}")

    elif text.upper().startswith("EXIT FULL "):
        player = text[10:].strip()
        pos    = next((p for p in state["open_positions"] if p["player"].lower() == player.lower()), None)
        if pos:
            res = exit_polymarket_position(pos["token_id"], pos["size_usd"], player)
            if "error" in res:
                tg(f"EXIT FULL FAILED: {player} — {res['error']}")
            else:
                state["open_positions"].remove(pos)
                tg(f"EXIT FULL: {player} — closed ${pos['size_usd']:.2f}")
                save_state()
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
        "r1_brief",  "r1_half",  "r1_monitor",
        "r2_brief",  "r2_half",  "r2_scan1", "r2_scan2",
        "r3_brief",  "r3_half",  "r3_morning", "r3_scan1", "r3_scan2", "r3_night",
        "r4_brief",  "r4_half",  "r4_morning", "r4_11", "r4_14", "r4_17", "r4_18",
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
            if hour == 13 and minute < 10 and not flags["r1_brief"]:
                send_morning_briefing(1)
                flags["r1_brief"] = True

            fetch_leaderboard()
            if not flags["r1_half"] and _field_half_through(state["last_leaderboard"]):
                send_halftime_report(1)
                flags["r1_half"] = True

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
            if hour == 13 and minute < 10 and not flags["r2_brief"]:
                send_morning_briefing(2)
                flags["r2_brief"] = True

            fetch_leaderboard()
            if not flags["r2_half"] and _field_half_through(state["last_leaderboard"]):
                send_halftime_report(2)
                flags["r2_half"] = True

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
            if hour == 13 and minute < 10 and not flags["r3_brief"]:
                send_morning_briefing(3)
                flags["r3_brief"] = True

            fetch_leaderboard()
            if not flags["r3_half"] and _field_half_through(state["last_leaderboard"]):
                send_halftime_report(3)
                flags["r3_half"] = True

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
                        res = exit_polymarket_position(pos["token_id"], pos["size_usd"], pos["player"])
                        if "error" in res:
                            tg(f"R4 EXIT FAILED: {pos['player']} — {res['error']}")
                            continue
                        state["open_positions"].remove(pos)
                        exited.append(pos["player"])
                tg(f"R4 PREVIEW\nExited (>4 back): {', '.join(exited) or 'None'}\n"
                   f"Remaining: {len(state['open_positions'])}/4 | Banked: ${state['banked_profit']:.2f}\n"
                   f"R4 rules: 15% edge min, 4-shot gate")
                flags["r3_night"] = True

            check_movement_triggers(3)

        # ── R4 SUNDAY ──
        elif rnd == 4:
            if hour == 13 and minute < 10 and not flags["r4_brief"]:
                send_morning_briefing(4)
                flags["r4_brief"] = True

            fetch_leaderboard()
            if not flags["r4_half"] and _field_half_through(state["last_leaderboard"]):
                send_halftime_report(4)
                flags["r4_half"] = True

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

            # Auto-settle when all R4 groups finish
            if state["all_groups_finished"] and not flags.get("r4_settled"):
                tg("R4 COMPLETE — all groups finished. Running settlement...")
                settle_positions()
                send_session_summary()
                flags["r4_settled"] = True

            if hour == 23 and minute < 10 and flags["r4_18"] and not flags.get("r4_settled"):
                send_session_summary()

            check_movement_triggers(4)

        save_state()      # heartbeat save every poll cycle
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
    print("  Connecting to Polymarket US...")
    get_pm_client()
    print("  Restoring saved state (if any)...")
    load_state()
    print("  Pre-loading leaderboard + market tokens...")
    lb = fetch_leaderboard()
    if lb:
        fetch_polymarket_odds(list(lb.keys()))
        print(f"  Leaderboard: {len(lb)} players loaded")
        print(f"  Token map  : {len(state['market_token_map'])} markets mapped")
    print()
    print("  Starting Telegram listener, veto worker + scheduler...")
    threading.Thread(target=telegram_listener, daemon=True).start()
    threading.Thread(target=veto_worker,       daemon=True).start()
    schedule_loop()
