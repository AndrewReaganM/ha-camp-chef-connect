"""Websocket relay to one grill through the Camp Chef cloud."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import time
import uuid

import aiohttp

from .api import CampChefAuthError
from .const import WS_URL
from .protocol import (
    GrillState,
    apply_frame,
    decode_message,
    encode_message,
    initial_read_frames,
    telemetry_read_frames,
)

_LOGGER = logging.getLogger(__name__)

KEEPALIVE_INTERVAL = 30
POLL_INTERVAL = 60
# The grill is treated as offline when it has not answered for this long.
STALE_AFTER = 3 * POLL_INTERVAL
RECONNECT_MIN = 5
RECONNECT_MAX = 300


class GrillConnection:
    """Keeps a multiplexed websocket channel open to one grill."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        grill_id: str,
        get_token: Callable[[], Awaitable[str]],
        on_update: Callable[[], None],
        on_auth_failed: Callable[[], None],
    ) -> None:
        self._session = session
        self._grill_id = grill_id
        self._get_token = get_token
        self._on_update = on_update
        self._on_auth_failed = on_auth_failed
        self.state = GrillState()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._channel: str | None = None
        self.task: asyncio.Task[None] | None = None
        self._last_frame = 0.0

    @property
    def path(self) -> str:
        return f"/client-socket/{self._grill_id}"

    @property
    def available(self) -> bool:
        """True while the socket is open and the grill answered recently."""
        return self._ws is not None and time.monotonic() - self._last_frame < STALE_AFTER

    def start(self, create_task: Callable[[Awaitable[None]], asyncio.Task[None]]) -> None:
        self.task = create_task(self._run())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None

    async def send_frames(self, frames: list[bytes]) -> None:
        """Send raw frames to the grill."""
        ws, channel = self._ws, self._channel
        if ws is None or channel is None or ws.closed:
            raise ConnectionError("Not connected to the grill")
        await ws.send_str(json.dumps({"channel": channel, "data": encode_message(frames)}))

    async def _run(self) -> None:
        delay = RECONNECT_MIN
        while True:
            started = time.monotonic()
            try:
                await self._session_once()
            except asyncio.CancelledError:
                raise
            except CampChefAuthError as err:
                _LOGGER.warning("Camp Chef rejected the session token: %s", err)
                self._on_auth_failed()
                return
            except (aiohttp.ClientError, TimeoutError, ConnectionError) as err:
                _LOGGER.debug("Grill %s socket error: %s", self._grill_id, err)
            except Exception:
                _LOGGER.exception("Unexpected error in grill %s socket", self._grill_id)
            finally:
                self._ws = None
                self._channel = None
                self._on_update()
            if time.monotonic() - started > RECONNECT_MAX:
                delay = RECONNECT_MIN
            _LOGGER.debug("Reconnecting to grill %s in %ss", self._grill_id, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)

    async def _session_once(self) -> None:
        channel = str(uuid.uuid4())
        # Nothing at all for STALE_AFTER (not even a channel ack) means a dead socket.
        async with self._session.ws_connect(
            WS_URL,
            timeout=aiohttp.ClientWSTimeout(ws_receive=STALE_AFTER, ws_close=10),
            heartbeat=None,
        ) as ws:
            self._ws = ws
            await self._open_channel(ws, channel)
            background = asyncio.gather(self._keepalive(ws), self._poll())
            try:
                await self._receive(ws, channel)
            finally:
                background.cancel()
                try:
                    await background
                except asyncio.CancelledError:
                    pass

    async def _open_channel(self, ws: aiohttp.ClientWebSocketResponse, channel: str) -> None:
        """Ask the server to open the grill channel.

        Frames may only be sent once the server answers with its own
        ``start`` message; anything sent earlier is rejected with
        "Channel information not found".
        """
        self._channel = None
        token = await self._get_token()
        await ws.send_str(
            json.dumps(
                {
                    "channel": channel,
                    "path": self.path,
                    "queryParams": {"jwt": [token]},
                    "start": True,
                }
            )
        )

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse, channel: str) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
                continue
            text = msg.data.strip()
            if not text:
                continue
            try:
                envelope = json.loads(text)
            except ValueError:
                continue
            if not isinstance(envelope, dict) or envelope.get("channel") != channel:
                continue
            if error := envelope.get("error"):
                if "auth" in error.lower() or "token" in error.lower():
                    raise CampChefAuthError(error)
                raise ConnectionError(f"Server closed grill channel: {error}")
            if envelope.get("start"):
                _LOGGER.debug("Grill %s channel open", self._grill_id)
                self._channel = channel
                await self.send_frames(initial_read_frames())
                continue
            if envelope.get("end"):
                # The official client reopens the channel when the server ends it.
                _LOGGER.debug("Grill %s channel ended; reopening", self._grill_id)
                await self._open_channel(ws, channel)
                continue
            if data := envelope.get("data"):
                changed = False
                for frame in decode_message(data):
                    _LOGGER.debug("Grill %s frame %s", self._grill_id, frame.hex())
                    changed |= apply_frame(self.state, frame)
                if changed:
                    self._last_frame = time.monotonic()
                    self._on_update()

    async def _keepalive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not ws.closed:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            await ws.send_str(" ")

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            if self._channel is not None:
                await self.send_frames(telemetry_read_frames(self.state.probe_count))
            # Let entities flip to unavailable if the grill stops answering.
            self._on_update()
