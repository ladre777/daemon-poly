"""
Tests for error classification, the circuit breaker, and Maker LLM failover.

The production failure these cover: Moonshot returned 404 for the configured
model, `maker.propose()` raised, and the exception unwound a pass holding
2,914 candidates. Both halves are tested here — the provider recovers on its
own, and a failure that cannot be recovered costs one candidate rather than
the pass.
"""
from __future__ import annotations

import httpx
import pytest

from core.errors import CircuitBreaker, FatalError, Severity, SystemicError, TransientError, classify
from core.llm_client import (
    AnthropicBackend,
    GeminiBackend,
    LLMRateLimited,
    LLMUnavailable,
    MakerLLM,
    MoonshotBackend,
)


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.parametrize(
    "status, expected",
    [
        (429, Severity.TRANSIENT),   # rate limited: try again
        (500, Severity.TRANSIENT),   # provider fault: try again
        (503, Severity.TRANSIENT),
        (401, Severity.FATAL),       # bad key: retrying cannot help
        (403, Severity.FATAL),
        (404, Severity.FATAL),       # wrong model/endpoint: configuration
        (422, Severity.SYSTEMIC),
    ],
)
def test_http_status_classification(status, expected):
    assert classify(_http_error(status)) is expected


def test_network_errors_are_transient():
    assert classify(httpx.ConnectTimeout("no route")) is Severity.TRANSIENT
    assert classify(httpx.ReadTimeout("slow")) is Severity.TRANSIENT


def test_unknown_exceptions_are_systemic_not_transient():
    """An unrecognised failure in a money loop is not assumed to be noise."""
    assert classify(ValueError("who knows")) is Severity.SYSTEMIC


def test_daemon_errors_carry_their_own_severity():
    assert classify(TransientError("x")) is Severity.TRANSIENT
    assert classify(FatalError("x")) is Severity.FATAL


# --------------------------------------------------------------------------
# circuit breaker
# --------------------------------------------------------------------------

def test_breaker_trips_only_on_consecutive_failures():
    breaker = CircuitBreaker(name="t", threshold=3, cooldown_seconds=0)
    assert breaker.record_failure("a") is False
    assert breaker.record_failure("b") is False
    breaker.record_success()                 # the run is broken
    assert breaker.record_failure("c") is False
    assert breaker.record_failure("d") is False
    assert breaker.is_open is False
    assert breaker.record_failure("e") is True
    assert breaker.is_open is True


def test_breaker_reopens_after_cooldown_probe_fails():
    clock = {"t": 0.0}
    breaker = CircuitBreaker(name="t", threshold=1, cooldown_seconds=60.0)
    breaker._now = lambda: clock["t"]

    breaker.record_failure("down")
    assert breaker.is_open is True

    clock["t"] = 61.0
    assert breaker.is_open is False          # one probe allowed through
    breaker.record_failure("still down")
    assert breaker.is_open is True           # and it re-opens on failure


# --------------------------------------------------------------------------
# Moonshot model self-healing
# --------------------------------------------------------------------------

def _moonshot_with_transport(handler, model="kimi-k2-turbo-preview"):
    backend = MoonshotBackend(api_key="k", base_url="https://moon.test/v1", model=model)
    backend._client = httpx.Client(
        base_url="https://moon.test/v1",
        transport=httpx.MockTransport(handler),
    )
    return backend


def test_moonshot_404_resolves_a_real_model_and_retries():
    """The exact production failure: configured model isn't on the account."""
    seen_models = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "moonshot-v1-8k"},
                {"id": "kimi-k2-0711-preview"},
            ]})
        import json
        model = json.loads(request.content)["model"]
        seen_models.append(model)
        if model == "kimi-k2-turbo-preview":
            return httpx.Response(404, json={"error": {"message": "model not found"}})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{\"probability_yes\": 0.6}"}}]
        })

    backend = _moonshot_with_transport(handler)
    out = backend.complete("sys", "user")

    assert out == "{\"probability_yes\": 0.6}"
    # Current Kimi preference de-prioritises retired previews after the
    # generally available Moonshot models returned by the account.
    assert backend.model == "moonshot-v1-8k"
    assert seen_models == ["kimi-k2-turbo-preview", "moonshot-v1-8k"]


