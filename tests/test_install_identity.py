"""The Connect install a bridge reports for update offers (Society AI agent-updates U9):
one id per folder on a machine, stable across restarts, the path never sent."""

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core import install_identity as ii  # noqa: E402


class InstallIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = mock.patch.object(ii, "CACHE_DIR", pathlib.Path(self.tmp.name) / "society-ai")
        self.cache.start()

    def tearDown(self):
        self.cache.stop()
        self.tmp.cleanup()

    def test_stable_per_folder_and_never_the_path(self):
        first = ii.install_identity()
        self.assertRegex(first["install_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(ii.install_identity(), first)
        self.assertNotIn(str(ii.REPO_DIR), str(first))
        machine_file = ii.CACHE_DIR / "machine-id"
        self.assertEqual(oct(machine_file.stat().st_mode & 0o777), "0o600")
        with mock.patch.object(ii, "REPO_DIR", pathlib.Path("/elsewhere/connect")):
            self.assertNotEqual(ii.install_identity()["install_id"], first["install_id"])

    def test_another_machine_is_another_install(self):
        first = ii.install_identity()["install_id"]
        (ii.CACHE_DIR / "machine-id").write_text("0" * 32)
        self.assertNotEqual(ii.install_identity()["install_id"], first)

    def test_machine_is_the_short_host_name(self):
        with mock.patch.object(ii.socket, "gethostname", return_value="MacBook-Pro-9.local"):
            self.assertEqual(ii.install_identity()["machine"], "MacBook-Pro-9")


if __name__ == "__main__":
    unittest.main()
