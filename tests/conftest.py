"""Fixtures: a fake Camp Chef cloud served from localhost."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import patch

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer
import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

GRILL_ID = "grill-1"
PIN = "123456"
TOKEN = "header.eyJleHAiOjQxMDI0NDQ4MDB9.sig"  # exp in 2100


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeGrill:
    """Characteristic values keyed by wsId, as the grill would report them."""

    def __init__(self) -> None:
        self.values: dict[int, bytes] = {
            1: bytes([0x11, 0x00, 0xE1, 0x03, 0x00]),  # RUN 225 smoke 3
            2: b"DWW2" + (160).to_bytes(2, "big") + (500).to_bytes(2, "big"),
            3: bytes([0]),
            4: bytes([70]),
            5: bytes([0, 0]),
            6: (221).to_bytes(2, "big"),
            16: (145).to_bytes(2, "big"),
            17: bytes([0, 0]),
            18: bytes([0, 0]),
            19: bytes([0, 0]),
            37: bytes([0xC4]),
            48: b"1.0.5",
            49: b"2.1.8",
        }
        self.writes: list[bytes] = []


class FakeCloud:
    def __init__(self) -> None:
        self.grill = FakeGrill()
        self.ws_open_messages: list[dict] = []
        self.reject_socket_auth = False
        self.sockets: set[web.WebSocketResponse] = set()
        app = web.Application()
        app.router.add_post("/auth/login-email", self.login_email)
        app.router.add_post("/auth/login-email-pin", self.login_pin)
        app.router.add_get("/auth/refresh-token", self.refresh)
        app.router.add_get("/auth/self", self.self_)
        app.router.add_post("/grill/rest/query", self.grills)
        app.router.add_get("/", self.socket)
        self.server = TestServer(app)

    def _authed(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    async def login_email(self, request: web.Request) -> web.Response:
        await request.json()
        return web.Response(status=204)

    async def login_pin(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("pin") != PIN:
            return web.json_response({"message": "Incorrect PIN"}, status=400)
        return web.json_response(TOKEN)

    async def refresh(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"message": "no"}, status=401)
        return web.json_response(TOKEN)

    async def self_(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"message": "no"}, status=401)
        return web.json_response({"_id": "user-1", "email": "me@example.com"})

    async def grills(self, request: web.Request) -> web.Response:
        if not self._authed(request):
            return web.json_response({"message": "no"}, status=401)
        return web.json_response(
            [{"_id": GRILL_ID, "name": "Woodwind", "model": "DWW2", "isOnline": True}]
        )

    async def socket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.add(ws)
        channel = None
        ready: set[str] = set()

        async def acknowledge(ch: str) -> None:
            # Like the real server, the channel only exists once it says so.
            await asyncio.sleep(0.05)
            ready.add(ch)
            if not ws.closed:
                await ws.send_str(json.dumps({"channel": ch, "start": True}))

        async for msg in ws:
            if msg.type != WSMsgType.TEXT or not msg.data.strip():
                continue
            env = json.loads(msg.data)
            if env.get("start"):
                self.ws_open_messages.append(env)
                channel = env["channel"]
                if self.reject_socket_auth or env["queryParams"]["jwt"] != [TOKEN]:
                    await ws.send_str(json.dumps({"channel": channel, "error": "Not authorized"}))
                else:
                    self._ack_task = asyncio.create_task(acknowledge(channel))
                continue
            if env.get("channel") != channel or not env.get("data"):
                continue
            if channel not in ready:
                await ws.send_str(
                    json.dumps({"channel": channel, "error": "Channel information not found."})
                )
                continue
            replies = []
            for part in env["data"].split("|"):
                frame = base64.b64decode(part)
                ws_id, op = frame[0], frame[1]
                if op == 0 and ws_id in self.grill.values:
                    replies.append(bytes([ws_id, 1]) + self.grill.values[ws_id])
                elif op == 2:
                    self.grill.writes.append(frame)
                    self.grill.values[ws_id] = frame[2:]
                    replies.append(bytes([ws_id, 3, 0]))
            if replies:
                data = "|".join(base64.b64encode(r).decode() for r in replies)
                await ws.send_str(json.dumps({"channel": channel, "data": data}))
        return ws


@pytest.fixture
async def cloud(socket_enabled):
    fake = FakeCloud()
    await fake.server.start_server()
    base = f"http://{fake.server.host}:{fake.server.port}"
    with (
        patch("custom_components.camp_chef_connect.api.API_URL", base),
        patch("custom_components.camp_chef_connect.connection.WS_URL", f"{base}/?path=multiplex"),
    ):
        yield fake
    for ws in list(fake.sockets):
        await ws.close()
    await fake.server.close()
