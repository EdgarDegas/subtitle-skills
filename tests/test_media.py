from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_subtitles.domain import SubtitleTrack
from codex_subtitles.errors import WorkflowError
from codex_subtitles.extraction import extract_tracks
from codex_subtitles.media import cached_source, demux_tracks, discover_videos, source_as_srt
from codex_subtitles.runtime import MediaTools


SRT = "1\n00:00:00,000 --> 00:00:01,000\nHello\n"


class MediaTests(unittest.TestCase):
    def test_offline_explicit_video_needs_a_local_source(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            video = Path(temp) / "absent" / "Show" / "S01" / "Show S01E01.mkv"
            with self.assertRaises(WorkflowError):
                discover_videos([video], recursive=False, offline_workspace=workspace)
            source = workspace / "Show" / "S01" / "sources" / f"{video.stem}.stream-3.srt"
            source.parent.mkdir(parents=True)
            source.write_text(SRT)
            self.assertEqual(discover_videos([video], recursive=False, offline_workspace=workspace), [video.resolve()])
            self.assertFalse(video.exists())

    def test_srt_processing_needs_no_media_installation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "input.srt"
            source.write_text(SRT)
            with patch("codex_subtitles.media.subprocess.run") as run:
                self.assertEqual(source_as_srt(source, root, MediaTools.in_workspace(root)), SRT)
            run.assert_not_called()

    def test_empty_and_partial_sources_are_not_cache_hits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "test.mkv"
            (root / "test.stream-3.srt").touch()
            (root / "test.stream-3.srt.partial").write_text(SRT)
            with self.assertRaises(WorkflowError):
                cached_source(video, root)

    def test_multitrack_demux_one_scan_and_no_clobber(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "video.mkv"
            video.write_bytes(b"unchanged video")
            outputs = [(2, root / "a.srt"), (3, root / "b.srt")]

            def ffmpeg(command, **kwargs):
                self.assertEqual(command.count("-i"), 1)
                for index, value in enumerate(command):
                    if value == "-f" and command[index + 1] == "srt":
                        Path(command[index + 2]).write_text(SRT)
                return subprocess.CompletedProcess(command, 0, stderr="")

            with patch.object(MediaTools, "require", return_value=Path("ffmpeg")), patch(
                "codex_subtitles.media.subprocess.run", side_effect=ffmpeg,
            ) as run:
                demux_tracks(video, outputs, MediaTools.in_workspace(root))
                self.assertEqual(run.call_count, 1)
                self.assertTrue(all(path.read_text() == SRT for _, path in outputs))
                with self.assertRaises(WorkflowError):
                    demux_tracks(video, outputs, MediaTools.in_workspace(root))
                self.assertEqual(run.call_count, 1)
            self.assertEqual(video.read_bytes(), b"unchanged video")

    def test_empty_demux_does_not_replace_existing_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / "a.srt"
            output.write_text(SRT)

            def empty(command, **kwargs):
                Path(command[-1]).touch()
                return subprocess.CompletedProcess(command, 0, stderr="")

            with patch.object(MediaTools, "require", return_value=Path("ffmpeg")), patch(
                "codex_subtitles.media.subprocess.run", side_effect=empty,
            ):
                with self.assertRaises(WorkflowError):
                    demux_tracks(root / "video.mkv", [(2, output)], MediaTools.in_workspace(root), overwrite=True)
            self.assertEqual(output.read_text(), SRT)

    def test_extract_skips_bitmap_and_uses_absolute_track_indexes(self):
        tracks = [SubtitleTrack(2, "subrip", "eng", "SDH", False, True),
                  SubtitleTrack(4, "hdmv_pgs_subtitle", "fra", "", False, False)]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("codex_subtitles.extraction.demux_tracks") as demux:
                outputs = extract_tracks(root / "video.mkv", tracks, root, MediaTools.in_workspace(root))
            self.assertEqual(len(outputs), 1)
            self.assertIn("stream-2.eng.SDH", outputs[0].name)
            self.assertEqual(demux.call_args.args[1][0][0], 2)


if __name__ == "__main__":
    unittest.main()
