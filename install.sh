#!/usr/bin/env bash
# Society AI Connect installer.
#
# Fetched and piped to bash by the one-command install the product renders:
#   curl -fsSL https://raw.githubusercontent.com/society-ai/connect/main/install.sh \
#     | bash -s -- --token sai_... --name my-agent [--url http://...] --yes
#
# Works from any folder. When this machine already has the agent installed,
# the existing checkout is found and updated in place wherever it lives, so
# the same command rotates a token or adds a persona without the user having
# to remember (or be told) which directory to stand in. Only a machine with
# no install at all clones, into ./society-ai-connect.
#
# Where it looks, in order:
#   0. --dir <folder>, when given: that checkout, or a new install there.
#   1. The current folder, if it is itself a checkout.
#   2. ./society-ai-connect, if it is a checkout.
#   3. The folder recorded at ~/.cache/society-ai/install-dir.
#   4. The WorkingDirectory of every installed bridge LaunchAgent (macOS).
# Among 3 and 4, a checkout that already knows the --name being set up wins;
# failing that, a single known checkout is used. Two or more unrelated
# checkouts is the one case that stops and asks, because picking for the
# user could point a live agent's service at the wrong code. The same folder
# reached by two spellings (coding vs Coding on a case-insensitive Mac disk,
# or through a symlink) counts once.
#
# Everything past this file's own resolution is setup.sh's job: it receives
# every argument except --dir untouched and guards all of its own re-run cases.
set -u

REPO_URL="https://github.com/society-ai/connect.git"
DIR="society-ai-connect"
STATE_DIR="$HOME/.cache/society-ai"
POINTER_FILE="$STATE_DIR/install-dir"
PLIST_DIR="$HOME/Library/LaunchAgents"

if ! command -v git >/dev/null 2>&1; then
    echo ""
    echo "  Git is not installed. On a Mac it comes with Apple's developer"
    echo "  tools. Install them with:"
    echo ""
    echo "      xcode-select --install"
    echo ""
    echo "  Then run this command again."
    exit 1
fi

# ── Read the arguments ─────────────────────────────────────────────────────
# Takes out --dir, which is this file's alone, and peeks at the agent name to
# pick between several existing checkouts. Everything else goes on to
# setup.sh, which parses it properly. Values of setup.sh's flags are skipped
# as values, so a value can never be mistaken for --dir.
AGENT_NAME=""
INSTALL_DIR=""
DIR_GIVEN=0
SETUP_ARGS=()
expect_value=""
for arg in "$@"; do
    if [ -n "$expect_value" ]; then
        case "$expect_value" in
            dir)  INSTALL_DIR="$arg" ;;
            name) AGENT_NAME="$arg"; SETUP_ARGS+=("$arg") ;;
            *)    SETUP_ARGS+=("$arg") ;;
        esac
        expect_value=""
        continue
    fi
    case "$arg" in
        --dir)                expect_value=dir; DIR_GIVEN=1 ;;
        --dir=*)              INSTALL_DIR="${arg#*=}"; DIR_GIVEN=1 ;;
        --name|--persona)     expect_value=name; SETUP_ARGS+=("$arg") ;;
        --name=*|--persona=*) AGENT_NAME="${arg#*=}"; SETUP_ARGS+=("$arg") ;;
        --token|--display-name|--url|--type|--command)
                              expect_value=other; SETUP_ARGS+=("$arg") ;;
        *)                    SETUP_ARGS+=("$arg") ;;
    esac
done
if [ "$expect_value" = dir ] || { [ "$DIR_GIVEN" = 1 ] && [ -z "$INSTALL_DIR" ]; }; then
    echo ""
    echo "  --dir needs a folder after it, for example:"
    echo ""
    echo "      --dir ~/society-ai-connect"
    echo ""
    exit 1
fi
# A ~ inside --dir=~/x is not expanded by the shell; do it here.
case "$INSTALL_DIR" in
    "~")   INSTALL_DIR="$HOME" ;;
    "~/"*) INSTALL_DIR="$HOME/${INSTALL_DIR#"~/"}" ;;
esac

# ── Helpers ────────────────────────────────────────────────────────────────

