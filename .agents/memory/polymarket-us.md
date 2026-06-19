---
name: Polymarket US trade integration
description: Why DAEMON-POLY trades the regulated Polymarket US app (not the self-custody CLOB), plus the non-obvious SDK quirks and the Replit secret-overwrite gotcha.
---

# Polymarket US (custodial) is the real money venue

**Why:** The operator's funds live in the regulated **Polymarket US** app (custodial). The old self-custody
Polygon CLOB wallets (POLYMARKET_PK / POLYMARKET_WALLET) were empty ($0). Anything built against
`py-clob-client` / `clob.polymarket.com` trades the wrong, empty venue.

**How to apply:** `bot.py` uses the `polymarket_us` pip package. Auth = API key id (UUID) +
base64 **Ed25519** secret via env `POLYMARKET_KEY_ID` / `POLYMARKET_SECRET_KEY`.

## Non-obvious SDK facts (reverse-engineered, not in docs/types)
- `markets.bbo(slug)` returns its data under a `marketData` wrapper that the SDK's own type does NOT show:
  `{'marketData':{'bestBid':{'value'..},'bestAsk':{'value'..}}}`. Code must unwrap it.
- In the US Open event, the per-player name is in each market's `description`
  ("Will <Name> win the 2026 U.S. Open..."), NOT in `question` (which is the generic "U.S. Open Winner").
- One `search.query` call returns all ~171 player markets with prices (`outcomePrices`, a JSON *string*) —
  no per-player loop needed for odds.
- **Test orders without spending money:** `orders.create` params are also accepted by `orders.preview`
  ({'request': params}); preview returns the would-be order. Use it to validate before going live.

## Trade-accounting rule (money safety)
**Why:** Orders are GTC marketable limits (buy at live bestAsk, sell at bestBid); a fill is not guaranteed,
and the architect flagged that mutating local position state before confirming the API result can orphan real
exchange positions or corrupt P&L.
**How to apply:** Only mutate `state['open_positions']` AFTER a successful (non-error) order result. Record the
actual order price as entry, not the signal price. For partial exits, decrement local shares/size proportionally.
Full exits use `orders.close_position`. Deeper fill-confirmation polling is still a known gap.

## Replit secret-overwrite gotcha
**Why:** `requestEnvVar` does NOT reliably overwrite a secret that already exists — symptom: stored
`POLYMARKET_SECRET_KEY` still base64-fails ("Incorrect padding") because it holds the old 0x hex private key.
**How to apply:** If a secret must change and a re-request doesn't take, have the user EDIT it directly in the
Secrets pane (agent cannot set/delete secrets). Verify the fix with an `account.balances()` auth check.
