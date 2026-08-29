"""Ticker shape helpers.

Kalshi tickers are ``SERIES-EVENT-STRIKE``. Only the pieces that more than
one caller needs live here, so that the split rule has exactly one
definition rather than one per reader.
"""
from __future__ import annotations


def family_of(ticker: str) -> str:
    """The series/family code for a market ticker.

    ``KXBTCD-26AUG2817-T80499.99`` -> ``KXBTCD``. A ticker with no hyphen is
    already a family. An empty or missing ticker is reported as such rather
    than as an empty-string family, because a bucket with no name silently
    merges with anything else that lost its ticker.
    """
    if not ticker:
        return "(no ticker)"
    return ticker.split("-", 1)[0].strip().upper() or "(no ticker)"