def test_kimi_payload_is_bounded_structured_and_cache_aware(caplog):
    """Kimi K2.6 must not receive unsupported temperature=0.3."""
    seen_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen_payload.update(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{\"probability_yes\": 0.6}"}}],
            "usage": {
                "prompt_tokens": 420,
                "cached_tokens": 360,
                "completion_tokens": 71,
            },
        })

    backend = MoonshotBackend(
        api_key="k",
        base_url="https://moon.test/v1",
        model="kimi-k2.6",
        max_tokens=321,
        prompt_cache_key="test-kalshi-maker-v1",
    )
    backend._client = httpx.Client(
        base_url="https://moon.test/v1",
        transport=httpx.MockTransport(handler),
    )

    with caplog.at_level("INFO"):
        assert backend.complete("stable rules", "current market", 0.3) == (
            "{\"probability_yes\": 0.6}"
        )

    assert seen_payload["max_tokens"] == 321
    assert seen_payload["response_format"] == {"type": "json_object"}
    assert seen_payload["prompt_cache_key"] == "test-kalshi-maker-v1"
    assert "temperature" not in seen_payload
    assert "cached_tokens=360" in caplog.text


def test_moonshot_404_with_no_usable_model_still_raises():
    """Self-healing must not invent a model that isn't there."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"error": {"message": "nope"}})

    backend = _moonshot_with_transport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        backend.complete("sys", "user")


def test_moonshot_does_not_reprobe_models_on_every_call():
    probes = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            probes["n"] += 1
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={})

    backend = _moonshot_with_transport(handler)
    for _ in range(3):
        with pytest.raises(httpx.HTTPStatusError):
            backend.complete("sys", "user")
    assert probes["n"] == 1


# --------------------------------------------------------------------------
# Gemini REST backend
# --------------------------------------------------------------------------

def _gemini_with_transport(handler, model="gemini-test"):
    backend = GeminiBackend(
        api_key="g-key", base_url="https://gemini.test/v1beta", model=model
    )
    backend._client = httpx.Client(
        base_url="https://gemini.test/v1beta",
        headers={"x-goog-api-key": "g-key"},
        transport=httpx.MockTransport(handler),
    )
    return backend


def test_gemini_backend_sends_system_instruction_and_parses_text():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "g-key"
        assert request.url.path.endswith("/models/gemini-test:generateContent")
        import json
        payload = json.loads(request.content)
        assert payload["systemInstruction"]["parts"][0]["text"] == "sys"
        assert payload["contents"][0]["parts"][0]["text"] == "user"
        assert payload["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [
                {"text": '{"probability_yes": '}, {"text": "0.6}"}
            ]}}]
        })

    backend = _gemini_with_transport(handler)
    assert backend.complete("sys", "user") == '{"probability_yes": 0.6}'


def test_gemini_backend_rejects_empty_candidate_text():
    backend = _gemini_with_transport(
        lambda request: httpx.Response(200, json={
            "candidates": [{"content": {"parts": []}}]
        })
    )
    with pytest.raises(SystemicError, match="empty text"):
        backend.complete("sys", "user")


# --------------------------------------------------------------------------
# failover
# --------------------------------------------------------------------------

class _StubBackend:
    def __init__(self, name, result=None, error=None):
        self.name = name
        self.model = "stub"
        self.configured = True
        self.calls = 0
        self._result = result
        self._error = error

    def complete(self, system, user, temperature=0.3):
        self.calls += 1
        if self._error:
            raise self._error
        return self._result


def test_primary_is_used_when_healthy():
    primary = _StubBackend("moonshot", result="ok")
    fallback = _StubBackend("anthropic", result="fallback")
    llm = MakerLLM(primary=primary, fallback=fallback)

    assert llm.complete("s", "u") == "ok"
    assert llm.last_provider == "moonshot"
    assert fallback.calls == 0


def test_failover_to_anthropic_when_primary_404s():
    primary = _StubBackend("moonshot", error=_http_error(404))
    fallback = _StubBackend("anthropic", result="fallback answer")
    llm = MakerLLM(primary=primary, fallback=fallback)

    assert llm.complete("s", "u") == "fallback answer"
    assert llm.last_provider == "anthropic"


def test_failover_to_gemini_when_moonshot_times_out():
    primary = _StubBackend("moonshot", error=httpx.ReadTimeout("slow"))
    fallback = _StubBackend("gemini", result="fallback answer")
    llm = MakerLLM(primary=primary, fallback=fallback)

    assert llm.complete("s", "u") == "fallback answer"
    assert llm.last_provider == "gemini"
    assert primary.calls == 1
    assert fallback.calls == 1


def test_rate_limited_gemini_fallback_is_transient_not_systemic():
    primary = _StubBackend("moonshot", error=httpx.ReadTimeout("slow"))
    fallback = _StubBackend("gemini", error=_http_error(429))
    llm = MakerLLM(primary=primary, fallback=fallback)

    with pytest.raises(LLMRateLimited, match="gemini") as exc_info:
        llm.complete("s", "u")

    assert classify(exc_info.value) is Severity.TRANSIENT


def test_gemini_rate_limit_starts_a_cooldown_before_the_next_fallback_call():
    primary = _StubBackend("moonshot", error=httpx.ReadTimeout("slow"))
    fallback = _StubBackend("gemini", error=_http_error(429))
    llm = MakerLLM(
        primary=primary,
        fallback=fallback,
        fallback_rate_limit_cooldown_seconds=60,
    )

    with pytest.raises(LLMRateLimited, match="gemini"):
        llm.complete("s", "u")
    with pytest.raises(LLMRateLimited, match="cooldown active"):
        llm.complete("s", "u")

    assert fallback.calls == 1
    assert primary.calls == 2


def test_moonshot_gemini_mode_keeps_moonshot_primary():
    from types import SimpleNamespace
    from core.llm_client import build_maker_llm

    models = SimpleNamespace(
        maker_provider="moonshot_gemini",
        moonshot_api_key="moonshot-key",
        moonshot_base_url="https://moon.test/v1",
        moonshot_model="kimi-test",
        maker_timeout_seconds=10.0,
        gemini_api_key="gemini-key",
        gemini_base_url="https://gemini.test/v1beta",
        gemini_model="gemini-test",
        gemini_timeout_seconds=8.0,
        maker_fallback_model="",
        anthropic_api_key="",
    )

    llm = build_maker_llm(models)

    assert llm.primary.name == "moonshot"
    assert llm.fallback.name == "gemini"


def test_fatal_primary_error_opens_the_breaker_immediately():
    """A bad key shouldn't be re-tried once per candidate for 2,914 candidates."""
    primary = _StubBackend("moonshot", error=_http_error(401))
    fallback = _StubBackend("anthropic", result="fallback")
    llm = MakerLLM(primary=primary, fallback=fallback, failure_threshold=3)

    for _ in range(5):
        assert llm.complete("s", "u") == "fallback"

    assert primary.calls == 1          # tried once, then stopped being asked
    assert fallback.calls == 5


