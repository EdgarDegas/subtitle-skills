from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_subtitles.config import SKILL_DIR
from codex_subtitles.errors import WorkflowError
from codex_subtitles.runtime import MediaTools, platform_key


class RuntimeTests(unittest.TestCase):
    def test_platform_aliases_and_unsupported_hosts(self):
        self.assertEqual(platform_key("Windows", "AMD64"), "windows-x86_64")
        self.assertEqual(platform_key("Linux", "aarch64"), "linux-arm64")
        self.assertEqual(platform_key("Darwin", "arm64"), "darwin-arm64")
        with self.assertRaises(WorkflowError):
            platform_key("Darwin", "i386")

    def test_tools_cannot_be_stored_in_skill(self):
        with self.assertRaises(WorkflowError):
            MediaTools.in_workspace(SKILL_DIR / "workspace")

    def test_missing_tools_never_fall_back_to_path(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("shutil.which", return_value="/a/system/ffmpeg") as which:
                with self.assertRaises(WorkflowError):
                    MediaTools.in_workspace(Path(temp)).require("ffmpeg")
            which.assert_not_called()

if __name__ == "__main__":
    unittest.main()
