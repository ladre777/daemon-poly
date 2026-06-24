---
name: daemon-poly-replit
description: "Deploy DÆMON-POLY prediction market trading agent on Replit. Wires ESPN (live match data), Claude API (signal intelligence), Polymarket API (market odds + execution), and Telegram (operator alerts) into a continuous polling loop. Use when building or updating the DÆMON-POLY agent for any live sports event — World Cup, golf majors, or other Polymarket/Kalshi events. API keys assumed already set in Replit Secrets."
platform: Replit (Python 3.11+)
version: 2.0 — World Cup 2026 Edition
---

# DÆMON-POLY // REPLIT DEPLOYMENT SKILL
## Prediction Market Trading Agent — ESPN + Claude + Polymarket + Telegram

---

## ENVIRONMENT ASSUMPTIONS

All four API credentials are already stored in **Replit Secrets** (not hardcoded).

| Secret Key | What It Is |
|---|---|
| `CLAUDE_API_KEY` | Anthropic API key — claude-sonnet-4-6 |
| `POLYMARKET_API_KEY` | Polymarket CLOB API key (for market reads + orders) |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your personal chat ID or private channel ID |

ESPN Public API requires **no key** — it's free with no auth.

To confirm secrets are live in Replit: open the padlock icon in the left sidebar. Each secret above must be present before running any file.

---

## PROJECT STRUCTURE

Create these files in the Replit project root:

```
daemon-poly/
├── main.py              ← Entry point. Run this.
├── scout.py             ← ESPN data layer
├── signal_engine.py     ← Claude intelligence layer
├── market_reader.py     ← Polymarket odds reader
├── executor.py          ← Polymarket order execution
├── telegram_ops.py      ← Telegram alert sender
├── gates.py             ← Pre-flight discipline gates
├── state.json           ← Runtime state (auto-created)
├── trade_log.csv        ← All signals + trades (auto-created)
└── requirements.txt     ← Dependencies
```

---

## STEP 1 — requirements.txt

Create `requirements.txt` with exactly this content:

```
anthropic>=0.28.0
requests>=2.31.0
python-telegram-bot>=21.0
schedule>=1.2.0
pytz>=2024.1
```

In the Replit Shell, run:
```bash
pip install -r requirements.txt
```

---

## STEP 2 — scout.py (ESPN Live Data Layer)

This polls the ESPN Public API for live World Cup match data. No API key needed.

```python
# scout.py
import requests
import json
from datetime import datetime

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"

def get_live_scoreboard() -> dict:
    """
    Returns live + upcoming World Cup matches from ESPN.
    Includes: scores, match status, clock, teams.
    """
    try:
        resp = requests.get(f"{ESPN_BASE}/scoreboard", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        events = data.get("events", [])
        
        matches = []
        for event in events:
            status = event.get("status", {})
            competitors = event.get("competitions", [{}])[0].get("competitors", [])
            
            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})
            
            match = {
                "id": event.get("id"),
                "name": event.get("name"),
                "date": event.get("date"),
                "status_type": status.get("type", {}).get("name", "unknown"),
                "status_detail": status.get("type", {}).get("detail", ""),
                "clock": status.get("displayClock", "0:00"),
                "period": status.get("period", 0),
                "home_team": home.get("team", {}).get("displayName", ""),
                "home_score": home.get("score", "0"),
                "home_record": home.get("records", [{}])[0].get("summary", ""),
                "away_team": away.get("team", {}).get("displayName", ""),
                "away_score": away.get("score", "0"),
                "away_record": away.get("records", [{}])[0].get("summary", ""),
                "venue": event.get("competitions", [{}])[0].get("venue", {}).get("fullName", ""),
            }
            matches.append(match)
        
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "match_count": len(matches),
            "matches": matches,
        }
    except Exception as e:
        return {"error": str(e), "matches": []}


def get_match_detail(event_id: str) -> dict:
    """
    Returns detailed stats for a specific match: xG, possession, shots.
    Used during hydration break in-play edge window.
    """
    try:
        resp = requests.get(
            f"{ESPN_BASE}/summary",
            params={"event": event_id},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
        
        boxscore = data.get("boxscore", {})
        teams = boxscore.get("teams", [])
        
        stats = {}
        for team in teams:
            name = team.get("team", {}).get("displayName", "unknown")
            team_stats = {}
            for stat in team.get("statistics", []):
                team_stats[stat.get("name", "")] = stat.get("displayValue", "")
            stats[name] = team_stats
        
        return {
            "event_id": event_id,
            "stats": stats,
            "plays": data.get("plays", [])[-5:],  # last 5 plays
        }
    except Exception as e:
        return {"error": str(e), "stats": {}}


def get_live_matches() -> list:
    """Returns only currently in-progress matches."""
    board = get_live_scoreboard()
    return [
        m for m in board.get("matches", [])
        if m.get("status_type") in ("STATUS_IN_PROGRESS", "STATUS_HALFTIME")
    ]


def is_hydration_break_window(match: dict) -> bool:
    """
    Returns True if the match clock is in a hydration break window:
    ~25-30 min (first half) or ~70-75 min (second half).
    These are the in-play Edge 3 trade windows.
    """
    clock = match.get("clock", "0:00")
    try:
        parts = clock.split(":")
        minutes = int(parts[0])
        period = match.get("period", 1)
        
        if period == 1 and 24 <= minutes <= 31:
            return True
        if period == 2 and 69 <= minutes <= 76:
            return True
    except Exception:
        pass
    return False
```

