"""Is the robot on? — a small, timeout-guarded probe of the daemon REST API.

The robot is a loan and is off most of the time, so this module's contract is:
never block the UI, never raise, always answer within ~2 s, and say clearly
which of "on", "off" or "answered something weird" happened.

Host resolution: $REACHY_HOST, then mDNS `reachy-mini.local`, then the last IP
that ever answered on the home wifi (192.168.1.8) — mDNS goes cold after robot
or daemon restarts.
"""

from __future__ import annotations

import os
import time
from typing import Any, Iterable

import httpx

DAEMON_PORT = 8000
DEFAULT_HOSTS = ("reachy-mini.local", "192.168.1.8")
# Seconds per host: two hosts → ~3 s of HTTP worst case, plus whatever the
# mDNS lookup of reachy-mini.local costs (a cold avahi adds a few seconds).
# Measured with the robot off: ~7 s. That is why the page renders the cards
# first and fills the status pill in afterwards.
PROBE_TIMEOUT = 1.5
CACHE_TTL = 5.0          # seconds

STATUS_PATH = "/api/daemon/status"
WIFI_PATH = "/wifi/status"          # note: unprefixed, like the update routes
MOTORS_ENABLE_PATH = "/api/motors/set_mode/enabled"
MEDIA_ACQUIRE_PATH = "/api/media/acquire"


def candidate_hosts() -> list[str]:
    hosts: list[str] = []
    env = os.environ.get("REACHY_HOST")
    if env:
        hosts.append(env)
    hosts.extend(h for h in DEFAULT_HOSTS if h not in hosts)
    return hosts


def _dig(obj: Any, keys: Iterable[str]) -> Any:
    """Find the first value under any of `keys`, at any depth.

    The daemon's status payload has moved between versions (1.8.3 → 1.9.0), so
    we look for the fields rather than assuming a shape.
    """
    keys = set(keys)
    stack = [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k in keys and not isinstance(v, (dict, list)):
                    return v
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


class RobotProbe:
    """Cached view of the robot. `transport` is for tests."""

    def __init__(
        self,
        hosts: list[str] | None = None,
        *,
        timeout: float = PROBE_TIMEOUT,
        ttl: float = CACHE_TTL,
        transport: httpx.BaseTransport | None = None,
        clock=time.monotonic,
    ) -> None:
        self._hosts = hosts
        self._timeout = timeout
        self._ttl = ttl
        self._transport = transport
        self._clock = clock
        self._cache: dict[str, Any] | None = None
        self._cached_at = -1e9

    # ---------------------------------------------------------------- http

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self._timeout, transport=self._transport)

    def _probe_once(self) -> dict[str, Any]:
        hosts = self._hosts if self._hosts is not None else candidate_hosts()
        errors: list[str] = []
        with self._client() as client:
            for host in hosts:
                base = f"http://{host}:{DAEMON_PORT}"
                try:
                    resp = client.get(base + STATUS_PATH)
                except httpx.TimeoutException:
                    errors.append(f"{host}: timeout")
                    continue
                except httpx.HTTPError as exc:
                    errors.append(f"{host}: {type(exc).__name__}")
                    continue
                if resp.status_code >= 400:
                    errors.append(f"{host}: HTTP {resp.status_code}")
                    continue
                try:
                    payload = resp.json()
                except ValueError:
                    errors.append(f"{host}: non-JSON answer on {STATUS_PATH}")
                    continue

                wifi = None
                try:
                    w = client.get(base + WIFI_PATH)
                    if w.status_code < 400:
                        wifi = w.json()
                except (httpx.HTTPError, ValueError):
                    wifi = None

                return {
                    "reachable": True,
                    "host": host,
                    "base_url": base,
                    "version": _dig(payload, ("version", "daemon_version")),
                    "motor_mode": _dig(payload, ("motor_control_mode", "motor_mode", "motors_mode")),
                    "wlan_ip": _dig(payload, ("wlan_ip",)),
                    "wifi": wifi,
                    "errors": errors,
                    "message": None,
                }

        return {
            "reachable": False,
            "host": None,
            "base_url": None,
            "version": None,
            "motor_mode": None,
            "wlan_ip": None,
            "wifi": None,
            "errors": errors,
            "message": "robot off — dev modes still available",
        }

    # --------------------------------------------------------------- public

    def status(self, *, force: bool = False) -> dict[str, Any]:
        now = self._clock()
        if not force and self._cache is not None and now - self._cached_at < self._ttl:
            out = dict(self._cache)
            out["cached"] = True
            out["age"] = now - self._cached_at
            return out
        result = self._probe_once()
        self._cache = result
        self._cached_at = now
        out = dict(result)
        out["cached"] = False
        out["age"] = 0.0
        return out

    def invalidate(self) -> None:
        self._cache = None
        self._cached_at = -1e9

    def preflight(self, action: str) -> dict[str, Any]:
        """The two standard POSTs. `action` is "motors" or "media"."""
        path = {"motors": MOTORS_ENABLE_PATH, "media": MEDIA_ACQUIRE_PATH}.get(action)
        if path is None:
            return {"ok": False, "error": f"unknown preflight action {action!r}"}
        st = self.status()
        if not st["reachable"]:
            return {"ok": False, "error": "robot is not reachable"}
        url = st["base_url"] + path
        try:
            with self._client() as client:
                resp = client.post(url)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": f"{type(exc).__name__} on {path}"}
        self.invalidate()  # motor mode just changed
        return {
            "ok": resp.status_code < 400,
            "action": action,
            "http_status": resp.status_code,
            "url": url,
        }
