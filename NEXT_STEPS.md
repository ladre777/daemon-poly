# NEXT_STEPS

Handoff for the next session. **Read this and `docs/PRODUCTION.md`.**

Last updated: **2026-08-21** (production push session).

---

## Ground truth

| | |
|---|---|
| Fills | Still **none historically** at last long review — treat first fill as a systems test |
| Risk posture | Fail-closed. Do **not** lower Checker/coherence/edge gates to force trades |
| Durable storage | Requires Railway Volume at `/data` or memory resets every redeploy |
| ESPN | Blocked from Railway. Do not touch ESPN client |
| Golf | Slash Golf **wired** in `main.py` — needs `SLASH_GOLF_API_KEY` |
| Checker | Default **Moonshot/Kimi** (`CHECKER_LLM_PROVIDER=moonshot`) — stops Claude burn |
| Crypto quant | Verified families: KXBTC15M, KXBTC, KXBTCD, KXETH. Vol spike refusal on |
| Weather | NOAA live + same-day error guidance (~1–2°F, not 3–4°F) |

---

## Done on 2026-08-21

1. **Checker → Moonshot/Kimi** — configurable provider; Claude optional fallback
2. **Vol spike refusal** — quant path declines when short-window vol spikes vs baseline
3. **Weather error guidance** — context tells Maker realistic same-day NWS error bands
4. **Slash Golf wired** — `SlashGolfClient` passed into `ContextEnricher`
5. **`docs/PRODUCTION.md`** — competitor comparison + go-live checklist

---

## Operator checklist (now)

1. Confirm Railway Volume mounted at `/data`
2. Env:
   ```bash
   CHECKER_LLM_PROVIDER=moonshot
   CHECKER_MODEL=kimi-k2-turbo-preview
   SLASH_GOLF_API_KEY=...
   RTI_FEED_ENABLED=true
   PERSIST_VOL_HISTORY=true
   LEDGER_DB_PATH=/data/daemon_kalshi.db
   ```
3. Prefer paper until funnel shows approvals:
   ```bash
   DRY_RUN=true
   # KALSHI_ENV=demo recommended until machinery proven
   ```
4. After redeploy, confirm logs:
   - `Checker LLM: primary=moonshot:...`
   - `Slash Golf grounding enabled` (if key set)
   - Vol history restore if volume present

---

## Single next engineering priorities (profit-oriented)

Ordered by structural edge from Becker / open-source bot research:

1. **Order lifecycle → enable maker mode** — makers earn; takers lose on average
2. **Atomic YES+NO arb execution** — detection exists; both legs or neither
3. **Faster 15m crypto lag path** — spot/RTI move vs lagged Kalshi book
4. **Weather normal-CDF path** — NWS high + horizon-dependent σ (same shape as quant crypto)
5. **Calibration time-split views** — already partially supported via `CALIBRATION_REGIME_SPLIT_AT`

Do **not** start by lowering `CHECKER_MIN_CONFIDENCE` or `MIN_EDGE_THRESHOLD`.

---

## What not to do

- Do not widen coherence gates because nothing approved
- Do not enable maker mode without resting-order cancel + reconcile
- Do not trade gold/silver until unit mismatch is resolved
- Do not run live money without durable `/data`
- Do not trust X equity curves without settled PnL

---

## Standing constraints

- Golf is the only sports priority
- Fail-closed; small reviewable changes; no silent risk weakening
- Progress = real edge + real fills, not activity volume

See **`docs/PRODUCTION.md`** for full competitor comparison and go-live gates.
