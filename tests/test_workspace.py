from __future__ import annotations

import unittest

from codex_subtitles.workspace import _compact_chunk_ranges, _completed_chunk_numbers


class WorkspaceTests(unittest.TestCase):
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
