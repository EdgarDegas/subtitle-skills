from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from codex_subtitles.config import RULESET_VERSION
from codex_subtitles.workspace import (
    _compact_chunk_ranges,
    _completed_chunk_numbers,
    ensure_layout,
    progress_path,
    read_json,
    update_progress,
)


class WorkspaceTests(unittest.TestCase):
    def test_bookkeeping_preserves_translation_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            state = Path(temp_name)
            video = state / "Movie.mkv"
            ensure_layout(state)
            update_progress(state, video, status="source_ready")
            self.assertNotIn("ruleset_version", read_json(progress_path(state, video)))
            update_progress(state, video, ruleset_version="older-ruleset", status="staged")
            for status in ("copying_source", "staged", "failed", "retry_ready"):
                update_progress(state, video, status=status, subtitle_offset_ms=500)
                progress = read_json(progress_path(state, video))
                self.assertEqual(progress["ruleset_version"], "older-ruleset")
                self.assertEqual(progress["runtime_ruleset_version"], RULESET_VERSION)

    def test_compact_non_contiguous_completed_chunks(self) -> None:
        episode = {"completed_chunks": [10, 2, 1, 3, 6, 8, 9, 8]}
        completed = _completed_chunk_numbers(episode, 15)
        self.assertEqual(completed, [1, 2, 3, 6, 8, 9, 10])
        self.assertEqual(_compact_chunk_ranges(completed), "1-3,6,8-10")

    def test_legacy_leading_count_is_displayed_as_range(self) -> None:
        completed = _completed_chunk_numbers({"chunks_completed": 3}, 15)
        self.assertEqual(_compact_chunk_ranges(completed), "1-3")


if __name__ == "__main__":
    unittest.main()
