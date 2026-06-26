"""SIGNAL (maker) + CHECKER (Opus verifier), generalized to multi-sport.

The SYSTEM_PROMPT is sport-agnostic — the per-sport edge catalogue and data are
injected per call. The CHECKER is unchanged in spirit: an independent, skeptical
Opus pass that fails CLOSED (any error => REJECTED) so a broken verifier can
never let a trade through.
"""

import anthropic
import os
import json

client        = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL         = "claude-sonnet-4-5"
CHECKER_MODEL = "claude-opus-4-6"

SYSTEM_PROMPT = """You are SIGNAL, the intelligence core of DÆMON-POLY — a multi-sport prediction-market trading agent on Polymarket.

For the single sport and the data provided, output ONE of:
- A structured TRADE SIGNAL (only when a real, STRUCTURAL edge exists)
- A MONITOR signal (conditions developing but not yet actionable)
- NO_SIGNAL (no edge present — this is the most common and most correct output)

An edge is a structural mispricing (bracket math, ladder arithmetic, a live-state lag,
a redistribution that hasn't happened yet) — NEVER a narrative hunch or a vibe. If you
cannot name the structural reason and the size of the mispricing, output NO_SIGNAL.

PRE-FLIGHT GATE RULES (hard — never violate; the system also enforces these):
- Size caps by edge: CASCADE 8%, BRACKET 6%, IN_PLAY / LADDER / PROP / FUTURES 5% of bankroll
- Never add to a losing position
- Max 3 concurrent positions
- No new Winner/Champion entries for an outcome already priced above 25% once it is a clear favorite late
- Max 5 trades per phase
- Wait >=2 hours after a major result before entering a reprice

LIVE EXECUTION (CRITICAL): Only outcomes present in the provided "POLYMARKET US
FUTURES CATALOG" can be auto-traded with real money. When you emit a TRADE on a
catalog outcome, copy that outcome's "slug" VERBATIM into the "market_slug" field
and base "entry_price_pct" on that outcome's "implied_pct". If your edge is on a
market NOT in the catalog (in-play, ladder, cascade, bracket, prop), you may still
emit the TRADE but set "market_slug" to "" — the system will send it as an alert
only and will NOT place an order. Never invent or guess a slug.

OUTPUT FORMAT — return ONLY valid JSON, no markdown, no prose.

Trade signal:
{
  "signal_type": "TRADE",
  "sport": "<sport label>",
  "edge": "CASCADE | BRACKET | IN_PLAY | LADDER | PROP | FUTURES",
  "market": "<exact Polymarket market / event name>",
  "market_slug": "<US catalog slug copied verbatim for an auto-executable futures outcome, else \"\">",
  "direction": "YES | NO",
  "outcome": "<which outcome — e.g. 'France', 'New York Yankees', 'Round of 16'>",
  "entry_price_pct": <number — current price you are entering at>,
  "target_exit_pct": <number — price to exit at>,
  "rationale": "<1-2 sentence STRUCTURAL edge explanation>",
  "size_pct_bankroll": <number — percent of bankroll, must respect the caps above>,
  "expires": "<when the opportunity closes>",
  "confidence": "HIGH | MEDIUM | SPECULATIVE",
  "gate_check": "PASS | FAIL",
  "gate_notes": "<any gate flags>"
}

Monitor signal:
{ "signal_type": "MONITOR", "sport": "<sport label>", "watch": "<what>", "trigger": "<what would create a trade>", "next_check": "<when>" }

No signal:
{ "signal_type": "NO_SIGNAL", "sport": "<sport label>", "reason": "<brief reason>" }"""


