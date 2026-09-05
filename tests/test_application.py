from __future__ import annotations

import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from codex_subtitles.application import (
    RunOptions,
    _compatible_completed_chunks,
    _next_chunk,
    process_video,
    rerender_saved_output,
)
from codex_subtitles.config import RULESET_VERSION
from codex_subtitles.domain import IrisCue, IrisResponse
from codex_subtitles.errors import WorkflowError
from codex_subtitles.language_profiles import DEFAULT_PROFILE
from codex_subtitles.media import destination_path
from codex_subtitles.srt import parse_srt
from codex_subtitles.workspace import (
    collection_dir,
    ensure_layout,
    output_path,
    progress_path,
    read_json,
    records_path,
    retry_path,
    write_json,
)


class ApplicationTests(unittest.TestCase):
    def test_manual_correction_rebuilds_completed_output_without_retranslation(self) -> None:
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
                    for cue_id in range(1, 4)
                ) + "\n", encoding="utf-8",
            )
            with patch("codex_subtitles.application.CodexClient") as client_type:
                client = client_type.return_value
                client.enrich_glossary.return_value = "No glossary changes needed"
                client.translate.side_effect = [
                    IrisResponse((IrisCue(cue_id, f"译文{cue_id}", False),))
                    for cue_id in range(1, 4)
                ] + [IrisResponse((IrisCue(2, "修正第二句", False),))]
                process_video(video, RunOptions(workspace_root=workspace, stage_only=True, chunk_cues=1))
                cache_bytes = {path: path.read_bytes() for path in (state / "chunks").rglob("*.jsonl")}
                original = parse_srt(output_path(state, video).read_text(encoding="utf-8"))
                process_video(video, RunOptions(workspace_root=workspace, sync_only=True))
                published = destination_path(video).read_bytes()

                result = process_video(video, RunOptions(
                    workspace_root=workspace, retry_cues="2", retry_reason="Fix cue 2",
                ))
                self.assertTrue(result.startswith("RETRY READY "))
                self.assertEqual(client.translate.call_count, 4)
                progress = read_json(progress_path(state, video))
                self.assertTrue(progress["assembly_required"])
                for flag in ("output_ready", "records_ready", "synced"):
                    self.assertFalse(progress[flag])
                for mode in ("sync_only", "normalize_only", "stage_only"):
                    with self.subTest(mode=mode), self.assertRaisesRegex(WorkflowError, "rebuild"):
                        process_video(video, RunOptions(
                            workspace_root=workspace, **{mode: True},
                        ))
                with self.assertRaisesRegex(WorkflowError, "rebuild"):
                    rerender_saved_output(video, workspace)
                self.assertEqual(destination_path(video).read_bytes(), published)
                self.assertEqual(
                    parse_srt(output_path(state, video).read_text(encoding="utf-8")), original,
                )
                client.translate.reset_mock(side_effect=True)
                client.translate.side_effect = AssertionError("completed chunks must not be translated again")
                client.enrich_glossary.reset_mock()

                result = process_video(video, RunOptions(
                    workspace_root=workspace, stage_only=True, chunk_cues=1, overwrite=True,
                ))
                self.assertTrue(result.startswith("STAGED "))
                client.translate.assert_not_called()
                client.enrich_glossary.assert_not_called()
                revised = parse_srt(output_path(state, video).read_text(encoding="utf-8"))
                self.assertEqual([cue.text for cue in revised], ["译文1", "修正第二句", "译文3"])
                self.assertEqual([cue.timestamp for cue in revised], [cue.timestamp for cue in original])
                self.assertEqual(
                    {path: path.read_bytes() for path in (state / "chunks").rglob("*.jsonl")}, cache_bytes,
                )
                result = process_video(video, RunOptions(
                    workspace_root=workspace, sync_only=True, overwrite=True,
                ))
                self.assertTrue(result.startswith("SYNCED "))
                self.assertEqual(
                    destination_path(video).read_bytes(), output_path(state, video).read_bytes(),
                )
                client.translate.assert_not_called()

    def test_sync_detects_edited_removed_and_untracked_corrections(self) -> None:
        for change in ("edit", "clear", "delete", "legacy", "missing_records"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                video = root / "media" / "Movie.mkv"
                workspace = root / "workspace"
                state = collection_dir(video, workspace)
                ensure_layout(state)
                (state / "sources" / "Movie.en.srt").write_text(
                    "1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8",
                )
                with patch("codex_subtitles.application.CodexClient") as client_type:
                    client = client_type.return_value
                    client.enrich_glossary.return_value = "No glossary changes needed"
                    client.translate.side_effect = [
                        IrisResponse((IrisCue(1, "你好", False),)),
                        IrisResponse((IrisCue(1, "您好", False),)),
                    ]
                    process_video(video, RunOptions(workspace_root=workspace, stage_only=True))
                    process_video(video, RunOptions(
                        workspace_root=workspace, retry_cues="1", retry_reason="Be polite",
                    ))
                    process_video(video, RunOptions(
                        workspace_root=workspace, stage_only=True, overwrite=True,
                    ))
                    process_video(video, RunOptions(workspace_root=workspace, sync_only=True))
                    staged = output_path(state, video).read_bytes()
                    published = destination_path(video).read_bytes()
                    patch_file = retry_path(state, video)
                    patches = read_json(patch_file)
                    if change == "edit":
                        patches["patches"][0]["text"] = "大家好"
                        write_json(patch_file, patches)
                    elif change == "clear":
                        patches["patches"] = []
                        write_json(patch_file, patches)
                    elif change == "delete":
                        patch_file.unlink()
                    elif change == "legacy":
                        record_file = records_path(state, video)
                        lines = record_file.read_text(encoding="utf-8").splitlines()
                        header = json.loads(lines[0])
                        header.pop("retry_fingerprint")
                        lines[0] = json.dumps(header)
                        record_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    else:
                        records_path(state, video).unlink()
                    self.assertTrue(read_json(progress_path(state, video))["synced"])
                    with self.assertRaisesRegex(WorkflowError, "rebuild"):
                        process_video(video, RunOptions(
                            workspace_root=workspace, sync_only=True, overwrite=True,
                        ))
                    self.assertEqual(output_path(state, video).read_bytes(), staged)
                    self.assertEqual(destination_path(video).read_bytes(), published)
                    self.assertFalse(read_json(progress_path(state, video))["synced"])
                    # Reassembly must recover from removals as well as additions.
                    client.translate.reset_mock(side_effect=True)
                    client.translate.side_effect = AssertionError("base cache must remain reusable")
                    process_video(video, RunOptions(
                        workspace_root=workspace, stage_only=True, overwrite=True,
                    ))
                    process_video(video, RunOptions(
                        workspace_root=workspace, sync_only=True, overwrite=True,
                    ))
                    expected = "大家好" if change == "edit" else (
                        "你好" if change in ("clear", "delete") else "您好"
                    )
                    self.assertEqual(
                        parse_srt(destination_path(video).read_text(encoding="utf-8"))[0].text,
                        expected,
                    )
                    client.translate.assert_not_called()

    def test_old_records_cannot_be_skipped_after_progress_updates(self) -> None:
        for progress_version in ("older-ruleset", RULESET_VERSION):
            with self.subTest(progress_version=progress_version), tempfile.TemporaryDirectory() as temp_name:
                root = Path(temp_name)
                video = root / "media" / "Movie.mkv"
                workspace = root / "workspace"
                state = collection_dir(video, workspace)
                ensure_layout(state)
                (state / "sources" / "Movie.en.srt").write_text(
                    "1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8",
                )
                with patch("codex_subtitles.application.CodexClient") as client_type:
                    client = client_type.return_value
                    client.enrich_glossary.return_value = "No glossary changes needed"
                    client.translate.return_value = IrisResponse((IrisCue(1, "你好", False),))
                    process_video(video, RunOptions(workspace_root=workspace, stage_only=True))
                    record_file = records_path(state, video)
                    record_file.write_text(
                        record_file.read_text(encoding="utf-8").replace(RULESET_VERSION, "older-ruleset"),
                        encoding="utf-8",
                    )
                    progress = read_json(progress_path(state, video))
                    progress["ruleset_version"] = progress_version
                    progress["chunk_ruleset_version"] = "older-ruleset"
                    write_json(progress_path(state, video), progress)
                    process_video(video, RunOptions(
                        workspace_root=workspace, source_only=True, overwrite=True,
                    ))
                    self.assertEqual(
                        read_json(progress_path(state, video))["ruleset_version"], progress_version,
                    )
                    for mode in ("stage_only", "normalize_only", "sync_only"):
                        with self.subTest(mode=mode), self.assertRaisesRegex(WorkflowError, "stale ruleset"):
                            process_video(video, RunOptions(workspace_root=workspace, **{mode: True}))
                    client.translate.reset_mock()
                    process_video(video, RunOptions(
                        workspace_root=workspace, stage_only=True, overwrite=True,
                    ))
                    self.assertEqual(
                        read_json(progress_path(state, video))["ruleset_version"], RULESET_VERSION,
                    )
                    self.assertTrue(process_video(
                        video, RunOptions(workspace_root=workspace, stage_only=True),
                    ).startswith("SKIP "))
                    client.translate.assert_not_called()

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

    def test_chunk_progress_uses_its_own_ruleset(self) -> None:
        for chunk_version, output_version, expected in (
            (RULESET_VERSION, "older-ruleset", {1, 2}),
            ("older-ruleset", RULESET_VERSION, set()),
        ):
            with self.subTest(chunk_version=chunk_version, output_version=output_version):
                self.assertEqual(
                    _compatible_completed_chunks(
                        {
                            "ruleset_version": output_version,
                            "chunk_ruleset_version": chunk_version,
                            "source_fingerprint": "source-a",
                            "completed_chunks": [1, 2],
                            "chunk_cues": 50,
                        },
                        source_fingerprint="source-a",
                        chunks_total=3,
                        chunk_cues=50,
                    ),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
