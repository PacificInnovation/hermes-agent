"""Tests for the Kai gateway ingest endpoint (gateway/kai_ingest.py).

Covers the hex HMAC-v0 verifier (mirrors the jarvis lib/slack/gateway-signature
suite) and the aiohttp handler: fail-closed 503, 413 size cap, 401 bad sig, 400
bad JSON, non-event_callback skip, 503 when the Slack adapter is absent, and the
200 dispatch path (inner event injected, envelope team_id patched, ts preserved).
"""

import asyncio
import hashlib
import hmac
import json
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform
from gateway import kai_ingest
from gateway.kai_ingest import (
    GATEWAY_SIG_REPLAY_WINDOW_SEC,
    KaiIngestServer,
    verify_gateway_signature,
)

SECRET = "gw_brain_secret"
NOW = 1_780_000_000


def _sign(body: bytes, ts: int, secret: str = SECRET) -> str:
    mac = hmac.new(
        secret.encode("utf-8"),
        b"v0:" + str(ts).encode("utf-8") + b":" + body,
        hashlib.sha256,
    )
    return "v0=" + mac.hexdigest()


def _signed_headers(body: bytes, ts: int | None = None, secret: str = SECRET) -> dict:
    ts = int(time.time()) if ts is None else ts
    return {
        "x-kai-gateway-signature": _sign(body, ts, secret),
        "x-kai-gateway-timestamp": str(ts),
    }


# --------------------------------------------------------------------------- #
# verify_gateway_signature unit tests (clock injected via now_fn)
# --------------------------------------------------------------------------- #

def _now() -> float:
    return NOW


BODY = b'{"type":"event_callback","event_id":"Ev1"}'


def test_verify_valid():
    h = {"x-kai-gateway-signature": _sign(BODY, NOW), "x-kai-gateway-timestamp": str(NOW)}
    assert verify_gateway_signature(BODY, h, SECRET, _now) is None


def test_verify_tampered_body():
    h = {"x-kai-gateway-signature": _sign(BODY, NOW), "x-kai-gateway-timestamp": str(NOW)}
    assert verify_gateway_signature(BODY + b"x", h, SECRET, _now) == "signature mismatch"


def test_verify_wrong_secret():
    h = {
        "x-kai-gateway-signature": _sign(BODY, NOW, "other"),
        "x-kai-gateway-timestamp": str(NOW),
    }
    assert verify_gateway_signature(BODY, h, SECRET, _now) == "signature mismatch"


def test_verify_missing_signature_header():
    h = {"x-kai-gateway-timestamp": str(NOW)}
    assert verify_gateway_signature(BODY, h, SECRET, _now) == "missing x-kai-gateway-signature header"


def test_verify_missing_timestamp_header():
    h = {"x-kai-gateway-signature": _sign(BODY, NOW)}
    assert verify_gateway_signature(BODY, h, SECRET, _now) == "missing x-kai-gateway-timestamp header"


def test_verify_empty_secret():
    h = {"x-kai-gateway-signature": _sign(BODY, NOW), "x-kai-gateway-timestamp": str(NOW)}
    assert verify_gateway_signature(BODY, h, "", _now) == "secret empty"


def test_verify_stale_timestamp():
    stale = NOW - GATEWAY_SIG_REPLAY_WINDOW_SEC - 1
    h = {"x-kai-gateway-signature": _sign(BODY, stale), "x-kai-gateway-timestamp": str(stale)}
    assert "out of window" in verify_gateway_signature(BODY, h, SECRET, _now)


def test_verify_future_timestamp():
    future = NOW + GATEWAY_SIG_REPLAY_WINDOW_SEC + 1
    h = {"x-kai-gateway-signature": _sign(BODY, future), "x-kai-gateway-timestamp": str(future)}
    assert "out of window" in verify_gateway_signature(BODY, h, SECRET, _now)


def test_verify_non_numeric_timestamp():
    h = {"x-kai-gateway-signature": _sign(BODY, NOW), "x-kai-gateway-timestamp": "1.7e9"}
    assert verify_gateway_signature(BODY, h, SECRET, _now) == "non-numeric timestamp"


