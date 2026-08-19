"""The registry must describe reality: valid schema, repos that exist,
scripts that exist and are executable."""

import json

import pytest

from launchpad import registry as reg


@pytest.fixture(scope="module")
def apps():
    return reg.load()


def test_loads_and_is_not_empty(apps):
    assert len(apps) >= 5
    assert {a.id for a in apps} >= {
        "sitar-guru", "memoire", "jukebox", "narrator", "reachying-for-the-stars"
    }


def test_every_field_is_populated(apps):
    for a in apps:
        assert a.name and a.emoji and a.pitch, a.id
        assert len(a.description) > 200, f"{a.id}: description is a stub"
        assert a.status in reg.STATUSES
        assert a.status_detail, a.id
        assert a.needs, a.id
        assert a.github_url and a.github_url.startswith("https://"), a.id


def test_repo_paths_exist(apps):
    for a in apps:
        assert a.repo_path.is_dir(), f"{a.id}: {a.repo_path} is not a directory"
        assert (a.repo_path / ".git").exists(), f"{a.id}: {a.repo_path} is not a git repo"


def test_launch_scripts_exist_and_are_executable(apps):
    for a in apps:
        for mode in ("robot", "dev"):
            path = a.script_path(mode)
            if path is None:
                continue
            assert path.exists(), f"{a.id}/{mode}: {path} missing"
            assert path.stat().st_mode & 0o111, f"{a.id}/{mode}: {path} not executable"


def test_link_apps_have_a_url_and_no_commands(apps):
    for a in apps:
        if a.mode == "link":
            assert a.url and a.url.startswith("https://"), a.id
            assert a.robot is None and a.dev is None, a.id


def test_dev_modes_do_not_need_the_robot(apps):
    """The robot is a loan and usually off — a dev command that needs it is a bug."""
    for a in apps:
        if a.dev is not None:
            assert a.dev.needs_robot is False, a.id


def test_concierge_block_is_complete(apps):
    for a in apps:
        c = a.concierge
        assert isinstance(c["activity"], bool), a.id
        assert isinstance(c["exclusive"], bool), a.id
        assert isinstance(c["interruptible"], bool), a.id
        assert c["voice_intents"], a.id
        # what the concierge's resource manager must hand over is a subset of
        # what the launchpad already tells the user it needs
        assert set(c["resources"]) <= set(a.needs) | {"network"}, a.id


def test_prereqs_report_missing_env(apps):
    narrator = reg.by_id(apps, "narrator")
    assert narrator.requires_env == ("ANTHROPIC_API_KEY",)
    assert narrator.missing_env({}) == ["ANTHROPIC_API_KEY"]
    assert narrator.missing_env({"ANTHROPIC_API_KEY": "sk-x"}) == []
    assert narrator.prereqs({"ANTHROPIC_API_KEY": "sk-x"})["repo_ok"] is True


# ------------------------------------------------------------ schema errors


def _write(tmp_path, apps_payload):
    p = tmp_path / "registry.json"
    p.write_text(json.dumps({"schema_version": 1, "apps": apps_payload}))
    return p


BASE = {
    "id": "x", "name": "x", "emoji": "x", "pitch": "p", "description": "d",
    "repo_path": "/tmp", "github_url": None, "status": "robot-tested",
    "status_detail": "s", "needs": ["mic"], "requires_env": [],
    "launch": {"mode": "process", "robot": None,
               "dev": {"label": "l", "cmd": ["./run.sh"], "needs_robot": False}, "url": None},
    "notes": [],
    "concierge": {"activity": True, "resources": ["mic"], "exclusive": True,
                  "interruptible": True, "voice_intents": ["x"]},
}


def test_rejects_unknown_status(tmp_path):
    bad = {**BASE, "status": "works-on-my-machine"}
    with pytest.raises(reg.RegistryError, match="unknown status"):
        reg.load(_write(tmp_path, [bad]))


def test_rejects_unknown_need(tmp_path):
    bad = {**BASE, "needs": ["telepathy"]}
    with pytest.raises(reg.RegistryError, match="unknown needs"):
        reg.load(_write(tmp_path, [bad]))


def test_rejects_duplicate_ids(tmp_path):
    with pytest.raises(reg.RegistryError, match="duplicate"):
        reg.load(_write(tmp_path, [BASE, dict(BASE)]))


def test_rejects_process_app_with_no_command(tmp_path):
    bad = {**BASE, "launch": {"mode": "process", "robot": None, "dev": None, "url": None}}
    with pytest.raises(reg.RegistryError, match="at least one command"):
        reg.load(_write(tmp_path, [bad]))


def test_rejects_link_app_without_url(tmp_path):
    bad = {**BASE, "launch": {"mode": "link", "robot": None, "dev": None, "url": None}}
    with pytest.raises(reg.RegistryError, match="needs a url"):
        reg.load(_write(tmp_path, [bad]))


def test_rejects_missing_concierge_key(tmp_path):
    c = dict(BASE["concierge"]); c.pop("interruptible")
    with pytest.raises(reg.RegistryError, match="concierge"):
        reg.load(_write(tmp_path, [{**BASE, "concierge": c}]))


def test_rejects_wrong_schema_version(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"schema_version": 99, "apps": []}))
    with pytest.raises(reg.RegistryError, match="schema_version"):
        reg.load(p)
