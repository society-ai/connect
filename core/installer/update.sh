#!/usr/bin/env bash
# Update the Society AI bridge to the latest released code, restart it, and
# roll back automatically if the new version does not come up.
#
# Usage: ./connect update [persona]
#
# persona     Optional. Updates the shared code once, then restarts that
#             persona's bridge service (label
#             io.societyai.claude-code-bridge.<persona>). Omit for the
#             default persona.
#
# The bridge launches this script itself (detached, so it survives the
# bridge's own restart) when you approve an update from Society AI. It is
# equally fine to run by hand from this folder.
#
# What it does, in order:
#   1. Records the version you are on now (git commit + adapter version).
#   2. Fetches the latest release (a tag vX.Y.Z), checks it is the code
#      Society AI released (core/installer/release.sh), and switches to it;
#      refuses to touch a tree with local changes to tracked files.
#   3. Installs dependencies, but only if requirements.txt or the channel
#      server's package-lock.json changed.
#   4. Restarts the bridge service and waits for it to register with the
#      hub (up to a minute).
#   5. On success, records the outcome. On any failure it puts the old
#      version back, restarts again, and records what happened.
#
# The outcome lands in <state dir>/update-result.json. The restarted bridge
# waits for it while <state dir>/update-pending.json is there and posts the
# result to your feed, so you hear how this update went even though the
# updated process is a new one.
#
# When this folder is already on the latest release (another agent on this
# machine updated it) and the bridge that asked runs an older version
# (SOCIETY_AI_RUNNING_VERSION, set by the bridge), it is restarted onto it.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"  # core/installer/ -> the repo root
cd "$REPO_DIR"

PERSONA="${1:-}"
if [ -n "$PERSONA" ]; then
    # Same shape service.sh accepts — also keeps the persona from carrying
    # path separators into the state dir below.
    if ! printf '%s' "$PERSONA" | grep -Eq '^[a-z0-9][a-z0-9._-]{0,62}$'; then
        echo "Error: invalid persona name: $PERSONA" >&2
        echo "(lowercase alphanumerics plus - _ . , max 63 chars)" >&2
        exit 1
    fi
    STATE_DIR="$HOME/.cache/society-ai/$PERSONA"
else
    STATE_DIR="$HOME/.cache/society-ai"
fi

MARKER="$STATE_DIR/update-result.json"
# Written right before a restart, removed with the outcome: the restarted bridge waits for the
# outcome while it is there, so it posts this update and not the one before.
PENDING="$STATE_DIR/update-pending.json"
BRIDGE_OUT_LOG="$STATE_DIR/bridge.log"
BRIDGE_ERR_LOG="$STATE_DIR/bridge.err.log"
LOCK_DIR="$REPO_DIR/.update.lock"

mkdir -p "$STATE_DIR"

step() {
    echo ""
    echo "==> $*"
}

# The adapter version from config.py ("" if it cannot be read). Read fresh
# each time — it changes when the pull lands.
adapter_version() {
    grep -E '^__version__ *= *"' core/config.py 2>/dev/null | head -1 | \
        sed -E 's/.*"([^"]+)".*/\1/' || true
}

# Write the outcome marker the bridge reports from at its next start.
# $1 = ok (true/false)  $2 = from  $3 = to  $4 = error ("" for none)
# $5 = rolled_back (true/false)
write_marker() {
    python3 - "$MARKER" "$1" "$2" "$3" "$4" "$5" <<'PYEOF'
import datetime
import json
import sys
path, ok, frm, to, err, rolled_back = sys.argv[1:7]
data = {
    "ok": ok == "true",
    "from": frm,
    "to": to,
    "error": err or None,
    "rolled_back": rolled_back == "true",
    "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
with open(path, "w") as f:
    json.dump(data, f)
PYEOF
    rm -f "$PENDING"
    echo "    Outcome recorded in $MARKER"
}

write_pending() {
    date -u +'{"at": "%Y-%m-%dT%H:%M:%SZ"}' > "$PENDING"
}

# ── Bridge registration check ──────────────────────────────────────────────
# Mirrored from setup.sh's snapshot_bridge_logs / verify_bridge_connected —
# keep the two in sync. (Not sourced from setup.sh because that file is an
# installer with top-level side effects.)

# Snapshot the byte sizes of the two bridge logs BEFORE the service starts,
# so verify_bridge_connected only trusts lines written by this run.
snapshot_bridge_logs() {
    OUT_OFFSET=$({ wc -c < "$BRIDGE_OUT_LOG"; } 2>/dev/null || echo 0)
    ERR_OFFSET=$({ wc -c < "$BRIDGE_ERR_LOG"; } 2>/dev/null || echo 0)
    OUT_OFFSET=$((OUT_OFFSET + 0))
    ERR_OFFSET=$((ERR_OFFSET + 0))
}

# Watch the bridge logs for the connection verdict. Reads ONLY bytes
# written after the snapshot above, so lines left over from before the
# restart can never produce a false verdict.
# Prints exactly one of: connected | auth-failed | timeout
verify_bridge_connected() {
    local waited=0 new_lines
    while :; do
        new_lines="$( { tail -c +"$((OUT_OFFSET + 1))" "$BRIDGE_OUT_LOG" 2>/dev/null
                        tail -c +"$((ERR_OFFSET + 1))" "$BRIDGE_ERR_LOG" 2>/dev/null; } || true )"
        if printf '%s\n' "$new_lines" | grep -q "Auth failed exchanging API key"; then
            echo "auth-failed"
            return 0
        fi
        if printf '%s\n' "$new_lines" | grep -q "Registered as "; then
            echo "connected"
            return 0
        fi
        if [ "$waited" -ge 60 ]; then
            echo "timeout"
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
}

