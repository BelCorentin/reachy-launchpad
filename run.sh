#!/usr/bin/env bash
# Start the launchpad on 127.0.0.1:7880 and open it.
#
# LOOPBACK ONLY, deliberately: this server executes shell commands (it launches
# the robot apps) with no authentication. Never bind it to 0.0.0.0.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST="127.0.0.1"
PORT="${LAUNCHPAD_PORT:-7880}"

cd "$ROOT"
uv sync --quiet

# Already up? Just open it again.
if curl -s -m2 "http://$HOST:$PORT/api/status" >/dev/null 2>&1; then
  echo "launchpad already running on http://$HOST:$PORT"
else
  echo "launchpad → http://$HOST:$PORT"
fi

( sleep 1
  for opener in xdg-open open; do
    command -v "$opener" >/dev/null && { "$opener" "http://$HOST:$PORT" >/dev/null 2>&1; break; }
  done ) &

exec uv run --quiet uvicorn --factory launchpad.app:get_app --host "$HOST" --port "$PORT"
