"""
Reflect: the mechanism that actually changes future behavior, not just
measures past behavior. Brier score and PF-09 tell you a category is
underperforming; they don't tell Maker *why*, and they can't rewrite its
own prompt. This worker periodically hands Claude a batch of settled trades
— reasoning included, wins and losses both — and asks for a short, honest
summary of what held up and what didn't. That summary gets saved to a
playbook file and prepended to future Maker/Checker prompts.

Be clear-eyed about what this is: prompt-level reflection, not model
training. Nothing about the underlying Kimi or Claude models changes; only
the text they're given each call does. It's a real mechanism — LLMs do
condition on this kind of context — but it's not gradient descent, and it
can drift or overfit to a small sample just like a person overreacting to a
short losing streak can. Treat the playbook as a nudge, not gospel, and
sanity-check it occasionally rather than letting it compound unsupervised.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import anthropic

from core.llm_client import first_text_block

from config import CONFIG
from memory.edge_store import EdgeStore

log = logging.getLogger("daemon_kalshi.reflect")

PLAYBOOK_PATH = Path(CONFIG.ledger_db_path).parent / "playbook.md"

SYSTEM_PROMPT = """You are reviewing the trading history of an autonomous \
prediction-market bot to help it improve. You'll be given a batch of settled \
trades: the market, the probability Maker stated, the reasoning it gave, \
whether Checker approved it and why, and the actual outcome. Identify \
concrete, specific patterns — not generic advice like "be more careful". \
Look for: types of reasoning that were systematically overconfident or \
underconfident, categories where the edge was consistently real vs. \
consistently illusory, and any recurring mistake in how information was \
weighted. Write 3-6 short bullet points a future version of Maker/Checker \
could actually act on. If the sample is too small or too mixed to say \
anything specific, say that plainly instead of inventing a pattern."""


class Reflector:
    def __init__(self, store: EdgeStore = None):
        self.store = store or EdgeStore()
        self._client = anthropic.Anthropic(api_key=CONFIG.models.anthropic_api_key)

    def _format_batch(self, edges: list[dict]) -> str:
        lines = []
        for e in edges:
            lines.append(
                f"- {e['ticker']} [{e['category']}/{e['source']}]: Maker said "
                f"{e['maker_probability']:.0%}, market was at {e['market_implied_probability']:.0%}. "
                f"Reasoning: {e['maker_reasoning']}. Checker: {e['checker_verdict']} "
                f"({e['checker_reasoning']}). Outcome: {e['outcome']}. PnL: {e['pnl']}."
            )
        return "\n".join(lines)

    def reflect(self, limit: int = 50) -> str | None:
        edges = [
            e for e in self.store.recent_edges(limit=limit)
            if e["settled"] and e["action_taken"] == "executed"
        ]
        if len(edges) < 10:
            log.info("Only %d settled trades so far — skipping reflection, too little to learn from", len(edges))
            return None

        resp = self._client.messages.create(
            model=CONFIG.models.checker_model,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": self._format_batch(edges)}],
        )
        playbook_text = first_text_block(resp)

        PLAYBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
        PLAYBOOK_PATH.write_text(
            f"<!-- generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} "
            f"from {len(edges)} settled trades -->\n\n{playbook_text}\n"
        )
        log.info("Playbook updated from %d settled trades", len(edges))
        return playbook_text

    @staticmethod
    def load_playbook() -> str | None:
        if PLAYBOOK_PATH.exists():
            return PLAYBOOK_PATH.read_text()
        return None
