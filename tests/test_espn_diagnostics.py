"""
ESPN refusal diagnostics.

Production logs every sports enrichment failing identically:

    RuntimeError: ESPN request failed [403]:
    https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard

Which tells us a request was refused and nothing else. A 403 from ESPN's
application and a 403 from a CDN bot filter are the same integer with
opposite fixes — one is answerable by changing the request, the other only by
changing where the request comes from. The response headers and body are what
separate them, and both were being discarded.

These tests cover the diagnostic and the header change. Neither can establish
that the headers *fix* anything: ESPN is unreachable from the test
environment, so the hypothesis is only testable against production.
"""
from __future__ import annotations

import httpx
import pytest

from core.espn_client import (
    BROWSER_HEADERS,
    MAX_BODY_LOG_CHARS,
    ESPNClient,
)


def _client_returning(status=403, body="", headers=None):
    """An ESPNClient whose transport serves one canned response."""

    def handler(request):
        return httpx.Response(status, text=body, headers=headers or {})

    client = ESPNClient()
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler), headers=dict(BROWSER_HEADERS)
    )
    return client


# --------------------------------------------------------------------------
# the headers actually sent
# --------------------------------------------------------------------------

def test_the_request_carries_the_full_header_set():
    """`Mozilla/5.0` alone is the prefix of a UA, not a UA, and a request with
    no Accept/Accept-Language/Referer is a recognisable bot signature."""
    sent = {}

    def handler(request):
        sent.update(request.headers)
        return httpx.Response(200, json={"ok": True})

    client = ESPNClient()
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler), headers=dict(BROWSER_HEADERS)
    )
    client.scoreboard("basketball", "wnba")

    assert "Chrome/" in sent["user-agent"]
    assert sent["user-agent"] != "Mozilla/5.0", "the old value was a bare prefix"
    assert sent["accept"] == "application/json, text/plain, */*"
    assert sent["accept-language"].startswith("en-US")
    assert sent["referer"] == "https://www.espn.com/"


def test_headers_can_be_overridden_for_a_controlled_comparison():
    """So the header hypothesis can be isolated rather than assumed."""
    client = ESPNClient(headers={"User-Agent": "Mozilla/5.0"})
    assert client._http.headers["user-agent"] == "Mozilla/5.0"
    assert "referer" not in client._http.headers


# --------------------------------------------------------------------------
# what a refusal now records
# --------------------------------------------------------------------------

def test_a_403_logs_status_headers_and_body(caplog):
    client = _client_returning(
        status=403,
        body='{"error":"forbidden"}',
        headers={"server": "cloudflare", "cf-ray": "8ab12cd34ef5-LHR",
                 "content-type": "application/json"},
    )
    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError):
            client.scoreboard("basketball", "wnba")

    logged = caplog.text
    assert "status=403" in logged
    assert "cloudflare" in logged, "must name who refused it"
    assert "8ab12cd34ef5-LHR" in logged
    assert '{"error":"forbidden"}' in logged, "the body is the diagnosis"


def test_the_log_names_the_edge_layer_that_refused():
    """The load-bearing distinction.

    A CDN naming itself in the response means the request never reached
    ESPN's application, which is what tells you whether changing headers can
    possibly help.
    """
    client = _client_returning(headers={"server": "AkamaiGHost",
                                        "akamai-grn": "0.abc123"})
    assert "AkamaiGHost" in client._describe_refusal(
        httpx.Response(403, headers={"server": "AkamaiGHost",
                                     "akamai-grn": "0.abc123"})
    )


def test_the_headers_we_sent_are_logged_alongside_the_refusal():
    """Otherwise a future reader cannot tell which header set was rejected."""
    client = _client_returning()
    described = client._describe_refusal(httpx.Response(403))
    assert "request headers sent=" in described
    for header in ("accept", "accept-language", "referer", "user-agent"):
        assert header in described.lower()


def test_cookie_names_are_kept_and_values_are_not():
    """A bot-detection cookie being set is the signal; its contents are not,
    and third-party cookie values do not belong in a log stream."""
    resp = httpx.Response(403, headers=[
        ("set-cookie", "bm_sz=SECRETVALUE123; Path=/"),
        ("set-cookie", "_abck=ANOTHERSECRET; Path=/"),
    ])
    described = _client_returning()._describe_refusal(resp)

    assert "bm_sz" in described and "_abck" in described
    assert "SECRETVALUE123" not in described
    assert "ANOTHERSECRET" not in described


def test_a_large_block_page_is_bounded_and_says_so():
    """Block pages are HTML and can be huge; the telling text is at the top."""
    body = "<html>" + "x" * 50_000
    described = _client_returning()._describe_refusal(httpx.Response(403, text=body))

    assert f"first {MAX_BODY_LOG_CHARS}" in described
    assert str(len(body)) in described, "must state the true length"
    assert len(described) < MAX_BODY_LOG_CHARS + 3000


def test_a_short_body_is_marked_complete():
    described = _client_returning()._describe_refusal(
        httpx.Response(403, text="Forbidden")
    )
    assert "complete" in described and "Forbidden" in described


def test_an_empty_body_does_not_break_the_diagnostic():
    described = _client_returning()._describe_refusal(httpx.Response(403))
    assert "status=403" in described
    assert "0 chars" in described


def test_a_success_logs_nothing_and_returns_data(caplog):
    def handler(request):
        return httpx.Response(200, json={"events": []})

    client = ESPNClient()
    client._http = httpx.Client(transport=httpx.MockTransport(handler))

    with caplog.at_level("ERROR"):
        assert client.scoreboard("basketball", "wnba") == {"events": []}
    assert "ESPN refused" not in caplog.text


def test_the_failure_still_raises_so_enrichment_degrades_loudly():
    """Unchanged behaviour: context.py catches this and proceeds without
    grounding. Logging more must not turn a hard failure into a silent one."""
    with pytest.raises(RuntimeError, match=r"ESPN request failed \[503\]"):
        _client_returning(status=503).scoreboard("basketball", "wnba")


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_every_non_200_is_diagnosed_not_just_403(status):
    client = _client_returning(status=status, body="nope")
    described = client._describe_refusal(httpx.Response(status, text="nope"))
    assert f"status={status}" in described
