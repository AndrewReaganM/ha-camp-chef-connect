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
    WifiStatus,
    WsId,
    apply_frame,
    decode_message,
    encode_message,
    initial_read_frames,
    read_frame,
    telemetry_read_frames,
)

_LOGGER = logging.getLogger(__name__)

KEEPALIVE_INTERVAL = 30
POLL_INTERVAL = 60
# The grill is treated as offline when it has not answered for this long.
# A dropped socket alone does not make it unavailable; this does.
STALE_AFTER = 3 * POLL_INTERVAL
RECONNECT_MIN = 5
RECONNECT_MAX = 300


class GrillConnection:
    """Keeps a multiplexed websocket channel open to one grill.

    The grill is usually switched off. The cloud keeps the channel open
    while it is offline and announces changes with a WIFI_STATUS frame
    (``00`` offline, ``02`` connected), so the channel is simply held open
    and the grill is re-read as soon as it reports in.
    """

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
        self._reported_offline = False
        self._was_online = False
        self._opened = False
        self._stale_timer: asyncio.TimerHandle | None = None

    @property
    def path(self) -> str:
        return f"/client-socket/{self._grill_id}"

    @property
    def available(self) -> bool:
        """True while the grill is online and answered recently.

        Survives brief socket reconnects; only the grill going quiet for
        STALE_AFTER, or the cloud reporting it offline, makes it False.
        """
        return (
            not self._reported_offline and time.monotonic() - self._last_frame < STALE_AFTER
        )

    @property
    def reported_offline(self) -> bool:
        """The cloud has said the grill is offline."""
        return self._reported_offline

    def start(self, create_task: Callable[[Awaitable[None]], asyncio.Task[None]]) -> None:
        self.task = create_task(self._run())

    async def stop(self) -> None:
        self._cancel_stale_timer()
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
        _LOGGER.debug("Grill %s send %s", self._grill_id, " ".join(f.hex() for f in frames))
        await ws.send_str(json.dumps({"channel": channel, "data": encode_message(frames)}))

    async def _run(self) -> None:
        delay = RECONNECT_MIN
        failures = 0
        while True:
            self._opened = False
            try:
                await self._session_once()
                err_text = "connection closed"
            except asyncio.CancelledError:
                raise
            except CampChefAuthError as err:
                _LOGGER.warning("Camp Chef rejected the session token: %s", err)
                self._on_auth_failed()
                return
            except (aiohttp.ClientError, TimeoutError, ConnectionError) as err:
                err_text = str(err) or type(err).__name__
            except Exception:
                _LOGGER.exception("Unexpected error in grill %s socket", self._grill_id)
                err_text = "unexpected error"
            finally:
                self._ws = None
                self._channel = None
                self._schedule_stale_check()
                self._on_update()
            if self._opened:
                delay, failures = RECONNECT_MIN, 0
            failures += 1
            if failures == 1:
                _LOGGER.warning(
                    "Lost connection to the Camp Chef cloud for grill %s (%s); reconnecting",
                    self._grill_id,
                    err_text,
                )
            else:
                _LOGGER.debug(
                    "Grill %s reconnect attempt %s failed (%s); next in %ss",
                    self._grill_id,
                    failures,
                    err_text,
                    delay,
                )
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
                if not self._opened:
                    _LOGGER.debug("Grill %s channel open", self._grill_id)
                self._opened = True
                self._channel = channel
                await self.send_frames(initial_read_frames())
                continue
            if envelope.get("end"):
                # The official client reopens the channel when the server ends it.
                _LOGGER.debug("Grill %s channel ended; reopening", self._grill_id)
                await self._open_channel(ws, channel)
                continue
            if data := envelope.get("data"):
                await self._handle_frames(decode_message(data))

    async def _handle_frames(self, frames: list[bytes]) -> None:
        changed = False
        heard_from_grill = False
        came_online = False
        for frame in frames:
            _LOGGER.debug("Grill %s frame %s", self._grill_id, frame.hex())
            if not apply_frame(self.state, frame):
                continue
            changed = True
            if frame[0] != WsId.WIFI_STATUS:
                heard_from_grill = True
            elif self.state.wifi_status == WifiStatus.CONNECTED:
                came_online = self._reported_offline
                self._reported_offline = False
            else:
                self._reported_offline = True
        if heard_from_grill:
            # Telemetry means it is online whatever the last status said.
            self._reported_offline = False
            self._last_frame = time.monotonic()
            self._cancel_stale_timer()
        if not changed:
            return
        self._log_transition()
        self._on_update()
        if came_online and self._channel is not None:
            await self.send_frames(initial_read_frames(self.state.probe_count))

    def _log_transition(self) -> None:
        online = self.available
        if online != self._was_online:
            _LOGGER.info("Grill %s is %s", self._grill_id, "online" if online else "offline")
            self._was_online = online

    async def _keepalive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while not ws.closed:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            await ws.send_str(" ")

    async def _poll(self) -> None:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            if self._channel is None:
                continue
            if self.available:
                await self.send_frames(telemetry_read_frames(self.state.probe_count))
            else:
                # Offline: one cheap read, answered as soon as the grill is back.
                await self.send_frames([read_frame(WsId.MODE)])
            # Let entities flip to unavailable if the grill stops answering.
            self._log_transition()
            self._on_update()

    def _schedule_stale_check(self) -> None:
        """While disconnected, still flip entities once the data goes stale."""
        self._cancel_stale_timer()
        remaining = STALE_AFTER - (time.monotonic() - self._last_frame)
        if remaining > 0:
            self._stale_timer = asyncio.get_running_loop().call_later(
                remaining + 1, self._stale_check
            )

    def _stale_check(self) -> None:
        self._stale_timer = None
        self._log_transition()
        self._on_update()

    def _cancel_stale_timer(self) -> None:
        if self._stale_timer:
            self._stale_timer.cancel()
            self._stale_timer = None
