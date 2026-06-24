import anthropic
import os
import json

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL  = "claude-sonnet-4-5"

SYSTEM_PROMPT = """You are SIGNAL, the intelligence core of DÆMON-POLY — a prediction market trading agent.

Your job is to analyze live FIFA World Cup 2026 match data and Polymarket odds, then output ONE of:
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
- Wait minimum 2 hours after major upset before entering (PF-WC-01)

OUTPUT FORMAT — return ONLY valid JSON, no markdown, no other text:

For a trade signal:
{
  "signal_type": "TRADE",
  "edge": "CASCADE | BRACKET | IN_PLAY | LADDER | PROP",
  "market": "<exact Polymarket market name>",
  "direction": "YES | NO",
  "outcome": "<which outcome — e.g. 'France', 'Champion', 'Round of 16'>",
  "entry_price_pct": <number — current market price you are entering at>,
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

Tournament: FIFA World Cup 2026. Knockout rounds June 28 – July 19 at MetLife Stadium.
Match winner markets settle at 90 min regulation ONLY (not extra time or penalties).
"""
    raw = ""
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{
                "role":    "user",
                "content": f"Analyze the following World Cup data and output a signal:\n\n{data_summary}",
            }],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except json.JSONDecodeError as e:
        return {"signal_type": "ERROR", "reason": f"Claude returned non-JSON: {e}", "raw": raw}
    except Exception as e:
        return {"signal_type": "ERROR", "reason": str(e)}


def run_in_play_signal(match: dict, match_detail: dict, current_odds: dict) -> dict:
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

Should I trade? Output JSON signal only.
"""
    raw = ""
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        return {"signal_type": "ERROR", "reason": str(e)}
