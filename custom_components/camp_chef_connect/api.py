"""REST client for the Camp Chef Connect cloud (a Lightning Server backend)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import aiohttp

from .const import API_URL

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


class CampChefError(Exception):
    """Base error for the Camp Chef cloud."""


class CampChefAuthError(CampChefError):
    """The token or PIN was rejected."""


class CampChefConnectionError(CampChefError):
    """The cloud could not be reached or returned an unexpected response."""


def token_expiry(token: str) -> float | None:
    """Return the ``exp`` claim of a JWT without verifying it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except (IndexError, ValueError, AttributeError):
        return None
    return float(exp) if isinstance(exp, (int, float)) else None


def token_needs_refresh(token: str, margin: float = 86400) -> bool:
    """True if the token expires within ``margin`` seconds."""
    exp = token_expiry(token)
    return exp is not None and exp - time.time() < margin


class CampChefApi:
    """Thin async wrapper around the endpoints the integration uses."""

    def __init__(self, session: aiohttp.ClientSession, token: str | None = None) -> None:
        self._session = session
        self.token = token

    async def _request(
        self, method: str, path: str, *, json_body: Any = None, auth: bool = True
    ) -> Any:
        headers = {"Accept": "application/json"}
        if auth:
            if not self.token:
                raise CampChefAuthError("Not logged in")
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with self._session.request(
                method,
                f"{API_URL}{path}",
                json=json_body,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status in (401, 403):
                    raise CampChefAuthError(await _error_message(resp))
                if resp.status >= 400:
                    raise CampChefConnectionError(
                        f"{method} {path} returned {resp.status}: {await _error_message(resp)}"
                    )
                if resp.content_length == 0:
                    return None
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CampChefConnectionError(f"{method} {path} failed: {err}") from err
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError as err:
            raise CampChefConnectionError(f"{method} {path} returned non-JSON") from err

    async def request_pin(self, email: str) -> None:
        """Ask the cloud to email a one-time login PIN."""
        await self._request("POST", "/auth/login-email", json_body=email, auth=False)

    async def login_with_pin(self, email: str, pin: str) -> str:
        """Exchange the emailed PIN for a session token."""
        try:
            token = await self._request(
                "POST",
                "/auth/login-email-pin",
                json_body={"email": email, "pin": pin},
                auth=False,
            )
        except CampChefConnectionError as err:
            # Lightning Server reports a wrong PIN as a 400.
            raise CampChefAuthError(str(err)) from err
        if not isinstance(token, str) or not token:
            raise CampChefAuthError("Login did not return a token")
        self.token = token
        return token

    async def refresh_token(self) -> str:
        """Trade the current token for a fresh one."""
        token = await self._request("GET", "/auth/refresh-token")
        if not isinstance(token, str) or not token:
            raise CampChefAuthError("Refresh did not return a token")
        self.token = token
        return token

    async def get_self(self) -> dict[str, Any]:
        """Return the logged-in user record."""
        user = await self._request("GET", "/auth/self")
        if not isinstance(user, dict):
            raise CampChefConnectionError("Unexpected /auth/self response")
        return user

    async def get_grills(self) -> list[dict[str, Any]]:
        """Return the grills the user has access to."""
        grills = await self._request(
            "POST", "/grill/rest/query", json_body={"condition": {"Always": True}}
        )
        if not isinstance(grills, list):
            raise CampChefConnectionError("Unexpected /grill/rest/query response")
        return grills


async def _error_message(resp: aiohttp.ClientResponse) -> str:
    try:
        body = await resp.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        return resp.reason or str(resp.status)
    if isinstance(body, dict):
        return str(body.get("message") or body.get("detail") or resp.status)
    return str(body)
