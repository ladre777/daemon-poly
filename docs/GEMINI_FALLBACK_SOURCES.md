# Gemini fallback implementation sources

## Official Google API contract

- Source: https://ai.google.dev/api/generate-content
- Source: https://ai.google.dev/gemini-api/docs/text-generation
- Gemini requests authenticate with the `x-goog-api-key` header.
- The official `v1beta` generation endpoint is `POST https://generativelanguage.googleapis.com/v1beta/{model=models/*}:generateContent`.
- The official text-generation documentation also describes the Interactions API (`POST https://generativelanguage.googleapis.com/v1beta/interactions`), with `model`, `system_instruction`, `input`, and `generation_config` fields. The implementation will use the established `generateContent` REST endpoint to avoid introducing a new SDK dependency.
- `GenerateContentResponse` contains a `candidates` array; candidate text is available through `candidate.content.parts[].text`.
- The generation request supports a system instruction and temperature via generation configuration.

## Official pricing and rate-limit constraints

- Pricing source: https://ai.google.dev/gemini-api/docs/pricing
- Rate-limit source: https://ai.google.dev/gemini-api/docs/rate-limits
- Google lists free access for selected Gemini models, but limits are project- and model-specific.
- Rate limits are evaluated across requests per minute, tokens per minute, and requests per day. Google directs developers to AI Studio for each project’s active limits, and notes that specified limits are not guaranteed.
- Free-tier content may be used to improve Google products. The bot should transmit no credentials or private account data, and Gemini must remain an optional fallback rather than a requirement for the quant path.

Retrieved: 2026-08-22.
