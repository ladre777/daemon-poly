"""Prompt-shaping regressions for the Maker's cache-aware Kimi path."""
from __future__ import annotations

from workers.maker import bounded_evidence_card, stable_system_prompt


def test_evidence_card_keeps_provenance_and_target_fact_when_bounded():
    evidence = (
        "SOURCE: NWS official station KNYC.\n"
        + "broad forecast detail\n" * 50
        + "TARGET FACT: market player/city/fixture match confirmed."
    )

    compact = bounded_evidence_card(evidence, 240)

    assert len(compact) <= 240
    assert compact.startswith("SOURCE: NWS official station KNYC.")
    assert "TARGET FACT: market player/city/fixture match confirmed." in compact
    assert "middle of evidence card compacted" in compact


def test_stable_system_prefix_contains_playbook_before_live_market_input():
    prompt = stable_system_prompt("Use NWS station evidence as a calibrated prior.")

    assert "Decision protocol:" in prompt
    assert "Lessons from past trades" in prompt
    assert "NWS station evidence" in prompt
    assert "LIVE EVIDENCE CARD" not in prompt
