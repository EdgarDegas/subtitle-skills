from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import unittest

from codex_subtitles.codex_client import CodexClient
from codex_subtitles.domain import Addition, IrisCue
from codex_subtitles.errors import ValidationIssue
from codex_subtitles.errors import WorkflowError
from codex_subtitles.language_profiles import DEFAULT_PROFILE
from codex_subtitles.protocol import (
    build_source_document,
    parse_source_document,
    parse_translation_document,
    serialize_source_document,
    serialize_translation_document,
    source_window_jsonl,
    validate_iris_cues,
)


def make_srt(texts: list[str]) -> str:
    return "\n\n".join(
        f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\n{text}"
        for index, text in enumerate(texts, start=1)
    ) + "\n"


class ProtocolTests(unittest.TestCase):
    def test_glossary_candidate_uses_source_when_aliases_are_empty(self) -> None:
        client = object.__new__(CodexClient)
        client._run = lambda *args, **kwargs: {
            "cues": [
                {
                    "id": 1,
                    "text": "雷诺阿",
                    "drop": False,
                    "additions": [],
                    "skip_checks": [],
                }
            ],
            "glossary_candidates": [
                {
                    "category": "artist",
                    "source": "Renoir",
                    "aliases": [],
                    "target": "雷诺阿",
                    "notes": "",
                    "cue_ids": [1],
                }
            ],
        }
        response = client.translate("ignored", request_id="test")
        self.assertEqual(response.glossary_candidates[0].aliases, ("Renoir",))

    def test_translation_schema_uses_supported_structured_output_subset(self) -> None:
        schema_path = (
            Path(__file__).parents[1]
            / "src"
            / "codex_subtitles"
            / "schemas"
            / "translation_output.schema.json"
        )
        schema_text = schema_path.read_text(encoding="utf-8")
        self.assertNotIn('"uniqueItems"', schema_text)
        self.assertIn('"target"', schema_text)
        self.assertNotIn('"zh_hans"', schema_text)

    def test_translation_documents_are_profile_bound_with_legacy_default_compatibility(self) -> None:
        source = build_source_document(make_srt(["Hello"]))
        records = validate_iris_cues(
            list(source.cues),
            [IrisCue(1, "你好", False)],
            retry=False,
        )
        document = serialize_translation_document(source.fingerprint, records)
        header, rest = document.split("\n", 1)
        value = json.loads(header)
        self.assertEqual(value["profile"], "zh-hans")

        legacy = dict(value)
        legacy.pop("profile")
        legacy_document = json.dumps(legacy, ensure_ascii=False) + "\n" + rest
        _, loaded = parse_translation_document(
            legacy_document,
            expected_fingerprint=source.fingerprint,
        )
        self.assertEqual(loaded, records)

        other = replace(
            DEFAULT_PROFILE,
            id="test-target",
            output_tag="test-target",
        )
        with self.assertRaisesRegex(WorkflowError, "expected test-target"):
            parse_translation_document(document, profile=other)

    def test_episode_fingerprint_changes_but_integer_ids_are_source_local(self) -> None:
        source = build_source_document(make_srt(["Yes", "Yes"]))
        same = build_source_document(make_srt(["Yes", "Yes"]))
        changed = build_source_document(make_srt(["Yes", "No"]))
        self.assertEqual(source.fingerprint, same.fingerprint)
        self.assertEqual([cue.id for cue in source.cues], [1, 2])
        self.assertEqual([cue.id for cue in changed.cues], [1, 2])
        self.assertNotEqual(source.fingerprint, changed.fingerprint)

    def test_fingerprint_ignores_original_srt_numbers(self) -> None:
        normal = make_srt(["A", "B"])
        renumbered = normal.replace("1\n00:00:01", "7\n00:00:01").replace(
            "2\n00:00:02", "9\n00:00:02"
        )
        self.assertEqual(
            build_source_document(normal).fingerprint,
            build_source_document(renumbered).fingerprint,
        )

    def test_source_index_round_trip_verifies_fingerprint(self) -> None:
        source = build_source_document(make_srt(["A", "B"]))
        loaded = parse_source_document(serialize_source_document(source))
        self.assertEqual(loaded, source)

    def test_window_marks_context_and_target_without_duplicate_translation(self) -> None:
        cues = build_source_document(make_srt(["A", "B", "C"])).cues
        lines = [
            json.loads(line)
            for line in source_window_jsonl(cues, {cues[1].id}).splitlines()
        ]
        self.assertEqual([line["role"] for line in lines], ["context", "target", "context"])
        self.assertEqual([line["id"] for line in lines], [1, 2, 3])
        self.assertNotIn("cue", lines[0])

    def test_ids_are_alignment_authority(self) -> None:
        cues = list(build_source_document(make_srt(["First", "Second"])).cues)
        result = validate_iris_cues(
            cues,
            [
                IrisCue(cues[1].id, "第二", False),
                IrisCue(cues[0].id, "第一", False),
            ],
            retry=False,
        )
        self.assertEqual([record.text for record in result], ["第一", "第二"])
        self.assertEqual([record.timestamp for record in result], [cue.timestamp for cue in cues])

    def test_missing_and_duplicate_ids_fail(self) -> None:
        cues = list(build_source_document(make_srt(["One", "Two"])).cues)
        with self.assertRaises(ValidationIssue) as missing:
            validate_iris_cues(cues, [IrisCue(1, "一", False)], retry=False)
        self.assertEqual(missing.exception.check, "cue id")
        with self.assertRaises(ValidationIssue) as duplicate:
            validate_iris_cues(
                cues,
                [IrisCue(1, "一", False), IrisCue(1, "一", False)],
                retry=False,
            )
        self.assertEqual(duplicate.exception.check, "cue id")

    def test_drop_and_multi_check_appeal(self) -> None:
        sounds = list(build_source_document(make_srt(["[LAUGHS]"])).cues)
        dropped = validate_iris_cues(sounds, [IrisCue(1, "", True)], retry=False)
        self.assertTrue(dropped[0].drop)

        dialogue = list(build_source_document(make_srt(["MAN: Heh, Chào cô"])).cues)
        appealed = IrisCue(
            1,
            "MAN: 哈哈 Chào cô",
            False,
            skip_checks=("speaker label", "laughter", "foreign text"),
        )
        with self.assertRaises(ValidationIssue) as initial:
            validate_iris_cues(dialogue, [appealed], retry=False)
        self.assertEqual(initial.exception.check, "appeal permission")
        result = validate_iris_cues(dialogue, [appealed], retry=True)
        self.assertEqual(
            result[0].skip_checks,
            ("foreign text", "laughter", "speaker label"),
        )

    def test_pun_is_a_validated_addition(self) -> None:
        cues = list(build_source_document(make_srt(["Finnish?"])).cues)
        result = validate_iris_cues(
            cues,
            [
                IrisCue(
                    1,
                    "芬兰语？",
                    False,
                    additions=(Addition("pun_note", "“芬兰语”与“完成”同音"),),
                )
            ],
            retry=False,
        )
        self.assertEqual(result[0].additions[0].text, "“芬兰语”与“完成”同音")
        with self.assertRaises(ValidationIssue) as invalid:
            validate_iris_cues(
                cues,
                [
                    IrisCue(
                        1,
                        "芬兰语？",
                        False,
                        additions=(Addition("pun_note", "Finnish 与 finish 同音"),),
                    )
                ],
                retry=False,
            )
        self.assertEqual(invalid.exception.check, "addition")


if __name__ == "__main__":
    unittest.main()