---

## STEP 3 — market_reader.py (Polymarket Odds Layer)

Reads live World Cup market odds from Polymarket's public gamma API.

```python
# market_reader.py
import requests
import os
import json
from typing import Optional

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

HEADERS = {
    "Authorization": f"Bearer {os.environ.get('POLYMARKET_API_KEY', '')}",
    "Content-Type": "application/json",
}

# Core World Cup market slugs — update as tournament progresses
WORLD_CUP_MARKETS = {
    "winner": "world-cup-winner",
    "golden_boot": "world-cup-golden-boot-winner",
    "continental": "world-cup-continent-winner-2026",
    "messi_ronaldo": "messi-vs-ronaldo-world-cup-contributions",
}

# Stage of Elimination market slugs (add per team as needed)
STAGE_ELIMINATION_TEAMS = [
    "france", "argentina", "spain", "england",
    "brazil", "germany", "portugal", "netherlands",
    "norway", "usa", "morocco", "colombia",
]


def get_market_by_slug(slug: str) -> Optional[dict]:
    """Fetch a single Polymarket market by its slug."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and len(data) > 0:
            return data[0]
        return None
    except Exception as e:
        return {"error": str(e)}


def get_winner_odds() -> dict:
    """
    Returns current World Cup Winner odds for top contenders.
    Parses the multi-outcome market into a clean team → probability dict.
    """
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"slug": WORLD_CUP_MARKETS["winner"], "limit": 1},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()
        
        if not markets:
            return {}
        
        market = markets[0]
        tokens = market.get("tokens", [])
        
        odds = {}
        for token in tokens:
            outcome = token.get("outcome", "")
            price = float(token.get("price", 0))
            odds[outcome] = round(price * 100, 1)
        
        # Sort by probability descending
        return dict(sorted(odds.items(), key=lambda x: x[1], reverse=True))
    except Exception as e:
        return {"error": str(e)}


def get_stage_elimination_odds(team: str) -> dict:
    """
    Returns Stage of Elimination odds for a specific team.
    Maps each round (Group Stage, R32, R16, QF, SF, Champion) to its probability.
    """
    slug = f"world-cup-{team.lower().replace(' ', '-')}-stage-of-elimination"
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"slug": slug},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()
        
        if not markets:
            return {"error": f"No market found for {team}"}
        
        market = markets[0]
        tokens = market.get("tokens", [])
        
        rounds = {}
        for token in tokens:
            outcome = token.get("outcome", "")
            price = float(token.get("price", 0))
            rounds[outcome] = round(price * 100, 1)
        
        # Sanity check: sum should be ~100
        total = sum(rounds.values())
        rounds["_sum_check"] = round(total, 1)
        
        return rounds
    except Exception as e:
        return {"error": str(e)}


def get_golden_boot_odds() -> dict:
    """Returns current Golden Boot market odds."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"slug": WORLD_CUP_MARKETS["golden_boot"]},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()
        if not markets:
            return {}
        
        market = markets[0]
        tokens = market.get("tokens", [])
        odds = {}
        for token in tokens:
            odds[token.get("outcome", "")] = round(float(token.get("price", 0)) * 100, 1)
        
        return dict(sorted(odds.items(), key=lambda x: x[1], reverse=True)[:10])
    except Exception as e:
        return {"error": str(e)}


def scan_stage_elimination_ladders() -> dict:
    """
    Scans all tracked teams' Stage of Elimination markets.
    Returns: teams where the sum of round probabilities differs from 100% by >5%
    — these are the ladder arbitrage candidates (Edge 4).
    """
    anomalies = {}
    for team in STAGE_ELIMINATION_TEAMS:
        odds = get_stage_elimination_odds(team)
        if "error" in odds:
            continue
        total = odds.get("_sum_check", 100)
        if abs(total - 100) > 5:
            anomalies[team] = {
                "rounds": {k: v for k, v in odds.items() if not k.startswith("_")},
                "sum": total,
                "deviation": round(total - 100, 1),
            }
    return anomalies


def search_markets(query: str, limit: int = 10) -> list:
    """General market search by keyword."""
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"search": query, "limit": limit, "active": "true"},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return [{"error": str(e)}]
```

