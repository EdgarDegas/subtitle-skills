from __future__ import annotations

import argparse
import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from codex_subtitles.cli import main, parse_chunk_range
from codex_subtitles.domain import TranslationCue
from codex_subtitles.srt import parse_srt
from codex_subtitles.workspace import (
    collection_dir,
    ensure_layout,
    load_records,
    output_path,
    progress_path,
    render_map_path,
    save_records,
    subtitle_offset_ms,
    update_progress,
)


class CliTests(unittest.TestCase):
    def test_offline_stage_uses_local_source_without_tools_or_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            video = root / "unmounted" / "Show" / "S01" / "Show S01E01.mkv"
            source = workspace / "Show" / "S01" / "sources" / "Show S01E01.stream-3.srt"
            source.parent.mkdir(parents=True)
            source.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
            with patch("codex_subtitles.cli.process_video", return_value="STAGED local") as process:
                result = main([
                    "translate",
                    "--stage-only",
                    "--profile",
                    "zh-hans",
                    "--workspace-dir",
                    str(workspace),
                    str(video),
                ])
            self.assertEqual(result, 0)
            self.assertEqual(process.call_args.args[0], video.resolve())
            self.assertTrue(process.call_args.args[1].stage_only)
            self.assertEqual(process.call_args.args[1].profile.id, "zh-hans")
            self.assertFalse(video.exists())

    def test_chunk_range_forms(self) -> None:
        self.assertEqual(parse_chunk_range("6"), (6, 6))
        self.assertEqual(parse_chunk_range("6-10"), (6, 10))
        self.assertEqual(parse_chunk_range("6-"), (6, None))

    def test_chunk_range_rejects_zero_reverse_and_lists(self) -> None:
        for value in ("0", "6-5", "1,2"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    parse_chunk_range(value)

    def test_offset_set_persists_and_rerenders_records_without_changing_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            video = root / "media" / "Show" / "S01" / "Show S01E01.mkv"
            video.parent.mkdir(parents=True)
            video.touch()
            state = collection_dir(video, workspace)
            ensure_layout(state)
            fingerprint = "episode-source"
            original = TranslationCue(
                id=1,
                source_number="1",
                timestamp="00:00:01,000 --> 00:00:02,000",
                source_text="Hello",
                text="你好",
                drop=False,
            )
            save_records(state, video, fingerprint, [original])
            update_progress(
                state,
                video,
                status="staged",
                source_fingerprint=fingerprint,
                records_ready=True,
            )

            result = main([
                "offset",
                "set",
                "--milliseconds",
                "-500",
                "--workspace-dir",
                str(workspace),
                str(video),
            ])

            self.assertEqual(result, 0)
            self.assertEqual(subtitle_offset_ms(state, video), -500)
            cues = parse_srt(output_path(state, video).read_text(encoding="utf-8"))
            self.assertEqual(cues[0].timestamp, "00:00:00,500 --> 00:00:01,500")
            self.assertEqual(load_records(state, video), [original])
            progress = json.loads(progress_path(state, video).read_text(encoding="utf-8"))
            self.assertTrue(progress["output_ready"])
            self.assertFalse(progress["synced"])
            header = json.loads(render_map_path(state, video).read_text().splitlines()[0])
            self.assertEqual(header["subtitle_offset_ms"], -500)
            self.assertFalse(video.with_name(f"{video.stem}.zh-hans.srt").exists())


if __name__ == "__main__":
    unittest.main()
