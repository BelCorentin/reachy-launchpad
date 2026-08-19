"""Robot probe — every path mocked, nothing ever touches the real robot."""

import httpx
import pytest

from launchpad.robot import RobotProbe, _dig, candidate_hosts

STATUS_1_9 = {
    "version": "1.9.0",
    "motor_control_mode": "enabled",
    "network": {"wlan_ip": "192.168.1.8"},
}


def handler_up(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/daemon/status":
        return httpx.Response(200, json=STATUS_1_9)
    if request.url.path == "/wifi/status":
        return httpx.Response(200, json={"ssid": "home", "connected": True})
    return httpx.Response(404)


def test_robot_up():
    p = RobotProbe(hosts=["reachy-mini.local"], transport=httpx.MockTransport(handler_up))
    st = p.status()
    assert st["reachable"] is True
    assert st["host"] == "reachy-mini.local"
    assert st["version"] == "1.9.0"
    assert st["motor_mode"] == "enabled"
    assert st["wlan_ip"] == "192.168.1.8"          # found nested, not top level
    assert st["wifi"] == {"ssid": "home", "connected": True}
    assert st["base_url"] == "http://reachy-mini.local:8000"


def test_robot_down_everywhere():
    def refuse(request):
        raise httpx.ConnectError("no route", request=request)

    p = RobotProbe(hosts=["reachy-mini.local", "192.168.1.8"], transport=httpx.MockTransport(refuse))
    st = p.status()
    assert st["reachable"] is False
    assert st["message"] == "robot off — dev modes still available"
    assert len(st["errors"]) == 2
    assert all("ConnectError" in e for e in st["errors"])


def test_timeout_is_not_an_exception():
    def slow(request):
        raise httpx.ConnectTimeout("too slow", request=request)

    p = RobotProbe(hosts=["reachy-mini.local"], transport=httpx.MockTransport(slow))
    st = p.status()
    assert st["reachable"] is False
    assert st["errors"] == ["reachy-mini.local: timeout"]


def test_falls_back_to_last_known_ip():
    def handler(request):
        if request.url.host == "reachy-mini.local":
            raise httpx.ConnectError("mDNS cold", request=request)
        return handler_up(request)

    p = RobotProbe(hosts=["reachy-mini.local", "192.168.1.8"], transport=httpx.MockTransport(handler))
    st = p.status()
    assert st["reachable"] is True
    assert st["host"] == "192.168.1.8"
    assert st["errors"] == ["reachy-mini.local: ConnectError"]


def test_http_error_and_garbage_are_survived():
    def handler(request):
        if request.url.host == "a":
            return httpx.Response(500)
        if request.url.host == "b":
            return httpx.Response(200, text="<html>not json</html>")
        return httpx.Response(404)

    p = RobotProbe(hosts=["a", "b", "c"], transport=httpx.MockTransport(handler))
    st = p.status()
    assert st["reachable"] is False
    assert st["errors"] == ["a: HTTP 500", "b: non-JSON answer on /api/daemon/status", "c: HTTP 404"]


def test_wifi_failure_does_not_sink_the_probe():
    def handler(request):
        if request.url.path == "/api/daemon/status":
            return httpx.Response(200, json={"version": "1.8.3"})
        raise httpx.ConnectError("boom", request=request)

    p = RobotProbe(hosts=["h"], transport=httpx.MockTransport(handler))
    st = p.status()
    assert st["reachable"] is True and st["wifi"] is None


def test_cache_ttl_and_force():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return handler_up(request)

    now = [1000.0]
    p = RobotProbe(hosts=["h"], transport=httpx.MockTransport(handler), ttl=5.0, clock=lambda: now[0])

    p.status()
    n = len(calls)
    assert p.status()["cached"] is True
    assert len(calls) == n                      # served from cache

    assert p.status(force=True)["cached"] is False
    assert len(calls) > n                       # force re-probes

    n = len(calls)
    now[0] += 6.0
    assert p.status()["cached"] is False
    assert len(calls) > n                       # TTL expired


def test_preflight_when_robot_is_down():
    def refuse(request):
        raise httpx.ConnectError("off", request=request)

    p = RobotProbe(hosts=["h"], transport=httpx.MockTransport(refuse))
    assert p.preflight("motors") == {"ok": False, "error": "robot is not reachable"}


def test_preflight_posts_the_two_standard_routes():
    posted = []

    def handler(request):
        if request.method == "POST":
            posted.append(str(request.url))
            return httpx.Response(200, json={"ok": True})
        return handler_up(request)

    p = RobotProbe(hosts=["reachy-mini.local"], transport=httpx.MockTransport(handler))
    assert p.preflight("motors")["ok"] is True
    assert p.preflight("media")["ok"] is True
    assert posted == [
        "http://reachy-mini.local:8000/api/motors/set_mode/enabled",
        "http://reachy-mini.local:8000/api/media/acquire",
    ]


def test_unknown_preflight_action():
    p = RobotProbe(hosts=["h"], transport=httpx.MockTransport(handler_up))
    assert p.preflight("dance")["ok"] is False


def test_dig_finds_fields_whatever_the_shape():
    assert _dig({"a": {"b": {"version": "2.0"}}}, ("version",)) == "2.0"
    assert _dig({"xs": [{"motor_mode": "disabled"}]}, ("motor_control_mode", "motor_mode")) == "disabled"
    assert _dig({"a": 1}, ("version",)) is None


def test_candidate_hosts_prefers_env(monkeypatch):
    monkeypatch.setenv("REACHY_HOST", "10.0.0.5")
    hosts = candidate_hosts()
    assert hosts[0] == "10.0.0.5"
    assert "reachy-mini.local" in hosts and "192.168.1.8" in hosts
    monkeypatch.delenv("REACHY_HOST")
    assert candidate_hosts()[0] == "reachy-mini.local"
