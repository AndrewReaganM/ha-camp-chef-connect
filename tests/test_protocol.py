"""Tests for the wire protocol; they need no Home Assistant install.

Run with: python -m pytest tests
"""

import importlib.util
from pathlib import Path
import sys

_PATH = Path(__file__).parents[1] / "custom_components/camp_chef_connect/protocol.py"
_spec = importlib.util.spec_from_file_location("camp_chef_protocol", _PATH)
protocol = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = protocol
_spec.loader.exec_module(protocol)

GrillState = protocol.GrillState
GrillModeType = protocol.GrillModeType
FaultCode = protocol.FaultCode


def test_message_round_trip():
    frames = [bytes([1, 0]), bytes([6, 1, 0x00, 0xE1])]
    assert protocol.decode_message(protocol.encode_message(frames)) == frames


def test_decode_skips_garbage():
    assert protocol.decode_message("AQA=||not base64!") == [bytes([1, 0])]


def test_run_mode_matches_app_encoding():
    # [RUN, 225 big-endian, smoke 3, pad]
    assert protocol.encode_run_mode(225, 3) == bytes([0x11, 0x00, 0xE1, 0x03, 0x00])
    assert protocol.write_frame(protocol.WsId.MODE, protocol.encode_standby_mode()) == bytes(
        [1, 2, 0]
    )


def test_apply_run_mode_then_standby():
    state = GrillState()
    assert protocol.apply_frame(state, bytes([1, 4, 0x11, 0x00, 0xE1, 0x05, 0x00]))
    assert state.mode is GrillModeType.RUN
    assert (state.set_temp_f, state.smoke_level) == (225, 5)
    assert protocol.apply_frame(state, bytes([1, 1, 0x00]))
    assert state.mode is GrillModeType.STANDBY
    assert state.set_temp_f is None


def test_apply_fan_mode():
    state = GrillState()
    protocol.apply_frame(state, bytes([1, 4, 0x01, 0x02]))
    assert state.mode is GrillModeType.FAN
    assert state.fan_level == 2


def test_temperatures_and_probes():
    state = GrillState()
    protocol.apply_frame(state, bytes([6, 4, 0x01, 0x2C]))  # 300
    protocol.apply_frame(state, bytes([16, 1, 0x00, 0xA0]))  # probe 1: 160
    protocol.apply_frame(state, bytes([17, 1, 0x00, 0x00]))  # probe 2: unplugged
    protocol.apply_frame(state, bytes([18, 1, 0x7F, 0xFF]))  # probe 3: out of range
    assert state.chamber_f == 300
    assert state.probes_f == {0: 160, 1: None, 2: None}


def test_model_and_probe_count():
    state = GrillState()
    payload = b"DWW2" + (160).to_bytes(2, "big") + (500).to_bytes(2, "big")
    assert protocol.apply_frame(state, bytes([2, 1]) + payload)
    assert (state.model_code, state.min_temp_f, state.max_temp_f) == ("DWW2", 160, 500)
    assert state.probe_count == 4
    state.model_code = "FTG1"
    assert state.probe_count == 2


def test_misc_characteristics():
    state = GrillState()
    protocol.apply_frame(state, bytes([4, 4, 62]))
    protocol.apply_frame(state, bytes([3, 4, 1]))
    protocol.apply_frame(state, bytes([5, 1, 0, 2]))
    protocol.apply_frame(state, bytes([37, 1, 0xC4]))  # -60
    protocol.apply_frame(state, bytes([48, 1]) + b"V1.2.3")
    assert state.pellet_level == 62
    assert state.transitioning is True
    assert state.fault is FaultCode.FLAME_OUT
    assert state.wifi_rssi == -60
    assert state.stm_firmware == "V1.2.3"


def test_status_frames_are_ignored():
    state = GrillState()
    assert not protocol.apply_frame(state, bytes([1, 3, 0]))  # write ack
    assert not protocol.apply_frame(state, bytes([1, 0]))  # too short
    assert not protocol.apply_frame(state, bytes([99, 4, 1]))  # unknown id
    assert state == GrillState()


def test_run_mode_trailer_round_trips():
    # Frame captured from a Woodwind 36 (DWWP): RUN 260 smoke 10, trailer 0x05.
    state = GrillState()
    assert protocol.apply_frame(state, bytes.fromhex("01011101040a05"))
    assert (state.set_temp_f, state.smoke_level, state.run_mode_trailer) == (260, 10, b"\x05")
    payload = protocol.encode_run_mode(265, 10, state.run_mode_trailer)
    assert payload == bytes.fromhex("1101090a05")


def test_real_woodwind_36_frames():
    state = GrillState()
    for hexframe in (
        "02014457575000a001f400",  # model DWWP 160-500
        "06040104",  # chamber 260
        "040164",  # pellets 100
        "10040000",  # probe 1 empty
        "2504c9",  # rssi -55
        "3001445757502056312e302e33",  # "DWWP V1.0.3"
    ):
        assert protocol.apply_frame(state, bytes.fromhex(hexframe))
    assert (state.model_code, state.min_temp_f, state.max_temp_f) == ("DWWP", 160, 500)
    assert (state.chamber_f, state.pellet_level, state.wifi_rssi) == (260, 100, -55)
    assert state.probes_f == {0: None}
    assert state.stm_firmware == "DWWP V1.0.3"
