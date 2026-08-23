"""
Maker/Checker LLM backend with optional failover.

2026-08-21: Model preference updated to current Kimi ids. Clearer failure
when MOONSHOT_API_KEY is missing (was surfacing as opaque "all providers failed").
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx

from config import DEFAULT_MAKER_FALLBACK_MODEL
from core.errors import CircuitBreaker, Severity, SystemicError, TransientError, classify

log = logging.getLogger("daemon_kalshi.llm")

_MOONSHOT_MODEL_PREFERENCE = (
    "kimi-k2.6",
    "kimi-k3",
    "kimi-k2.5",
    "kimi-k2.7-code",
    "kimi-k2",
    "kimi-latest",
    "moonshot-v1-128k",
    "moonshot-v1-32k",
    "moonshot-v1-8k",
    "kimi-k2-turbo-preview",
    "kimi-k2-0711-preview",
    "kimi-k2-0905-preview",
)

_DEPRIORITISED_MODEL_MARKERS = ("code", "vision", "audio", "embed")
_MAX_MODEL_ATTEMPTS = 4
_ERROR_BODY_CHARS = 400


class LLMUnavailable(SystemicError):
    """No configured provider could produce a completion."""


class LLMRateLimited(TransientError):
    """An optional provider rejected the request due to a temporary quota."""


def first_text_block(response) -> str:
    for block in getattr(response, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            return text
    return ""


class MoonshotBackend:
    name = "moonshot"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 15.0,
        max_tokens: int = 800,
        prompt_cache_key: str = "",
        disable_thinking: bool = True,
    ):
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or "").rstrip("/")
        self.model = model
        self._max_tokens = max(1, int(max_tokens))
        self._prompt_cache_key = (prompt_cache_key or "").strip()
        self._disable_thinking = bool(disable_thinking)
        self._client = httpx.Client(
            base_url=self._base_url or "https://api.moonshot.ai/v1",
            headers={"Authorization": f"Bearer {self._api_key}"} if self._api_key else {},
            timeout=timeout,
        )
        self._probed = False
        self._available: list[str] = []
        self._rejected: set[str] = set()
        self._no_temperature: set[str] = set()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def complete(self, system: str, user: str, temperature: float = 0.3) -> str:
        if not self._api_key:
            raise LLMUnavailable(
                "Moonshot API key is empty. Set MOONSHOT_API_KEY in Railway."
            )
        model = self.model
        last_error: Optional[httpx.HTTPStatusError] = None

        for _ in range(_MAX_MODEL_ATTEMPTS):
            try:
                text = self._post(model, system, user, temperature)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                if status not in (400, 404):
                    raise
                if status == 400 and model not in self._no_temperature:
                    self._no_temperature.add(model)
                    try:
                        text = self._post(model, system, user, None)
                    except httpx.HTTPStatusError as retry_error:
                        self._no_temperature.discard(model)
                        last_error = retry_error
                        self._reject(model, retry_error)
                        model = self._next_model()
                        if model is None:
                            raise
                        continue
                    else:
                        log.warning(
                            "Moonshot model %r rejects `temperature`; sending "
                            "without it from now on.", model,
                        )
                        self._adopt(model)
                        return text
                last_error = e
                self._reject(model, e)
                model = self._next_model()
                if model is None:
                    raise
                continue
            else:
                self._adopt(model)
                return text

        raise last_error if last_error else SystemicError("Moonshot: no usable model")

    def _adopt(self, model: str) -> None:
        if model != self.model:
            log.warning(
                "Moonshot: switched to model %r. Set MOONSHOT_MODEL=%s permanently.",
                model, model,
            )
            self.model = model

    def _reject(self, model: str, error: httpx.HTTPStatusError) -> None:
        self._rejected.add(model)
        body = ""
        try:
            body = error.response.text[:_ERROR_BODY_CHARS]
        except Exception:
            pass
        log.warning("Moonshot rejected model %r with HTTP %d: %s",
                    model, error.response.status_code, body or "(no body)")

    def _post(self, model: str, system: str, user: str,
              temperature: Optional[float]) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
        }
        # Kimi K2.6 rejects the 0.3 value used by other OpenAI-compatible
        # providers. Omitting temperature retains the model's supported default.
        if (
            temperature is not None
            and not model.startswith("kimi-k2.6")
            and model not in self._no_temperature
        ):
            payload["temperature"] = temperature
        if self._prompt_cache_key:
            payload["prompt_cache_key"] = self._prompt_cache_key
        # K2.6 defaults to deep thinking, whose internal reasoning shares the
        # max_tokens budget with content. These one-shot, source-grounded
        # JSON decisions need a complete bounded answer, not a tool loop.
        if self._disable_thinking and model.startswith("kimi-k2.6"):
            payload["thinking"] = {"type": "disabled"}

        started = time.perf_counter()
        resp = self._client.post("/chat/completions", json=payload)
        elapsed_ms = (time.perf_counter() - started) * 1000
        resp.raise_for_status()
        body = resp.json()
        usage = body.get("usage") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        cached_tokens = usage.get(
            "cached_tokens", prompt_details.get("cached_tokens", 0)
        )
        try:
            choice = body["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise SystemicError(f"Moonshot response had no message content: {e}") from e
        if not isinstance(content, str) or not content.strip():
            raise SystemicError("Moonshot response had empty message content")
        log.info(
            "Kimi completion model=%s elapsed_ms=%.0f prompt_tokens=%s "
            "cached_tokens=%s completion_tokens=%s finish_reason=%s",
            model,
            elapsed_ms,
            usage.get("prompt_tokens", "?"),
            cached_tokens,
            usage.get("completion_tokens", "?"),
            choice.get("finish_reason", "?"),
        )
        return content

    def _next_model(self) -> Optional[str]:
        if not self._probed:
            self._probed = True
            self._available = self._list_models()
        for candidate in self._ranked(self._available):
            if candidate not in self._rejected:
                return candidate
        return None

    def _list_models(self) -> list[str]:
        try:
            resp = self._client.get("/models")
            resp.raise_for_status()
            available = [
                m.get("id") for m in resp.json().get("data", [])
                if isinstance(m, dict) and m.get("id")
            ]
        except Exception as e:
            log.warning("Could not list Moonshot models: %s", e)
            return []

        if not available:
            log.warning("Moonshot listed no models for this key")
        else:
            log.info("Moonshot models available to this key: %s",
                     ", ".join(sorted(available)))
        return available

    @staticmethod
    def _ranked(available: list[str]) -> list[str]:
        ranked = [m for m in _MOONSHOT_MODEL_PREFERENCE if m in available]
        rest = [m for m in available if m not in ranked]

        def specialised(model: str) -> bool:
            lowered = model.lower()
            return any(marker in lowered for marker in _DEPRIORITISED_MODEL_MARKERS)

        general = sorted(m for m in rest if not specialised(m))
        ranked += sorted(general, reverse=True)
        ranked += sorted(m for m in rest if specialised(m))
        return ranked


class GeminiBackend:
    """Gemini REST backend used only as an optional fallback provider."""

    name = "gemini"

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 12.0):
        self._api_key = (api_key or "").strip()
        self._base_url = (base_url or "").rstrip("/")
        self.model = model
        self._client = httpx.Client(
            base_url=self._base_url or "https://generativelanguage.googleapis.com/v1beta",
            headers={"x-goog-api-key": self._api_key} if self._api_key else {},
            timeout=timeout,
        )

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def complete(self, system: str, user: str, temperature: float = 0.3) -> str:
        if not self._api_key:
            raise LLMUnavailable("Gemini API key is empty. Set GEMINI_API_KEY in Railway.")
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }
        resp = self._client.post(f"/models/{self.model}:generateContent", json=payload)
        if resp.status_code >= 400:
            # httpx's default HTTPStatusError message is just the status line
            # ("Client error '429 Too Many Requests' for url ...") — it drops
            # the JSON body, which for Gemini is the only place that says
            # *which* quota was hit: per-minute request count, daily request
            # count, and token-count quotas are all reported as a plain 429
            # with no other signal, and only the body's QuotaFailure detail
            # (or RetryInfo.retryDelay) distinguishes "wait a few seconds"
            # from "wait until tomorrow" from "this key has no quota at all".
            # Logged once here, at the only point the body is still in hand —
            # raise_for_status() below discards it.
            log.warning(
                "Gemini HTTP %s on %s: %s",
                resp.status_code, self.model, resp.text[:800],
            )
        resp.raise_for_status()
        try:
            parts = resp.json()["candidates"][0]["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts if isinstance(part, dict))
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise SystemicError(f"Gemini response had no text content: {e}") from e
        if not text.strip():
            raise SystemicError("Gemini response had empty text content")
        return text


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int = 500):
        self._api_key = (api_key or "").strip()
        self.model = model
        self._max_tokens = max_tokens
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def complete(self, system: str, user: str, temperature: float = 0.3) -> str:
        if not self._api_key:
            raise LLMUnavailable("Anthropic API key is empty.")
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self._api_key)
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            system=system,
            temperature=temperature,
            messages=[{"role": "user", "content": user}],
        )
        return first_text_block(resp)


_TIMEOUT_NAMES = frozenset({
    "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout",
    "TimeoutException", "APITimeoutError", "Timeout", "TimeoutError",
})


def _is_timeout(exc: BaseException) -> bool:
    for cls in type(exc).__mro__:
        if cls.__name__ in _TIMEOUT_NAMES:
            return True
    return "timed out" in str(exc).lower() or "timeout" in str(exc).lower()


def _is_rate_limited(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int):
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 429


class MakerLLM:
    def __init__(
        self,
        primary,
        fallback=None,
        failure_threshold: int = 3,
        cooldown_seconds: float = 600.0,
        timeout_threshold: int = 2,
        fallback_rate_limit_cooldown_seconds: float = 900.0,
    ):
        self.primary = primary if (primary and primary.configured) else None
        self.fallback = fallback if (fallback and fallback.configured) else None
        self.breaker = CircuitBreaker(
            name="maker-llm-primary",
            threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
        )
        self.timeout_threshold = timeout_threshold
        self.fallback_rate_limit_cooldown_seconds = max(
            0.0, fallback_rate_limit_cooldown_seconds
        )
        self._fallback_rate_limited_until = 0.0
        self.timeouts_seen = 0
        self.last_provider = ""

    @property
    def configured(self) -> bool:
        return bool(self.primary or self.fallback)

    @property
    def on_fallback(self) -> bool:
        if not self.fallback:
            return False
        if not self.primary:
            return True
        return self.breaker.is_open

    def describe(self) -> str:
        primary = f"{self.primary.name}:{self.primary.model}" if self.primary else "none"
        fallback = f"{self.fallback.name}:{self.fallback.model}" if self.fallback else "none"
        return f"primary={primary} fallback={fallback}"

    def complete(self, system: str, user: str, temperature: float = 0.3) -> str:
        if not self.configured:
            raise LLMUnavailable(
                "No Maker LLM configured. Set MOONSHOT_API_KEY "
                "(exact name) in Railway Variables."
            )

        errors: list[str] = []
        rate_limited_error: Optional[BaseException] = None
        was_open = self.breaker.opened_at is not None
        breaker_open = self.breaker.is_open
        if was_open and not breaker_open:
            self.timeouts_seen = 0

        if self.primary and not breaker_open:
            try:
                text = self.primary.complete(system, user, temperature)
                self.breaker.record_success()
                self.last_provider = self.primary.name
                return text
            except Exception as e:
                severity = classify(e)
                errors.append(f"{self.primary.name}: {e}")
                if _is_rate_limited(e):
                    rate_limited_error = e
                tripped = self.breaker.record_failure(str(e))
                if severity is Severity.FATAL:
                    self.breaker.consecutive_failures = max(
                        self.breaker.consecutive_failures, self.breaker.threshold
                    )
                    if not tripped and self.breaker.opened_at is None:
                        self.breaker.record_failure(str(e))
                elif _is_timeout(e):
                    self.timeouts_seen += 1
                    if self.timeouts_seen >= self.timeout_threshold:
                        self.breaker.consecutive_failures = max(
                            self.breaker.consecutive_failures,
                            self.breaker.threshold,
                        )
                        if self.breaker.opened_at is None:
                            self.breaker.record_failure(str(e))
                log.warning(
                    "Maker primary %s failed (%s): %s%s",
                    self.primary.name, severity.value, e,
                    " — falling back" if self.fallback else "",
                )

        if self.fallback:
            remaining = self._fallback_rate_limited_until - time.monotonic()
            if remaining > 0:
                raise LLMRateLimited(
                    f"{self.fallback.name}: cooldown active for {remaining:.0f}s after quota exhaustion"
                )
            try:
                text = self.fallback.complete(system, user, temperature)
                self.last_provider = self.fallback.name
                return text
            except Exception as e:
                errors.append(f"{self.fallback.name}: {e}")
                if _is_rate_limited(e):
                    self._fallback_rate_limited_until = (
                        time.monotonic() + self.fallback_rate_limit_cooldown_seconds
                    )
                    raise LLMRateLimited(f"{self.fallback.name}: {e}") from e

        if rate_limited_error is not None:
            provider = self.primary.name if self.primary else "LLM"
            raise LLMRateLimited(f"{provider}: {rate_limited_error}") from rate_limited_error

        detail = "; ".join(errors) if errors else (
            "no provider answered (check MOONSHOT_API_KEY and MOONSHOT_MODEL)"
        )
        raise LLMUnavailable(detail)


def build_maker_llm(models_config) -> MakerLLM:
    preference = (getattr(models_config, "maker_provider", "moonshot") or "moonshot").lower()

    moonshot = MoonshotBackend(
        api_key=models_config.moonshot_api_key,
        base_url=models_config.moonshot_base_url,
        model=models_config.moonshot_model,
        timeout=getattr(models_config, "maker_timeout_seconds", 15.0),
        max_tokens=getattr(models_config, "moonshot_max_tokens", 800),
        prompt_cache_key=getattr(models_config, "moonshot_prompt_cache_key", ""),
        disable_thinking=getattr(models_config, "moonshot_disable_thinking", True),
    )
    gemini = GeminiBackend(
        api_key=getattr(models_config, "gemini_api_key", ""),
        base_url=getattr(models_config, "gemini_base_url", ""),
        model=getattr(models_config, "gemini_model", "gemini-3.5-flash-lite"),
        timeout=getattr(models_config, "gemini_timeout_seconds", 12.0),
    )
    fallback_model = (getattr(models_config, "maker_fallback_model", "") or "").strip()
    if not fallback_model:
        fallback_model = DEFAULT_MAKER_FALLBACK_MODEL
        log.warning(
            "MAKER_FALLBACK_MODEL is empty; using cheap default %r rather than "
            "CHECKER_MODEL=%r.",
            fallback_model,
            getattr(models_config, "checker_model", ""),
        )
    anthropic_backend = AnthropicBackend(
        api_key=models_config.anthropic_api_key,
        model=fallback_model,
    )

    if preference == "moonshot":
        if not moonshot.configured:
            log.error(
                "MAKER_LLM_PROVIDER=moonshot but MOONSHOT_API_KEY is empty. "
                "LLM Maker will fail until the key is set."
            )
        return MakerLLM(primary=moonshot, fallback=None)
    if preference == "moonshot_gemini":
        return MakerLLM(
            primary=moonshot,
            fallback=gemini,
            fallback_rate_limit_cooldown_seconds=getattr(
                models_config, "gemini_rate_limit_cooldown_seconds", 900.0
            ),
        )
    if preference == "gemini":
        return MakerLLM(primary=gemini, fallback=None)
    if preference == "anthropic":
        return MakerLLM(primary=anthropic_backend, fallback=None)
    return MakerLLM(primary=moonshot, fallback=anthropic_backend)


def build_checker_llm(models_config) -> MakerLLM:
    preference = (getattr(models_config, "checker_provider", "moonshot") or "moonshot").lower()

    moonshot = MoonshotBackend(
        api_key=models_config.moonshot_api_key,
        base_url=models_config.moonshot_base_url,
        model=getattr(models_config, "checker_model", None) or models_config.moonshot_model,
        timeout=getattr(models_config, "checker_timeout_seconds", 12.0),
        max_tokens=getattr(models_config, "moonshot_max_tokens", 800),
        prompt_cache_key=getattr(models_config, "moonshot_prompt_cache_key", ""),
        disable_thinking=getattr(models_config, "moonshot_disable_thinking", True),
    )
    gemini = GeminiBackend(
        api_key=getattr(models_config, "gemini_api_key", ""),
        base_url=getattr(models_config, "gemini_base_url", ""),
        model=getattr(models_config, "gemini_model", "gemini-3.5-flash-lite"),
        timeout=getattr(models_config, "gemini_timeout_seconds", 12.0),
    )
    anthropic_backend = AnthropicBackend(
        api_key=models_config.anthropic_api_key,
        model=getattr(models_config, "checker_anthropic_model", None)
        or "claude-haiku-4-5-20251001",
        max_tokens=getattr(models_config, "checker_max_tokens", 1200),
    )

    if preference == "moonshot":
        return MakerLLM(primary=moonshot, fallback=None)
    if preference == "moonshot_gemini":
        return MakerLLM(
            primary=moonshot,
            fallback=gemini,
            fallback_rate_limit_cooldown_seconds=getattr(
                models_config, "gemini_rate_limit_cooldown_seconds", 900.0
            ),
        )
    if preference == "gemini":
        # Gemini alone, no fallback, is what produced a Checker that
        # abstains on every call rather than on judgment: the free tier's
        # 429s hit before three-and-a-half hours of paper trading logged a
        # single successful Checker verdict, and MakerLLM has nothing to
        # fall through to.
        #
        # Anthropic, not Moonshot. Falling back to Moonshot would put the
        # Checker back on Kimi the moment Gemini rate-limits — the exact
        # same-model problem independence was restored to fix, just moved
        # from "always" to "whenever Gemini is under load". Claude Haiku is
        # a distinct vendor from both Kimi (Maker's primary) and Gemini
        # (Checker's primary), and anthropic_backend is already built above
        # for the default branch below — this reuses it rather than paying
        # for a second cheap model nobody asked for.
        return MakerLLM(
            primary=gemini,
            fallback=anthropic_backend if anthropic_backend.configured else None,
        )
    if preference == "anthropic":
        return MakerLLM(primary=anthropic_backend, fallback=None)
    return MakerLLM(
        primary=moonshot,
        fallback=anthropic_backend if anthropic_backend.configured else None,
    )
