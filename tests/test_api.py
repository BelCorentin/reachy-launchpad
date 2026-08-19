"""API surface via TestClient, against a fake registry and a fake robot."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from launchpad import registry as reg
from launchpad.app import assert_local_only, create_app
from launchpad.procman import ProcessManager

FAKE_SCRIPT = """#!/usr/bin/env bash
echo "hello from $1"
exec python3 -u -c 'import time
while True:
    print("tick", flush=True); time.sleep(0.2)'
"""


class FakeProbe:
    def __init__(self, reachable=True):
        self.reachable = reachable
        self.preflights = []

    def status(self, force=False):
        return {
            "reachable": self.reachable,
            "host": "reachy-mini.local" if self.reachable else None,
            "base_url": "http://reachy-mini.local:8000" if self.reachable else None,
            "version": "1.9.0" if self.reachable else None,
            "motor_mode": "enabled" if self.reachable else None,
            "wlan_ip": None, "wifi": None, "errors": [], "cached": False, "age": 0.0,
            "message": None if self.reachable else "robot off — dev modes still available",
            "forced": force,
        }

    def preflight(self, action):
        self.preflights.append(action)
        if not self.reachable:
            return {"ok": False, "error": "robot is not reachable"}
        return {"ok": True, "action": action, "http_status": 200, "url": "http://x"}


def _entry(app_id, repo, *, needs_env=None, robot=True, dev=True, mode="process", url=None):
    return {
        "id": app_id, "name": app_id, "emoji": "🤖", "pitch": "does a thing",
        "description": "x" * 250,
        "repo_path": str(repo), "github_url": "https://github.com/BelCorentin/x",
        "status": "built-not-robot-tested", "status_detail": "never met the robot",
        "needs": ["motion"], "requires_env": needs_env or [],
        "launch": {
            "mode": mode,
            "robot": {"label": "run on robot", "cmd": ["./run.sh", "robot"], "needs_robot": True} if robot else None,
            "dev": {"label": "dev mode", "cmd": ["./run.sh", "dev"], "needs_robot": False} if dev else None,
            "url": url,
        },
        "notes": ["careful"],
        "concierge": {"activity": True, "resources": ["motion"], "exclusive": True,
                      "interruptible": True, "voice_intents": ["do a thing"]},
    }


@pytest.fixture
def env(tmp_path):
    repo = tmp_path / "fakeapp"
    repo.mkdir()
    script = repo / "run.sh"
    script.write_text(FAKE_SCRIPT)
    script.chmod(0o755)

    payload = {
        "schema_version": 1,
        "apps": [
            _entry("fake", repo),
            _entry("other", repo),
            _entry("keyed", repo, needs_env=["NOPE_MISSING_KEY"]),
            _entry("web", repo, robot=False, dev=False, mode="link",
                   url="https://huggingface.co/spaces/x/y"),
        ],
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(payload))
    apps = reg.load(path)
    probe = FakeProbe(reachable=True)
    manager = ProcessManager()
    client = TestClient(create_app(apps, manager=manager, probe=probe))
    yield client, probe, manager
    manager.stop()


def wait_for(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------------- pages


def test_index_is_served(env):
    client, *_ = env
    r = client.get("/")
    assert r.status_code == 200
    assert "reachy launchpad" in r.text
    assert "/api/apps" in r.text


def test_the_page_never_binds_anything_but_loopback():
    assert_local_only("127.0.0.1")
    with pytest.raises(AssertionError, match="loopback"):
        assert_local_only("0.0.0.0")


# -------------------------------------------------------------------- data


def test_apps_endpoint_shape(env):
    client, *_ = env
    body = client.get("/api/apps").json()
    ids = [a["id"] for a in body["apps"]]
    assert ids == ["fake", "other", "keyed", "web"]
    fake = body["apps"][0]
    assert fake["need_icons"] == ["🦾"]
    assert fake["status_label"] == "built, not robot-tested"
    assert fake["prereqs"]["repo_ok"] is True and fake["prereqs"]["scripts_ok"] is True
    assert body["apps"][2]["prereqs"]["missing_env"] == ["NOPE_MISSING_KEY"]
    assert body["concierge_teaser"]["status"] == "plan, not started"


def test_robot_endpoint(env):
    client, probe, _ = env
    assert client.get("/api/robot").json()["reachable"] is True
    probe.reachable = False
    body = client.get("/api/robot?force=true").json()
    assert body["reachable"] is False
    assert body["message"].startswith("robot off")
    assert body["forced"] is True


def test_preflight_endpoints(env):
    client, probe, _ = env
    assert client.post("/api/robot/preflight/motors").json()["ok"] is True
    assert client.post("/api/robot/preflight/media").json()["ok"] is True
    assert probe.preflights == ["motors", "media"]
    probe.reachable = False
    r = client.post("/api/robot/preflight/motors")
    assert r.status_code == 409 and r.json()["ok"] is False


# ----------------------------------------------------------------- launching


def test_launch_status_logs_stop_roundtrip(env):
    client, _, manager = env
    r = client.post("/api/launch", json={"app_id": "fake", "mode": "dev"})
    assert r.status_code == 200, r.text
    run = r.json()["run"]
    assert run["app_id"] == "fake" and run["mode"] == "dev" and run["running"] is True

    st = client.get("/api/status").json()
    assert st["running"] is True and st["run"]["label"] == "dev mode"

    assert wait_for(lambda: client.get("/api/logs").json()["total"] >= 2)
    logs = client.get("/api/logs").json()
    assert "hello from dev" in logs["lines"]
    later = client.get(f"/api/logs?since={logs['total']}").json()
    assert len(later["lines"]) == later["total"] - logs["total"]   # only what is new

    stopped = client.post("/api/stop").json()
    assert stopped["stopped"] is True and stopped["app_id"] == "fake"
    assert client.get("/api/status").json()["running"] is False


def test_second_launch_conflicts_then_force_works(env):
    client, *_ = env
    client.post("/api/launch", json={"app_id": "fake", "mode": "dev"})
    r = client.post("/api/launch", json={"app_id": "other", "mode": "dev"})
    assert r.status_code == 409 and "is running" in r.json()["detail"]

    r = client.post("/api/launch", json={"app_id": "fake", "mode": "dev", "force": True})
    assert r.status_code == 200
    client.post("/api/stop")


def test_robot_mode_refused_when_robot_is_off(env):
    client, probe, _ = env
    probe.reachable = False
    r = client.post("/api/launch", json={"app_id": "fake", "mode": "robot"})
    assert r.status_code == 412 and "not reachable" in r.json()["detail"]
    # ... but the dev mode still works, which is the whole point
    assert client.post("/api/launch", json={"app_id": "fake", "mode": "dev"}).status_code == 200
    client.post("/api/stop")


def test_missing_env_blocks_launch(env):
    client, *_ = env
    r = client.post("/api/launch", json={"app_id": "keyed", "mode": "dev"})
    assert r.status_code == 412 and "NOPE_MISSING_KEY" in r.json()["detail"]


def test_link_app_cannot_be_launched(env):
    client, *_ = env
    r = client.post("/api/launch", json={"app_id": "web", "mode": "dev"})
    assert r.status_code == 400 and "web app" in r.json()["detail"]


def test_unknown_app_and_bad_mode(env):
    client, *_ = env
    assert client.post("/api/launch", json={"app_id": "ghost", "mode": "dev"}).status_code == 404
    assert client.post("/api/launch", json={"app_id": "fake", "mode": "sideways"}).status_code == 400


def test_stop_when_nothing_runs(env):
    client, *_ = env
    assert client.post("/api/stop").json() == {"stopped": False, "reason": "nothing running"}


def test_real_registry_boots_the_real_app():
    """The shipped registry.json must actually build a server."""
    client = TestClient(create_app(probe=FakeProbe(reachable=False)))
    body = client.get("/api/apps").json()
    assert len(body["apps"]) >= 5
    assert client.get("/").status_code == 200
