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

from core.errors import CircuitBreaker, FatalError, Severity, TransientError, classify
from core.llm_client import (
    AnthropicBackend,
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
    # Preference order picks k2 over the generic moonshot-v1 model.
    assert backend.model == "kimi-k2-0711-preview"
    assert seen_models == ["kimi-k2-turbo-preview", "kimi-k2-0711-preview"]


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
    assert ranked[0] == "kimi-k3"           # newest general model first
    assert ranked[1] == "kimi-k2.6"
    assert all("code" in m for m in ranked[2:])


def test_known_good_names_outrank_everything():
    ranked = MoonshotBackend._ranked(["kimi-k3", "kimi-k2-turbo-preview"])
    assert ranked[0] == "kimi-k2-turbo-preview"


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
