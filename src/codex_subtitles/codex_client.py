from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .config import (
    CURATOR_MODEL,
    CURATOR_NAME,
    TRANSLATION_SCHEMA,
    TRANSLATOR_MODEL,
    TRANSLATOR_NAME,
)
from .domain import Addition, GlossaryCandidate, IrisCue, IrisResponse
from .errors import WorkflowError


def append_log(path: Path, message: str) -> None:
    from datetime import datetime

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\n")


class CodexClient:
    def __init__(
        self,
        *,
        executable: str,
        work_dir: Path,
        log_path: Path,
    ) -> None:
        resolved = shutil.which(executable) if os.sep not in executable else executable
        if not resolved or not Path(resolved).is_file():
            raise WorkflowError(f"codex executable not found: {executable}")
        self.executable = str(resolved)
        self.work_dir = work_dir
        self.log_path = log_path

    def _invoke(
        self,
        prompt: str,
        *,
        role: str,
        model: str,
        request_id: str,
        schema: Path | None = None,
        editable_file: Path | None = None,
        web_search: bool = False,
    ) -> str:
        response_path = self.work_dir / f"{request_id}.response.txt"
        response_path.unlink(missing_ok=True)
        command = [
            self.executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--model",
            model,
            "-c",
            'model_reasoning_effort="low"',
            "--cd",
            str(self.work_dir),
            "--output-last-message",
            str(response_path),
            "--json",
            "-c",
            'web_search="live"' if web_search else 'web_search="disabled"',
        ]
        if editable_file is None:
            command.extend(["--sandbox", "read-only"])
        else:
            # A file-scoped profile, not write access to the show or season.
            profile = (
                'permissions.atlas={extends=":read-only",filesystem={'
                + json.dumps(str(editable_file.resolve()), ensure_ascii=False)
                + '="write"},network={enabled=false}}'
            )
            command.extend(["-c", 'default_permissions="atlas"', "-c", profile])
            command.extend(["-c", 'approval_policy="never"'])
        if schema is not None:
            command.extend(["--output-schema", str(schema)])
        command.append("-")
        raw_path = self.log_path.with_suffix(".codex.jsonl")
        append_log(self.log_path, f"CODEX START: role={role} model={model} request={request_id}")
        with raw_path.open("a", encoding="utf-8") as output:
            process = subprocess.run(
                command,
                input=prompt,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        if process.returncode:
            raise WorkflowError(
                f"codex exec failed for {role}; inspect {raw_path}"
            )
        if not response_path.is_file():
            raise WorkflowError(f"codex exec did not write {response_path}")
        append_log(self.log_path, f"CODEX COMPLETE: role={role} model={model} request={request_id}")
        return response_path.read_text(encoding="utf-8").strip()

    def _run(
        self, prompt: str, *, role: str, model: str, schema: Path, request_id: str
    ) -> dict[str, object]:
        response = self._invoke(
            prompt, role=role, model=model, schema=schema, request_id=request_id
        )
        try:
            value = json.loads(response)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"invalid {role} response JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise WorkflowError(f"{role} response is not an object")
        return value

    def translate(self, prompt: str, *, request_id: str) -> IrisResponse:
        value = self._run(
            prompt,
            role=TRANSLATOR_NAME,
            model=TRANSLATOR_MODEL,
            schema=TRANSLATION_SCHEMA,
            request_id=request_id,
        )
        raw_cues = value.get("cues")
        raw_candidates = value.get("glossary_candidates")
        if not isinstance(raw_cues, list) or not isinstance(raw_candidates, list):
            raise WorkflowError("Iris response needs cues and glossary_candidates arrays")
        cues: list[IrisCue] = []
        for item in raw_cues:
            if not isinstance(item, dict):
                raise WorkflowError("Iris cue is not an object")
            skips = item.get("skip_checks")
            additions = item.get("additions")
            cue_id = item.get("id")
            if (
                not isinstance(cue_id, int)
                or isinstance(cue_id, bool)
                or cue_id < 1
                or not isinstance(item.get("drop"), bool)
                or not isinstance(skips, list)
                or not isinstance(additions, list)
                or not all(isinstance(addition, dict) for addition in additions)
            ):
                raise WorkflowError("Iris cue has invalid id, drop, additions, or skip_checks")
            cues.append(
                IrisCue(
                    id=cue_id,
                    text=str(item.get("text") or "").strip(),
                    drop=bool(item["drop"]),
                    additions=tuple(Addition.from_dict(addition) for addition in additions),
                    skip_checks=tuple(str(check) for check in skips),
                )
            )
        candidates: list[GlossaryCandidate] = []
        for item in raw_candidates:
            if not isinstance(item, dict):
                raise WorkflowError("glossary candidate is not an object")
            aliases = item.get("aliases")
            cue_ids = item.get("cue_ids")
            source = str(item.get("source") or "").strip()
            if (
                not isinstance(aliases, list)
                or not isinstance(cue_ids, list)
                or not all(
                    isinstance(cue_id, int) and not isinstance(cue_id, bool) and cue_id > 0
                    for cue_id in cue_ids
                )
            ):
                raise WorkflowError("glossary candidate has invalid aliases or cue_ids")
            normalized_aliases = tuple(
                str(alias).strip() for alias in aliases if str(alias).strip()
            )
            if not normalized_aliases and source:
                normalized_aliases = (source,)
            candidates.append(
                GlossaryCandidate(
                    category=str(item.get("category") or "").strip(),
                    source=source,
                    aliases=normalized_aliases,
                    target=str(item.get("target") or "").strip(),
                    notes=str(item.get("notes") or "").strip(),
                    cue_ids=tuple(cue_ids),
                )
            )
        return IrisResponse(tuple(cues), tuple(candidates))

    def edit_glossary(
        self, prompt: str, *, glossary: Path, request_id: str
    ) -> str:
        return self._invoke(
            prompt,
            role=CURATOR_NAME,
            model=CURATOR_MODEL,
            request_id=request_id,
            editable_file=glossary,
        )

    def enrich_glossary(
        self, prompt: str, *, glossary: Path, request_id: str
    ) -> str:
        return self._invoke(
            prompt,
            role=CURATOR_NAME,
            model=CURATOR_MODEL,
            request_id=request_id,
            editable_file=glossary,
            web_search=True,
        )
