"""Sprint 24 Phase 2-brains — hermes's ingest endpoint for the unified Kai gateway.

The unified Kai Slack gateway (its own Vercel project) receives all Slack events,
routes acquisitions-surface ones to hermes, and POSTs the FULL Slack
``event_callback`` envelope signed with ``GATEWAY_BRAIN_SECRET`` (hex HMAC-v0,
mirroring the jarvis brain ingest at ``lib/slack/gateway-signature.ts``). This
module:

1. Runs an always-on aiohttp server bound to Railway's ``$PORT`` so the gateway's
   ``BRAIN_ACQ_URL`` can reach it. Started from ``gateway/run.py::start_gateway``.
2. Fail-closed ``503`` while ``GATEWAY_BRAIN_SECRET`` is unset — deployed-but-inert
   until cutover, exactly like the jarvis side.
3. After verifying the HMAC, injects the INNER Slack event into the EXISTING Slack
   pipeline via ``runner.adapters[Platform.SLACK]._handle_slack_message`` so a
   forwarded event processes identically to a Socket-Mode-delivered one.

NOT live until: the gateway is provisioned, ``BRAIN_ACQ_URL`` points here, the acq
channels are flipped to ``surface='acq'``, AND hermes Socket Mode is turned OFF at
cutover (else events double-process — though hermes also dedups by event ``ts``).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from typing import Callable, Optional, Set

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - aiohttp is a gateway dependency
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform

logger = logging.getLogger(__name__)

# Match the gateway fanout's MAX_BODY_BYTES (jarvis gateway/src/limits.ts) so the
# ingest never 413s a body the gateway already signed and sent.
MAX_BODY_BYTES = 1_000_000

# 5-minute replay window (symmetric — future drift also rejected). Mirrors the
# jarvis verifier's GATEWAY_SIG_REPLAY_WINDOW_SEC.
GATEWAY_SIG_REPLAY_WINDOW_SEC = 5 * 60

# Canonical unsigned integer (ASCII digits only) — mirror the JS ``^\d+$`` check
# so the exact header bytes we sign round-trip.
_TS_RE = re.compile(r"[0-9]+")


def verify_gateway_signature(
    raw_body: bytes,
    headers,
    secret: str,
    now_fn: Callable[[], float] = time.time,
) -> Optional[str]:
    """Verify the gateway's hex HMAC-v0 signature.

    Returns ``None`` on success, or a short error string (for the 401 log).
    Headers (set by the gateway fanout):
      ``x-kai-gateway-signature: v0=<hex hmac-sha256>``
      ``x-kai-gateway-timestamp: <unix seconds>``
    Signed bytes: ``v0:{timestamp}:{raw_body}`` keyed by ``secret``. Mirrors
    jarvis ``verifyGatewaySignature`` (hex, canonical-int ts, symmetric replay
    window, length-guarded timing-safe compare).
    """
    sig_header = headers.get("x-kai-gateway-signature")
    ts_str = headers.get("x-kai-gateway-timestamp")

    if not sig_header:
        return "missing x-kai-gateway-signature header"
    if not ts_str:
        return "missing x-kai-gateway-timestamp header"
    if not secret:
        return "secret empty"

    if not _TS_RE.fullmatch(ts_str):
        return "non-numeric timestamp"
    timestamp = int(ts_str)
    now_sec = int(now_fn())
    if abs(now_sec - timestamp) > GATEWAY_SIG_REPLAY_WINDOW_SEC:
        return f"timestamp out of window (drift={now_sec - timestamp}s)"

    eq_idx = sig_header.find("=")
    if eq_idx < 0:
        return "malformed signature header"
    version = sig_header[:eq_idx]
    provided_hex = sig_header[eq_idx + 1 :]
    if version != "v0" or not provided_hex:
        return "no v0 signature in header"

    expected_hex = hmac.new(
        secret.encode("utf-8"),
        b"v0:" + ts_str.encode("utf-8") + b":" + raw_body,
        hashlib.sha256,
    ).hexdigest()

    # compare_digest already tolerates unequal lengths, but keep the explicit
    # guard for parity with the jarvis verifier and a clean mismatch message.
    if len(expected_hex) != len(provided_hex):
        return "signature mismatch"
    if not hmac.compare_digest(expected_hex, provided_hex):
        return "signature mismatch"
    return None


class KaiIngestServer:
    """Always-on aiohttp server that injects gateway-forwarded Slack events into
    the live Slack adapter's pipeline.

    Holds a ``GatewayRunner`` reference so the request handler can reach
    ``runner.adapters[Platform.SLACK]``. Lifecycle (start/stop) is driven by
    ``gateway/run.py::start_gateway``.
    """

    def __init__(self, runner, host: str = "0.0.0.0", port: int = 0) -> None:
        self._runner = runner
        self._host = host
        self._port = port
        self._app_runner = None  # web.AppRunner once started
        self._bg: Set[asyncio.Task] = set()

    def build_app(self):
        """Build the aiohttp application (separated out for testability)."""
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/ingest/slack", self._handle_ingest)
        return app

    async def start(self) -> None:
        if not AIOHTTP_AVAILABLE:
            logger.error("[slack-ingest] aiohttp unavailable; ingest server not started")
            return
        self._app_runner = web.AppRunner(self.build_app())
        await self._app_runner.setup()
        site = web.TCPSite(self._app_runner, self._host, self._port)
        await site.start()
        logger.info(
            "[slack-ingest] listening on %s:%d (POST /ingest/slack)",
            self._host,
            self._port,
        )

    async def stop(self) -> None:
        if self._app_runner is not None:
            await self._app_runner.cleanup()
            self._app_runner = None
            logger.info("[slack-ingest] stopped")

    async def _handle_health(self, request):
        return web.json_response({"status": "ok", "service": "kai-slack-ingest"})

    async def _handle_ingest(self, request):
        # 1. Fail closed until the shared secret is configured (deployed-but-inert).
        secret = os.environ.get("GATEWAY_BRAIN_SECRET")
        if not secret:
            logger.error("[slack-ingest] GATEWAY_BRAIN_SECRET not set; refusing all requests")
            return web.json_response({"error": "ingest auth not configured"}, status=503)

        # 2. Bound body size BEFORE hashing — cheap reject. Check Content-Length
        #    first, then the actual read (Content-Length can lie / be absent).
        if (request.content_length or 0) > MAX_BODY_BYTES:
            return web.json_response({"error": "payload too large"}, status=413)
        try:
            raw_body = await request.read()
        except Exception as e:  # pragma: no cover - aiohttp read failure
            logger.error("[slack-ingest] failed to read body: %s", e)
            return web.json_response({"error": "bad request"}, status=400)
        if len(raw_body) > MAX_BODY_BYTES:
            return web.json_response({"error": "payload too large"}, status=413)

        # 3. Verify the gateway HMAC.
        sig_error = verify_gateway_signature(raw_body, request.headers, secret)
        if sig_error:
            logger.warning("[slack-ingest] auth fail: %s", sig_error)
            return web.json_response({"error": "unauthorized"}, status=401)

        # 4. Parse + shape-guard before reaching the pipeline.
        try:
            payload = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError):
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"ok": True, "skipped": "not_event_callback"})
        event = payload.get("event")
        if (
            payload.get("type") != "event_callback"
            or not isinstance(event, dict)
            or not isinstance(event.get("type"), str)
        ):
            return web.json_response({"ok": True, "skipped": "not_event_callback"})

        # 5. Reach the live Slack adapter. Retryable 503 if it isn't connected
        #    (hermes is up, but the Slack platform hasn't come up yet).
        slack = self._runner.adapters.get(Platform.SLACK)
        if slack is None:
            logger.error("[slack-ingest] Slack adapter not connected; cannot dispatch")
            return web.json_response({"error": "slack adapter not connected"}, status=503)

        # 6. Inject the INNER event (what Bolt's @app.event handlers receive) into
        #    the native pipeline. _handle_slack_message reads team only from the
        #    inner event, so patch the envelope team_id down (hermes is
        #    multi-workspace). The inner event already carries `ts` — the dedup
        #    key that suppresses Slack's at-least-once redeliveries downstream.
        inner = dict(event)
        if not inner.get("team") and not inner.get("team_id"):
            team_id = payload.get("team_id")
            if team_id:
                inner["team_id"] = team_id

        # create_task + ack: handle_message itself returns fast (it spawns the
        # agent turn as a background task), but _handle_slack_message does
        # pre-dispatch I/O (thread-context fetch, media download). Decouple the
        # ack from that, mirroring the webhook adapter. A failure inside the task
        # is logged; the gateway's at-least-once + the `ts` dedup cover redelivery.
        task = asyncio.create_task(slack._handle_slack_message(inner))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

        return web.json_response({"ok": True, "dispatched": True})
