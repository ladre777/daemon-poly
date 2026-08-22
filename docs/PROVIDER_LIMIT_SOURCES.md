# Provider-limit sources used by the fallback safeguards

Kimi API documentation: https://platform.kimi.ai/docs/api/chat

Kimi documents the OpenAI-compatible `/v1/chat/completions` API, including `max_tokens` and JSON response-format support. The response carries token-use metadata, including reasoning content for models that produce it.

Kimi rate-limit documentation: https://platform.kimi.ai/docs/pricing/limits

Kimi publishes account-tier limits for concurrency, requests per minute, and token throughput. It states that capacity pressure can lead to temporary rate-limit adjustments.

Gemini rate-limit documentation: https://ai.google.dev/gemini-api/docs/rate-limits

Google states that Gemini API limits are project- and model-specific across requests per minute, tokens per minute, and requests per day. It directs customers to AI Studio for active limits and notes that published capacity is not guaranteed.

Retrieved: 2026-08-22.
