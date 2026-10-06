"""Exercise the production Google verifier with locally signed RSA tokens and public keys."""

import json
import time
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fl_common.errors import PlatformError
from fl_scheduler.auth.google import GoogleIdentity, GoogleSettings
from google.auth import crypt, jwt
from pydantic import SecretStr


@pytest.fixture
def google_tokens(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = crypt.RSASigner(key, key_id="test-key")
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    fetched = []

    def certificates(url, **kwargs):
        fetched.append((url, kwargs))
        return SimpleNamespace(status=200, data=json.dumps({"test-key": public}).encode())

    monkeypatch.setattr("fl_scheduler.auth.google.Request", lambda: certificates)
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": "fletcherlake-client",
        "iat": now,
        "exp": now + 600,
        "sub": "google-subject",
        "email": "alice@example.edu",
        "email_verified": True,
        "hd": "example.edu",
        "nonce": "nonce",
    }
    return signer, claims, fetched


async def exchange_token(token):
    settings = GoogleSettings(
        "fletcherlake-client",
        SecretStr("client-secret"),
        "https://scheduler.test/api/auth/callback",
        "example.edu",
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"id_token": token}))
    async with httpx.AsyncClient(transport=transport) as client:
        return await GoogleIdentity(settings, client).exchange("code", "nonce", "verifier")


async def test_production_google_verifier_accepts_signature_and_bounds_key_fetch(google_tokens):
    signer, claims, fetched = google_tokens
    token = jwt.encode(signer, claims).decode()
    identity = await exchange_token(token)
    assert identity.subject == claims["sub"]
    assert fetched[0][0] == "https://www.googleapis.com/oauth2/v1/certs"
    assert fetched[0][1]["timeout"] == 15


@pytest.mark.parametrize("failure", ["signature", "audience", "issuer", "expiry"])
async def test_production_google_verifier_rejects_invalid_tokens(google_tokens, failure):
    signer, claims, _ = google_tokens
    if failure == "audience":
        claims["aud"] = "different-client"
    elif failure == "issuer":
        claims["iss"] = "https://evil.test"
    elif failure == "expiry":
        claims["iat"], claims["exp"] = int(time.time()) - 600, int(time.time()) - 300
    token = jwt.encode(signer, claims).decode()
    if failure == "signature":
        parts = token.split(".")
        parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
        token = ".".join(parts)
    with pytest.raises(PlatformError) as error:
        await exchange_token(token)
    assert error.value.code == "UNAUTHENTICATED"
