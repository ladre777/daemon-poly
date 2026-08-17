"""
Naming the commonest startup failure instead of printing Kalshi's word for it.

VERIFIED AGAINST PRODUCTION, 2026-08-17. Pointing a working bot at
``KALSHI_ENV=prod`` while it still held a demo API key produced only this::

    Kalshi rejected a reconciliation call: Kalshi API error 401:
    {"error":{"code":"authentication_error","message":"authentication_error",
              "details":"NOT_FOUND"}}

"NOT_FOUND" on an authentication error reads like a routing bug or a bad URL.
It actually means the key ID does not exist *in that environment*: Kalshi
issues separate credentials for demo and production, and a key from one is
simply unknown to the other.

The bot's behaviour was already correct — it refused to start rather than
trade against an account it could not verify, and held before exiting so the
restart loop did not hammer the exchange. Only the explanation was missing.
"""
from __future__ import annotations

from config import CONFIG
from core.kalshi_client import diagnose_auth_failure

#: The real payload, as logged by production.
LIVE_401 = (
    'Kalshi rejected a reconciliation call: Kalshi API error 401: '
    '{"error":{"code":"authentication_error","message":"authentication_error",'
    '"details":"NOT_FOUND"}}'
)


def test_the_real_production_error_is_recognised():
    assert diagnose_auth_failure(RuntimeError(LIVE_401), "prod")


def test_it_names_the_environment_mismatch_as_the_likely_cause():
    text = diagnose_auth_failure(RuntimeError(LIVE_401), "prod")

    assert "SEPARATE credentials" in text
    assert "'demo' key is unknown to 'prod'" in text


def test_it_says_this_is_authentication_not_an_outage():
    """The two have completely different responses: wait, versus fix a key."""
    text = diagnose_auth_failure(RuntimeError(LIVE_401), "prod")

    assert "not an outage" in text


def test_it_names_the_variables_to_check():
    text = diagnose_auth_failure(RuntimeError(LIVE_401), "prod")

    assert "KALSHI_API_KEY_ID" in text
    assert "KALSHI_PRIVATE_KEY_PEM" in text


# -- the URL must match the environment being reported on ------------------


def test_the_prod_url_is_quoted_for_prod():
    """The first cut read CONFIG.kalshi.rest_base, which resolves against the
    *running* config — so a message about 'prod' printed the demo URL and sent
    the reader to check the wrong credentials. A diagnostic that contradicts
    itself is worse than the bare 401 it replaces."""
    text = diagnose_auth_failure(RuntimeError(LIVE_401), "prod")

    assert "api.elections.kalshi.com" in text
    assert "demo-api.kalshi.co" not in text


def test_the_demo_url_is_quoted_for_demo():
    text = diagnose_auth_failure(RuntimeError(LIVE_401), "demo")

    assert "demo-api.kalshi.co" in text
    assert "api.elections.kalshi.com" not in text


def test_the_url_does_not_follow_the_running_config():
    """Pinned directly: reporting on 'prod' while CONFIG says demo must still
    quote the prod URL."""
    CONFIG.kalshi.env = "demo"

    text = diagnose_auth_failure(RuntimeError(LIVE_401), "prod")

    assert "api.elections.kalshi.com" in text


def test_the_environment_falls_back_to_config_when_not_given():
    CONFIG.kalshi.env = "prod"

    text = diagnose_auth_failure(RuntimeError(LIVE_401))

    assert "'prod' environment" in text


# -- it must stay quiet about everything else ------------------------------


def test_a_timeout_gets_no_authentication_advice():
    """Appended unconditionally by the caller, so a wrong guess here would
    send every outage after a credential that is perfectly fine."""
    assert diagnose_auth_failure(RuntimeError("read operation timed out")) == ""


def test_a_500_gets_no_authentication_advice():
    assert diagnose_auth_failure(RuntimeError("Kalshi API error 500: upstream")) == ""


def test_a_rate_limit_gets_no_authentication_advice():
    assert diagnose_auth_failure(RuntimeError("Kalshi API error 429: slow down")) == ""


def test_an_authentication_error_without_a_status_code_is_still_caught():
    assert diagnose_auth_failure(RuntimeError("authentication_error")) != ""