def test_transient_primary_errors_retry_until_the_threshold():
    primary = _StubBackend("moonshot", error=_http_error(500))
    fallback = _StubBackend("anthropic", result="fallback")
    llm = MakerLLM(primary=primary, fallback=fallback, failure_threshold=3)

    for _ in range(5):
        llm.complete("s", "u")

    assert primary.calls == 3          # retried, then the breaker opened
    assert fallback.calls == 5


def test_primary_recovery_resets_the_breaker():
    class Flaky(_StubBackend):
        def complete(self, system, user, temperature=0.3):
            self.calls += 1
            if self.calls <= 2:
                raise _http_error(500)
            return "recovered"

    primary = Flaky("moonshot")
    fallback = _StubBackend("anthropic", result="fallback")
    llm = MakerLLM(primary=primary, fallback=fallback, failure_threshold=5)

    assert llm.complete("s", "u") == "fallback"
    assert llm.complete("s", "u") == "fallback"
    assert llm.complete("s", "u") == "recovered"
    assert llm.breaker.consecutive_failures == 0


def test_both_providers_failing_raises_rather_than_returning_junk():
    primary = _StubBackend("moonshot", error=_http_error(500))
    fallback = _StubBackend("anthropic", error=RuntimeError("also down"))
    llm = MakerLLM(primary=primary, fallback=fallback)

    with pytest.raises(LLMUnavailable):
        llm.complete("s", "u")


