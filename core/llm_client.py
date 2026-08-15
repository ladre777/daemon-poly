"""
The Maker's text-completion backend, with a fallback provider.

Why this exists as its own module rather than inline in maker.py:

Production run 2026-08-15 found 2,914 tradeable candidates and proposed on
none of them, because every Maker call returned:

    HTTP 404 for https://api.moonshot.ai/v1/chat/completions

A 404 on a chat-completions endpoint that exists almost always means the
*model* is not available to the calling account (Moonshot returns 404, not
400, for an unknown or unpermitted model id). The configured default was
`kimi-k2-turbo-preview`, which is not necessarily enabled on every account or
on both of Moonshot's regional platforms.

Two independent fixes, because either alone still leaves the bot mute:

1. Resolve the model instead of asserting it. On a 404 we ask the provider
   what models the key actually has (`GET /models`), pick the best match, and
   retry once — then remember it. A wrong model name becomes a logged
   self-correction rather than a permanent outage.

2. Fall back to a second provider. Anthropic is already a hard dependency of
   this system (the Checker runs on it), so if Moonshot is unreachable,
   unpermitted, or misconfigured, the Maker uses Anthropic rather than
   proposing nothing at all. Slower and dearer per call, which is exactly why
   it is the fallback and not the default — but a bot that trades on the
   expensive path beats a bot that does not trade.

Neither fix silently invents a probability. If both providers fail, this
raises and the caller skips the candidate; nothing downstream ever receives a
fabricated number.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from core.errors import CircuitBreaker, Severity, SystemicError, classify

log = logging.getLogger("daemon_kalshi.llm")

# Ordered preference when the configured Moonshot model turns out not to
# exist for this key. Kimi K2 first (what the system was designed around),
# then the general-purpose Moonshot models. Any model the account actually
# has is better than no Maker at all.
_MOONSHOT_MODEL_PREFERENCE = (
    "kimi-k2-turbo-preview",
    "kimi-k2-0711-preview",
    "kimi-k2",
    "kimi-latest",
    "moonshot-v1-128k",
    "moonshot-v1-32k",
    "moonshot-v1-8k",
)

# Substrings marking models that are the wrong tool for this job even when
# the account has them. Code-specialised checkpoints answer a probability
# question worse than the general chat models do, so they are tried last
# rather than first when falling back to whatever the key actually has.
_DEPRIORITISED_MODEL_MARKERS = ("code", "vision", "audio", "embed")

# How many distinct model ids to try before giving up and letting the caller
# fail over to the other provider. Bounded so a key with thirty models cannot
# turn one candidate into thirty API calls.
_MAX_MODEL_ATTEMPTS = 4

# Truncation for provider error bodies in logs. The body is where the actual
# reason lives ("model not found", "unsupported parameter"), and not logging
# it is what turned a one-line diagnosis into two deploy cycles.
_ERROR_BODY_CHARS = 400


class LLMUnavailable(SystemicError):
    """No configured provider could produce a completion."""


class MoonshotBackend:
    """OpenAI-compatible chat completions against Moonshot."""

    name = "moonshot"

    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 30.0):
        self._api_key = api_key
        self._base_url = base_url
        self.model = model
        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
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
        """Post a completion, walking the account's model list if need be.

        Both rejections we have actually seen in production are handled here,
        because either one alone still leaves the Maker silent:

          404 — the configured model is not on this key at all.
          400 — the model exists but rejects the request as sent. Observed on
                a newer checkpoint that will not accept `temperature`.

        A 400 is therefore retried once on the same model without the
        sampling parameter before moving on, and only then is the model
        treated as unusable. Anything other than 400/404 propagates: a 401 is
        a credential problem and trying four models with a bad key is four
        ways to fail.
        """
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
                    # Try once without `temperature` before blaming the model.
                    self._no_temperature.add(model)
                    try:
                        text = self._post(model, system, user, None)
                    except httpx.HTTPStatusError as retry_error:
                        self._no_temperature.discard(model)
                        last_error = retry_error
                        self._reject(model, retry_error)
                        model = self._next_model()
                        if model is None:
                            raise last_error
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
                    raise last_error
                continue
            else:
                self._adopt(model)
                return text

        raise last_error if last_error else SystemicError("Moonshot: no usable model")

    def _adopt(self, model: str) -> None:
        if model != self.model:
            log.warning(
                "Moonshot: switched to model %r for the rest of this process. "
                "Set MOONSHOT_MODEL=%s to make this permanent.", model, model,
            )
            self.model = model

    def _reject(self, model: str, error: httpx.HTTPStatusError) -> None:
        self._rejected.add(model)
        body = ""
        try:
            body = error.response.text[:_ERROR_BODY_CHARS]
        except Exception:                           # noqa: BLE001 - diagnostic only
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
        }
        if temperature is not None and model not in self._no_temperature:
            payload["temperature"] = temperature
        resp = self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise SystemicError(f"Moonshot response had no message content: {e}") from e

    def _next_model(self) -> Optional[str]:
        """The next model id worth trying, probing the account once if needed."""
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
        except Exception as e:                      # noqa: BLE001 - diagnostic path
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
        """Known-good names first, then general models, then specialised ones."""
        ranked = [m for m in _MOONSHOT_MODEL_PREFERENCE if m in available]
        rest = [m for m in available if m not in ranked]

        def specialised(model: str) -> bool:
            lowered = model.lower()
            return any(marker in lowered for marker in _DEPRIORITISED_MODEL_MARKERS)

        general = sorted(m for m in rest if not specialised(m))
        # Newest-looking general model first: "kimi-k3" should be tried before
        # "kimi-k2.6" when neither is on the known-good list.
        ranked += sorted(general, reverse=True)
        ranked += sorted(m for m in rest if specialised(m))
        return ranked


class AnthropicBackend:
    """Fallback provider. Same prompt, same contract: return raw text."""

    name = "anthropic"

    def __init__(self, api_key: str, model: str, max_tokens: int = 500):
        self._api_key = api_key
        self.model = model
        self._max_tokens = max_tokens
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def complete(self, system: str, user: str, temperature: float = 0.3) -> str:
        if self._client is None:
            import anthropic                        # imported lazily: optional path
            self._client = anthropic.Anthropic(api_key=self._api_key)
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self._max_tokens,
            system=system,
            temperature=temperature,
            messages=[{"role": "user", "content": user}],
        )
        return resp.content[0].text if resp.content else ""


class MakerLLM:
    """Primary provider with automatic failover to the secondary.

    Failover is sticky within a cooldown window rather than per-call: once
    Moonshot has failed `threshold` times in a row we stop calling it for a
    while, instead of paying its latency on every single candidate before
    falling back. It heals itself when the cooldown expires.
    """

    def __init__(
        self,
        primary,
        fallback=None,
        failure_threshold: int = 3,
        cooldown_seconds: float = 600.0,
    ):
        self.primary = primary if (primary and primary.configured) else None
        self.fallback = fallback if (fallback and fallback.configured) else None
        self.breaker = CircuitBreaker(
            name="maker-llm-primary",
            threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
        )
        self.last_provider = ""

    @property
    def configured(self) -> bool:
        return bool(self.primary or self.fallback)

    @property
    def on_fallback(self) -> bool:
        """True when calls are currently going to the secondary provider.

        The caller uses this to tighten its per-pass call budget: the fallback
        is there to keep the bot alive through an outage, and running full
        volume through it is a cost decision nobody made.
        """
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
                "No Maker LLM configured. Set MOONSHOT_API_KEY or ANTHROPIC_API_KEY."
            )

        errors: list[str] = []

        if self.primary and not self.breaker.is_open:
            try:
                text = self.primary.complete(system, user, temperature)
                self.breaker.record_success()
                self.last_provider = self.primary.name
                return text
            except Exception as e:                  # noqa: BLE001 - failover point
                severity = classify(e)
                errors.append(f"{self.primary.name}: {e}")
                tripped = self.breaker.record_failure(str(e))
                if severity is Severity.FATAL:
                    # Wrong key or wrong endpoint: no number of retries fixes
                    # it, so open the breaker immediately and let the fallback
                    # carry the load rather than failing every candidate first.
                    self.breaker.consecutive_failures = max(
                        self.breaker.consecutive_failures, self.breaker.threshold
                    )
                    if not tripped and self.breaker.opened_at is None:
                        self.breaker.record_failure(str(e))
                log.warning(
                    "Maker primary provider %s failed (%s): %s%s",
                    self.primary.name, severity.value, e,
                    " — falling back" if self.fallback else "",
                )

        if self.fallback:
            try:
                text = self.fallback.complete(system, user, temperature)
                self.last_provider = self.fallback.name
                return text
            except Exception as e:                  # noqa: BLE001 - last resort
                errors.append(f"{self.fallback.name}: {e}")

        raise LLMUnavailable("; ".join(errors) or "all providers failed")


def build_maker_llm(models_config) -> MakerLLM:
    """Wire a MakerLLM from ModelConfig, honouring MAKER_LLM_PROVIDER."""
    preference = (getattr(models_config, "maker_provider", "auto") or "auto").lower()

    moonshot = MoonshotBackend(
        api_key=models_config.moonshot_api_key,
        base_url=models_config.moonshot_base_url,
        model=models_config.moonshot_model,
    )
    anthropic_backend = AnthropicBackend(
        api_key=models_config.anthropic_api_key,
        model=getattr(models_config, "maker_fallback_model", "") or models_config.checker_model,
    )

    if preference == "moonshot":
        return MakerLLM(primary=moonshot, fallback=None)
    if preference == "anthropic":
        return MakerLLM(primary=anthropic_backend, fallback=None)
    return MakerLLM(primary=moonshot, fallback=anthropic_backend)
