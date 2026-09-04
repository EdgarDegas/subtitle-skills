from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_subtitles.errors import WorkflowError
from codex_subtitles.glossary import (
    ensure_glossary,
    load_glossary,
    validate_glossary_edit,
)


class GlossaryTests(unittest.TestCase):
    def test_atlas_may_merge_into_notes_but_output_must_remain_valid(self) -> None:
        before = (
            "# Terms\n\n## People\n\n"
            "| ID | 原文及别名 | 简中 | 备注 | 范围 |\n"
            "|---|---|---|---|---|\n"
            "| brothers | Jimmy; Chuck | Brothers | Source one | 按需 |\n"
        )
        merged = "# Terms\n\n## Background\nJimmy is Chuck's younger brother. Source one.\n"
        validate_glossary_edit(before, merged, allow_merge=True)
        validate_glossary_edit(merged, merged, allow_merge=True)
        with self.assertRaises(WorkflowError):
            validate_glossary_edit(before, "", allow_merge=True)
        with self.assertRaises(WorkflowError):
            validate_glossary_edit(before, before + "| malformed |\n", allow_merge=True)

    def test_direct_edit_may_add_alias_but_not_change_confirmed_chinese(self) -> None:
        before = (
            "# Show 术语表\n\n## 人物\n\n"
            "| ID | 原文及别名 | 简中 | 备注 | 范围 |\n"
            "|---|---|---|---|---|\n"
            "| james-mcgill | James McGill | 詹姆斯·麦吉尔 | | 按需 |\n"
        )
        after = before.replace(
            "James McGill |", "James McGill; McGill |"
        )
        validate_glossary_edit(before, after)
        with self.assertRaisesRegex(WorkflowError, "changed a confirmed"):
            validate_glossary_edit(
                before, before.replace("詹姆斯·麦吉尔", "吉米·麦吉尔")
            )

    def test_ensure_glossary_never_resets_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            state = Path(temp_name) / "Show" / "S01"
            state.mkdir(parents=True)
            path = ensure_glossary(state)
            custom = "# Show 术语表\n\n用户内容\n"
            path.write_text(custom, encoding="utf-8")
            ensure_glossary(state)
            self.assertEqual(path.read_text(encoding="utf-8"), custom)

    def test_markdown_categories_are_free_form(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            state = Path(temp_name) / "Show" / "S01"
            state.mkdir(parents=True)
            path = ensure_glossary(state)
            path.write_text(
                "# Show 术语表\n\n## 食物\n\n"
                "| ID | 原文及别名 | 简中 | 备注 | 范围 |\n"
                "|---|---|---|---|---|\n"
                "| cinnabon | Cinnabon | 肉桂卷 | 品牌 | 按需 |\n",
                encoding="utf-8",
            )
            entries = load_glossary(state)
            self.assertEqual(entries[0].key, "食物/cinnabon")


if __name__ == "__main__":
    unittest.main()
