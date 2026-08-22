# Kimi context-optimization sources

## Official context-caching guidance

Source: https://platform.kimi.ai/docs/guide/use-context-caching-feature-of-kimi-api

Kimi context caching is automatic for normal `/v1/chat/completions` calls. It is best suited to frequent requests that repeat large fixed initial context. A cacheable prefix must exceed 256 prompt tokens. Kimi recommends keeping fixed knowledge content and instructions stable and placing fixed content before dynamic questions so the service can detect cache hits.

## Official chat-completions reference

Source: https://platform.kimi.ai/docs/api/chat

Kimi responses expose `usage.prompt_tokens`, `usage.completion_tokens`, `usage.cached_tokens`, and `usage.total_tokens`. The API supports `max_tokens`, JSON response formatting, streaming, and a `prompt_cache_key` used to optimize cache hit rates for similar agent requests.

## Official streaming guidance

Source: https://platform.kimi.ai/docs/guide/utilize-the-streaming-output-feature-of-the-kimi-api

Streaming reduces time-to-first-token but does not reduce total inference work. It is therefore useful for user-facing progress, not as the core fix for a bot that must parse a completed structured response before acting.

Retrieved: 2026-08-22.

## K2.6 thinking-mode decision

Kimi K2.6 enables thinking by default, and its `reasoning_content` and final `content` share one `max_tokens` budget. Official guidance recommends a much larger budget for deep, multi-step reasoning. For this bot's one-shot, source-grounded JSON probability decisions, default thinking consumed the 800-token output cap before emitting any final content in the 2026-08-22 deployment. The bot therefore sends `thinking: {"type": "disabled"}` only for `kimi-k2.6`; this preserves the complete bounded answer and avoids substituting a lower-quality model. This setting is configurable through `MOONSHOT_DISABLE_THINKING`.

Source: https://platform.kimi.ai/docs/guide/use-thinking-models
Source: https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart
