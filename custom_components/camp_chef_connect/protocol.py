"""Camp Chef Connect wire protocol.

The Camp Chef cloud relays the grill's BLE characteristics over a websocket.
Each frame is ``[wsId, op, payload...]`` where ``wsId`` identifies the
characteristic and the payload uses the same encoding as BLE. Frames are
base64 encoded and several may be joined with ``|`` in one message.

This module is pure Python with no Home Assistant imports so it can be
unit tested in isolation.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from enum import IntEnum

PROBE_FIRST_WS_ID = 16
PROBE_LAST_WS_ID = 31

# Probe readings outside this range mean "no probe plugged in".
PROBE_MIN_VALID_F = -40
PROBE_MAX_VALID_F = 700

FRAME_SEPARATOR = "|"


class WsId(IntEnum):
    """Characteristic identifiers used on the websocket relay."""

    MODE = 1
    MODEL = 2
    TRANSITIONING = 3
    PELLET_LEVEL = 4
    LAST_FAULT = 5
    CHAMBER = 6
    WIFI_RSSI = 37
    STM_FIRMWARE = 48
    ESP_FIRMWARE = 49


class Op(IntEnum):
    """Second byte of every frame."""

    GET = 0
    GET_RESPONSE = 1
    SET = 2
    SET_RESPONSE = 3
    NOTIFY = 4


class GrillModeType(IntEnum):
    """First byte of the MODE characteristic."""

    STANDBY = 0
    FAN = 1
    FEED = 2
    RECORD = 3
    HIGH = 16
    RUN = 17
    COOK_PROGRAM = 18
    DEBUG = 127


# Modes in which the firepot is lit.
LIT_MODES = frozenset(
    {GrillModeType.FEED, GrillModeType.HIGH, GrillModeType.RUN, GrillModeType.COOK_PROGRAM}
)

# Modes from which the set temperature may be changed remotely.
ADJUSTABLE_MODES = frozenset({GrillModeType.RUN, GrillModeType.HIGH})


class FaultCode(IntEnum):
    """Second byte of the LAST_FAULT characteristic."""

    NONE = 0
    SENSOR = 1
    FLAME_OUT = 2
    OVER_TEMP = 3


@dataclass
class GrillState:
    """Latest known values reported by the grill."""

    mode: GrillModeType | None = None
    set_temp_f: int | None = None
    smoke_level: int | None = None
    fan_level: int | None = None
    chamber_f: int | None = None
    probes_f: dict[int, int | None] = field(default_factory=dict)
    pellet_level: int | None = None
    transitioning: bool | None = None
    fault: FaultCode | None = None
    wifi_rssi: int | None = None
    model_code: str | None = None
    min_temp_f: int | None = None
    max_temp_f: int | None = None
    stm_firmware: str | None = None
    esp_firmware: str | None = None

    @property
    def probe_count(self) -> int:
        """Number of probe ports, derived the same way the official app does."""
        if self.model_code and self.model_code[0] in ("F", "G"):
            return 2
        return 4


def decode_message(data: str) -> list[bytes]:
    """Split a websocket ``data`` string into raw frames."""
    frames: list[bytes] = []
    for part in data.split(FRAME_SEPARATOR):
        if not part:
            continue
        try:
            frames.append(base64.b64decode(part, validate=True))
        except (binascii.Error, ValueError):
            continue
    return frames


def encode_message(frames: list[bytes]) -> str:
    """Join raw frames into a websocket ``data`` string."""
    return FRAME_SEPARATOR.join(base64.b64encode(f).decode("ascii") for f in frames)


def read_frame(ws_id: int) -> bytes:
    """Frame requesting the current value of a characteristic."""
    return bytes((ws_id, Op.GET))


def write_frame(ws_id: int, payload: bytes) -> bytes:
    """Frame writing a value to a characteristic."""
    return bytes((ws_id, Op.SET)) + payload


def encode_run_mode(set_temp_f: int, smoke_level: int) -> bytes:
    """MODE payload for RUN at a set temperature and smoke level.

    The official app allocates five bytes and leaves the last one zero.
    """
    return (
        bytes((GrillModeType.RUN,))
        + int(set_temp_f).to_bytes(2, "big", signed=True)
        + bytes((int(smoke_level) & 0xFF, 0))
    )


def encode_standby_mode() -> bytes:
    """MODE payload that shuts the grill down."""
    return bytes((GrillModeType.STANDBY,))


def initial_read_frames(probe_count: int = 4) -> list[bytes]:
    """Frames that read every characteristic the integration exposes."""
    ids = [
        WsId.MODEL,
        WsId.MODE,
        WsId.CHAMBER,
        WsId.PELLET_LEVEL,
        WsId.TRANSITIONING,
        WsId.LAST_FAULT,
        WsId.WIFI_RSSI,
        WsId.STM_FIRMWARE,
        WsId.ESP_FIRMWARE,
    ]
    ids += [PROBE_FIRST_WS_ID + i for i in range(probe_count)]
    return [read_frame(i) for i in ids]


def telemetry_read_frames(probe_count: int) -> list[bytes]:
    """Frames that re-read the values that change during a cook."""
    ids = [WsId.MODE, WsId.CHAMBER, WsId.PELLET_LEVEL, WsId.TRANSITIONING, WsId.LAST_FAULT]
    ids += [PROBE_FIRST_WS_ID + i for i in range(probe_count)]
    return [read_frame(i) for i in ids]


def _i16(payload: bytes) -> int:
    return int.from_bytes(payload[:2], "big", signed=True)


def _probe_value(raw: int) -> int | None:
    if raw == 0 or not PROBE_MIN_VALID_F <= raw <= PROBE_MAX_VALID_F:
        return None
    return raw


def _text(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace").strip("\x00").strip()


def apply_frame(state: GrillState, frame: bytes) -> bool:
    """Apply one grill-to-client frame to ``state``.

    Returns True if the frame carried a value (whether or not it changed).
    Status-only frames (write acknowledgements) and unknown characteristics
    return False.
    """
    if len(frame) < 3 or frame[1] == Op.SET_RESPONSE:
        return False
    ws_id = frame[0]
    payload = frame[2:]

    if ws_id == WsId.MODE:
        try:
            mode = GrillModeType(payload[0])
        except ValueError:
            return False
        state.mode = mode
        if mode == GrillModeType.RUN and len(payload) >= 4:
            state.set_temp_f = _i16(payload[1:3])
            state.smoke_level = payload[3]
        elif mode != GrillModeType.RUN:
            state.set_temp_f = None
            state.smoke_level = None
        state.fan_level = payload[1] if mode == GrillModeType.FAN and len(payload) >= 2 else None
        return True

    if ws_id == WsId.MODEL and len(payload) >= 8:
        state.model_code = _text(payload[:4])
        state.min_temp_f = _i16(payload[4:6])
        state.max_temp_f = _i16(payload[6:8])
        return True

    if ws_id == WsId.CHAMBER and len(payload) >= 2:
        state.chamber_f = _i16(payload)
        return True

    if PROBE_FIRST_WS_ID <= ws_id <= PROBE_LAST_WS_ID and len(payload) >= 2:
        state.probes_f[ws_id - PROBE_FIRST_WS_ID] = _probe_value(_i16(payload))
        return True

    if ws_id == WsId.PELLET_LEVEL:
        state.pellet_level = payload[0]
        return True

    if ws_id == WsId.TRANSITIONING:
        state.transitioning = payload[0] != 0
        return True

    if ws_id == WsId.LAST_FAULT and len(payload) >= 2:
        try:
            state.fault = FaultCode(payload[1])
        except ValueError:
            return False
        return True

    if ws_id == WsId.WIFI_RSSI:
        state.wifi_rssi = int.from_bytes(payload[:1], "big", signed=True)
        return True

    if ws_id == WsId.STM_FIRMWARE:
        state.stm_firmware = _text(payload)
        return True

    if ws_id == WsId.ESP_FIRMWARE:
        state.esp_firmware = _text(payload)
        return True

    return False
