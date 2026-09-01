from __future__ import annotations

import unittest

from codex_subtitles.config import TOP_POSITION_TAG
from codex_subtitles.domain import Addition, TranslationCue
from codex_subtitles.srt import (
    normalize_text,
    parse_srt,
    render_translation,
    shift_srt_timing,
)


class SrtTests(unittest.TestCase):
    def test_punctuation_policy(self) -> None:
        self.assertEqual(
            normalize_text("你好。\n等等……\n好, 好."),
            "你好\n等等...\n好, 好",
        )

    def test_render_expands_addition_without_changing_source_identity(self) -> None:
        records = [
            TranslationCue(
                id=1,
                source_number="7",
                timestamp="00:00:01,000 --> 00:00:02,000",
                source_text="Finnish?",
                text="芬兰语？",
                drop=False,
                additions=(Addition("pun_note", "“芬兰语”与“完成”同音"),),
            ),
            TranslationCue(
                id=2,
                source_number="9",
                timestamp="00:00:03,000 --> 00:00:04,000",
                source_text="[LAUGHS]",
                text="",
                drop=True,
            ),
        ]
        rendered = render_translation(records)
        cues = parse_srt(rendered.document)
        self.assertEqual(rendered.dropped_cues, 1)
        self.assertEqual(rendered.addition_counts, {"pun_note": 1})
        self.assertEqual(len(cues), 2)
        self.assertTrue(cues[0].text.startswith(TOP_POSITION_TAG))
        self.assertEqual(cues[1].text, "芬兰语？")
        self.assertEqual(cues[0].timestamp, "00:00:01,000 --> 00:00:03,500")
        self.assertEqual(
            [(entry.output_number, entry.source_id, entry.role) for entry in rendered.mapping],
            [(1, 1, "pun_note"), (2, 1, "main")],
        )

    def test_final_srt_offset_shifts_timing_only_and_clamps_at_zero(self) -> None:
        document = (
            "1\n00:00:01,000 --> 00:00:02,000\n第一条\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n第二条\n"
        )
        later = parse_srt(shift_srt_timing(document, 750))
        self.assertEqual(later[0].timestamp, "00:00:01,750 --> 00:00:02,750")
        self.assertEqual([cue.text for cue in later], ["第一条", "第二条"])

        earlier = parse_srt(shift_srt_timing(document, -1_500))
        self.assertEqual(earlier[0].timestamp, "00:00:00,000 --> 00:00:00,500")
        self.assertEqual(earlier[1].timestamp, "00:00:01,500 --> 00:00:02,500")


if __name__ == "__main__":
    unittest.main()