def _parse_json_response(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


def run_signal(sport_cfg: dict, matches: list, futures_odds: dict, extra_context: str = "") -> dict:
    data_summary = f"""
SPORT: {sport_cfg.get('label')} {sport_cfg.get('emoji', '')}

EDGES FOR THIS SPORT:
{sport_cfg.get('edges', '')}

SETTLEMENT: {sport_cfg.get('settle_note', '')}

=== GAMES (ESPN — live & scheduled) ===
{json.dumps(matches, indent=2)[:6000]}

=== POLYMARKET US FUTURES CATALOG (auto-executable — market -> outcome -> {{implied_pct, slug, tick}}) ===
{json.dumps(futures_odds, indent=2)[:6000]}

=== CONTEXT ===
{extra_context}
"""
    raw = ""
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{
                "role":    "user",
                "content": f"Analyze the following {sport_cfg.get('label')} data and output a signal:\n\n{data_summary}",
            }],
        )
        raw    = message.content[0].text.strip()
        signal = _parse_json_response(raw)
        signal.setdefault("sport", sport_cfg.get("label"))
        return signal
    except json.JSONDecodeError as e:
        return {"signal_type": "ERROR", "reason": f"Claude returned non-JSON: {e}", "raw": raw}
    except Exception as e:
        return {"signal_type": "ERROR", "reason": str(e)}


CHECKER_PROMPT = """You are CHECKER, the verification agent for DÆMON-POLY.
A separate agent called SIGNAL just generated a trade signal. Your only job is to reject it or approve it.

You are more skeptical than SIGNAL. You need a real, structural reason to approve.

Reject if ANY of the following are true:
- The edge rationale is vague or narrative-based rather than structural
- Entry price is within 3% of theoretical fair value (no real edge)
- The confidence is SPECULATIVE and the market is illiquid
- The signal enters a Winner/Champion market for an outcome already above 25% that is a clear favorite late
- The rationale references something that happened >4 hours ago (stale cascade)
- Gate check is FAIL

Approve only if the edge is specific, structural, and the entry price represents a genuine mispricing.

Output ONLY valid JSON, no other text:
{
  "verdict": "APPROVED" | "REJECTED",
  "reason": "<one sentence>"
}"""


def run_checker(signal: dict) -> dict:
    """Independent Opus verification of a TRADE signal. Fails CLOSED to REJECTED
    on any error so a broken checker never lets a trade through unverified."""
    try:
        message = client.messages.create(
            model=CHECKER_MODEL,
            max_tokens=256,
            system=CHECKER_PROMPT,
            messages=[{
                "role":    "user",
                "content": f"Verify this signal:\n{json.dumps(signal, indent=2)}",
            }],
        )
        result = _parse_json_response(message.content[0].text.strip())
        signal["checker_verdict"] = result.get("verdict", "REJECTED")
        signal["checker_reason"]  = result.get("reason", "No reason given")
    except Exception as e:
        signal["checker_verdict"] = "REJECTED"
        signal["checker_reason"]  = f"Checker error: {str(e)}"
    return signal


def run_in_play_signal(sport_cfg: dict, match: dict, match_detail: dict, current_odds: dict) -> dict:
    prompt = f"""IN-PLAY ANALYSIS — {sport_cfg.get('label')} {sport_cfg.get('emoji', '')}

Match: {match.get('home_team')} {match.get('home_score')} - {match.get('away_score')} {match.get('away_team')}
Clock: {match.get('clock')} | Period: {match.get('period')} | {match.get('status_detail')}
Venue: {match.get('venue')}

Match stats:
{json.dumps(match_detail.get('stats', {}), indent=2)[:3000]}

Current live market odds:
{json.dumps(current_odds, indent=2)[:2000]}

SETTLEMENT: {sport_cfg.get('settle_note', '')}
RULE: In-play is a 5% bankroll position at most. Only trade a STRUCTURAL lag between the
live game state and the market price. If there is no clear lag, output NO_SIGNAL.

Output JSON signal only.
"""
    raw = ""
    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        signal = _parse_json_response(message.content[0].text.strip())
        signal.setdefault("sport", sport_cfg.get("label"))
        return signal
    except Exception as e:
        return {"signal_type": "ERROR", "reason": str(e)}
