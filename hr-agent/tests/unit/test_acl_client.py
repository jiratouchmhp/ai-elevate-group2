"""Identity envelope (X-HR-Identity) — the ACL derives identity only from this (SDD D7/D9)."""

from __future__ import annotations

import time

from app.integration import acl_client
from app.integration.acl import Ctx

C = Ctx(employee_id="EMP001", session_id="hrs-1", invocation_id="inv-1", agent_id="workweek_agent",
        correlation_id="corr-1")


def test_seal_unseal_roundtrip():
    got = acl_client.unseal(acl_client.seal(C))
    assert got == C


def test_tampered_body_rejected():
    env = acl_client.seal(C)
    body, mac = env.rsplit(".", 1)
    forged = acl_client.seal(Ctx("EMP004", "hrs-1", "inv-1", "workweek_agent", "corr-1")).rsplit(".", 1)[0]
    assert acl_client.unseal(f"{forged}.{mac}") is None
    assert acl_client.unseal(f"{body}.{'0' * len(mac)}") is None


def test_missing_or_garbage_rejected():
    assert acl_client.unseal(None) is None
    assert acl_client.unseal("") is None
    assert acl_client.unseal("no-dot") is None


def test_expired_envelope_rejected(monkeypatch):
    env = acl_client.seal(C)
    real = time.time
    monkeypatch.setattr(acl_client.time, "time", lambda: real() + acl_client.ENVELOPE_TTL_S + 5)
    assert acl_client.unseal(env) is None


def test_wrong_secret_rejected(monkeypatch):
    env = acl_client.seal(C)
    monkeypatch.setattr(acl_client.config, "IDENTITY_ENVELOPE_SECRET", b"other-secret")
    assert acl_client.unseal(env) is None