# ── Failure paths ──────────────────────────────────────────────────────────

# Stop before anything changed. The service (if running) is untouched, so
# nothing gets rolled back — we just record why the update did not happen.
abort() {
    local err="$1"
    echo "" >&2
    echo "Update stopped: $err" >&2
    write_marker false "$FROM_LABEL" "$TO_LABEL" "$err" false
    exit 1
}

# Undo the update: put the previous code back, reinstall its dependencies
# if they had changed, restart the bridge, and record what happened. Called
# after the pull landed, so the tree is at the NEW commit when we get here.
rollback() {
    local err="$1" verdict
    step "The update did not take: $err"
    echo "    Putting the previous version back ($FROM_LABEL)..."

    # Tracked files only — untracked files (.env, .env.<persona>, venv/)
    # are never touched by a hard reset.
    if ! git reset --hard "$FROM_SHA"; then
        write_marker false "$FROM_LABEL" "$TO_LABEL" \
            "$err (and restoring the previous version failed too — the tree may need attention)" false
        exit 1
    fi

    if [ "$REQS_CHANGED" = "yes" ]; then
        echo "    Reinstalling the previous dependencies..."
        # Best effort: a pip failure here should not stop the restart below.
        ./venv/bin/pip install -r requirements.txt || \
            echo "    Warning: dependency reinstall failed; the bridge may still start fine."
    fi
    if [ "$CHANNEL_DEPS_CHANGED" = "yes" ]; then
        echo "    Reinstalling the previous channel server dependencies..."
        (cd agents/claude_code/runner/channel && npm ci --silent) || \
            echo "    Warning: channel reinstall failed; sessions may fall back to the spawn path."
    fi

    echo "    Restarting the bridge on the previous version..."
    write_pending
    snapshot_bridge_logs
    if ! ./connect service restart ${PERSONA:+"$PERSONA"}; then
        write_marker false "$FROM_LABEL" "$TO_LABEL" \
            "$err (and the service did not restart after rollback — run ./connect service start ${PERSONA:-} by hand)" true
        exit 1
    fi

    echo "    Waiting for the bridge to register again (up to a minute)..."
    verdict="$(verify_bridge_connected)"
    if [ "$verdict" = "connected" ]; then
        write_marker false "$FROM_LABEL" "$TO_LABEL" "$err" true
        echo "    Rolled back. The bridge is running the previous version again."
    else
        write_marker false "$FROM_LABEL" "$TO_LABEL" \
            "$err (and after rollback the bridge did not confirm registration: $verdict)" true
        echo "    Rolled back, but the bridge did not confirm it is connected ($verdict)."
        echo "    Check the log: $BRIDGE_ERR_LOG"
    fi
    exit 1
}

# ── The update itself ──────────────────────────────────────────────────────

echo "Society AI bridge updater"
[ -n "$PERSONA" ] && echo "Persona: $PERSONA"

# These are filled in as we learn them; abort/rollback read them.
FROM_SHA=""
FROM_LABEL=""
TO_LABEL=""
REQS_CHANGED="no"
CHANNEL_DEPS_CHANGED="no"

step "Making sure no other update is running"
# Personas share this folder, so two bridges approving an update at the
# same time must take turns. Whoever gets here second waits; once the
# first finishes, the second sees "already up to date" and simply
# restarts onto the fresh code.
WAITED=0
until mkdir "$LOCK_DIR" 2>/dev/null; do
    if [ "$WAITED" -ge 120 ]; then
        FROM_LABEL="$(adapter_version)"
        abort "another update has held the lock for over 2 minutes (stale? remove $LOCK_DIR by hand)"
    fi
    echo "    Another update is running, waiting..."
    sleep 2
    WAITED=$((WAITED + 2))
done
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT
echo "    Clear."

step "Recording the current version"
FROM_SHA="$(git rev-parse HEAD)"
FROM_LABEL="$(adapter_version)"
[ -n "$FROM_LABEL" ] || FROM_LABEL="$(git rev-parse --short HEAD)"
echo "    Currently on $FROM_LABEL (commit $(git rev-parse --short HEAD))"

