# Releases

Users install releases, never the main branch: `install.sh` and `./connect update` take the newest tag
`vX.Y.Z` and check it before they use it.

## Making one (Society AI)

1. The maintainer agent prepares it: `__version__` in `core/config.py` set to the new version, the notes,
   the agent types it changes. It asks Lior for the go.
2. With the go, Lior tags `main`: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The release workflow (`.github/workflows/release.yml`) then:
   - refuses a tag that is not on `main` or does not match `core/config.py`;
   - runs `./connect dev check`;
   - builds `society-ai-connect-vX.Y.Z.tar` (`git archive` of the tagged tree), `registry.json` and
     `SHA256SUMS`, and checks the archive hashes the same on macOS (installers recompute it there);
   - signs the files with GitHub artifact attestations (Sigstore, keyless: the signature says this
     workflow, in this repo, built them);
   - publishes the GitHub Release.
4. Moving Society AI's registry pin to the release is a separate deploy, also with Lior's go
   (`scripts/sync-connect-registry.sh` in the Society AI repo).

## What installers check

`core/installer/release.sh`, used by `install.sh` and `./connect update`:

- the checked-out tag's `git archive` hashes to the release's `SHA256SUMS`, so the code is what the
  release workflow built;
- when the GitHub CLI (`gh`) is installed and signed in, the archive's attestation, so it was built by
  Society AI's release workflow. Without `gh` only the checksum is checked; install `gh` for the full
  check.

A release that fails a check is not installed. `SOCIETY_AI_SKIP_VERIFY=1` skips the check, for forks and
test installs only, and says so.