def test_no_configured_provider_raises_rather_than_silently_no_op():
    llm = MakerLLM(
        primary=MoonshotBackend(api_key="", base_url="https://x.test/v1", model="m"),
        fallback=AnthropicBackend(api_key="", model="m"),
    )
    assert llm.configured is False
    with pytest.raises(LLMUnavailable):
        llm.complete("s", "u")


def test_on_fallback_reports_the_active_provider():
    """The per-pass call budget depends on this being accurate."""
    primary = _StubBackend("moonshot", error=_http_error(401))
    fallback = _StubBackend("anthropic", result="fallback")
    llm = MakerLLM(primary=primary, fallback=fallback, failure_threshold=3)

    assert llm.on_fallback is False
    llm.complete("s", "u")                   # fatal error opens the breaker
    assert llm.on_fallback is True


def test_on_fallback_is_false_without_a_fallback_configured():
    primary = _StubBackend("moonshot", result="ok")
    llm = MakerLLM(primary=primary, fallback=None)
    assert llm.on_fallback is False


# --------------------------------------------------------------------------
# Moonshot 400 handling — the second production rejection
# --------------------------------------------------------------------------

def test_400_retries_without_temperature_before_blaming_the_model():
    """Observed in production: a newer checkpoint rejects `temperature`."""
    import json
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "kimi-k3"}]})
        body = json.loads(request.content)
        seen.append(("temperature" in body, body["model"]))
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "unsupported parameter"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    backend = _moonshot_with_transport(handler, model="kimi-k3")
    assert backend.complete("sys", "user") == "ok"
    assert seen == [(True, "kimi-k3"), (False, "kimi-k3")]

    # And it remembers, rather than paying the 400 on every later call.
    assert backend.complete("sys", "user") == "ok"
    assert seen[-1] == (False, "kimi-k3")


