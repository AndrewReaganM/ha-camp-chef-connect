"""Per-grill runtime object shared by the entities."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .connection import GrillConnection
from .const import DEFAULT_MAX_TEMP_F, DEFAULT_MIN_TEMP_F, DOMAIN
from .protocol import (
    ADJUSTABLE_MODES,
    LIT_MODES,
    GrillState,
    WsId,
    encode_run_mode,
    encode_standby_mode,
    read_frame,
    write_frame,
)


class CampChefGrill:
    """One grill: cloud metadata plus a live websocket relay."""

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        info: dict[str, Any],
        get_token: Callable[[], Awaitable[str]],
        on_auth_failed: Callable[[], None],
    ) -> None:
        self.hass = hass
        self.info = info
        self.grill_id: str = info["_id"]
        self._listeners: list[CALLBACK_TYPE] = []
        self.connection = GrillConnection(
            session, self.grill_id, get_token, self._notify, on_auth_failed
        )

    @property
    def state(self) -> GrillState:
        return self.connection.state

    @property
    def available(self) -> bool:
        return self.connection.available

    @property
    def name(self) -> str:
        return self.info.get("name") or f"Camp Chef {self.model or 'grill'}"

    @property
    def model(self) -> str | None:
        return self.state.model_code or self.info.get("model")

    @property
    def min_temp_f(self) -> int:
        return self.state.min_temp_f or DEFAULT_MIN_TEMP_F

    @property
    def max_temp_f(self) -> int:
        return self.state.max_temp_f or DEFAULT_MAX_TEMP_F

    @property
    def is_lit(self) -> bool:
        return self.state.mode in LIT_MODES

    def start(self) -> None:
        self.connection.start(
            lambda coro: self.hass.async_create_background_task(
                coro, f"{DOMAIN} grill {self.grill_id}"
            )
        )

    async def stop(self) -> None:
        await self.connection.stop()

    async def async_wait_for_model(self, timeout: float) -> None:
        """Wait until the grill reports its model, or give up after ``timeout``."""
        if self.state.model_code:
            return
        reported = asyncio.Event()
        remove = self.async_add_listener(
            lambda: reported.set() if self.state.model_code else None
        )
        waiter = asyncio.ensure_future(reported.wait())
        # Stop waiting early if the connection gives up (e.g. rejected token).
        pending = {waiter, self.connection.task} - {None}
        try:
            await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            remove()

    @callback
    def async_add_listener(self, update: CALLBACK_TYPE) -> CALLBACK_TYPE:
        self._listeners.append(update)

        @callback
        def remove() -> None:
            self._listeners.remove(update)

        return remove

    def _notify(self) -> None:
        for update in list(self._listeners):
            update()

    async def async_set_run(self, set_temp_f: int, smoke_level: int) -> None:
        """Change set temperature and smoke on a grill that is already running."""
        if self.state.mode not in ADJUSTABLE_MODES:
            raise HomeAssistantError(
                "The grill must already be lit and running to change its temperature; "
                "start it at the controller"
            )
        if not self.min_temp_f <= set_temp_f <= self.max_temp_f:
            raise HomeAssistantError(
                f"Temperature must be between {self.min_temp_f} and {self.max_temp_f} °F"
            )
        payload = encode_run_mode(set_temp_f, smoke_level, self.state.run_mode_trailer)
        await self._write(write_frame(WsId.MODE, payload))

    async def async_shutdown(self) -> None:
        """Put the grill into its shutdown (standby) cycle."""
        await self._write(write_frame(WsId.MODE, encode_standby_mode()))

    async def _write(self, frame: bytes) -> None:
        try:
            await self.connection.send_frames([frame, read_frame(WsId.MODE)])
        except (ConnectionError, aiohttp.ClientError) as err:
            raise HomeAssistantError(f"Could not reach the grill: {err}") from err