---

## STEP 4 — signal_engine.py (Claude Intelligence Layer)

The brain of DÆMON-POLY. Feeds live match data + market odds to claude-sonnet-4-6 and returns a structured trade signal.

```python
# signal_engine.py
import anthropic
import os
import json
from typing import Optional

client = anthropic.Anthropic(api_key=os.environ["CLAUDE_API_KEY"])
MODEL = "claude-sonnet-4-6"

SYSTEM_PROMPT = """You are SIGNAL, the intelligence core of DÆMON-POLY — a prediction market trading agent.

Your job is to analyze live World Cup match data and Polymarket odds, then output ONE of:
- A structured TRADE SIGNAL (when a real edge exists)
- A MONITOR signal (when conditions are developing but not actionable yet)
- NO_SIGNAL (when no edge is present — this is most common)

You are evaluating five specific edges:

EDGE 1 — ELIMINATION CASCADE REPRICE
When a favored team is eliminated, the surviving team absorbs less probability than math justifies.
Signal when: Top-5 team eliminated + their survivor is still priced below what the updated bracket math implies.
Wait at least 2 hours after elimination before signaling (PF-WC-01).

EDGE 2 — BRACKET REVELATION WINDOW
The bracket is fixed by venue/date before teams are confirmed.
Signal when: A team's bracket path is now confirmed but their Stage of Elimination market still prices an old opponent.

EDGE 3 — HYDRATION BREAK IN-PLAY
Mandatory 3-minute breaks occur ~25-30 min and ~70-75 min per half.
Signal when: Match clock is in that window + score differential hasn't fully moved the 90-min market.
Match winner markets resolve at REGULATION (90 min), NOT extra time or penalties.
Only signal if lead is ≥1 goal and market probability hasn't moved at least 15 percentage points.
Max hold time: 20 minutes. Cap: 5% bankroll.

EDGE 4 — STAGE OF ELIMINATION LADDER ARBITRAGE
Sum of all Stage of Elimination outcomes should equal ~100%.
Signal when: The sum deviates by more than 5% — buy the underpriced round outcomes.
Also signal when Kalshi and Polymarket show ≥5% spread on same team advancement.

EDGE 5 — STAR PLAYER EXIT CASCADE
When Messi or Mbappe's team is eliminated, their Golden Boot probability collapses.
Signal when: Team just eliminated + remaining scorer probability hasn't redistributed correctly.

PRE-FLIGHT GATE RULES (hard rules — never violate):
- Max 8% bankroll: Winner market cascade
- Max 6% bankroll: Bracket edge
- Max 5% bankroll: In-play (Edge 3), player props (Edge 5), ladder arb (Edge 4)
- Never add to a losing position
- Max 3 concurrent Polymarket positions
- No new Winner market entries for teams priced above 25% (QF or later)
- Max 5 trades per tournament phase (R32, R16, QF, SF)

OUTPUT FORMAT — return ONLY valid JSON, no other text:

For a trade signal:
{
  "signal_type": "TRADE",
  "edge": "CASCADE | BRACKET | IN_PLAY | LADDER | PROP",
  "market": "<exact Polymarket market name>",
  "direction": "YES | NO",
  "outcome": "<which outcome — e.g. 'France', 'Champion', 'Round of 16'>",
  "entry_price_pct": <number — current market price you're entering at>,
  "target_exit_pct": <number — price to exit at>,
  "rationale": "<1-2 sentence edge explanation>",
  "size_pct_bankroll": <number — percent of bankroll, must respect gate caps>,
  "expires": "<when opportunity closes — e.g. '2 hours', 'end of match', 'June 27'>",
  "confidence": "HIGH | MEDIUM | SPECULATIVE",
  "gate_check": "PASS | FAIL",
  "gate_notes": "<any gate flags>"
}

For a monitor signal:
{
  "signal_type": "MONITOR",
  "watch": "<what to watch>",
  "trigger": "<what event would create a trade signal>",
  "next_check": "<when to re-evaluate>"
}

For no signal:
{
  "signal_type": "NO_SIGNAL",
  "reason": "<brief reason>"
}"""


def run_signal(
    live_matches: list,
    winner_odds: dict,
    golden_boot_odds: dict,
    ladder_anomalies: dict,
    additional_context: str = "",
) -> dict:
    """
    Main signal generation function.
    Feeds all live data to Claude and returns a structured signal dict.
    """
    
    # Build the data payload for Claude
    data_summary = f"""
=== LIVE MATCH DATA ===
{json.dumps(live_matches, indent=2)}

=== WORLD CUP WINNER ODDS (Current Polymarket) ===
{json.dumps(winner_odds, indent=2)}

=== GOLDEN BOOT ODDS (Top 10) ===
{json.dumps(golden_boot_odds, indent=2)}

=== STAGE OF ELIMINATION LADDER ANOMALIES ===
(Teams where sum of round probabilities deviates >5% from 100%)
{json.dumps(ladder_anomalies, indent=2)}

=== ADDITIONAL CONTEXT ===
{additional_context}

Today: June 24, 2026. Tournament phase: Group Stage (final matchday). 
Knockout starts June 28. Final is July 19 at MetLife Stadium.
"""

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Analyze the following World Cup data and output a signal:\n\n{data_summary}"
                }
            ]
        )
        
        raw = message.content[0].text.strip()
        
        # Parse JSON response
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        
        return json.loads(raw)
    
    except json.JSONDecodeError as e:
        return {
            "signal_type": "ERROR",
            "reason": f"Claude returned non-JSON: {str(e)}",
            "raw": raw if 'raw' in locals() else "no response"
        }
    except Exception as e:
        return {
            "signal_type": "ERROR",
            "reason": str(e)
        }


def run_in_play_signal(match: dict, match_detail: dict, current_odds: dict) -> dict:
    """
    Specialized signal for Edge 3 (hydration break in-play).
    Faster and more focused than the full signal run.
    """
    prompt = f"""
HYDRATION BREAK IN-PLAY ANALYSIS

Match: {match['home_team']} {match['home_score']} - {match['away_score']} {match['away_team']}
Clock: {match['clock']} | Period: {match['period']}
Venue: {match['venue']}

Match Stats:
{json.dumps(match_detail.get('stats', {}), indent=2)}

Current 90-min match winner market odds:
{json.dumps(current_odds, indent=2)}

RULE: Match winner markets settle at 90-min regulation only. Extra time and penalties do NOT count.
RULE: Only signal if team leads by ≥1 goal AND market hasn't moved ≥15pp from pre-match.
RULE: Max hold is 20 minutes. This is a 5% bankroll position at most.

Should I trade? Output JSON signal.
"""
    
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        return {"signal_type": "ERROR", "reason": str(e)}
```

