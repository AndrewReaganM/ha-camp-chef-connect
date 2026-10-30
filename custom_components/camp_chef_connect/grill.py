"""Per-grill runtime object shared by the entities."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Any

import aiohttp

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .connection import GrillConnection
from .const import DEFAULT_MAX_TEMP_F, DEFAULT_MIN_TEMP_F, DOMAIN
from .protocol import (
    ADJUSTABLE_MODES,
    LIT_MODES,
    GrillModeType,
    GrillState,
    WsId,
    encode_run_mode,
    encode_standby_mode,
    read_frame,
    write_frame,
)

_LOGGER = logging.getLogger(__name__)


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
        # Until the grill reports its own model, use the cloud's record so the
        # probe count is right even when the grill is off.
        if isinstance(model := info.get("model"), str) and len(model) == 4:
            self.connection.state.model_code = model

    @property
    def cloud_reports_online(self) -> bool:
        """The cloud's online flag from setup; False only if it says offline."""
        return info_online if (info_online := self.info.get("isOnline")) is not None else True

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

    async def async_wait_for_first_report(self, timeout: float) -> None:
        """Wait until the grill reports in, or give up after ``timeout``.

        Skipped when the cloud already says the grill is off, which is most
        of the time, so startup is not held up waiting for it.
        """
        if self.available or not self.cloud_reports_online:
            return
        reported = asyncio.Event()
        remove = self.async_add_listener(
            lambda: reported.set() if self.available or self.connection.reported_offline else None
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

    async def async_set_run(
        self, set_temp_f: int, smoke_level: int, requested_by: str = "unknown"
    ) -> None:
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
        _LOGGER.debug(
            "Grill %s: %s requested set %s°F smoke %s (was %s°F smoke %s)",
            self.grill_id,
            requested_by,
            set_temp_f,
            smoke_level,
            self.state.set_temp_f,
            self.state.smoke_level,
        )
        payload = encode_run_mode(set_temp_f, smoke_level, self.state.run_mode_trailer)
        await self._write(write_frame(WsId.MODE, payload))
        # Show the new values now rather than ~1 s later when the grill
        # confirms; dashboard controls otherwise snap back and resend the
        # old value. The read-back sent with the write corrects any refusal.
        self.state.mode = GrillModeType.RUN
        self.state.set_temp_f = set_temp_f
        self.state.smoke_level = smoke_level
        self._notify()

    async def async_shutdown(self, requested_by: str = "unknown") -> None:
        """Put the grill into its shutdown (standby) cycle."""
        _LOGGER.debug("Grill %s: %s requested shutdown", self.grill_id, requested_by)
        await self._write(write_frame(WsId.MODE, encode_standby_mode()))
        self.state.mode = GrillModeType.STANDBY
        self.state.set_temp_f = None
        self.state.smoke_level = None
        self._notify()

    async def _write(self, frame: bytes) -> None:
        try:
            await self.connection.send_frames([frame, read_frame(WsId.MODE)])
        except (ConnectionError, aiohttp.ClientError) as err:
            raise HomeAssistantError(f"Could not reach the grill: {err}") from err
