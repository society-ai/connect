#!/usr/bin/env bash
# Launch the local Society AI status & control panel and open it in a browser.
#
# The panel is localhost-only (127.0.0.1). It shows each persona's bridge
# health + live sessions and lets you edit the user-controllable .env config
# and connect/disconnect/restart agents. A token (printed below, also stored
# 0600 at ~/.cache/society-ai/status-token) gates every action.
#
# Usage:
#   ./connect status          # start the panel and open the browser
#   ./connect status --no-open# start without opening a browser
#   STATUS_PORT=9000 ./connect status

set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"  # core/installer/ -> the repo root
cd "$REPO_DIR"

if [ ! -x "venv/bin/python" ]; then
    echo "Error: venv/bin/python not found. Run ./connect setup first." >&2
    exit 1
fi

OPEN_FLAG="--open"
[ "${1:-}" = "--no-open" ] && OPEN_FLAG=""

exec ./venv/bin/python -m core.status_server $OPEN_FLAG