def test_verify_no_v0_segment():
    h = {"x-kai-gateway-signature": "v9=deadbeef", "x-kai-gateway-timestamp": str(NOW)}
    assert verify_gateway_signature(BODY, h, SECRET, _now) == "no v0 signature in header"


def test_verify_wrong_hex_length_no_throw():
    h = {"x-kai-gateway-signature": "v0=abc", "x-kai-gateway-timestamp": str(NOW)}
    # Must return a clean mismatch, not raise.
    assert verify_gateway_signature(BODY, h, SECRET, _now) == "signature mismatch"


def test_verify_malformed_header_no_delimiter():
    h = {"x-kai-gateway-signature": "deadbeef", "x-kai-gateway-timestamp": str(NOW)}
    assert verify_gateway_signature(BODY, h, SECRET, _now) == "malformed signature header"


# --------------------------------------------------------------------------- #
# Handler integration tests (aiohttp TestClient)
# --------------------------------------------------------------------------- #

class _FakeSlack:
    def __init__(self):
        self.events = []
        self.called = asyncio.Event()

    async def _handle_slack_message(self, event):
        self.events.append(event)
        self.called.set()


class _FakeRunner:
    def __init__(self, slack=None):
        self.adapters = {}
        if slack is not None:
            self.adapters[Platform.SLACK] = slack


def _app(runner):
    return KaiIngestServer(runner).build_app()


def _env_body(team_at_envelope=True, inner_team=None) -> bytes:
    event = {"type": "app_mention", "ts": "1700000000.000100", "user": "U1", "channel": "C1", "text": "hi"}
    if inner_team is not None:
        event["team"] = inner_team
    envelope = {"type": "event_callback", "event": event}
    if team_at_envelope:
        envelope["team_id"] = "T1"
    return json.dumps(envelope).encode("utf-8")


@pytest.mark.asyncio
async def test_503_when_secret_unset(monkeypatch):
    monkeypatch.delenv("GATEWAY_BRAIN_SECRET", raising=False)
    slack = _FakeSlack()
    async with TestClient(TestServer(_app(_FakeRunner(slack)))) as client:
        body = _env_body()
        resp = await client.post("/ingest/slack", data=body, headers=_signed_headers(body))
        assert resp.status == 503
    assert slack.events == []


@pytest.mark.asyncio
async def test_401_bad_signature(monkeypatch):
    monkeypatch.setenv("GATEWAY_BRAIN_SECRET", SECRET)
    slack = _FakeSlack()
    async with TestClient(TestServer(_app(_FakeRunner(slack)))) as client:
        body = _env_body()
        bad = {"x-kai-gateway-signature": "v0=bad", "x-kai-gateway-timestamp": str(int(time.time()))}
        resp = await client.post("/ingest/slack", data=body, headers=bad)
        assert resp.status == 401
    assert slack.events == []


@pytest.mark.asyncio
async def test_413_oversized_before_auth(monkeypatch):
    monkeypatch.setenv("GATEWAY_BRAIN_SECRET", SECRET)
    monkeypatch.setattr(kai_ingest, "MAX_BODY_BYTES", 50)
    slack = _FakeSlack()
    async with TestClient(TestServer(_app(_FakeRunner(slack)))) as client:
        body = b"x" * 200  # > 50, unsigned: proves size cap fires before sig
        resp = await client.post("/ingest/slack", data=body, headers={"content-type": "application/json"})
        assert resp.status == 413
    assert slack.events == []


@pytest.mark.asyncio
async def test_200_dispatch_patches_team_id_and_preserves_ts(monkeypatch):
    monkeypatch.setenv("GATEWAY_BRAIN_SECRET", SECRET)
    slack = _FakeSlack()
    async with TestClient(TestServer(_app(_FakeRunner(slack)))) as client:
        body = _env_body(team_at_envelope=True)  # inner has no team
        resp = await client.post("/ingest/slack", data=body, headers=_signed_headers(body))
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "dispatched": True}
        await asyncio.wait_for(slack.called.wait(), timeout=2)
    assert len(slack.events) == 1
    inner = slack.events[0]
    assert inner["type"] == "app_mention"
    assert inner["team_id"] == "T1"        # patched from the envelope
    assert inner["ts"] == "1700000000.000100"  # dedup key preserved