# A directory is a usable install when it is a git checkout of this repo's
# working tree shape. setup.sh is the file we are about to exec, so its
# absence means the folder cannot serve us even if .git is there.
is_checkout() {
    [ -n "${1:-}" ] && [ -d "$1/.git" ] && [ -f "$1/connect" ]
}

abs_path() {
    (cd "$1" 2>/dev/null && pwd)
}

# Does this checkout already hold the identity being set up? Either as an
# additional persona (.env.<name>) or as the primary one (AGENT_NAME in
# .env). Same read of AGENT_NAME that setup.sh does.
knows_agent() {
    local dir="$1" name="$2" primary
    [ -n "$name" ] || return 1
    [ -f "$dir/.env.$name" ] && return 0
    primary="$(grep -E '^AGENT_NAME=' "$dir/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    [ "$primary" = "$name" ]
}

# WorkingDirectory out of an installed LaunchAgent plist. plistlib when
# python3 is around (it is what service.sh writes these with), otherwise a
# read of the XML the template produces.
plist_workdir() {
    local plist="$1" value=""
    if command -v python3 >/dev/null 2>&1; then
        value="$(python3 -c 'import plistlib, sys
with open(sys.argv[1], "rb") as f:
    print(plistlib.load(f).get("WorkingDirectory", ""))' "$plist" 2>/dev/null || true)"
    fi
    if [ -z "$value" ]; then
        value="$(grep -A1 '<key>WorkingDirectory</key>' "$plist" 2>/dev/null \
            | sed -n 's/.*<string>\(.*\)<\/string>.*/\1/p' | head -1 || true)"
    fi
    printf '%s' "$value"
}

CANDIDATES=""
add_candidate() {
    local dir resolved known
    dir="${1:-}"
    [ -n "$dir" ] || return 0
    resolved="$(abs_path "$dir")" || return 0
    [ -n "$resolved" ] || return 0
    is_checkout "$resolved" || return 0
    # Same folder, not same spelling: a Mac disk ignores letter case, so
    # ~/coding/x and ~/Coding/x are one folder, and a symlink is another way
    # to reach the same one. -ef compares the folders themselves.
    while IFS= read -r known; do
        [ -n "$known" ] || continue
        [ "$resolved" -ef "$known" ] && return 0
    done <<EOF
$CANDIDATES
EOF
    CANDIDATES="${CANDIDATES}${resolved}
"
}

# The command the user ran, rebuilt with --dir <folder> so they can paste it
# back as is. Every argument is shell-quoted; the token is in it, as it was in
# the command they pasted.
retry_command() {
    local cmd arg
    cmd="curl -fsSL https://raw.githubusercontent.com/society-ai/connect/main/install.sh | bash -s --"
    for arg in ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}; do
        cmd="$cmd $(printf '%q' "$arg")"
    done
    printf '%s --dir %s' "$cmd" "$(printf '%q' "$1")"
}

remember_install_dir() {
    mkdir -p "$STATE_DIR" 2>/dev/null || return 0
    printf '%s\n' "$1" >"$POINTER_FILE" 2>/dev/null || true
}

# ── Resolve which checkout to use ──────────────────────────────────────────
TARGET=""

if [ -n "$INSTALL_DIR" ]; then
    # Named on the command line: use it if it holds the agent, otherwise
    # install a new copy there (below). Discovery is skipped either way.
    if is_checkout "$INSTALL_DIR"; then
        TARGET="$(abs_path "$INSTALL_DIR")"
    elif [ -e "$INSTALL_DIR" ]; then
        echo ""
        echo "  $INSTALL_DIR is not a folder the agent is installed in."
        echo ""
        echo "  After --dir, give either a folder that holds the agent, or a"
        echo "  new folder name to install it into, for example:"
        echo ""
        echo "      --dir ~/society-ai-connect"
        echo ""
        exit 1
    else
        DIR="$INSTALL_DIR"
    fi
elif is_checkout "$PWD"; then
    # Standing inside a checkout is an explicit choice: honour it, and never
    # let discovery second-guess it.
    TARGET="$PWD"
