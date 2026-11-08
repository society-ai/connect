"""Releases (core/installer/release.sh): the newest vX.Y.Z tag, and checking a checkout against its release."""

import hashlib
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RELEASE_SH = ROOT / "core" / "installer" / "release.sh"


def sh(script: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True)


class Releases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        for cmd in ("git init -q", "git config user.email t@example.com", "git config user.name t",
                    "echo one > a.txt", "git add a.txt", "git commit -qm one"):
            self.assertEqual(sh(cmd, str(self.repo)).returncode, 0, cmd)
        for tag in ("v0.9.0", "v0.10.0", "v0.10.1", "v0.2.30", "not-a-release"):
            sh(f"git tag {tag}", str(self.repo))

    def tearDown(self):
        self.tmp.cleanup()

    def archive_sha(self, tag: str) -> str:
        out = subprocess.run(["git", "archive", "--format=tar", "--prefix=society-ai-connect/", tag],
                             cwd=self.repo, capture_output=True, check=True).stdout
        return hashlib.sha256(out).hexdigest()

    def run_release(self, body: str, sums: str = "") -> subprocess.CompletedProcess:
        # curl is replaced by a function that serves the release's SHA256SUMS; gh is hidden.
        sums_file = pathlib.Path(self.tmp.name) / "SHA256SUMS"
        sums_file.write_text(sums)
        script = (f"curl() {{ cat {sums_file}; }}\ncommand() {{ [ \"$2\" = gh ] && return 1; builtin command \"$@\"; }}\n"
                  f"source {RELEASE_SH}\n{body}")
        return sh(script, str(self.repo))

    def test_the_newest_release_tag_sorts_by_version_not_text(self):
        out = self.run_release(f"latest_release_tag {self.repo}")
        self.assertEqual(out.stdout.strip(), "v0.10.1")

    def test_a_checkout_that_matches_its_release_passes(self):
        sums = f"{self.archive_sha('v0.10.1')}  society-ai-connect-v0.10.1.tar\n"
        out = self.run_release("verify_release v0.10.1", sums)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("is the code Society AI released", out.stdout)

    def test_a_checkout_that_differs_from_its_release_is_refused(self):
        sums = f"{'0' * 64}  society-ai-connect-v0.10.1.tar\n"
        out = self.run_release("verify_release v0.10.1", sums)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("does not match what Society AI released", out.stderr)

    def test_a_release_without_a_checksum_is_refused(self):
        out = self.run_release("verify_release v0.10.1", "abc  registry.json\n")
        self.assertNotEqual(out.returncode, 0)

    def test_skipping_the_check_says_so(self):
        out = self.run_release("SOCIETY_AI_SKIP_VERIFY=1 verify_release v0.10.1")
        self.assertEqual(out.returncode, 0)
        self.assertIn("WARNING", out.stdout)


if __name__ == "__main__":
    unittest.main()
