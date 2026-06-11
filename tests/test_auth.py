"""HMAC device-token mint/verify. Offline.

auth.py reads settings live, so we monkeypatch the settings singleton.
"""

from __future__ import annotations

import time

import pytest

from server import auth


@pytest.fixture
def enable_auth(monkeypatch):
    monkeypatch.setattr(auth.settings, "JARVIZ_AUTH_SECRET", "s3cret-key")
    monkeypatch.setattr(auth.settings, "JARVIZ_AUTH_TOKEN_TTL_S", 3600)
    monkeypatch.setattr(auth.settings, "JARVIZ_AUTH_ALLOWED_DEVICES", "")
    return auth.settings


# ---- auth disabled (default LAN prototype) ----

def test_auth_disabled_mint_is_empty_and_verify_always_true(monkeypatch):
    monkeypatch.setattr(auth.settings, "JARVIZ_AUTH_SECRET", "")
    assert auth.auth_enabled() is False
    assert auth.mint_token("c", "d") == ""
    assert auth.verify_token("c", "d", "") is True
    assert auth.verify_token("c", "d", "anything") is True


# ---- happy path ----

def test_mint_then_verify_roundtrip(enable_auth):
    tok = auth.mint_token("client-1", "dev-1")
    assert tok and "." in tok
    assert auth.verify_token("client-1", "dev-1", tok) is True


def test_token_bound_to_device_and_client(enable_auth):
    tok = auth.mint_token("client-1", "dev-1")
    assert auth.verify_token("client-1", "dev-2", tok) is False  # wrong device
    assert auth.verify_token("client-X", "dev-1", tok) is False  # wrong client


def test_tampered_signature_rejected(enable_auth):
    tok = auth.mint_token("client-1", "dev-1")
    sig, ts = tok.split(".", 1)
    bad = ("A" + sig[1:] if sig[0] != "A" else "B" + sig[1:]) + "." + ts
    assert auth.verify_token("client-1", "dev-1", bad) is False


def test_malformed_tokens_rejected(enable_auth):
    for bad in ("", "no-dot", "sig.not-an-int", ".", "sig."):
        assert auth.verify_token("client-1", "dev-1", bad) is False


def test_expired_token_rejected(enable_auth, monkeypatch):
    tok = auth.mint_token("client-1", "dev-1")
    # Jump now forward past the TTL.
    real = time.time()
    monkeypatch.setattr(auth.time, "time", lambda: real + 3601)
    assert auth.verify_token("client-1", "dev-1", tok) is False


def test_future_dated_token_rejected(enable_auth):
    # age < 0 must be rejected (clock skew / forged future ts).
    future_ts = int(time.time()) + 10_000
    sig = auth._sign("client-1", "dev-1", future_ts, "s3cret-key")
    tok = f"{sig}.{future_ts}"
    assert auth.verify_token("client-1", "dev-1", tok) is False


# ---- allowlist bypass ----

def test_allowlisted_device_bypasses(enable_auth, monkeypatch):
    monkeypatch.setattr(auth.settings, "JARVIZ_AUTH_ALLOWED_DEVICES", "dev-1, dev-2")
    assert auth.device_allowed("dev-1") is True
    assert auth.mint_token("c", "dev-1") == ""          # no token issued
    assert auth.verify_token("c", "dev-1", "") is True   # passes without token
    assert auth.device_allowed("dev-3") is False
