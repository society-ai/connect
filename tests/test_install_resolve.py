"""The one-command installer (install.sh): which existing install it picks, and --dir.

Each test runs install.sh in a throwaway HOME, with fake installs whose
./connect only reports where it ran and what it got, so nothing is cloned or
set up for real.
"""

import os
import pathlib
import plistlib
import shlex
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "install.sh"

FAKE_CONNECT = """#!/bin/sh
printf 'RAN-IN %s\\n' "$PWD"
for a in "$@"; do printf 'ARG %s\\n' "$a"; done
"""


class InstallerResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # realpath: macOS hands out /var/... which is a symlink to /private/var/...
        self.base = pathlib.Path(os.path.realpath(self.tmp.name))
        self.home = self.base / "home"
        (self.home / "Library" / "LaunchAgents").mkdir(parents=True)
        self.cwd = self.base / "elsewhere"
        self.cwd.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def make_install(self, path: pathlib.Path) -> pathlib.Path:
        (path / ".git").mkdir(parents=True)
        connect = path / "connect"
        connect.write_text(FAKE_CONNECT)
        connect.chmod(0o755)
        return path

    def point_to(self, path: str):
        cache = self.home / ".cache" / "society-ai"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "install-dir").write_text(path + "\n")

    def add_service(self, label: str, workdir: str):
        plist = self.home / "Library" / "LaunchAgents" / f"io.societyai.claude-code-bridge{label}.plist"
        with open(plist, "wb") as f:
            plistlib.dump({"Label": "x", "WorkingDirectory": workdir}, f)

    def run_installer(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ, HOME=str(self.home), GIT_CEILING_DIRECTORIES=str(self.base))
        return subprocess.run(["bash", str(INSTALL_SH), *args], cwd=self.cwd, env=env,
                              capture_output=True, text=True, timeout=60)

    def ran_in(self, result) -> str:
        lines = [l for l in result.stdout.splitlines() if l.startswith("RAN-IN ")]
        self.assertEqual(len(lines), 1, result.stdout + result.stderr)
        return lines[0][len("RAN-IN "):]

    def setup_args(self, result) -> list:
        return [l[len("ARG "):] for l in result.stdout.splitlines() if l.startswith("ARG ")]

    # ── The same folder under two spellings counts once ─────────────────────

    def test_symlinked_path_to_the_same_install_is_one_install(self):
        real = self.make_install(self.base / "Coding" / "agent")
        (self.base / "link").symlink_to(self.base / "Coding")
        self.point_to(str(self.base / "link" / "agent"))
        self.add_service("", str(real))

        result = self.run_installer("--token", "sai_x", "--name", "buzz", "--yes")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("more than one place", result.stdout)
        self.assertTrue(os.path.samefile(self.ran_in(result), real))

    def test_different_letter_case_on_a_case_insensitive_disk_is_one_install(self):
        real = self.make_install(self.base / "Coding" / "agent")
        lower = self.base / "coding" / "agent"
        if not lower.exists():
            self.skipTest("this disk tells letter case apart")
        self.point_to(str(lower))
        self.add_service("", str(real))
        self.add_service(".searchpro", str(real))

        result = self.run_installer("--token", "sai_x", "--name", "buzz", "--yes")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("more than one place", result.stdout)
        self.assertTrue(os.path.samefile(self.ran_in(result), real))

    # ── Two real installs: stop, and hand back a command to paste ───────────

    def test_two_real_installs_stop_with_a_paste_ready_command(self):
        one = self.make_install(self.base / "one")
        two = self.make_install(self.base / "two")
        self.add_service("", str(one))
        self.add_service(".other", str(two))

        result = self.run_installer("--token", "sai_x", "--name", "buzz",
                                    "--display-name", "Buzz Light", "--yes")

        self.assertEqual(result.returncode, 1)
        self.assertIn("more than one place", result.stdout)
        self.assertNotIn("RAN-IN", result.stdout)
        command = next(l.strip() for l in result.stdout.splitlines() if l.strip().startswith("curl "))
        pasted = shlex.split(command.split("| bash -s --", 1)[1])
        self.assertEqual(pasted[:-1], ["--token", "sai_x", "--name", "buzz",
                                       "--display-name", "Buzz Light", "--yes", "--dir"])
        self.assertIn(pasted[-1], (str(one), str(two)))

    def test_the_pasted_command_then_works(self):
        one = self.make_install(self.base / "one")
        two = self.make_install(self.base / "two")
        self.add_service("", str(one))
        self.add_service(".other", str(two))

        result = self.run_installer("--token", "sai_x", "--name", "buzz", "--yes", "--dir", str(two))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ran_in(result), str(two))
        self.assertEqual((self.home / ".cache" / "society-ai" / "install-dir").read_text().strip(), str(two))

    # ── --dir ───────────────────────────────────────────────────────────────

    def test_dir_is_not_passed_on_to_setup(self):
        target = self.make_install(self.base / "mine")

        result = self.run_installer("--dir=" + str(target), "--token", "sai_x", "--name", "buzz", "--yes")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.setup_args(result), ["setup", "--token", "sai_x", "--name", "buzz", "--yes"])

    def test_a_flag_value_is_never_read_as_dir(self):
        target = self.make_install(self.base / "mine")

        result = self.run_installer("--dir", str(target), "--display-name", "--dir", "--name", "buzz", "--yes")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.ran_in(result), str(target))
        self.assertEqual(self.setup_args(result), ["setup", "--display-name", "--dir", "--name", "buzz", "--yes"])

    def test_dir_at_an_existing_folder_without_the_agent_stops_safely(self):
        result = self.run_installer("--token", "sai_x", "--name", "buzz", "--yes", "--dir", str(self.home))

        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a folder the agent is installed in", result.stdout)
        self.assertNotIn("delete", result.stdout)
        self.assertNotIn("RAN-IN", result.stdout)

    def test_dir_without_a_folder_stops(self):
        for args in (["--yes", "--dir"], ["--yes", "--dir="]):
            with self.subTest(args=args):
                result = self.run_installer(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn("--dir needs a folder", result.stdout)


if __name__ == "__main__":
    unittest.main()
