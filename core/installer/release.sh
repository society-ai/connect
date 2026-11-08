#!/usr/bin/env bash
# Releases: what install.sh and update.sh install, and how they check it (docs/contribute/releases.md).
#
# Sourced, not run. A release is a tag vX.Y.Z on main that the release workflow built, checked and signed
# (.github/workflows/release.yml): its GitHub Release carries society-ai-connect-<tag>.tar (the tagged tree,
# from git archive), registry.json and SHA256SUMS, with GitHub artifact attestations for each.
#
#   latest_release_tag [<remote or url>]   the newest vX.Y.Z tag
#   verify_release <tag>                   the checked-out tag matches its release: the tree's archive hashes
#                                          to the published SHA256SUMS, and, when the GitHub CLI is installed,
#                                          the archive's attestation says our release workflow built it
#
# SOCIETY_AI_SKIP_VERIFY=1 skips the check (for a fork or a test install), and says so loudly.

RELEASE_REPO="${SOCIETY_AI_RELEASE_REPO:-society-ai/connect}"
RELEASE_PREFIX="society-ai-connect"

latest_release_tag() {
    local remote="${1:-origin}"
    git ls-remote --tags --refs "$remote" 'v*' 2>/dev/null \
        | sed -E 's#.*refs/tags/##' \
        | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
        | sort -t. -k1,1V -k2,2n -k3,3n \
        | tail -1
}

_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum | awk '{print $1}'; else shasum -a 256 | awk '{print $1}'; fi
}

verify_release() {
    local tag="$1" sums expected actual archive
    if [ "${SOCIETY_AI_SKIP_VERIFY:-}" = "1" ]; then
        echo "  WARNING: not checking that $tag is the code Society AI released (SOCIETY_AI_SKIP_VERIFY=1)."
        return 0
    fi
    sums="$(curl -fsSL "https://github.com/$RELEASE_REPO/releases/download/$tag/SHA256SUMS" 2>/dev/null)" || {
        echo "  Could not download the checksums of release $tag. Check your internet connection." >&2
        return 1
    }
    expected="$(printf '%s\n' "$sums" | awk -v f="$RELEASE_PREFIX-$tag.tar" '$2 == f || $2 == "*"f {print $1}')"
    if [ -z "$expected" ]; then
        echo "  Release $tag has no checksum for $RELEASE_PREFIX-$tag.tar." >&2
        return 1
    fi
    actual="$(git archive --format=tar --prefix="$RELEASE_PREFIX/" "$tag" | _sha256)"
    if [ "$actual" != "$expected" ]; then
        echo "  The code checked out for $tag does not match what Society AI released. Not installing it." >&2
        return 1
    fi
    echo "  Checked: $tag is the code Society AI released (checksum)."
    if command -v gh >/dev/null 2>&1; then
        archive="$(mktemp)"
        git archive --format=tar --prefix="$RELEASE_PREFIX/" "$tag" > "$archive"
        if gh attestation verify "$archive" --repo "$RELEASE_REPO" >/dev/null 2>&1; then
            echo "  Checked: signed by Society AI's release workflow (attestation)."
        else
            rm -f "$archive"
            echo "  The release's signature (attestation) could not be verified with gh. Not installing it." >&2
            echo "  If gh is not signed in, run: gh auth login" >&2
            return 1
        fi
        rm -f "$archive"
    fi
    return 0
}
