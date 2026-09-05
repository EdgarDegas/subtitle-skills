from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from codex_subtitles.application import (
    RunOptions,
    _compatible_completed_chunks,
    _next_chunk,
    process_video,
)
from codex_subtitles.config import RULESET_VERSION
from codex_subtitles.domain import IrisCue, IrisResponse
from codex_subtitles.language_profiles import DEFAULT_PROFILE
from codex_subtitles.srt import parse_srt
from codex_subtitles.workspace import (
    collection_dir,
    ensure_layout,
    output_path,
    progress_path,
    read_json,
)


class ApplicationTests(unittest.TestCase):
    def test_resume_later_chunks_writes_preview_then_assembles_cached_episode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            video = root / "media" / "Show" / "S01" / "Show S01E01.mkv"
            workspace = root / "workspace"
            state = collection_dir(video, workspace)
            ensure_layout(state)
            source = state / "sources" / f"{video.stem}.embedded-stream-3.srt"
            source.write_text(
                "\n\n".join(
                    f"{cue_id}\n00:00:0{cue_id},000 --> 00:00:0{cue_id},900\nLine {cue_id}"
                    for cue_id in range(1, 6)
                ) + "\n",
                encoding="utf-8",
            )
            with patch("codex_subtitles.application.CodexClient") as client_type:
                client = client_type.return_value
                client.enrich_glossary.return_value = "No glossary changes needed"
                client.translate.side_effect = [
                    IrisResponse(tuple(IrisCue(cue_id, f"译文{cue_id}", False) for cue_id in ids))
                    for ids in ((1, 2), (3, 4), (5,))
                ]
                process_video(
                    video,
                    RunOptions(
                        workspace_root=workspace, stage_only=True,
                        chunk_cues=2, chunk_range=(1, 1),
                    ),
                )
                result = process_video(
                    video,
                    RunOptions(
                        workspace_root=workspace, stage_only=True,
                        chunk_cues=2, chunk_range=(2, None),
                    ),
                )
                self.assertTrue(result.startswith("PARTIAL "))
                progress = read_json(progress_path(state, video))
                self.assertEqual(progress["status"], "partial")
                self.assertEqual(progress["completed_chunks"], [1, 2, 3])
                self.assertTrue(progress["assembly_required"])
                self.assertFalse(progress["output_ready"])
                preview = parse_srt(Path(progress["preview"]).read_text(encoding="utf-8"))
                self.assertEqual([cue.number for cue in preview], ["1", "2", "3"])
                self.assertEqual([cue.text for cue in preview], ["译文3", "译文4", "译文5"])
                self.assertEqual(preview[0].timestamp, "00:00:03,000 --> 00:00:03,900")
                self.assertEqual(client.translate.call_count, 3)

                client.translate.reset_mock()
                result = process_video(
                    video, RunOptions(workspace_root=workspace, stage_only=True, chunk_cues=2)
                )
                self.assertTrue(result.startswith("STAGED "))
                client.translate.assert_not_called()
                output = parse_srt(output_path(state, video).read_text(encoding="utf-8"))
                self.assertEqual([cue.text for cue in output], [f"译文{i}" for i in range(1, 6)])

    def test_source_only_reuses_durable_source_even_with_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            video = root / "media" / "Show" / "S01" / "Show S01E01.mkv"
            video.parent.mkdir(parents=True)
            video.touch()
            workspace = root / "workspace"
            state = collection_dir(video, workspace)
            ensure_layout(state)
            source = state / "sources" / f"{video.stem}.embedded-stream-3.srt"
            source.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
                encoding="utf-8",
            )
            with patch("codex_subtitles.application.acquire_source") as acquire:
                result = process_video(
                    video,
                    RunOptions(workspace_root=workspace, source_only=True, overwrite=True),
                )
            acquire.assert_not_called()
            self.assertIn(str(source), result)

    def test_source_only_explicit_track_bypasses_durable_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            video = root / "media" / "Show" / "S01" / "Show S01E01.mkv"
            video.parent.mkdir(parents=True)
            video.touch()
            workspace = root / "workspace"
            state = collection_dir(video, workspace)
            ensure_layout(state)
            cached = state / "sources" / f"{video.stem}.embedded-stream-2.srt"
            cached.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
                encoding="utf-8",
            )
            selected = state / "sources" / f"{video.stem}.embedded-stream-3.srt"
            with patch(
                "codex_subtitles.application.acquire_source",
                return_value=(selected, "embedded stream 3"),
            ) as acquire:
                result = process_video(
                    video,
                    RunOptions(
                        workspace_root=workspace,
                        source_only=True,
                        overwrite=True,
                        requested_track=3,
                    ),
                )
            acquire.assert_called_once()
            self.assertIn(str(selected), result)

    def test_completed_chunks_resume_only_same_source_and_policy(self) -> None:
        progress = {
            "ruleset_version": RULESET_VERSION,
            "source_fingerprint": "source-a",
            "chunk_cues": 50,
            "completed_chunks": [1, 2, 5],
        }
        completed = _compatible_completed_chunks(
            progress,
            source_fingerprint="source-a",
            chunks_total=6,
            chunk_cues=50,
        )
        self.assertEqual(completed, {1, 2, 5})
        self.assertEqual(_next_chunk(completed, 6), 3)
        self.assertEqual(
            _compatible_completed_chunks(
                progress,
                source_fingerprint="source-b",
                chunks_total=6,
                chunk_cues=50,
            ),
            set(),
        )
        other_profile = type(DEFAULT_PROFILE)(
            **{
                **DEFAULT_PROFILE.__dict__,
                "id": "test-target",
                "output_tag": "test-target",
            }
        )
        self.assertEqual(
            _compatible_completed_chunks(
                progress,
                source_fingerprint="source-a",
                chunks_total=6,
                chunk_cues=50,
                profile=other_profile,
            ),
            set(),
        )

    def test_legacy_leading_progress_is_migrated(self) -> None:
        progress = {
            "ruleset_version": RULESET_VERSION,
            "source_fingerprint": "source-a",
            "chunk_cues": 50,
            "chunks_completed": 3,
        }
        self.assertEqual(
            _compatible_completed_chunks(
                progress,
                source_fingerprint="source-a",
                chunks_total=6,
                chunk_cues=50,
            ),
            {1, 2, 3},
        )


if __name__ == "__main__":
    unittest.main()
