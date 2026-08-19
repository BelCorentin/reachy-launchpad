"""The app registry: one entry per Reachy Mini thing Corentin built.

This file is the *contract*. `registry.json` is data; everything that reads it —
the launchpad UI, its API, and later the Reachy Concierge voice hub — reads it
through here, so the schema only has to be enforced in one place.

Schema (schema_version 1)
-------------------------

Top level::

    {
      "schema_version": 1,
      "updated":        "YYYY-MM-DD",
      "consumers":      [str, ...],      # who reads this file (documentation only)
      "apps":           [AppEntry, ...]
    }

``AppEntry``:

============== ========= ====================================================
field          type      meaning
============== ========= ====================================================
id             str       slug, unique, stable — the concierge's activity id
name           str       display name (lowercase, playful)
emoji          str       one emoji, used as the card icon
pitch          str       one line, what it does, for a card and for speech
description    str       the long story: what it does, how it works, the
                         details worth knowing. `\\n\\n`-separated paragraphs.
repo_path      str       may start with `~`; must exist on disk
github_url     str|null  where the code lives
status         enum      see STATUSES — how much reality it has seen
status_detail  str       *why* it has that status, with dates
needs          [enum]    see NEEDS — physical resources it takes over
requires_env   [str]     env vars that must be set before it can run at all
launch         Launch    how to start it (below)
notes          [str]     gotchas, one per line, shown in the card
concierge      Concierge forward-compat block for the voice hub (below)
============== ========= ====================================================

``Launch``::

    {
      "mode":  "process" | "link",
      "robot": Command | null,     # the real thing, needs the robot powered on
      "dev":   Command | null,     # the robot-free mode (this is the point:
                                   # the robot is a loan and usually off)
      "url":   str | null          # mode == "link" only
    }

``Command``::

    {"label": str, "cmd": [str, ...], "needs_robot": bool, "hint": str?}

``cmd[0]`` is resolved relative to ``repo_path`` when it starts with ``./``;
the process manager runs it with ``cwd=repo_path``.

``Concierge`` — not used by the launchpad, used by the planned voice hub::

    {
      "activity":        bool,    # can the concierge start it at all?
      "resources":       [enum],  # what its resource manager must hand over
      "exclusive":       bool,    # does it need sole ownership of them?
      "interruptible":   bool,    # can "reachy, stop" cut it mid-run?
      "wants_raw_audio": bool,    # needs mic frames, not a transcript
      "voice_intents":   [str],   # example phrasings that should select it
      "note":            str?     # anything special about driving it by voice
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path(__file__).with_name("registry.json")

SCHEMA_VERSION = 1

STATUSES = {
    # ran on the real robot and did the thing
    "robot-tested",
    # code + offline tests green, never met the hardware
    "built-not-robot-tested",
    # lives on the web (HF Space); you open it, you don't launch it
    "deployed-web",
}

NEEDS = {"mic", "camera", "motion", "speaker", "network", "api-key"}

NEED_ICONS = {
    "mic": "🎤",
    "camera": "📷",
    "motion": "🦾",
    "speaker": "🔊",
    "network": "🌐",
    "api-key": "🔑",
}

STATUS_LABELS = {
    "robot-tested": "robot-tested",
    "built-not-robot-tested": "built, not robot-tested",
    "deployed-web": "deployed on the web",
}


class RegistryError(ValueError):
    """The registry file does not honour the schema."""


@dataclass(frozen=True)
class Command:
    label: str
    cmd: tuple[str, ...]
    needs_robot: bool
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "cmd": list(self.cmd),
            "needs_robot": self.needs_robot,
            "hint": self.hint,
        }


@dataclass(frozen=True)
class App:
    id: str
    name: str
    emoji: str
    pitch: str
    description: str
    repo_path: Path
    github_url: str | None
    status: str
    status_detail: str
    needs: tuple[str, ...]
    requires_env: tuple[str, ...]
    mode: str
    robot: Command | None
    dev: Command | None
    url: str | None
    notes: tuple[str, ...]
    concierge: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- checks

    def command(self, mode: str) -> Command:
        """`mode` is "robot" or "dev"."""
        cmd = self.robot if mode == "robot" else self.dev if mode == "dev" else None
        if cmd is None:
            raise RegistryError(f"{self.id} has no {mode!r} command")
        return cmd

    def missing_env(self, environ: dict[str, str] | None = None) -> list[str]:
        env = os.environ if environ is None else environ
        return [k for k in self.requires_env if not env.get(k)]

    def script_path(self, mode: str) -> Path | None:
        """Absolute path of the script a command invokes, when it is a local one."""
        try:
            cmd = self.command(mode)
        except RegistryError:
            return None
        head = cmd.cmd[0]
        if head.startswith("./") or head.startswith("../"):
            return (self.repo_path / head).resolve()
        return None

    def prereqs(self, environ: dict[str, str] | None = None) -> dict[str, Any]:
        """Everything the UI needs to decide whether a button may be pressed."""
        scripts_ok = True
        for mode in ("robot", "dev"):
            p = self.script_path(mode)
            if p is not None and not p.exists():
                scripts_ok = False
        return {
            "repo_ok": self.repo_path.is_dir(),
            "scripts_ok": scripts_ok,
            "missing_env": self.missing_env(environ),
        }

    def to_dict(self, environ: dict[str, str] | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "emoji": self.emoji,
            "pitch": self.pitch,
            "description": self.description,
            "repo_path": str(self.repo_path),
            "github_url": self.github_url,
            "status": self.status,
            "status_label": STATUS_LABELS[self.status],
            "status_detail": self.status_detail,
            "needs": list(self.needs),
            "need_icons": [NEED_ICONS[n] for n in self.needs],
            "requires_env": list(self.requires_env),
            "mode": self.mode,
            "robot": self.robot.to_dict() if self.robot else None,
            "dev": self.dev.to_dict() if self.dev else None,
            "url": self.url,
            "notes": list(self.notes),
            "concierge": self.concierge,
            "prereqs": self.prereqs(environ),
        }


# ------------------------------------------------------------------ parsing


def _req(d: dict[str, Any], key: str, kind: type, where: str) -> Any:
    if key not in d:
        raise RegistryError(f"{where}: missing {key!r}")
    val = d[key]
    if not isinstance(val, kind):
        raise RegistryError(f"{where}: {key!r} must be {kind.__name__}, got {type(val).__name__}")
    return val


def _parse_command(raw: Any, where: str) -> Command | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise RegistryError(f"{where}: command must be an object or null")
    cmd = _req(raw, "cmd", list, where)
    if not cmd or not all(isinstance(c, str) and c for c in cmd):
        raise RegistryError(f"{where}: cmd must be a non-empty list of non-empty strings")
    return Command(
        label=_req(raw, "label", str, where),
        cmd=tuple(cmd),
        needs_robot=bool(_req(raw, "needs_robot", bool, where)),
        hint=raw.get("hint"),
    )


def _parse_app(raw: dict[str, Any]) -> App:
    where = f"app {raw.get('id', '?')!r}"
    app_id = _req(raw, "id", str, where)
    status = _req(raw, "status", str, where)
    if status not in STATUSES:
        raise RegistryError(f"{where}: unknown status {status!r} (expected one of {sorted(STATUSES)})")

    needs = _req(raw, "needs", list, where)
    bad = [n for n in needs if n not in NEEDS]
    if bad:
        raise RegistryError(f"{where}: unknown needs {bad} (expected a subset of {sorted(NEEDS)})")

    launch = _req(raw, "launch", dict, where)
    mode = _req(launch, "mode", str, f"{where}.launch")
    if mode not in {"process", "link"}:
        raise RegistryError(f"{where}.launch: mode must be 'process' or 'link', got {mode!r}")

    robot = _parse_command(launch.get("robot"), f"{where}.launch.robot")
    dev = _parse_command(launch.get("dev"), f"{where}.launch.dev")
    url = launch.get("url")

    if mode == "process" and robot is None and dev is None:
        raise RegistryError(f"{where}.launch: mode 'process' needs at least one command")
    if mode == "link" and not url:
        raise RegistryError(f"{where}.launch: mode 'link' needs a url")
    if robot is not None and not robot.needs_robot:
        raise RegistryError(f"{where}.launch.robot: needs_robot must be true")
    if dev is not None and dev.needs_robot:
        raise RegistryError(f"{where}.launch.dev: needs_robot must be false — that is the point of a dev mode")

    concierge = raw.get("concierge", {})
    if not isinstance(concierge, dict):
        raise RegistryError(f"{where}.concierge: must be an object")
    for key in ("activity", "resources", "exclusive", "interruptible", "voice_intents"):
        if key not in concierge:
            raise RegistryError(f"{where}.concierge: missing {key!r} — the concierge plan depends on it")
    bad = [r for r in concierge["resources"] if r not in NEEDS]
    if bad:
        raise RegistryError(f"{where}.concierge: unknown resources {bad}")

    return App(
        id=app_id,
        name=_req(raw, "name", str, where),
        emoji=_req(raw, "emoji", str, where),
        pitch=_req(raw, "pitch", str, where),
        description=_req(raw, "description", str, where),
        repo_path=Path(_req(raw, "repo_path", str, where)).expanduser(),
        github_url=raw.get("github_url"),
        status=status,
        status_detail=_req(raw, "status_detail", str, where),
        needs=tuple(needs),
        requires_env=tuple(raw.get("requires_env", [])),
        mode=mode,
        robot=robot,
        dev=dev,
        url=url,
        notes=tuple(raw.get("notes", [])),
        concierge=concierge,
    )


def load(path: Path | str | None = None) -> list[App]:
    """Parse and validate the registry. Raises RegistryError on anything off."""
    path = Path(path) if path is not None else REGISTRY_PATH
    data = json.loads(path.read_text())
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise RegistryError(f"schema_version {version!r}, this code speaks {SCHEMA_VERSION}")
    raw_apps = _req(data, "apps", list, "registry")
    apps = [_parse_app(a) for a in raw_apps]
    ids = [a.id for a in apps]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise RegistryError(f"duplicate app ids: {sorted(dupes)}")
    return apps


def by_id(apps: list[App], app_id: str) -> App:
    for a in apps:
        if a.id == app_id:
            return a
    raise KeyError(app_id)
