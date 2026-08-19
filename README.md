# reachy-launchpad

One local page for every Reachy Mini app on this laptop: what each one is, how it
works, whether the robot is even on, and one button to run it.

```
┌─ 🟢 robot up · daemon 1.9.0 · motors enabled ──── 🦾 enable motors · 🎥 acquire media ─┐
│  🎻 sitar guru   🧠 mémoire   🎵 jukebox   👁️ narrator   ✨ reachying for the stars   │
│  ▶ run on robot   🧪 dev mode   ⏹ stop   📜 logs                                       │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

The robot is a **loan and is usually off**, so the page is built to be useful with
no robot at all: every app that has a robot-free mode gets a 🧪 button that stays
enabled, and the robot-only buttons grey out with a tooltip saying why.

It is also the **first brick of [Reachy Concierge](#the-concierge-connection)** — the
planned voice hub where you just ask Reachy for an activity. The registry here is
that hub's menu.

## Run it

```bash
./run.sh          # uv sync + uvicorn on 127.0.0.1:7880 + opens a browser
```

## Security — loopback only, on purpose

This server **executes shell commands** (that is its whole job) with your
environment, including `ANTHROPIC_API_KEY` and your HF token, and it has **no
authentication**. It binds `127.0.0.1` and `launchpad/app.py::assert_local_only`
refuses anything else. If you ever need it from a phone, put a real
authenticating proxy in front of it and forward to localhost — do not change the
bind address. (Same rule as reachy-memoire's :7860 vs :7870 split.)

## What is in it

| file | what |
|---|---|
| `launchpad/registry.json` | the data: one entry per app |
| `launchpad/registry.py` | the schema + validation (documented in its docstring) |
| `launchpad/procman.py` | one app at a time, own process group, log ring buffer |
| `launchpad/robot.py` | timeout-guarded daemon probe, 5 s cache, the two preflight POSTs |
| `launchpad/app.py` | FastAPI: `/api/apps`, `/api/robot`, `/api/launch`, `/api/stop`, `/api/logs` |
| `launchpad/static/index.html` | the whole frontend — vanilla JS, no build step, no CDN |

House style copied from reachy-memoire's hub: FastAPI + everything inline, works
on a LAN with no internet.

### The apps it knows about

| app | status | robot-free mode |
|---|---|---|
| 🎻 [sitar guru](https://github.com/BelCorentin/reachy-sitar-guru) | built, not robot-tested | `./run.sh --dry-run --source demo` |
| 🧠 [mémoire](https://github.com/BelCorentin/reachy-memoire) | robot-tested (bring-up + face lock) | — (use `reachy-mini-daemon --sim`) |
| 🎵 [jukebox](https://github.com/BelCorentin/reachy-jukebox) | built, not robot-tested | `./run.sh --source webcam` |
| 👁️ [narrator](https://github.com/BelCorentin/reachy-narrator) | built, not robot-tested | `./run.sh --source webcam` |
| ✨ [reachying for the stars](https://github.com/BelCorentin/reachyng-for-the-stars) | deployed (private HF Space) | opens in the browser, not launched |

## Operations

**Start / stop.** `./run.sh` in a terminal; Ctrl-C stops the launchpad. Stopping
the launchpad does **not** stop a robot app it started — the child lives in its
own process group. Press ⏹ in the page first, or `pkill -g <pid>`.

**One app at a time.** Launching while something runs returns 409. The page's
buttons grey out with the reason. This is not politeness: they all fight over the
same mic, camera, motors and WebRTC media session. Use the ⏹ button, or POST
`/api/launch` with `{"force": true}` to stop-then-start.

**Stopping.** SIGINT first (the apps say goodbye and re-centre the head on it),
SIGTERM after 5 s, SIGKILL after 3 more — always to the whole process group, so
`run.sh`'s python child dies with the shell.

**Logs.** Last 200 lines per run, polled every 1.5 s by the 📜 drawer. This is a
tail for eyeballing, not the record — each app writes its own `logs/run-*.log`.

**Robot preflights.** The two POSTs every app's `run.sh` already does, exposed as
buttons for when you want them without launching anything:
`/api/motors/set_mode/enabled` (motors boot **disabled**, and go back to disabled
after every daemon update — silently) and `/api/media/acquire` (the WebRTC
signalling server on :8443 only starts after it).

**Robot probe.** `REACHY_HOST` if set, then mDNS `reachy-mini.local`, then
`192.168.1.8` (the last address it had on home wifi — mDNS goes cold after
restarts). 1.5 s timeout per host, answer cached 5 s; with the robot off the
whole probe takes ~7 s the first time (cold mDNS), which is why the cards render
before the status pill resolves. Reads the daemon version
and motor mode out of `/api/daemon/status` by searching for the fields rather
than assuming the payload shape, because it changed between 1.8.3 and 1.9.0.

**Prerequisites.** An app whose `requires_env` is unset (narrator's
`ANTHROPIC_API_KEY`) is blocked with a 412 and an explanatory tooltip. The env is
read from the **launchpad's** environment, so export it in the shell you start
`./run.sh` from.

**Adding an app.** Append an entry to `launchpad/registry.json` and run the
tests — they check the repo exists, the script exists and is executable, the
status is one of the three known values, and the concierge block is complete.

## Tests

```bash
uv run pytest
```

No robot, no network: the daemon probe is an `httpx.MockTransport`, and the
process manager is exercised against a fake `run.sh` that execs a python child —
so the process-group kill is genuinely tested (a child that ignores SIGINT is
escalated, and a survivor fails the test).

## The concierge connection

The plan on top of this: **Reachy Concierge**, a voice hub where you say « accorde
mon sitar » and Reachy hands the mic, speaker and motors to the tuning coach, then
takes them back when you are done. Its layers: a resource manager that owns the
devices, a thin voice shell (VAD + STT + tool-use + TTS), activities as plugins
behind a common interface, and read-only connectors (curiosity quiz, PhD standup).

Every registry entry already carries a `concierge` block declaring what the
activity needs, whether it can be interrupted, whether it wants raw mic frames,
and how you would ask for it out loud. The launchpad is the registry's first
consumer; the concierge is the second.

Full plan: the vault note `Reachy Concierge` (`999. 🌳 LIFE/06 Personal
Projects/Coding related/`).