step "Checking the working tree"
DIRTY="$(git status --porcelain --untracked-files=no || true)"
if [ -n "$DIRTY" ]; then
    echo "    These tracked files have local changes:"
    printf '%s\n' "$DIRTY" | head -10 | sed 's/^/      /'
    abort "tracked files have local changes; commit, stash, or restore them, then run ./connect update again"
fi
echo "    Clean."

step "Fetching the latest release"
# Only releases are installed: tags vX.Y.Z that Society AI's release workflow built and signed
# (core/installer/release.sh).
# shellcheck source=release.sh
source "$REPO_DIR/core/installer/release.sh"
LATEST_TAG="$(latest_release_tag origin)"
if [ -z "$LATEST_TAG" ]; then
    abort "no release found (is the network up?)"
fi
if ! git fetch --quiet origin "refs/tags/$LATEST_TAG:refs/tags/$LATEST_TAG"; then
    abort "could not download release $LATEST_TAG (is the network up?)"
fi
if [ "$(git rev-parse "$LATEST_TAG^{commit}")" != "$FROM_SHA" ]; then
    if ! verify_release "$LATEST_TAG"; then
        abort "release $LATEST_TAG did not pass its check, so it was not installed"
    fi
    if ! git checkout --quiet --detach "$LATEST_TAG"; then
        abort "could not switch to release $LATEST_TAG"
    fi
fi

TO_SHA="$(git rev-parse HEAD)"
TO_LABEL="$(adapter_version)"
[ -n "$TO_LABEL" ] || TO_LABEL="$(git rev-parse --short HEAD)"

if [ "$TO_SHA" = "$FROM_SHA" ]; then
    # Another agent on this machine already updated this folder, but the bridge that asked
    # still runs the version it started with: restart it onto the code here.
    RUNNING="${SOCIETY_AI_RUNNING_VERSION:-}"
    if [ -n "$RUNNING" ] && [ "$RUNNING" != "$FROM_LABEL" ]; then
        step "This folder is already on $FROM_LABEL; restarting the bridge, which runs $RUNNING"
        write_pending
        snapshot_bridge_logs
        if ! ./connect service restart ${PERSONA:+"$PERSONA"}; then
            write_marker false "$RUNNING" "$FROM_LABEL" "the service did not restart" false
            exit 1
        fi
        VERDICT="$(verify_bridge_connected)"
        if [ "$VERDICT" = "connected" ]; then
            write_marker true "$RUNNING" "$FROM_LABEL" "" false
            echo ""
            echo "Done! The bridge now runs $FROM_LABEL."
        else
            write_marker false "$RUNNING" "$FROM_LABEL" \
                "the bridge did not confirm registration after the restart ($VERDICT)" false
            echo "The bridge did not confirm it is connected ($VERDICT). Check the log: $BRIDGE_ERR_LOG"
            exit 1
        fi
        exit 0
    fi
    echo ""
    echo "Already up to date ($FROM_LABEL). Nothing to do."
    exit 0
fi
echo "    Got $TO_LABEL (commit $(git rev-parse --short HEAD))"

step "Checking dependencies"
if git diff --quiet "$FROM_SHA" "$TO_SHA" -- requirements.txt; then
    echo "    requirements.txt did not change; skipping the install."
else
    REQS_CHANGED="yes"
    echo "    requirements.txt changed; installing into the venv..."
    if ! ./venv/bin/pip install -r requirements.txt; then
        rollback "installing the new dependencies failed"
    fi
fi
# The channel server (Node) only matters when npm is here and it was
# installed before; setup.sh owns first installs.
if [ -d agents/claude_code/runner/channel/node_modules ] && command -v npm &>/dev/null \
    && ! git diff --quiet "$FROM_SHA" "$TO_SHA" -- agents/claude_code/runner/channel/package-lock.json; then
    CHANNEL_DEPS_CHANGED="yes"
    echo "    The channel server's package-lock.json changed; installing..."
    if ! (cd agents/claude_code/runner/channel && npm ci --silent); then
        rollback "installing the new channel server dependencies failed"
    fi
fi

step "Restarting the bridge on the new version"
write_pending
snapshot_bridge_logs
if ! ./connect service restart ${PERSONA:+"$PERSONA"}; then
    rollback "the service did not restart"
fi

step "Waiting for the bridge to register (up to a minute)"
VERDICT="$(verify_bridge_connected)"
case "$VERDICT" in
    connected)
        write_marker true "$FROM_LABEL" "$TO_LABEL" "" false
        echo ""
        echo "Done! Updated from $FROM_LABEL to $TO_LABEL."
        echo "The bridge is connected and running the new version."
        ;;
    auth-failed)
        rollback "the bridge could not sign in after the update"
        ;;
    *)
        rollback "the bridge did not confirm registration within a minute of the update"
        ;;
esac
