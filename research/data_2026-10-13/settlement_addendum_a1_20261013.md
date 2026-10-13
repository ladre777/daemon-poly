# Addendum A1 to the Settlement-Source Packet (Revision 4, signed 2026-10-13 14:45 EDT)

**Date:** 2026-10-13 ~14:50 EDT
**Countersigned by:** Claude (independent reviewer, Satoshi paper stack)

---

## A1.1 Tier K certification

LaDre confirmed 2026-10-13 14:46 EDT that reading Kalshi's public `expiration_value` purely as a settlement cross-check is permitted under the applicable terms.

Tier K — Kalshi `KXBTC15M` `expiration_value` as an independent settlement verifier — is **certified** as part of the settlement-source packet, effective from LaDre's 14:46 EDT terms confirmation.

**Certified figure (§6 row 5):** 491/491 Kalshi `expiration_value` vs venue settlement reference, maximum difference $0.000000, windows 2026-10-02 10:30Z to 2026-10-13 07:15Z. Plus 8 `no_markets` and 1 JSON error in the source file, stated for completeness. VERIFIED by Claude's independent recompute.

## A1.2 Regime alarm

The tier-K regime alarm is active as a **§4 void trigger**: any future nonzero diff between tier-K `expiration_value` and the venue's settlement reference for the same window voids the certification under the §4 automatic procedure. (§4 governs where §5 differs.)

## A1.3 Scope

This addendum changes only the Tier K status (from unsigned monitor to certified) and activates its regime alarm as a void trigger. No other section of Revision 4 is modified. Revision 4 as signed (with erratum E1) stands unchanged.

---

**Countersignature:**

- [x] **COUNTERSIGNED** — Tier K certified per LaDre's 2026-10-13 14:46 EDT terms confirmation, with the A1.2 void trigger.

Signed: Claude (independent reviewer, Satoshi paper stack) Date: 2026-10-13, 14:50 EDT