def test_walks_to_the_next_model_when_one_is_unusable():
    """404 on the configured model, 400 on the first replacement."""
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [
                {"id": "kimi-k3"}, {"id": "kimi-k2.6"},
            ]})
        model = json.loads(request.content)["model"]
        if model == "kimi-k2-turbo-preview":
            return httpx.Response(404, json={})
        if model == "kimi-k3":
            return httpx.Response(400, json={"error": {"message": "nope"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    backend = _moonshot_with_transport(handler)
    assert backend.complete("sys", "user") == "ok"
    assert backend.model == "kimi-k2.6"


def test_model_walk_is_bounded():
    """A key with many models must not turn one candidate into many calls."""
    import json
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={
                "data": [{"id": f"kimi-m{i}"} for i in range(20)]
            })
        posts.append(json.loads(request.content)["model"])
        return httpx.Response(404, json={})

    backend = _moonshot_with_transport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        backend.complete("sys", "user")
    assert len(set(posts)) <= 4


def test_auth_failure_is_not_retried_across_models():
    """A 401 is a credential problem; four models is four ways to fail."""
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request.url.path)
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    backend = _moonshot_with_transport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        backend.complete("sys", "user")
    assert len(posts) == 1


def test_code_models_are_tried_after_general_ones():
    ranked = MoonshotBackend._ranked(
        ["kimi-k2.7-code", "kimi-k2.6", "kimi-k3", "kimi-k2.7-code-highspeed"]
    )
    assert ranked[0] == "kimi-k2.6"         # configured production model first
    assert ranked[1] == "kimi-k3"
    assert all("code" in m for m in ranked[2:])


def test_known_good_names_outrank_everything():
    ranked = MoonshotBackend._ranked(["kimi-k3", "kimi-k2-turbo-preview"])
    assert ranked[0] == "kimi-k3"


# --------------------------------------------------------------------------
# Anthropic response shape
# --------------------------------------------------------------------------

class _Block:
    def __init__(self, text=None):
        if text is not None:
            self.text = text


class _ThinkingBlock:
    """No .text attribute at all — exactly what production hit."""

    def __init__(self, thinking):
        self.thinking = thinking


class _Response:
    def __init__(self, content):
        self.content = content


def test_first_text_block_skips_a_thinking_block():
    """Production: 'ThinkingBlock' object has no attribute 'text'.

    content[0] is a thinking block whenever the model reasons; the answer is
    further down the list.
    """
    from core.llm_client import first_text_block

    resp = _Response([_ThinkingBlock("let me consider"), _Block('{"verdict": "approve"}')])
    assert first_text_block(resp) == '{"verdict": "approve"}'


def test_first_text_block_returns_the_first_of_several():
    from core.llm_client import first_text_block

    resp = _Response([_Block("first"), _Block("second")])
    assert first_text_block(resp) == "first"


def test_first_text_block_is_empty_when_there_is_no_text():
    """No text is 'no usable answer', which callers refuse — not a crash."""
    from core.llm_client import first_text_block

    assert first_text_block(_Response([_ThinkingBlock("only thinking")])) == ""
    assert first_text_block(_Response([])) == ""
    assert first_text_block(_Response(None)) == ""


def test_first_text_block_skips_empty_text_blocks():
    from core.llm_client import first_text_block

    assert first_text_block(_Response([_Block(""), _Block("real")])) == "real"


# -- the fallback must never inherit the Checker's model -------------------
#
# build_maker_llm used to resolve an empty MAKER_FALLBACK_MODEL with
# `... or models_config.checker_model`, so clearing the variable silently
# routed the Maker's high-volume path -- tens of calls per pass -- onto
# whatever the Checker was configured with. The Checker is the low-volume
# path and is picked for judgement quality, not unit cost, so that
# inheritance points the expensive model at the expensive workload.
# config.py had warned about exactly this in prose since the fallback was
# added; the code one file over did it anyway.


def _models(fallback, checker="claude-sonnet-5"):
    from config import ModelConfig

    m = ModelConfig()
    m.maker_fallback_model = fallback
    m.checker_model = checker
    m.anthropic_api_key = "test-key"
    m.moonshot_api_key = "test-key"
    # Exercise the legacy Anthropic degraded route explicitly. Production uses
    # moonshot_gemini, where Gemini is the only fallback provider.
    m.maker_provider = "legacy_auto"
    return m


def test_an_empty_fallback_does_not_inherit_the_checker_model():
    from core.llm_client import build_maker_llm

    llm = build_maker_llm(_models("", checker="some-expensive-model"))

    assert llm.fallback.model != "some-expensive-model"


def test_an_empty_fallback_uses_the_explicit_cheap_default():
    from config import DEFAULT_MAKER_FALLBACK_MODEL
    from core.llm_client import build_maker_llm

    llm = build_maker_llm(_models(""))

    assert llm.fallback.model == DEFAULT_MAKER_FALLBACK_MODEL


def test_a_whitespace_only_fallback_is_treated_as_empty():
    from config import DEFAULT_MAKER_FALLBACK_MODEL
    from core.llm_client import build_maker_llm

    llm = build_maker_llm(_models("   "))

    assert llm.fallback.model == DEFAULT_MAKER_FALLBACK_MODEL


def test_degrading_is_loud(caplog):
    """A cost guardrail must not silently take effect. It also must not take
    an unattended trading bot offline, which is why this warns rather than
    raising -- so the warning has to name the variable AND the model in use."""
    from core.llm_client import build_maker_llm

    with caplog.at_level("WARNING"):
        build_maker_llm(_models("", checker="some-expensive-model"))

    assert "MAKER_FALLBACK_MODEL" in caplog.text
    assert "some-expensive-model" in caplog.text


def test_an_explicit_fallback_is_honoured_unchanged():
    from core.llm_client import build_maker_llm

    llm = build_maker_llm(_models("claude-haiku-4-5-20251001"))

    assert llm.fallback.model == "claude-haiku-4-5-20251001"


def test_config_and_client_share_one_default():
    """Two places resolving "what does the degraded path cost" must not be
    able to drift apart."""
    from config import DEFAULT_MAKER_FALLBACK_MODEL, ModelConfig

    assert ModelConfig().maker_fallback_model == DEFAULT_MAKER_FALLBACK_MODEL
