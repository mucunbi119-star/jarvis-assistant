"""Check committed launcher bytes and locally generated distribution archives.

Working-copy and archive converters can differ. Check both the committed data
and archive output, instead of relying on checkout-only EOL conversion. These
checks do not claim to execute Windows CMD on other operating systems.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = ("setup.bat", "start.bat")


class WindowsDistributionTests(unittest.TestCase):
    def assert_cmd_safe_bytes(self, content: bytes, name: str):
        self.assertTrue(content, f"{name}: empty launcher")
        self.assertTrue(content.isascii(), f"{name}: batch syntax must not depend on the Windows code page")
        self.assertFalse(content.startswith(b"\xef\xbb\xbf"), f"{name}: CMD must not see a UTF-8 BOM")
        self.assertIn(b"\r\n", content, f"{name}: Windows batch file has no CRLF line endings")
        remainder = content.replace(b"\r\n", b"")
        self.assertNotIn(b"\n", remainder, f"{name}: standalone LF can break downloaded batch commands")
        self.assertNotIn(b"\r", remainder, f"{name}: standalone CR is not a complete line ending")

    def test_shipped_launchers_are_encoding_independent_windows_files(self):
        for name in LAUNCHERS:
            with self.subTest(name=name):
                self.assert_cmd_safe_bytes((ROOT / name).read_bytes(), name)

    @unittest.skipUnless(shutil.which("git"), "Git is needed to verify the downloadable archive")
    def test_git_archive_preserves_downloadable_launcher_bytes(self):
        # All mutations are confined to a fresh local repository; no user Git
        # configuration, remote, credential, or existing index is modified.
        environment = os.environ.copy()
        environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        for selector in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
            environment.pop(selector, None)
        with tempfile.TemporaryDirectory(prefix="jarvis-launcher-archive-") as directory:
            repository = Path(directory)
            files = (*LAUNCHERS, ".gitattributes", "PYTHON_SETUP.html")
            for name in files:
                shutil.copyfile(ROOT / name, repository / name)

            def git(*arguments: str) -> bytes:
                result = subprocess.run(
                    ["git", "-c", "core.autocrlf=false", "-C", str(repository), *arguments],
                    env=environment, check=True, capture_output=True, timeout=15,
                )
                return result.stdout

            git("init", "--quiet")
            git("add", "--", *files)
            git("-c", "user.name=Launcher regression", "-c", "user.email=launcher-test@example.invalid",
                "commit", "--quiet", "-m", "Test the distributed launcher bytes")
            # CRLF must be in committed data too: an exporter need not apply
            # the same checkout conversion as the developer's working copy.
            for name in LAUNCHERS:
                with self.subTest(committed=name):
                    self.assert_cmd_safe_bytes(git("show", f"HEAD:{name}"), name)
            archive = git("archive", "--format=zip", "HEAD")
            with zipfile.ZipFile(io.BytesIO(archive)) as downloaded:
                self.assertIsNone(downloaded.testzip())
                guide = downloaded.read("PYTHON_SETUP.html").decode("utf-8")
                self.assertIn("https://www.python.org/", guide)
                for name in LAUNCHERS:
                    with self.subTest(name=name):
                        content = downloaded.read(name)
                        self.assert_cmd_safe_bytes(content, name)
                        self.assertEqual(content, (ROOT / name).read_bytes(),
                                         f"{name}: the distributed file differs from the launcher that was reviewed")


if __name__ == "__main__":
    unittest.main()
