"""Self-improvement layer: loss lessons + per-edge performance stats.

Two mechanisms, both fed back into the SIGNAL prompt every cycle:

1. LOSS LESSONS — when a position closes negative, Claude analyzes the losing
   signal and produces one root cause + one new RULE. Rules accumulate in a
   markdown file that lives next to STATE_FILE (the Railway /data volume), so
   lessons survive restarts. The most recent rules are injected into the
   signal context each cycle.

2. EDGE STATS — every close updates a per-edge win/loss/PnL tally in state.
   The summary is injected into the signal context so SIGNAL can see which
   edge types are actually working and self-weight accordingly.

Everything here is fail-safe: a learning failure must never break trading, so
all entry points swallow exceptions after printing them.
"""

import json
import os
from datetime import datetime, timezone

from gates import STATE_FILE, STATE_LOCK, load_state, save_state

# Lessons persist next to the state file so they live on the Railway volume.
LESSONS_FILE = os.environ.get(
    "LESSONS_FILE",
    os.path.join(os.path.dirname(STATE_FILE) or ".", "wc_skill_lessons.md"),
)

MAX_LESSON_CHARS = 2400   # tail of lessons injected into the prompt
MAX_RULES_KEPT   = 40     # hard cap so the file can't grow unbounded


def log_loss_and_learn(position: dict, pnl_pct: float):
    """Analyze a losing closed position and append one learned RULE.

    Called from the CLOSE flow when pnl_pct < 0. Returns the lesson dict or
    None. Never raises."""
    if pnl_pct >= 0:
        return None
    try:
        from signal_engine import client, MODEL
        prompt = (
            "DÆMON-POLY closed a LOSING prediction-market position.\n"
            f"PnL: {pnl_pct}%\n"
            f"Position: {json.dumps(position, indent=2)}\n\n"
            "Give the single most likely structural root cause (one sentence), and ONE "
            "new preventive rule starting with 'RULE:'. The rule must be concrete and "
            "checkable before entry (price level, timing window, market structure), "
            "not a platitude.\n"
            'Output ONLY JSON: {"root_cause": "<sentence>", "new_rule": "RULE: <rule>"}'
        )
        msg = client.messages.create(
            model=MODEL, max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        lesson = json.loads(raw)
        rule = lesson.get("new_rule", "")
        if not rule:
            return None
        entry = (
            f"\n## LESSON — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"Sport: {position.get('sport', '?')} | Edge: {position.get('edge', '?')} | "
            f"Loss: {pnl_pct}%\n"
            f"Market: {position.get('market', '?')} / {position.get('outcome', '?')}\n"
            f"Root cause: {lesson.get('root_cause', '?')}\n"
            f"**{rule}**\n"
        )
        _append_lesson(entry)
        return lesson
    except Exception as e:
        print(f"[LEARN] loss analysis failed (non-fatal): {e}")
        return None


def _append_lesson(entry: str):
    try:
        existing = ""
        if os.path.exists(LESSONS_FILE):
            with open(LESSONS_FILE) as f:
                existing = f.read()
        blocks = [b for b in existing.split("\n## ") if b.strip()]
        # Keep only the newest MAX_RULES_KEPT-1 blocks, then add the new one.
        if len(blocks) >= MAX_RULES_KEPT:
            blocks = blocks[-(MAX_RULES_KEPT - 1):]
            existing = "\n## ".join([""] + blocks) if blocks else ""
        with open(LESSONS_FILE, "w") as f:
            f.write(existing.rstrip() + "\n" + entry)
    except Exception as e:
        print(f"[LEARN] could not write lesson (non-fatal): {e}")


def load_lessons(max_chars: int = MAX_LESSON_CHARS) -> str:
    """Tail of the learned-rules file for prompt injection ('' if none)."""
    try:
        if not os.path.exists(LESSONS_FILE):
            return ""
        with open(LESSONS_FILE) as f:
            text = f.read().strip()
        return text[-max_chars:] if text else ""
    except Exception:
        return ""


def record_edge_result(edge: str, sport: str, pnl_pct: float):
    """Update per-edge W/L/PnL tallies in state. Never raises."""
    try:
        if not edge:
            edge = "UNKNOWN"
        # Runs on a background thread — hold the state lock across the whole
        # read-modify-write so we never clobber concurrent position updates.
        with STATE_LOCK:
            state = load_state()
            stats = state.setdefault("edge_stats", {})
            s = stats.setdefault(edge, {"wins": 0, "losses": 0, "total_pnl_pct": 0.0, "by_sport": {}})
            if pnl_pct >= 0:
                s["wins"] += 1
            else:
                s["losses"] += 1
            s["total_pnl_pct"] = round(s.get("total_pnl_pct", 0.0) + pnl_pct, 2)
            if sport:
                sp = s["by_sport"].setdefault(sport, {"wins": 0, "losses": 0})
                sp["wins" if pnl_pct >= 0 else "losses"] += 1
            save_state(state)
    except Exception as e:
        print(f"[LEARN] edge stat update failed (non-fatal): {e}")


def edge_stats_summary() -> str:
    """One-line-per-edge performance summary for prompt injection ('' if empty)."""
    try:
        stats = load_state().get("edge_stats", {})
        if not stats:
            return ""
        lines = []
        for edge, s in sorted(stats.items()):
            n = s.get("wins", 0) + s.get("losses", 0)
            if n == 0:
                continue
            wr = 100.0 * s.get("wins", 0) / n
            lines.append(
                f"{edge}: {s.get('wins', 0)}W-{s.get('losses', 0)}L "
                f"({wr:.0f}% win) net {s.get('total_pnl_pct', 0.0):+.1f}%"
            )
        return "\n".join(lines)
    except Exception:
        return ""


def learning_context() -> str:
    """Combined lessons + edge stats block for the signal prompt ('' if nothing yet)."""
    parts = []
    stats = edge_stats_summary()
    if stats:
        parts.append("=== EDGE PERFORMANCE (actual results — weight edges accordingly) ===\n" + stats)
    lessons = load_lessons()
    if lessons:
        parts.append("=== LEARNED RULES (from past losses — treat as hard constraints) ===\n" + lessons)
    return "\n\n".join(parts)
