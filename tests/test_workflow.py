from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from codex_subtitles.domain import GlossaryCandidate, IrisCue, IrisResponse
from codex_subtitles.curation import ensure_episode_enrichment
from codex_subtitles.errors import WorkflowError
from codex_subtitles.glossary import ensure_glossary
from codex_subtitles.protocol import (
    build_source_document,
    serialize_translation_document,
    validate_iris_cues,
)
from codex_subtitles.workflow import TranslationEngine, manual_retry
from codex_subtitles.workspace import ensure_layout, read_json


def make_srt(texts: list[str]) -> str:
    cues = []
    for index, text in enumerate(texts, start=1):
        minute, second = divmod(index, 60)
        cues.append(
            f"{index}\n00:{minute:02d}:{second:02d},000 --> "
            f"00:{minute:02d}:{second:02d},900\n{text}"
        )
    return "\n\n".join(cues) + "\n"


def source_window(prompt: str) -> list[dict[str, object]]:
    payload = prompt.split("ORDERED SOURCE WINDOW JSONL START\n", 1)[1].split(
        "ORDERED SOURCE WINDOW JSONL END", 1
    )[0]
    return [json.loads(line) for line in payload.splitlines() if line.strip()]


class FakeClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.enrichment_calls = 0

    def enrich_glossary(self, prompt: str, *, glossary: Path, request_id: str) -> str:
        self.enrichment_calls += 1
        if "Jimmy is younger; Chuck is older" not in glossary.read_text(encoding="utf-8"):
            with glossary.open("a", encoding="utf-8") as handle:
                handle.write("\n## Background\nJimmy is younger; Chuck is older. Test fixture.\n")
        return "Shared context checked; nothing else needed"

    def translate(self, prompt: str, *, request_id: str) -> IrisResponse:
        self.prompts.append(prompt)
        targets = [record for record in source_window(prompt) if record["role"] == "target"]
        return IrisResponse(
            tuple(
                IrisCue(int(record["id"]), f"译文{record['id']}", False)
                for record in targets
            )
        )

    def edit_glossary(self, prompt: str, *, glossary: Path, request_id: str) -> str:
        raise AssertionError("Atlas should not be called without candidates")


class RepairingClient(FakeClient):
    def translate(self, prompt: str, *, request_id: str) -> IrisResponse:
        self.prompts.append(prompt)
        targets = [record for record in source_window(prompt) if record["role"] == "target"]
        cues = []
        for record in targets:
            cue = int(record["id"])
            if "cue-local retry" in prompt:
                text = "你好"
            elif cue == 2:
                text = "MAN: 你好"
            else:
                text = f"译文{cue}"
            cues.append(IrisCue(cue, text, False))
        return IrisResponse(tuple(cues))


class AtlasFailingClient(FakeClient):
    def translate(self, prompt: str, *, request_id: str) -> IrisResponse:
        targets = [record for record in source_window(prompt) if record["role"] == "target"]
        self.prompts.append(prompt)
        return IrisResponse(
            tuple(IrisCue(int(record["id"]), f"译文{record['id']}", False) for record in targets),
            (GlossaryCandidate("机构", "Sandpiper", (), "桑德派珀", "", (1,)),),
        )

    def edit_glossary(self, prompt: str, *, glossary: Path, request_id: str) -> str:
        raise WorkflowError("simulated Atlas failure")