---

## STEP 5 — telegram_ops.py (Operator Alert Layer)

Sends formatted signals to your Telegram.

```python
# telegram_ops.py
import os
import requests
from datetime import datetime

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

EDGE_EMOJI = {
    "CASCADE": "🌊",
    "BRACKET": "🗂",
    "IN_PLAY": "⚡",
    "LADDER": "📊",
    "PROP": "🎯",
}

CONFIDENCE_EMOJI = {
    "HIGH": "🔴",
    "MEDIUM": "🟡",
    "SPECULATIVE": "⚪",
}


def send_message(text: str, parse_mode: str = "HTML") -> dict:
    """Send a raw message to Telegram."""
    try:
        resp = requests.post(
            f"{BASE_URL}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
            },
            timeout=10,
        )
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def send_trade_signal(signal: dict) -> dict:
    """
    Formats and sends a TRADE signal to Telegram.
    Matches the DÆMON-POLY Telegram signal format.
    """
    edge = signal.get("edge", "UNKNOWN")
    edge_emoji = EDGE_EMOJI.get(edge, "🔵")
    conf = signal.get("confidence", "MEDIUM")
    conf_emoji = CONFIDENCE_EMOJI.get(conf, "⚪")
    gate = signal.get("gate_check", "UNKNOWN")
    gate_icon = "✅" if gate == "PASS" else "⚠️"
    
    text = f"""
{conf_emoji} <b>DÆMON-POLY SIGNAL</b>
──────────────────────
<b>TYPE:</b> {edge_emoji} {edge}
<b>MARKET:</b> {signal.get('market', 'N/A')}
<b>DIRECTION:</b> {signal.get('direction', 'N/A')} — {signal.get('outcome', '')}
<b>ENTRY:</b> {signal.get('entry_price_pct', '?')}¢
<b>TARGET EXIT:</b> {signal.get('target_exit_pct', '?')}¢
<b>EDGE:</b> {signal.get('rationale', 'N/A')}
<b>GATE CHECK:</b> {gate_icon} {gate}
<b>SIZE:</b> {signal.get('size_pct_bankroll', '?')}% bankroll
<b>CONFIDENCE:</b> {conf}
<b>EXPIRES:</b> {signal.get('expires', 'N/A')}
──────────────────────
⚡ <i>ACTION REQUIRED: APPROVE / SKIP</i>
<code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</code>
""".strip()
    
    return send_message(text)


def send_monitor_signal(signal: dict) -> dict:
    """Sends a MONITOR (watch) signal to Telegram."""
    text = f"""
🟡 <b>DÆMON-POLY MONITOR</b>
──────────────────────
<b>WATCHING:</b> {signal.get('watch', 'N/A')}
<b>TRIGGER:</b> {signal.get('trigger', 'N/A')}
<b>NEXT CHECK:</b> {signal.get('next_check', 'N/A')}
──────────────────────
<code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</code>
""".strip()
    
    return send_message(text)


def send_status(message: str) -> dict:
    """Send a plain status/heartbeat message."""
    return send_message(f"🤖 DÆMON-POLY\n{message}")


def send_error(error: str) -> dict:
    """Send an error alert."""
    return send_message(f"🔴 DÆMON-POLY ERROR\n{error}")
```

