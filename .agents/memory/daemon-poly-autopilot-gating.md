---
name: DAEMON-POLY autopilot gating
description: Where the autopilot OFF safety is (and isn't) enforced in bot.py, and what any new trade-entry path must do.
---

# Autopilot enforcement is per-entry-path, not centralized

`execute_trade` and `queue_trade` do **NOT** check `state["autopilot"]`. The OFF =
"signal-only, never place real orders" contract is enforced **only** at each entry
source individually:
- The Claude scan path (`run_scan`) returns early when autopilot is OFF, before any
  parsing/queueing.
- The deterministic model path (`deterministic_entry_check`) checks
  `not state["autopilot"]` at its top.

**Why:** there is no single chokepoint. `veto_worker` → `execute_trade` fires real
Polymarket orders unconditionally once a trade is queued. So any code that can reach
`queue_trade` is a potential OFF-bypass.

**How to apply:** when adding ANY new way to enter a position (new trigger, new
command, new scheduled scan), gate it on `state["autopilot"]` at the entry point.
Do not assume a downstream guard will catch it.

# Queue safety guard lives inside queue_trade

`queue_trade` (under `state["veto_lock"]`) atomically enforces: no duplicate pending
for the same player, max 2 total (open+pending) per player (matches preflight PF-04),
and max 6 total committed (open+pending). It returns `True` on enqueue, `False` if
blocked. This protects ALL entry sources at once — prefer relying on it over
re-implementing dedupe/cap checks in each caller.

# Deterministic "guaranteed trigger" path

`deterministic_entry_check` removes Claude's non-deterministic ENTER decision: it
compares the shots-back win-prob model (`_estimate_true_probs`, exp decay k=0.28)
against live Polymarket odds and queues directly when model edge ≥ threshold and
`run_preflight` passes. **Why:** Claude flip-flops ENTER/AVOID on identical data, so
movement triggers alone didn't reliably fire trades. **Caveat:** the model is crude
(shots-back only, no holes-remaining/skill), so "guaranteed" means it fires whenever
the simple model disagrees with the market by the threshold — risk is bounded by the
edge threshold, shot gate, and position/exposure caps, not by model sophistication.
