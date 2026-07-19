"""SIGNAL (maker) + CHECKER (Haiku verifier), generalized to multi-sport.

SIGNAL calls use a Kimi K2 client (OpenAI-compatible, OPENAI_API_KEY) via
Moonshot's API — free-tier / very cheap, handles the high-frequency polling.
CHECKER stays on Claude Haiku (ANTHROPIC_API_KEY) — cheapest Claude, only
fires on rare TRADE signals, still fails CLOSED on any error.
"""

import anthropic
import openai
import os
import json

# ── SIGNAL client: Kimi K2 via Moonshot (OpenAI-compatible) ─────────────────
signal_client = openai.OpenAI(
    api_key=os.environ.get("OPENAI_API_KEY", ""),
    base_url=os.environ.get("OPENAI_BASE_URL", "https://api.moonshot.cn/v1"),
)
SIGNAL_MODEL = os.environ.get("SIGNAL_MODEL", "kimi-k2")

# ── CHECKER client: Claude Haiku (Anthropic) — safety gate only ──────────────
checker_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
CHECKER_MODEL  = "claude-haiku-4-5"

# Legacy aliases so learning.py and any other importer still works
client = signal_client
MODEL  = SIGNAL_MODEL

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
    """Extract the JSON object from a model reply. Robust to code fences and
    to models that prepend analysis prose before the JSON (sonnet-4-6 does
    this even when told to output only JSON)."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Fall back: scan for the first balanced {...} block that parses.
    start = raw.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(raw)):
            ch = raw[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start:i + 1])
                    except json.JSONDecodeError:
                        break
        start = raw.find("{", start + 1)
    raise json.JSONDecodeError("no JSON object found in model reply", raw, 0)


def _compact_futures(futures_odds: dict, top_n: int = 30) -> dict:
    """Sort each market's outcomes by implied_pct DESC and keep top_n, so a
    char-truncated prompt never hides the favorites (a 75-outcome golf catalog
    once cut the tournament leader out of the model's view)."""
    out = {}
    for market, outcomes in futures_odds.items():
        if isinstance(outcomes, dict) and outcomes and all(
            isinstance(v, dict) for v in outcomes.values()
        ):
            ranked = sorted(
                outcomes.items(),
                key=lambda kv: kv[1].get("implied_pct", 0) or 0,
                reverse=True,
            )[:top_n]
            out[market] = dict(ranked)
        else:
            out[market] = outcomes
    return out


def run_signal(sport_cfg: dict, matches: list, futures_odds: dict, extra_context: str = "") -> dict:
    data_summary = f"""
SPORT: {sport_cfg.get('label')} {sport_cfg.get('emoji', '')}

EDGES FOR THIS SPORT:
{sport_cfg.get('edges', '')}

SETTLEMENT: {sport_cfg.get('settle_note', '')}

=== GAMES (ESPN — live & scheduled) ===
{json.dumps(matches, indent=2)[:6000]}

=== POLYMARKET US FUTURES CATALOG (auto-executable — market -> outcome -> {{implied_pct, slug, tick}}) ===
{json.dumps(_compact_futures(futures_odds), indent=1)[:9000]}

=== CONTEXT ===
{extra_context}
"""
    raw = ""
    try:
        response = signal_client.chat.completions.create(
            model=SIGNAL_MODEL,
            max_tokens=3000,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Analyze the following {sport_cfg.get('label')} data and output a signal:\n\n"
                        f"{data_summary}\n\n"
                        "IMPORTANT: Your reply MUST end with the JSON signal object. "
                        "Keep any analysis before it brief."
                    ),
                },
            ],
        )
        raw    = response.choices[0].message.content.strip()
        signal = _parse_json_response(raw)
        signal.setdefault("sport", sport_cfg.get("label"))
        return signal
    except json.JSONDecodeError as e:
        return {"signal_type": "ERROR", "reason": f"Kimi returned non-JSON: {e}", "raw": raw}
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
    """Claude Haiku verification of a TRADE signal. Fails CLOSED to REJECTED
    on any error so a broken checker never lets a trade through unverified."""
    try:
        message = checker_client.messages.create(
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
        response = signal_client.chat.completions.create(
            model=SIGNAL_MODEL,
            max_tokens=1500,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        signal = _parse_json_response(response.choices[0].message.content.strip())
        signal.setdefault("sport", sport_cfg.get("label"))
        return signal
    except Exception as e:
        return {"signal_type": "ERROR", "reason": str(e)}