---

## STEP 6 — gates.py (Pre-Flight Discipline Layer)

Enforces all PF gates before any signal is forwarded to Telegram.

```python
# gates.py
import json
import os
from datetime import datetime, timezone
from pathlib import Path

STATE_FILE = "state.json"

DEFAULT_STATE = {
    "active_positions": [],          # list of active position dicts
    "trades_this_phase": 0,          # resets each tournament phase
    "current_phase": "GROUP_STAGE",  # GROUP_STAGE, R32, R16, QF, SF, FINAL
    "last_signal_time": None,
    "phase_trade_counts": {
        "GROUP_STAGE": 0,
        "R32": 0,
        "R16": 0,
        "QF": 0,
        "SF": 0,
        "FINAL": 0,
    },
    "total_bankroll_deployed_pct": 0.0,
}

# Gate caps
CAPS = {
    "CASCADE": 8,
    "BRACKET": 6,
    "IN_PLAY": 5,
    "LADDER": 5,
    "PROP": 5,
}

MAX_CONCURRENT = 3
MAX_TRADES_PER_PHASE = 5
MAX_WINNER_ENTRY_PCT = 25  # don't enter Winner market above this in QF+


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return DEFAULT_STATE.copy()


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_gates(signal: dict) -> tuple[bool, list]:
    """
    Runs all pre-flight gates against a TRADE signal.
    Returns (passed: bool, violations: list[str])
    """
    state = load_state()
    violations = []
    
    edge = signal.get("edge", "")
    size = float(signal.get("size_pct_bankroll", 0))
    entry_price = float(signal.get("entry_price_pct", 0))
    phase = state.get("current_phase", "GROUP_STAGE")
    
    # PF-01: Size cap by edge type
    cap = CAPS.get(edge, 5)
    if size > cap:
        violations.append(f"PF-01: Size {size}% exceeds {edge} cap of {cap}%")
    
    # PF-03: Max concurrent positions
    active = len(state.get("active_positions", []))
    if active >= MAX_CONCURRENT:
        violations.append(f"PF-03: Already {active} active positions (max {MAX_CONCURRENT})")
    
    # PF-08: Winner market price ceiling in QF+
    late_phases = ("QF", "SF", "FINAL")
    if phase in late_phases and "winner" in signal.get("market", "").lower():
        if entry_price > MAX_WINNER_ENTRY_PCT:
            violations.append(
                f"PF-08: Winner market entry at {entry_price}% exceeds {MAX_WINNER_ENTRY_PCT}% ceiling in {phase}"
            )
    
    # PF-10: Max trades per phase
    phase_count = state.get("phase_trade_counts", {}).get(phase, 0)
    if phase_count >= MAX_TRADES_PER_PHASE:
        violations.append(
            f"PF-10: {phase_count} trades already in {phase} phase (max {MAX_TRADES_PER_PHASE})"
        )
    
    # Total bankroll deployed check
    total_deployed = state.get("total_bankroll_deployed_pct", 0)
    if total_deployed + size > 80:
        violations.append(
            f"BANKROLL: Adding {size}% would put total deployed at {total_deployed + size}% (max 80%)"
        )
    
    passed = len(violations) == 0
    return passed, violations


def record_signal_sent(signal: dict):
    """Call this after a signal is forwarded to Telegram."""
    state = load_state()
    state["last_signal_time"] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def record_trade_opened(signal: dict, approved: bool = False):
    """Call this when operator approves and trade is placed."""
    if not approved:
        return
    state = load_state()
    phase = state.get("current_phase", "GROUP_STAGE")
    
    position = {
        "market": signal.get("market"),
        "direction": signal.get("direction"),
        "outcome": signal.get("outcome"),
        "entry_price": signal.get("entry_price_pct"),
        "target_exit": signal.get("target_exit_pct"),
        "size_pct": signal.get("size_pct_bankroll"),
        "edge": signal.get("edge"),
        "opened_at": datetime.now(timezone.utc).isoformat(),
    }
    
    state["active_positions"].append(position)
    state["phase_trade_counts"][phase] = state["phase_trade_counts"].get(phase, 0) + 1
    state["total_bankroll_deployed_pct"] = (
        state.get("total_bankroll_deployed_pct", 0) + float(signal.get("size_pct_bankroll", 0))
    )
    save_state(state)


def update_phase(new_phase: str):
    """Call when tournament advances to next phase."""
    state = load_state()
    state["current_phase"] = new_phase
    save_state(state)
    return state


def get_state_summary() -> str:
    state = load_state()
    return (
        f"Phase: {state['current_phase']} | "
        f"Active positions: {len(state['active_positions'])} | "
        f"Deployed: {state['total_bankroll_deployed_pct']}% | "
        f"Phase trades: {state['phase_trade_counts'].get(state['current_phase'], 0)}/{MAX_TRADES_PER_PHASE}"
    )
```