@pytest.mark.asyncio
async def test_does_not_overwrite_existing_inner_team(monkeypatch):
    monkeypatch.setenv("GATEWAY_BRAIN_SECRET", SECRET)
    slack = _FakeSlack()
    async with TestClient(TestServer(_app(_FakeRunner(slack)))) as client:
        body = _env_body(team_at_envelope=True, inner_team="TX")
        resp = await client.post("/ingest/slack", data=body, headers=_signed_headers(body))
        assert resp.status == 200
        await asyncio.wait_for(slack.called.wait(), timeout=2)
    assert slack.events[0]["team"] == "TX"      # inner team untouched
    assert "team_id" not in slack.events[0]      # not patched when team present


@pytest.mark.asyncio
async def test_400_invalid_json(monkeypatch):
    monkeypatch.setenv("GATEWAY_BRAIN_SECRET", SECRET)
    slack = _FakeSlack()
    async with TestClient(TestServer(_app(_FakeRunner(slack)))) as client:
        body = b"{bad json"
        resp = await client.post("/ingest/slack", data=body, headers=_signed_headers(body))
        assert resp.status == 400
    assert slack.events == []


@pytest.mark.asyncio
async def test_skips_non_event_callback(monkeypatch):
    monkeypatch.setenv("GATEWAY_BRAIN_SECRET", SECRET)
    slack = _FakeSlack()
    async with TestClient(TestServer(_app(_FakeRunner(slack)))) as client:
        body = json.dumps({"type": "url_verification", "challenge": "x"}).encode("utf-8")
        resp = await client.post("/ingest/slack", data=body, headers=_signed_headers(body))
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "skipped": "not_event_callback"}
    assert slack.events == []


@pytest.mark.asyncio
async def test_503_when_slack_adapter_absent(monkeypatch):
    monkeypatch.setenv("GATEWAY_BRAIN_SECRET", SECRET)
    async with TestClient(TestServer(_app(_FakeRunner(slack=None)))) as client:
        body = _env_body()
        resp = await client.post("/ingest/slack", data=body, headers=_signed_headers(body))
        assert resp.status == 503


@pytest.mark.asyncio
async def test_skips_event_without_ts(monkeypatch):
    # An inner event with no top-level `ts` would bypass hermes' ts-keyed dedup,
    # so the ingest rejects it rather than inject an undedupable event.
    monkeypatch.setenv("GATEWAY_BRAIN_SECRET", SECRET)
    slack = _FakeSlack()
    async with TestClient(TestServer(_app(_FakeRunner(slack)))) as client:
        body = json.dumps(
            {"type": "event_callback", "team_id": "T1", "event": {"type": "reaction_added", "user": "U1"}}
        ).encode("utf-8")
        resp = await client.post("/ingest/slack", data=body, headers=_signed_headers(body))
        assert resp.status == 200
        assert await resp.json() == {"ok": True, "skipped": "no_ts"}
    assert slack.events == []


@pytest.mark.asyncio
async def test_502_when_dispatch_raises(monkeypatch):
    # A transient failure in the Slack pipeline must surface as 5xx so the
    # gateway re-forwards (at-least-once), not be swallowed after a 200.
    monkeypatch.setenv("GATEWAY_BRAIN_SECRET", SECRET)

    class _RaisingSlack:
        async def _handle_slack_message(self, event):
            raise RuntimeError("slack api down")

    async with TestClient(TestServer(_app(_FakeRunner(_RaisingSlack())))) as client:
        body = _env_body()
        resp = await client.post("/ingest/slack", data=body, headers=_signed_headers(body))
        assert resp.status == 502


@pytest.mark.asyncio
async def test_health(monkeypatch):
    async with TestClient(TestServer(_app(_FakeRunner()))) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        assert (await resp.json())["status"] == "ok"
