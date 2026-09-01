"""Opt-in smoke test using Agent-provisioned workspace tools, never real user video."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from codex_subtitles.application import RunOptions, process_video
from codex_subtitles.extraction import extract_tracks
from codex_subtitles.media import cached_source, probe_tracks
from codex_subtitles.runtime import MediaTools
from codex_subtitles.srt import parse_srt
from codex_subtitles.workspace import collection_dir, source_dir


@unittest.skipUnless(os.environ.get("VIDEO_SUBTITLES_TEST_WORKSPACE"), "set VIDEO_SUBTITLES_TEST_WORKSPACE for real media smoke test")
class MediaIntegrationTests(unittest.TestCase):
    def test_real_probe_multitrack_extract_and_sdh_source_selection(self):
        workspace = Path(os.environ["VIDEO_SUBTITLES_TEST_WORKSPACE"])
        tools = MediaTools.in_workspace(workspace)
        tools.versions()
        with tempfile.TemporaryDirectory(prefix="subtitle-media-smoke-") as temp:
            root = Path(temp)
            normal = root / "normal.srt"
            sdh = root / "sdh.srt"
            normal.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            sdh.write_text("1\n00:00:00,000 --> 00:00:01,000\n[DOOR OPENS]\n\n2\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
            video = root / "media" / "Synthetic" / "S01" / "Synthetic S01E01.mkv"
            video.parent.mkdir(parents=True)
            command = [str(tools.require("ffmpeg")), "-v", "error", "-nostdin", "-n",
                       "-f", "lavfi", "-i", "color=c=black:s=16x16:r=1:d=2",
                       "-i", str(normal), "-i", str(sdh), "-map", "0:v", "-map", "1:0", "-map", "2:0",
                       "-c:v", "ffv1", "-c:s", "srt", "-metadata:s:s:0", "language=eng",
                       "-metadata:s:s:1", "language=eng", "-metadata:s:s:0", "title=English",
                       "-metadata:s:s:1", "title=English SDH", "-disposition:s:1", "hearing_impaired", str(video)]
            subprocess.run(command, check=True, capture_output=True, timeout=20)
            original_hash = hashlib.sha256(video.read_bytes()).hexdigest()
            tracks = probe_tracks(video, tools)
            self.assertEqual([t.index for t in tracks], [1, 2])
            self.assertTrue(tracks[1].hearing_impaired)
            files = extract_tracks(video, tracks, root / "extracted", tools)
            self.assertEqual(len(files), 2)
            self.assertEqual([len(parse_srt(path.read_text())) for path in files], [1, 2])
            # An isolated workspace can use the tested tools via an explicit fixture link.
            staged = root / "workspace"
            stage_tools = MediaTools.in_workspace(staged)
            stage_tools.directory.parent.mkdir(parents=True)
            stage_tools.directory.symlink_to(tools.directory, target_is_directory=True)
            result = process_video(video, RunOptions(workspace_root=staged, source_only=True, overwrite=True))
            self.assertTrue(result.startswith("SOURCE READY"))
            source = cached_source(video, source_dir(collection_dir(video, staged)))
            self.assertIn("stream-2", source.name)
            self.assertEqual(len(parse_srt(source.read_text())), 2)
            self.assertEqual(hashlib.sha256(video.read_bytes()).hexdigest(), original_hash)


if __name__ == "__main__":
    unittest.main()