---

## STEP 7 — executor.py (Polymarket Order Execution)

Handles actual trade placement via Polymarket CLOB API.
**Only called after operator approves a signal via Telegram.**

```python
# executor.py
import os
import requests
import json
import csv
from datetime import datetime

CLOB_API = "https://clob.polymarket.com"

HEADERS = {
    "Authorization": f"Bearer {os.environ.get('POLYMARKET_API_KEY', '')}",
    "Content-Type": "application/json",
}

TRADE_LOG = "trade_log.csv"

FIELDNAMES = [
    "timestamp", "signal_type", "edge", "market", "direction",
    "outcome", "entry_price_pct", "target_exit_pct", "size_pct",
    "confidence", "rationale", "gate_check", "executed", "execution_id"
]


def log_signal(signal: dict, executed: bool = False, execution_id: str = ""):
    """Append every signal (executed or not) to trade_log.csv."""
    write_header = not os.path.exists(TRADE_LOG)
    with open(TRADE_LOG, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if write_header:
            writer.writeheader()
        writer.writerow({
            "timestamp": datetime.utcnow().isoformat(),
            "signal_type": signal.get("signal_type", ""),
            "edge": signal.get("edge", ""),
            "market": signal.get("market", ""),
            "direction": signal.get("direction", ""),
            "outcome": signal.get("outcome", ""),
            "entry_price_pct": signal.get("entry_price_pct", ""),
            "target_exit_pct": signal.get("target_exit_pct", ""),
            "size_pct": signal.get("size_pct_bankroll", ""),
            "confidence": signal.get("confidence", ""),
            "rationale": signal.get("rationale", ""),
            "gate_check": signal.get("gate_check", ""),
            "executed": executed,
            "execution_id": execution_id,
        })


def get_order_book(token_id: str) -> dict:
    """
    Fetches the current order book for a Polymarket token.
    token_id is the specific outcome token (e.g., 'France' in the Winner market).
    """
    try:
        resp = requests.get(
            f"{CLOB_API}/book",
            params={"token_id": token_id},
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": str(e)}


def place_market_order(token_id: str, side: str, amount_usdc: float) -> dict:
    """
    Places a market order on Polymarket CLOB.
    
    token_id: The specific outcome token ID
    side: "BUY" or "SELL"
    amount_usdc: Dollar amount to trade (USDC)
    
    NOTE: Polymarket CLOB uses USDC on Polygon. Ensure wallet is funded.
    NOTE: For NY compliance, use Kalshi for regulated execution.
    """
    try:
        order_payload = {
            "order": {
                "tokenID": token_id,
                "side": side,
                "type": "MARKET",
                "amount": str(amount_usdc),
            }
        }
        
        resp = requests.post(
            f"{CLOB_API}/order",
            json=order_payload,
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        return result
    except Exception as e:
        return {"error": str(e)}


def dry_run_signal(signal: dict) -> str:
    """
    Simulates execution without placing a real order.
    Use this to verify everything is wired correctly before going live.
    """
    log_signal(signal, executed=False, execution_id="DRY_RUN")
    return f"DRY RUN: Would {signal.get('direction')} {signal.get('outcome')} at {signal.get('entry_price_pct')}¢"
```

---

## STEP 8 — main.py (Entry Point — Run This)

The orchestration loop. Polls ESPN, pulls odds, runs SIGNAL, sends to Telegram.

