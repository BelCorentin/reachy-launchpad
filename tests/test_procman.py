"""Process manager against a fake long-running app — no robot, no network.

The fake mimics the real run.sh shape: a shell script that execs a python child
which prints and keeps running, so the process-group kill is genuinely exercised
(killing the shell alone would leave the python behind).
"""

import os
import time

import pytest

from launchpad.procman import LaunchError, ProcessManager

SCRIPT = """#!/usr/bin/env bash
echo "fake app starting"
exec python3 -u -c '
import sys, time
print("child alive", flush=True)
open(sys.argv[1], "w").write(str(__import__("os").getpid()))
while True:
    print("tick", flush=True)
    time.sleep(0.2)
' "$1"
"""


@pytest.fixture
def fake_app(tmp_path):
    script = tmp_path / "run.sh"
    script.write_text(SCRIPT)
    script.chmod(0o755)
    return tmp_path


def wait_for(pred, timeout=8.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_launch_run_logs_stop(fake_app):
    pm = ProcessManager()
    child_pidfile = fake_app / "child.pid"
    snap = pm.launch(
        app_id="fake", mode="dev", label="fake dev",
        cmd=["./run.sh", str(child_pidfile)], cwd=fake_app,
    )
    try:
        assert snap["running"] is True
        assert pm.is_running()
        assert pm.status()["run"]["app_id"] == "fake"
        assert pm.status()["run"]["uptime"] >= 0

        assert wait_for(lambda: pm.logs()["total"] >= 3), pm.logs()
        logs = pm.logs()
        assert "fake app starting" in logs["lines"]
        assert "child alive" in logs["lines"]

        # incremental polling hands back only what is new
        seen = logs["total"]
        later = pm.logs(since=seen)
        assert len(later["lines"]) == later["total"] - seen

        assert wait_for(lambda: child_pidfile.exists())
        child = int(child_pidfile.read_text())
        assert alive(child)
    finally:
        result = pm.stop()

    assert result["stopped"] is True
    assert result["signals"][0] == "SIGINT"
    assert not pm.is_running()
    # the whole group died, not just the shell
    assert wait_for(lambda: not alive(child)), "child survived the group kill"
    assert any("process exited" in ln for ln in pm.logs()["lines"])


def test_second_launch_is_refused_while_one_runs(fake_app):
    pm = ProcessManager()
    pm.launch(app_id="a", mode="dev", label="a", cmd=["./run.sh", str(fake_app / "p1")], cwd=fake_app)
    try:
        with pytest.raises(LaunchError, match="already running"):
            pm.launch(app_id="b", mode="dev", label="b", cmd=["./run.sh", str(fake_app / "p2")], cwd=fake_app)
    finally:
        pm.stop()


def test_relaunch_allowed_after_stop(fake_app):
    pm = ProcessManager()
    pm.launch(app_id="a", mode="dev", label="a", cmd=["./run.sh", str(fake_app / "p1")], cwd=fake_app)
    pm.stop()
    snap = pm.launch(app_id="b", mode="dev", label="b", cmd=["./run.sh", str(fake_app / "p2")], cwd=fake_app)
    try:
        assert snap["app_id"] == "b"
    finally:
        pm.stop()


def test_stubborn_child_is_escalated_to_sigterm(tmp_path):
    """A child that ignores SIGINT still dies."""
    script = tmp_path / "run.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "exec python3 -u -c '\n"
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "print(\"ignoring SIGINT\", flush=True)\n"
        "while True: time.sleep(0.1)\n'\n"
    )
    script.chmod(0o755)
    pm = ProcessManager()
    snap = pm.launch(app_id="stubborn", mode="dev", label="x", cmd=["./run.sh"], cwd=tmp_path)
    assert wait_for(lambda: pm.logs()["total"] >= 1)
    result = pm.stop(timeout_int=0.6, timeout_term=2.0)
    assert result["stopped"] is True
    assert "SIGTERM" in result["signals"]
    assert not alive(snap["pid"])


def test_stop_with_nothing_running():
    pm = ProcessManager()
    assert pm.stop() == {"stopped": False, "reason": "nothing running"}
    assert pm.status() == {"running": False, "run": None}
    assert pm.logs()["lines"] == []


def test_missing_repo_and_missing_script(tmp_path):
    pm = ProcessManager()
    with pytest.raises(LaunchError, match="repo not found"):
        pm.launch(app_id="x", mode="dev", label="x", cmd=["./run.sh"], cwd=tmp_path / "nope")
    with pytest.raises(LaunchError, match="launch script not found"):
        pm.launch(app_id="x", mode="dev", label="x", cmd=["./run.sh"], cwd=tmp_path)


def test_non_executable_script_is_refused(tmp_path):
    script = tmp_path / "run.sh"
    script.write_text("#!/usr/bin/env bash\ntrue\n")
    script.chmod(0o644)
    pm = ProcessManager()
    with pytest.raises(LaunchError, match="not executable"):
        pm.launch(app_id="x", mode="dev", label="x", cmd=["./run.sh"], cwd=tmp_path)


def test_ring_buffer_keeps_the_tail_and_counts_everything(tmp_path):
    script = tmp_path / "run.sh"
    script.write_text("#!/usr/bin/env bash\nfor i in $(seq 1 50); do echo line-$i; done\n")
    script.chmod(0o755)
    pm = ProcessManager(log_lines=10)
    pm.launch(app_id="chatty", mode="dev", label="x", cmd=["./run.sh"], cwd=tmp_path)
    assert wait_for(lambda: not pm.is_running())
    time.sleep(0.2)
    logs = pm.logs()
    assert logs["total"] == 51                      # 50 lines + the exit marker
    assert len(logs["lines"]) == 10                 # ring buffer kept the tail
    assert "line-50" in logs["lines"]
    assert "line-1" not in logs["lines"]
    # asking for everything since 0 cannot invent the dropped lines
    assert pm.logs(since=0)["lines"] == logs["lines"]