elif is_checkout "$PWD/$DIR"; then
    TARGET="$(abs_path "$PWD/$DIR")"
else
    if [ -f "$POINTER_FILE" ]; then
        add_candidate "$(head -1 "$POINTER_FILE" 2>/dev/null || true)"
    fi
    for plist in "$PLIST_DIR"/io.societyai.claude-code-bridge*.plist; do
        [ -f "$plist" ] || continue
        add_candidate "$(plist_workdir "$plist")"
    done

    NAMED=""
    NAMED_COUNT=0
    TOTAL_COUNT=0
    FIRST=""
    while IFS= read -r dir; do
        [ -n "$dir" ] || continue
        TOTAL_COUNT=$((TOTAL_COUNT + 1))
        [ -n "$FIRST" ] || FIRST="$dir"
        if knows_agent "$dir" "$AGENT_NAME"; then
            NAMED="$dir"
            NAMED_COUNT=$((NAMED_COUNT + 1))
        fi
    done <<EOF
$CANDIDATES
EOF

    if [ "$NAMED_COUNT" = 1 ]; then
        TARGET="$NAMED"
    elif [ "$NAMED_COUNT" -gt 1 ] || [ "$TOTAL_COUNT" -gt 1 ]; then
        echo ""
        echo "  This computer has the agent installed in more than one place:"
        echo ""
        printf '%s' "$CANDIDATES" | sed 's/^/      /'
        echo ""
        echo "  Choose one by running the command again with --dir and that"
        echo "  folder. To use the first one, paste this:"
        echo ""
        echo "      $(retry_command "$FIRST")"
        echo ""
        echo "  For another one, change the folder after --dir."
        echo ""
        exit 1
    elif [ "$TOTAL_COUNT" = 1 ]; then
        TARGET="$FIRST"
    fi
fi

# ── Update in place, or clone when this machine has no agent yet ───────────
if [ -n "$TARGET" ]; then
    echo "  Using the agent already installed in $TARGET"
    # Switch it to the latest release when there is a newer one and it passes its check; setup restarts
    # the agent afterwards. A tree with local changes is left as it is.
    # shellcheck source=core/installer/release.sh
    if ! (cd "$TARGET" && [ -z "$(git status --porcelain --untracked-files=no)" ] \
            && source core/installer/release.sh && TAG="$(latest_release_tag origin)" && [ -n "$TAG" ] \
            && git fetch --quiet origin "refs/tags/$TAG:refs/tags/$TAG" \
            && { [ "$(git rev-parse "$TAG^{commit}")" = "$(git rev-parse HEAD)" ] \
                 || { verify_release "$TAG" && git checkout --quiet --detach "$TAG"; }; }); then
        echo "  Could not update it (that is OK). Continuing with the version you have."
    fi
elif [ -e "$DIR" ]; then
    echo ""
    echo "  A '$DIR' file or folder already exists here, but it is not the"
    echo "  agent. Move or delete it, then run this command again."
    exit 1
else
    # The latest release (a tag vX.Y.Z that Society AI built and signed), never the main branch.
    TAG="$(git ls-remote --tags --refs "$REPO_URL" 'v*' 2>/dev/null | sed -E 's#.*refs/tags/##' \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -t. -k1,1V -k2,2n -k3,3n | tail -1)"
    if [ -z "$TAG" ] || ! git -c advice.detachedHead=false clone --quiet --branch "$TAG" "$REPO_URL" "$DIR"; then
        echo ""
        echo "  Could not download the agent. Check your internet connection"
        echo "  and run this command again."
        exit 1
    fi
    # shellcheck source=core/installer/release.sh
    if ! (cd "$DIR" && source core/installer/release.sh && verify_release "$TAG"); then
        echo ""
        echo "  The download did not pass its check, so it was not installed. Please tell Society AI:"
        echo "  https://github.com/society-ai/connect/issues"
        rm -rf "$DIR"
        exit 1
    fi
    TARGET="$(abs_path "$DIR")"
    echo "  Installed $TAG into $TARGET"
fi

remember_install_dir "$TARGET"

cd "$TARGET" || exit 1
exec ./connect setup ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}