```python
# main.py
import schedule
import time
import os
from datetime import datetime, timezone

from scout import get_live_scoreboard, get_live_matches, get_match_detail, is_hydration_break_window
from market_reader import get_winner_odds, get_golden_boot_odds, scan_stage_elimination_ladders, get_market_by_slug
from signal_engine import run_signal, run_in_play_signal
from telegram_ops import send_trade_signal, send_monitor_signal, send_status, send_error
from gates import check_gates, record_signal_sent, get_state_summary
from executor import log_signal, dry_run_signal

# ── CONFIG ──────────────────────────────────────────────
DRY_RUN = True          # Set to False when ready to execute real trades
POLL_INTERVAL_MIN = 2   # How often to run the main loop (minutes)
IN_PLAY_INTERVAL_SEC = 45  # How often to check in-play during live matches
# ─────────────────────────────────────────────────────────


def run_main_analysis():
    """Main analysis loop — runs every POLL_INTERVAL_MIN minutes."""
    print(f"\n[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] Running main analysis...")
    
    try:
        # 1. Get live match data
        board = get_live_scoreboard()
        live_matches = get_live_matches()
        
        # 2. Get current market odds
        winner_odds = get_winner_odds()
        golden_boot_odds = get_golden_boot_odds()
        ladder_anomalies = scan_stage_elimination_ladders()
        
        # 3. Build context summary
        context = f"Live matches in progress: {len(live_matches)}"
        if ladder_anomalies:
            context += f"\nLadder anomalies detected for: {', '.join(ladder_anomalies.keys())}"
        context += f"\nAgent state: {get_state_summary()}"
        
        # 4. Run signal engine
        signal = run_signal(
            live_matches=live_matches,
            winner_odds=winner_odds,
            golden_boot_odds=golden_boot_odds,
            ladder_anomalies=ladder_anomalies,
            additional_context=context,
        )
        
        print(f"  Signal: {signal.get('signal_type')} | Edge: {signal.get('edge', 'N/A')}")
        
        # 5. Handle signal output
        if signal.get("signal_type") == "TRADE":
            passed, violations = check_gates(signal)
            signal["gate_check"] = "PASS" if passed else "FAIL"
            signal["gate_notes"] = "; ".join(violations) if violations else "All gates passed"
            
            log_signal(signal, executed=False)
            
            if passed:
                send_trade_signal(signal)
                record_signal_sent(signal)
                
                if DRY_RUN:
                    result = dry_run_signal(signal)
                    print(f"  DRY RUN: {result}")
            else:
                print(f"  GATE FAIL: {signal['gate_notes']}")
                # Still log gate failures — useful for analysis
                send_error(f"Signal blocked by gates:\n{signal['gate_notes']}\nMarket: {signal.get('market')}")
        
        elif signal.get("signal_type") == "MONITOR":
            send_monitor_signal(signal)
            log_signal(signal)
        
        elif signal.get("signal_type") == "NO_SIGNAL":
            print(f"  No edge: {signal.get('reason')}")
        
        else:
            print(f"  Unknown/error signal: {signal}")
    
    except Exception as e:
        error_msg = f"Main loop error: {str(e)}"
        print(f"  ERROR: {error_msg}")
        send_error(error_msg)


def run_in_play_check():
    """
    Fast in-play check — runs more frequently during live matches.
    Specifically targets Edge 3 (hydration break) windows.
    """
    live_matches = get_live_matches()
    
    for match in live_matches:
        if is_hydration_break_window(match):
            print(f"  ⚡ HYDRATION BREAK: {match['home_team']} vs {match['away_team']} @ {match['clock']}")
            
            # Get detailed match stats
            detail = get_match_detail(match["id"])
            
            # Get current match-specific market odds
            match_slug = f"{match['home_team'].lower().replace(' ', '-')}-vs-{match['away_team'].lower().replace(' ', '-')}"
            current_odds = get_market_by_slug(match_slug) or {}
            
            signal = run_in_play_signal(match, detail, current_odds)
            
            if signal.get("signal_type") == "TRADE":
                passed, violations = check_gates(signal)
                signal["gate_check"] = "PASS" if passed else "FAIL"
                signal["gate_notes"] = "; ".join(violations) if violations else ""
                
                log_signal(signal)
                if passed:
                    send_trade_signal(signal)
                    record_signal_sent(signal)


def send_heartbeat():
    """Daily status update to Telegram."""
    summary = get_state_summary()
    send_status(
        f"⚙️ HEARTBEAT\n{summary}\n"
        f"Mode: {'DRY RUN' if DRY_RUN else '🔴 LIVE'}\n"
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )


def main():
    print("=" * 50)
    print("DÆMON-POLY // World Cup 2026 Agent")
    print(f"Mode: {'DRY RUN ⚪' if DRY_RUN else 'LIVE 🔴'}")
    print(f"Poll interval: {POLL_INTERVAL_MIN} min")
    print("=" * 50)
    
    # Confirm secrets loaded
    required_secrets = ["CLAUDE_API_KEY", "POLYMARKET_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    missing = [s for s in required_secrets if not os.environ.get(s)]
    if missing:
        print(f"FATAL: Missing Replit Secrets: {missing}")
        return
    
    # Startup message
    send_status(
        f"🟢 Agent online\n"
        f"Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}\n"
        f"Poll: every {POLL_INTERVAL_MIN}min\n"
        f"World Cup Group Stage finals day"
    )
    
    # Schedule main analysis loop
    schedule.every(POLL_INTERVAL_MIN).minutes.do(run_main_analysis)
    
    # Schedule in-play checks (more frequent)
    schedule.every(IN_PLAY_INTERVAL_SEC).seconds.do(run_in_play_check)
    
    # Daily heartbeat
    schedule.every().day.at("12:00").do(send_heartbeat)
    
    # Run immediately on startup
    run_main_analysis()
    
    # Keep alive loop
    while True:
        schedule.run_pending()
        time.sleep(10)


if __name__ == "__main__":
    main()
```

