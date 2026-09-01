from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_subtitles.curation import enqueue_curation, retry_pending
from codex_subtitles.domain import GlossaryCandidate
from codex_subtitles.errors import WorkflowError
from codex_subtitles.glossary import ensure_glossary
from codex_subtitles.workspace import ensure_layout, read_json


class DirectEditClient:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def edit_glossary(self, prompt: str, *, glossary: Path, request_id: str) -> str:
        self.calls += 1
        if self.fail:
            raise WorkflowError("simulated Atlas failure")
        text = glossary.read_text(encoding="utf-8")
        glossary.write_text(
            text
            + "\n## 机构\n\n"
            + "| ID | 原文及别名 | 简中 | 备注 | 范围 |\n"
            + "|---|---|---|---|---|\n"
            + "| sandpiper | Sandpiper Crossing; Sandpiper | 桑德派珀 | 养老社区 | 按需 |\n",
            encoding="utf-8",
        )
        return "新增桑德派珀条目"


class CurationTests(unittest.TestCase):
    def test_failed_direct_edit_stays_pending_and_retries_without_iris(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            state = root / "Show" / "S01"
            ensure_layout(state)
            glossary = ensure_glossary(state)
            original = glossary.read_text(encoding="utf-8")
            video = root / "media" / "Show S01E01.mkv"
            candidate = GlossaryCandidate(
                "机构", "Sandpiper Crossing", ("Sandpiper",), "桑德派珀",
                "养老社区", (14,),
            )
            job = enqueue_curation(
                state, video, "source-fingerprint", [candidate], request_id="atlas-c001"
            )
            assert job is not None
            failed = retry_pending(
                DirectEditClient(fail=True), state, video, "source-fingerprint",
                log_path=state / "logs" / "episode.log",
            )
            self.assertEqual(failed["glossary_status"], "pending")
            self.assertEqual(glossary.read_text(encoding="utf-8"), original)
            self.assertEqual(read_json(job)["attempts"], 1)

            client = DirectEditClient()
            completed = retry_pending(
                client, state, video, "source-fingerprint",
                log_path=state / "logs" / "episode.log",
            )
            self.assertEqual(completed["glossary_status"], "complete")
            self.assertEqual(client.calls, 1)
            self.assertIn("Sandpiper Crossing", glossary.read_text(encoding="utf-8"))
            self.assertEqual(read_json(job)["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