class WorkflowTests(unittest.TestCase):
    def test_later_glossary_edit_preserves_completed_chunks_on_resume(self) -> None:
        class EditingClient(FakeClient):
            def translate(self, prompt: str, *, request_id: str) -> IrisResponse:
                response = super().translate(prompt, request_id=request_id)
                if response.cues[0].id == 2:
                    return IrisResponse(response.cues, (
                        GlossaryCandidate("机构", "Acme", ("Acme",), "艾克米", "", (2,)),
                    ))
                return response

            def edit_glossary(self, prompt: str, *, glossary: Path, request_id: str) -> str:
                with glossary.open("a", encoding="utf-8") as handle:
                    handle.write("\n## Background from chunk 2\nAcme is a company.\n")
                return "Added company background"

        with tempfile.TemporaryDirectory() as temp_name:
            state = Path(temp_name) / "Show" / "S01"
            ensure_layout(state)
            video = Path("Show S01E01.mkv")
            source = build_source_document(make_srt(["Hello", "Acme", "Goodbye"]))
            first = EditingClient()
            initial = TranslationEngine(
                client=first, state_dir=state, video=video,
                log_path=state / "logs" / "test.log", chunk_cues=1,
            ).translate(source, chunk_range=(1, 2))
            self.assertEqual(len(first.prompts), 2)

            resumed = FakeClient()
            complete = TranslationEngine(
                client=resumed, state_dir=state, video=video,
                log_path=state / "logs" / "test.log", chunk_cues=1,
            ).translate(source)
            self.assertEqual(len(resumed.prompts), 1)
            self.assertEqual(
                [cue["id"] for cue in source_window(resumed.prompts[0]) if cue["role"] == "target"],
                [3],
            )
            self.assertIn("Acme is a company", resumed.prompts[0])
            self.assertEqual(complete.records[:2], initial.records)

            assembled = FakeClient()
            again = TranslationEngine(
                client=assembled, state_dir=state, video=video,
                log_path=state / "logs" / "test.log", chunk_cues=1,
            ).translate(source)
            self.assertEqual(assembled.prompts, [])
            self.assertEqual(again.records, complete.records)

    def test_legacy_caches_reuse_latest_valid_records_and_promote_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            state = Path(temp_name) / "Show" / "S01"
            ensure_layout(state)
            video = Path("Show S01E01.mkv")
            source = build_source_document(make_srt(["Hello"]))
            client = FakeClient()
            engine = TranslationEngine(
                client=client, state_dir=state, video=video, log_path=state / "logs" / "test.log",
            )
            engine.translate(source)
            cache_dir = next((state / "chunks").iterdir())
            for path in cache_dir.glob("chunk-001*.jsonl"):
                path.unlink()
            for ordinal, (text, fingerprint) in enumerate((
                ("旧译文", source.fingerprint),
                ("已确认译文", source.fingerprint),
                ("其他来源", "different-source"),
            ), start=1):
                records = validate_iris_cues(
                    list(source.cues), [IrisCue(1, text, False)], retry=False,
                )
                legacy = cache_dir / f"chunk-001.g{ordinal:010x}.jsonl"
                legacy.write_text(serialize_translation_document(fingerprint, records), encoding="utf-8")
                os.utime(legacy, ns=(ordinal * 1_000_000_000, ordinal * 1_000_000_000))
            with ensure_glossary(state).open("a", encoding="utf-8") as handle:
                handle.write("\nUpdated glossary after translation.\n")
            client.prompts.clear()
            run = engine.translate(source)
            self.assertEqual(client.prompts, [])
            self.assertEqual(run.records[0].text, "已确认译文")
            self.assertTrue((cache_dir / "chunk-001.jsonl").is_file())

    def test_episodes_reuse_one_fact_and_unchanged_research_can_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            state = Path(temp_name) / "Show" / "S01"
            ensure_layout(state)
            source = build_source_document(make_srt(["I am a bad brother"]))
            client = FakeClient()
            first = Path("Show S01E01.mkv")
            second = Path("Show S01E02.mkv")
            log = state / "logs" / "test.log"
            ensure_episode_enrichment(client, state, first, source, log_path=log)
            glossary = ensure_glossary(state)
            before = glossary.read_bytes()
            ensure_episode_enrichment(client, state, second, source, log_path=log)
            self.assertEqual(glossary.read_bytes(), before)
            self.assertEqual(client.enrichment_calls, 2)
            ensure_episode_enrichment(client, state, first, source, log_path=log)
            self.assertEqual(client.enrichment_calls, 2)

    def test_failed_enrichment_rolls_back_and_blocks_iris_then_resumes(self) -> None:
        class FailingResearch(FakeClient):
            def enrich_glossary(self, prompt, **kwargs):
                super().enrich_glossary(prompt, **kwargs)
                raise WorkflowError("research interrupted after edit")

        with tempfile.TemporaryDirectory() as temp_name:
            state = Path(temp_name) / "Show" / "S01"
            ensure_layout(state)
            glossary = ensure_glossary(state)
            original = glossary.read_text(encoding="utf-8")
            video = Path("Show S01E01.mkv")
            source = build_source_document(make_srt(["I am a bad brother"]))
            failed = FailingResearch()
            with self.assertRaisesRegex(WorkflowError, "Iris has not started"):
                TranslationEngine(client=failed, state_dir=state, video=video,
                                  log_path=state / "logs" / "test.log").translate(source)
            self.assertEqual(failed.prompts, [])
            self.assertEqual(glossary.read_text(encoding="utf-8"), original)
            job = next((state / "glossary-jobs" / video.stem).glob("*.json"))
            self.assertEqual(read_json(job)["status"], "pending")
            resumed = FakeClient()
            TranslationEngine(client=resumed, state_dir=state, video=video,
                              log_path=state / "logs" / "test.log").translate(source)
            self.assertEqual(read_json(job)["attempts"], 2)
            self.assertEqual(read_json(job)["status"], "complete")
            self.assertEqual(len(resumed.prompts), 1)

    def test_cue_retry_enriches_first_and_research_repeats_for_changed_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            state = Path(temp_name) / "Show" / "S01"
            ensure_layout(state)
            video = Path("Show S01E01.mkv")
            client = FakeClient()
            manual_retry(
                build_source_document(make_srt(["I'm a bad brother"])), selector="1", reason="Fix relationship",
                state_dir=state, video=video, client=client, log_path=state / "logs" / "test.log",
            )
            ensure_episode_enrichment(
                client, state, video, build_source_document(make_srt(["You're a bad brother"])),
                log_path=state / "logs" / "test.log",
            )
            self.assertEqual(client.enrichment_calls, 2)
            self.assertTrue(all("Jimmy is younger; Chuck is older" in prompt for prompt in client.prompts))

    def test_atlas_failure_does_not_discard_or_repeat_validated_iris_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            state = root / "Show" / "S01"
            ensure_layout(state)
            ensure_glossary(state)
            video = root / "media" / "Show S01E01.mkv"
            video.parent.mkdir()
            video.touch()
            log = state / "logs" / "episode.log"
            log.touch()
            source = build_source_document(make_srt(["Sandpiper"]))

            first = AtlasFailingClient()
            run = TranslationEngine(
                client=first, state_dir=state, video=video, log_path=log, chunk_cues=50
            ).translate(source)
            self.assertEqual([record.text for record in run.records], ["译文1"])

            resumed = FakeClient()
            second_run = TranslationEngine(
                client=resumed, state_dir=state, video=video, log_path=log, chunk_cues=50
            ).translate(source)
            self.assertEqual([record.text for record in second_run.records], ["译文1"])
            self.assertEqual(resumed.prompts, [])

    def test_fifty_cue_chunks_have_ten_cue_context_and_resume_from_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            state = root / "Show" / "S01"
            ensure_layout(state)
            ensure_glossary(state)
            video = root / "media" / "Show S01E01.mkv"
            video.parent.mkdir()
            video.touch()
            log = state / "logs" / "episode.log"
            log.touch()
            client = FakeClient()
            engine = TranslationEngine(
                client=client, state_dir=state, video=video, log_path=log, chunk_cues=50
            )
            source = build_source_document(make_srt([f"Line {index}" for index in range(125)]))
            run = engine.translate(source)
            records = list(run.records)
            self.assertTrue(run.complete)
            self.assertEqual(len(records), 125)
            self.assertEqual(
                [len(source_window(prompt)) for prompt in client.prompts],
                [60, 70, 35],
            )

            resumed_client = FakeClient()
            resumed = TranslationEngine(
                client=resumed_client,
                state_dir=state,
                video=video,
                log_path=log,
                chunk_cues=50,
            )
            resumed_run = resumed.translate(source)
            resumed_records = list(resumed_run.records)
            self.assertEqual(len(resumed_records), 125)
            self.assertEqual(resumed_client.prompts, [])
            self.assertEqual(client.enrichment_calls, 1)
            self.assertEqual(resumed_client.enrichment_calls, 0)
            self.assertTrue(all("Jimmy is younger; Chuck is older" in prompt for prompt in client.prompts))
            self.assertTrue((state / "indexes" / f"{video.stem}.source.jsonl").is_file())

    def test_named_local_retry_repairs_only_failed_id_with_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            state = root / "Show" / "S01"
            ensure_layout(state)
            ensure_glossary(state)
            video = root / "media" / "Show S01E01.mkv"
            video.parent.mkdir()
            video.touch()
            log = state / "logs" / "episode.log"
            log.touch()
            client = RepairingClient()
            engine = TranslationEngine(
                client=client, state_dir=state, video=video, log_path=log, chunk_cues=3
            )
            run = engine.translate(
                build_source_document(make_srt(["First", "MAN: Hello", "Third"]))
            )
            records = list(run.records)
            self.assertEqual([record.text for record in records], ["译文1", "你好", "译文3"])
            self.assertEqual(len(client.prompts), 2)
            retry_targets = [
                record
                for record in source_window(client.prompts[1])
                if record["role"] == "target"
            ]
            self.assertEqual([record["id"] for record in retry_targets], [2])
            self.assertIn("speaker label", client.prompts[1])

    def test_chunk_range_stops_cleanly_and_full_run_resumes_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            state = root / "Show" / "S01"
            ensure_layout(state)
            ensure_glossary(state)
            video = root / "media" / "Show S01E01.mkv"
            video.parent.mkdir()
            video.touch()
            log = state / "logs" / "episode.log"
            log.touch()
            source = build_source_document(make_srt([f"Line {index}" for index in range(125)]))

            preview_client = FakeClient()
            preview_engine = TranslationEngine(
                client=preview_client,
                state_dir=state,
                video=video,
                log_path=log,
                chunk_cues=50,
            )
            preview_run = preview_engine.translate(source, chunk_range=(1, 1))
            self.assertFalse(preview_run.complete)
            self.assertEqual((preview_run.chunks_completed, preview_run.chunks_total), (1, 3))
            self.assertEqual((preview_run.chunk_start, preview_run.chunk_end), (1, 1))
            self.assertEqual(len(preview_run.records), 50)
            self.assertEqual(len(preview_client.prompts), 1)

            resume_client = FakeClient()
            resume_engine = TranslationEngine(
                client=resume_client,
                state_dir=state,
                video=video,
                log_path=log,
                chunk_cues=50,
            )
            complete_run = resume_engine.translate(source)
            self.assertTrue(complete_run.complete)
            self.assertEqual(len(complete_run.records), 125)
            self.assertEqual(len(resume_client.prompts), 2)

    def test_later_chunk_range_uses_absolute_chunk_numbers_and_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            state = root / "Show" / "S01"
            ensure_layout(state)
            ensure_glossary(state)
            video = root / "media" / "Show S01E01.mkv"
            video.parent.mkdir()
            video.touch()
            log = state / "logs" / "episode.log"
            log.touch()
            source = build_source_document(
                make_srt([f"Line {index}" for index in range(125)])
            )
            client = FakeClient()
            progress: list[tuple[int, int]] = []
            run = TranslationEngine(
                client=client,
                state_dir=state,
                video=video,
                log_path=log,
                chunk_cues=50,
            ).translate(
                source,
                chunk_range=(2, None),
                progress_callback=lambda chunk, total: progress.append((chunk, total)),
            )
            self.assertFalse(run.complete)
            self.assertEqual((run.chunk_start, run.chunk_end, run.chunks_total), (2, 3, 3))
            self.assertEqual([record.id for record in run.records], list(range(51, 126)))
            self.assertEqual(progress, [(2, 3), (3, 3)])
            self.assertEqual(len(client.prompts), 2)

    def test_chunk_range_outside_episode_fails_before_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            state = root / "Show" / "S01"
            ensure_layout(state)
            ensure_glossary(state)
            video = root / "media" / "Show S01E01.mkv"
            video.parent.mkdir()
            video.touch()
            log = state / "logs" / "episode.log"
            log.touch()
            client = FakeClient()
            engine = TranslationEngine(
                client=client,
                state_dir=state,
                video=video,
                log_path=log,
                chunk_cues=50,
            )
            with self.assertRaisesRegex(WorkflowError, "outside 1-3"):
                engine.translate(
                    build_source_document(
                        make_srt([f"Line {index}" for index in range(125)])
                    ),
                    chunk_range=(4, None),
                )
            self.assertEqual(client.prompts, [])


if __name__ == "__main__":
    unittest.main()