---

## STEP 9 — REPLIT KEEP-ALIVE (Free Tier)

Replit free tier sleeps after inactivity. Two options:

### Option A: Add a status endpoint (add to main.py)
```python
# Add at top of main.py imports:
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

class StatusHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"DAEMON-POLY ONLINE")
    def log_message(self, *args):
        pass  # suppress HTTP logs

def start_status_server():
    server = HTTPServer(("0.0.0.0", 8080), StatusHandler)
    server.serve_forever()

# In main(), before the schedule loop:
Thread(target=start_status_server, daemon=True).start()
```

Then use **UptimeRobot** (free) to ping your Replit URL every 5 minutes.
Your Replit URL format: `https://[replit-project-name].[username].repl.co`

### Option B: Replit Always On (paid)
Enable in Replit settings under "Always On" — requires Core plan.

---

## STEP 10 — FIRST RUN CHECKLIST

Before running `main.py`, verify each item:

```
□ All 4 secrets in Replit Secrets panel (padlock icon)
□ requirements.txt installed (pip install -r requirements.txt in Shell)
□ DRY_RUN = True in main.py
□ Telegram bot created via @BotFather
□ TELEGRAM_CHAT_ID confirmed (message @userinfobot to get your ID)
□ Test ESPN API: python -c "from scout import get_live_scoreboard; import json; print(json.dumps(get_live_scoreboard(), indent=2))"
□ Test Telegram: python -c "from telegram_ops import send_status; send_status('Test ping')"
□ Test Polymarket: python -c "from market_reader import get_winner_odds; import json; print(json.dumps(get_winner_odds(), indent=2))"
□ Test Claude: python -c "from signal_engine import run_signal; print(run_signal([], {}, {}, {}))"
□ Run main.py — confirm heartbeat arrives in Telegram
□ Monitor for first NO_SIGNAL cycles (expected — most cycles produce no edge)
□ When first TRADE signal arrives, review it manually before flipping DRY_RUN = False
```

---

## TROUBLESHOOTING

| Problem | Fix |
|---|---|
| `KeyError: CLAUDE_API_KEY` | Secret not in Replit Secrets. Check padlock panel. |
| ESPN returns empty matches | No matches today. Expected between tournament rounds. |
| `JSONDecodeError` from signal_engine | Claude returned markdown instead of JSON. Check SYSTEM_PROMPT — it should already handle this. |
| Polymarket returns 401 | API key issue. Confirm POLYMARKET_API_KEY is correct and not expired. |
| Telegram bot not responding | Make sure you started a chat with the bot first. Message it `/start` in Telegram. |
| `get_stage_elimination_odds` returns no market | Market slug format varies. Run `search_markets("france elimination")` to find correct slug. |
| Agent sleeps after 30 min (free tier) | Add status endpoint + UptimeRobot (Step 9). |

---

## TOURNAMENT PHASE UPDATES

When the tournament advances, update the phase in gates.py:

```python
# After group stage ends (June 27):
from gates import update_phase
update_phase("R32")

# After Round of 32 (July 3):
update_phase("R16")

# After Round of 16 (July 7):
update_phase("QF")

# After QF (July 10):
update_phase("SF")

# Final:
update_phase("FINAL")
```

This resets per-phase trade counts and adjusts gate caps automatically.

---

## KEY DATES — WORLD CUP 2026

| Event | Date | Agent Action |
|---|---|---|
| Group Stage ends | June 27 | Run bracket scan; update_phase("R32") |
| Round of 32 begins | June 28 | Cascade + in-play monitoring active |
| Round of 32 ends | July 3 | update_phase("R16") |
| Round of 16 | July 5-7 | Ladder arb + Golden Boot priority |
| Quarterfinals | July 9-10 | update_phase("QF"); no new Winner entries >25% |
| Semifinals | July 14-15 | update_phase("SF") |
| Third-place play | July 18 | — |
| **FINAL** | **July 19** | Peak volume. Hold positions. In-play only. |
| The Open Championship | July 13 | Begin golf module deployment alongside WC QF |

---

*DÆMON-POLY Replit Skill v2.0 — World Cup 2026*
*Stack: Python 3.11 | Replit | ESPN Public API | claude-sonnet-4-6 | Polymarket CLOB | Telegram Bot API*
*Operator: FLSD Staffing LLC / AXIOM Stack*
```
