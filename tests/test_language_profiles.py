from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from codex_subtitles.glossary import glossary_path
from codex_subtitles.glossary import parse_glossary
from codex_subtitles.language_profiles import (
    DEFAULT_PROFILE,
    LanguageProfile,
    get_profile,
    profile_ids,
)
from codex_subtitles.media import destination_path
from codex_subtitles.prompts import iris_requirements
from codex_subtitles.workspace import output_path, progress_path, records_path


class LanguageProfileTests(unittest.TestCase):
    def test_registry_has_explicit_simplified_chinese_default(self) -> None:
        self.assertEqual(profile_ids(), ("zh-hans",))
        self.assertIs(get_profile("ZH-HANS"), DEFAULT_PROFILE)
        self.assertEqual(DEFAULT_PROFILE.normalize_text("你好。\n等等……"), "你好\n等等...")
        self.assertTrue(
            DEFAULT_PROFILE.is_explicit_target_track("zho Simplified Chinese")
        )

    def test_default_paths_remain_legacy_compatible_and_future_profiles_isolate_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            state = root / "Show" / "S01"
            video = root / "Show S01E01.mkv"
            self.assertEqual(
                output_path(state, video),
                state / "outputs" / "Show S01E01.zh-hans.srt",
            )
            self.assertEqual(
                records_path(state, video),
                state / "records" / "Show S01E01.jsonl",
            )
            self.assertEqual(
                progress_path(state, video),
                state / "progress" / "Show S01E01.json",
            )
            self.assertEqual(
                glossary_path(state),
                root / "Show" / "glossary.md",
            )

            future = replace(
                DEFAULT_PROFILE,
                id="test-target",
                output_tag="test-target",
                glossary_column="目标",
                glossary_value_key="target",
            )
            self.assertEqual(
                destination_path(video, future).name,
                "Show S01E01.test-target.srt",
            )
            self.assertEqual(
                records_path(state, video, future).name,
                "Show S01E01.test-target.jsonl",
            )
            self.assertEqual(
                progress_path(state, video, future).name,
                "Show S01E01.test-target.json",
            )
            self.assertEqual(
                glossary_path(state, future).name,
                "glossary.test-target.md",
            )

    def test_generic_modules_consume_the_injected_profile(self) -> None:
        class TestProfile(LanguageProfile):
            def normalize_text(self, text: str) -> str:
                return text.strip().upper()

            def foreign_text_residue(self, text: str) -> str | None:
                return None

            def translated_speaker_label(self, text: str) -> bool:
                return False

            def validate_pun_note(self, text: str) -> str | None:
                return None

        profile = TestProfile(
            id="test-target",
            language_name="Test Target",
            native_name="测试目标",
            output_tag="test-target",
            glossary_column="目标",
            glossary_value_key="target",
            default_scope="local",
            global_scope="global",
            existing_track_markers=("test target",),
            translation_instructions="TEST PROFILE INSTRUCTION",
            curator_instructions="TEST CURATOR INSTRUCTION",
            pun_note_max_chars=20,
        )
        self.assertIn(
            "TEST PROFILE INSTRUCTION",
            iris_requirements("(none)", profile),
        )
        self.assertEqual(profile.normalize_text(" hello "), "HELLO")
        document = (
            "# Glossary\n\n## names\n\n"
            "| ID | 原文及别名 | 目标 | 备注 | 范围 |\n"
            "|---|---|---|---|---|\n"
            "| example | Example | EXAMPLE | | local |\n"
        )
        entries = parse_glossary(document, profile)
        self.assertEqual(entries[0].target, "EXAMPLE")


if __name__ == "__main__":
    unittest.main()
