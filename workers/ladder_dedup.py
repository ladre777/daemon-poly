"""
One question, asked once — capping Checker calls across a strike ladder.

Why this exists
---------------
A strike ladder is many markets on one ``event_ticker``, and the quant path
prices every strike on it from a *single* model: one sigma, one spot, one
horizon. The strikes therefore are not independent opinions — they are one
opinion, evaluated at several points. Sending all of them to the Checker asks
a second model the same question over and over and pays for every repeat.

Measured over 501 rows / 15 passes of production data:

* 23.2 Checker calls per pass, across 6.8 ladders per pass
* the distribution is bimodal: 75% of ladders are 1-2 strikes, but 14 deep
  ladders — 14% of them — generate 55% of all Checker calls
* collapsing every ladder to one call would save 70.7%; keeping the best
  three saves 45.4% while touching only 16% of ladders

Keeping three rather than one is deliberate. The strikes on a ladder are not
interchangeable: the Checker's judgement on a near-the-money strike is not
transferable to a deep out-of-the-money one, and a single survivor would make
the pass's whole exposure to an event hinge on one verdict.

What this is not
----------------
This is **quota headroom and signal quality, not a cost measure.** The dollar
saving is roughly $3/day, and no setting of the cap fits a Gemini free tier
at 500 calls/day. The reason to do it is that the Checker stops spending its
budget re-answering a question it has already answered, and the pass stops
being dominated by whichever event happened to have the deepest ladder.

Selection is by ``|edge|`` descending, so what survives is the strike where
the model and the market disagree most — the one actually worth a second
opinion. Ties break on arrival order, so a pass is reproducible.

``CoherenceGate`` runs upstream of this and already removes ~30.5% of
proposals, including whole ladders whose model output contradicts itself.
This cap applies to what survives that.
"""
from __future__ import annotations

import logging
from collections import defaultdict

log = logging.getLogger("daemon_kalshi.ladder_dedup")


def group_key(proposal) -> tuple[str, str]:
    """The unit a duplicate is measured within.

    Direction is part of the key because "YES above 84.49" and "NO above
    84.49" are genuinely different trades against the same model output, and
    collapsing them together would let one side crowd out the other.
    """
    c = proposal.candidate
    return (c.event_ticker or c.ticker, proposal.direction)


def select_for_checker(proposals: list, cap: int) -> tuple[list, int]:
    """Keep at most ``cap`` proposals per (event, direction), best edge first.

    Returns ``(kept, dropped_count)``. ``kept`` preserves the caller's
    original ordering — the priority sort upstream still decides what is
    looked at first; this only removes.

    A ``cap`` of zero or less disables the cap and keeps everything, so the
    behaviour can be turned off from configuration without a code change.
    """
    if cap <= 0 or not proposals:
        return list(proposals), 0

    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, proposal in enumerate(proposals):
        groups[group_key(proposal)].append(index)

    keep: set[int] = set()
    for key, indices in groups.items():
        if len(indices) <= cap:
            keep.update(indices)
            continue
        # Largest |edge| first; arrival order breaks ties so the choice is
        # deterministic and a pass can be replayed from the ledger.
        ranked = sorted(
            indices, key=lambda i: (-abs(proposals[i].edge_size), i)
        )
        keep.update(ranked[:cap])
        log.info(
            "Ladder cap on %s|%s: %d proposals -> %d (kept |edge| >= %.2f%%)",
            key[0], key[1], len(indices), cap,
            abs(proposals[ranked[cap - 1]].edge_size) * 100,
        )

    kept = [p for i, p in enumerate(proposals) if i in keep]
    return kept, len(proposals) - len(kept)
