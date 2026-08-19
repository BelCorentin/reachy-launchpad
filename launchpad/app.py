"""The launchpad server.

SECURITY — READ THIS BEFORE CHANGING THE BIND ADDRESS.
This process shells out: POST /api/launch runs a command from the registry on
this laptop, with this user's environment (including ANTHROPIC_API_KEY and the
HF token). There is no authentication and there never will be. It binds
127.0.0.1 and nothing else. `assert_local_only()` below is called by the
launcher; if you ever need this on the network, put a real auth layer in front
and route to localhost — do not change the bind.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import registry as reg
from .procman import LaunchError, ProcessManager
from .robot import RobotProbe

HOST = "127.0.0.1"       # loopback only — see the module docstring
PORT = 7880

STATIC = Path(__file__).with_name("static")


def assert_local_only(host: str) -> None:
    """Refuse to start on anything but loopback."""
    assert host in {"127.0.0.1", "localhost", "::1"}, (
        f"reachy-launchpad must bind loopback only (got {host!r}): it executes "
        "shell commands with no authentication."
    )


class LaunchBody(BaseModel):
    app_id: str
    mode: str = "dev"          # "robot" | "dev"
    force: bool = False        # stop whatever runs first


def create_app(
    apps: list[reg.App] | None = None,
    *,
    manager: ProcessManager | None = None,
    probe: RobotProbe | None = None,
) -> FastAPI:
    apps = reg.load() if apps is None else apps
    manager = manager or ProcessManager()
    probe = probe or RobotProbe()

    api = FastAPI(title="reachy launchpad", docs_url=None, redoc_url=None)
    api.state.apps = apps
    api.state.manager = manager
    api.state.probe = probe

    # ------------------------------------------------------------- pages

    @api.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    # -------------------------------------------------------------- data

    @api.get("/api/apps")
    def list_apps() -> dict[str, Any]:
        return {
            "apps": [a.to_dict() for a in apps],
            "concierge_teaser": {
                "id": "concierge",
                "emoji": "🎙️",
                "name": "reachy concierge",
                "pitch": "chat with reachy to pick the activity — this registry is its menu",
                "status": "plan, not started",
            },
        }

    @api.get("/api/robot")
    def robot_status(force: bool = False) -> dict[str, Any]:
        return probe.status(force=force)

    @api.post("/api/robot/preflight/{action}")
    def robot_preflight(action: str) -> dict[str, Any]:
        result = probe.preflight(action)
        if not result.get("ok"):
            return JSONResponse(status_code=409, content=result)  # type: ignore[return-value]
        return result

    @api.get("/api/status")
    def status() -> dict[str, Any]:
        return manager.status()

    @api.get("/api/logs")
    def logs(since: int = 0) -> dict[str, Any]:
        return manager.logs(since=since)

    # ----------------------------------------------------------- actions

    @api.post("/api/launch")
    def launch(body: LaunchBody) -> dict[str, Any]:
        try:
            app = reg.by_id(apps, body.app_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown app {body.app_id!r}")
        if body.mode not in {"robot", "dev"}:
            raise HTTPException(status_code=400, detail="mode must be 'robot' or 'dev'")
        if app.mode == "link":
            raise HTTPException(
                status_code=400,
                detail=f"{app.id} is a web app — open {app.url} instead of launching it",
            )
        try:
            command = app.command(body.mode)
        except reg.RegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        missing = app.missing_env()
        if missing:
            raise HTTPException(
                status_code=412,
                detail=f"{', '.join(missing)} not set in the launchpad's environment",
            )

        if command.needs_robot and not probe.status()["reachable"]:
            raise HTTPException(
                status_code=412,
                detail="robot is not reachable — power it on, or use the dev mode",
            )

        if manager.is_running():
            if not body.force:
                cur = manager.status()["run"]
                raise HTTPException(
                    status_code=409,
                    detail=f"{cur['app_id']} ({cur['label']}) is running — stop it first",
                )
            manager.stop()

        try:
            snap = manager.launch(
                app_id=app.id,
                mode=body.mode,
                label=command.label,
                cmd=command.cmd,
                cwd=app.repo_path,
            )
        except LaunchError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return {"launched": True, "run": snap}

    @api.post("/api/stop")
    def stop() -> dict[str, Any]:
        return manager.stop()

    return api


app = None  # built by main() / uvicorn factory below


def get_app() -> FastAPI:
    return create_app()


def main() -> None:
    import uvicorn

    assert_local_only(HOST)
    host = os.environ.get("LAUNCHPAD_HOST", HOST)
    assert_local_only(host)
    port = int(os.environ.get("LAUNCHPAD_PORT", PORT))
    uvicorn.run(create_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
