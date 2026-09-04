from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_subtitles.codex_client import CodexClient


class CodexClientTests(unittest.TestCase):
    def test_atlas_has_only_direct_glossary_write_and_no_output_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            executable = root / "codex"
            executable.touch()
            glossary = root / "show" / "glossary.md"
            glossary.parent.mkdir()
            glossary.write_text("# Terms\n", encoding="utf-8")
            work = root / "work"
            work.mkdir()
            captured = []

            def run(command, **kwargs):
                captured.append(command)
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text("没有需要更新的条目", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0)

            with patch("codex_subtitles.codex_client.subprocess.run", side_effect=run):
                client = CodexClient(
                    executable=str(executable),
                    work_dir=work,
                    log_path=root / "episode.log",
                )
                summary = client.edit_glossary("Edit it", glossary=glossary, request_id="atlas-test")
                client.enrich_glossary("Research it", glossary=glossary, request_id="atlas-enrichment-test")

            command = captured[0]
            self.assertEqual(summary, "没有需要更新的条目")
            self.assertNotIn("--output-schema", command)
            self.assertNotIn("--sandbox", command)
            config = " ".join(command)
            self.assertIn('default_permissions="atlas"', config)
            self.assertIn(str(glossary.resolve()), config)
            self.assertIn('extends=":read-only"', config)
            self.assertIn("network={enabled=false}", config)
            self.assertIn('web_search="disabled"', config)
            self.assertNotIn("--output-schema", captured[1])
            research_config = " ".join(captured[1])
            self.assertIn('web_search="live"', research_config)
            self.assertIn("network={enabled=false}", research_config)
            self.assertIn(str(glossary.resolve()), research_config)


if __name__ == "__main__":
    unittest.main()
