from __future__ import annotations

import json
import re
from dataclasses import asdict
from pathlib import Path

from .config import RULESET_VERSION, TRANSLATOR_MODEL, TRANSLATOR_NAME
from .domain import GlossaryCandidate, GlossaryEntry, TranslationCue
from .errors import WorkflowError
from .language_profiles import DEFAULT_PROFILE, LanguageProfile
from .workspace import append_jsonl, atomic_write, title_dir


def _profiled_name(base: str, profile: LanguageProfile) -> str:
    return base if profile.id == DEFAULT_PROFILE.id else f"{base}.{profile.id}"


def _headers(profile: LanguageProfile) -> dict[str, str]:
    return {
        "ID": "id",
        "原文及别名": "aliases",
        profile.glossary_column: "target",
        "备注": "notes",
        "范围": "scope",
    }


def glossary_path(
    state_dir: Path, profile: LanguageProfile = DEFAULT_PROFILE
) -> Path:
    return title_dir(state_dir) / f"{_profiled_name('glossary', profile)}.md"


def feedback_path(
    state_dir: Path, profile: LanguageProfile = DEFAULT_PROFILE
) -> Path:
    return title_dir(state_dir) / f"{_profiled_name('glossary-feedback', profile)}.jsonl"


def updates_path(
    state_dir: Path, profile: LanguageProfile = DEFAULT_PROFILE
) -> Path:
    return title_dir(state_dir) / f"{_profiled_name('glossary-updates', profile)}.jsonl"


def usage_index_path(
    state_dir: Path, profile: LanguageProfile = DEFAULT_PROFILE
) -> Path:
    return title_dir(state_dir) / f"{_profiled_name('glossary-usage', profile)}.jsonl"


def ensure_glossary(
    state_dir: Path, profile: LanguageProfile = DEFAULT_PROFILE
) -> Path:
    path = glossary_path(state_dir, profile)
    if not path.exists():
        atomic_write(
            path,
            f"# {title_dir(state_dir).name} {profile.native_name}术语表\n\n"
            "译名保持一致，背景资料由 Atlas 增量核实与合并，不自动清空本表。\n",
        )
    return path


def _cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    parts = re.split(r"(?<!\\)\|", stripped)
    return [part.strip().replace(r"\|", "|") for part in parts]


def load_glossary(
    state_dir: Path, profile: LanguageProfile = DEFAULT_PROFILE
) -> list[GlossaryEntry]:
    path = ensure_glossary(state_dir, profile)
    return parse_glossary(path.read_text(encoding="utf-8"), profile)


def parse_glossary(
    document: str, profile: LanguageProfile = DEFAULT_PROFILE
) -> list[GlossaryEntry]:
    """Read Markdown without generating, normalizing, or rewriting its contents."""
    lines = document.splitlines()
    entries: list[GlossaryEntry] = []
    category = ""
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith("## "):
            category = line[3:].strip()
            index += 1
            continue
        if category and line.startswith("|"):
            headers = _cells(line)
            expected_headers = _headers(profile)
            if set(expected_headers).issubset(headers):
                positions = {
                    expected_headers[name]: headers.index(name)
                    for name in expected_headers
                }
                index += 2
                while index < len(lines) and lines[index].strip().startswith("|"):
                    values = _cells(lines[index])
                    if len(values) != len(headers):
                        raise WorkflowError(f"glossary row has the wrong number of columns: line {index + 1}")
                    if len(values) >= len(headers):
                        aliases = tuple(
                            alias.strip()
                            for alias in re.split(r"[;；]", values[positions["aliases"]])
                            if alias.strip()
                        )
                        entry_id = values[positions["id"]].strip()
                        target = values[positions["target"]].strip()
                        if not entry_id or not aliases or not target:
                            raise WorkflowError(
                                f"glossary row is missing ID, aliases, or target: line {index + 1}"
                            )
                        if entry_id and aliases and target:
                            entries.append(
                                GlossaryEntry(
                                    category=category,
                                    id=entry_id,
                                    aliases=aliases,
                                    target=target,
                                    notes=values[positions["notes"]].strip(),
                                    scope=(
                                        values[positions["scope"]].strip()
                                        or profile.default_scope
                                    ),
                                )
                            )
                    index += 1
                continue
            raise WorkflowError(f"glossary table has unrecognized headers: line {index + 1}")
        index += 1
    duplicate = len({entry.key for entry in entries}) != len(entries)
    if duplicate:
        raise WorkflowError("glossary contains duplicate category/ID keys")
    return entries


def validate_glossary_edit(
    before: str,
    after: str,
    profile: LanguageProfile = DEFAULT_PROFILE,
    *,
    allow_merge: bool = False,
) -> None:
    """Only structural/read-only safety checks; Atlas owns all terminology edits."""
    if not after.strip():
        raise WorkflowError("Atlas left an empty glossary")
    old_entries = parse_glossary(before, profile)
    new_entries = {entry.key: entry for entry in parse_glossary(after, profile)}
    if allow_merge:
        return  # Atlas owns semantic merging; Markdown must still parse correctly.
    for old in old_entries:
        new = new_entries.get(old.key)
        if new is None:
            raise WorkflowError(f"Atlas removed an existing glossary entry: {old.key}")
        if (new.target, new.notes, new.scope) != (old.target, old.notes, old.scope):
            raise WorkflowError(f"Atlas changed a confirmed glossary entry: {old.key}")
        if not set(old.aliases).issubset(new.aliases):
            raise WorkflowError(f"Atlas removed existing aliases: {old.key}")


def _alias_occurs(alias: str, text: str) -> bool:
    if re.fullmatch(r"[A-Za-z0-9 .&'’\-]+", alias):
        return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", text, re.I))
    return alias.casefold() in text.casefold()


def glossary_context(
    state_dir: Path, video: Path, profile: LanguageProfile = DEFAULT_PROFILE
) -> str:
    # Include the notes even when a chunk contains only pronouns.
    return f"Current episode: {video.name}\n" + ensure_glossary(
        state_dir, profile
    ).read_text(encoding="utf-8")


def record_feedback(
    state_dir: Path,
    video: Path,
    candidates: list[GlossaryCandidate],
    *,
    chunk: int,
    chunks_total: int,
    attempt: int,
    request_kind: str,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> list[dict[str, object]]:
    records = [
        {
            "video": video.name,
            "season": state_dir.name,
            "chunk": chunk,
            "chunks_total": chunks_total,
            "attempt": attempt,
            "request_kind": request_kind,
            "ruleset": RULESET_VERSION,
            "role": TRANSLATOR_NAME,
            "model": TRANSLATOR_MODEL,
            "profile": profile.id,
            **asdict(candidate),
        }
        for candidate in candidates
    ]
    for record in records:
        record["aliases"] = list(record["aliases"])
        record["cue_ids"] = list(record["cue_ids"])
    append_jsonl(feedback_path(state_dir, profile), records)
    return records


def write_usage(
    state_dir: Path,
    video: Path,
    output: Path,
    records: list[TranslationCue],
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> int:
    entries = load_glossary(state_dir, profile)
    usage: list[dict[str, object]] = []
    for record in records:
        for entry in entries:
            matched = [alias for alias in entry.aliases if _alias_occurs(alias, record.source_text)]
            if not matched:
                continue
            usage.append(
                {
                    "entry": entry.key,
                    "category": entry.category,
                    "id": entry.id,
                    "matched": matched,
                    "profile": profile.id,
                    profile.glossary_value_key: entry.target,
                    "video": video.name,
                    "season": state_dir.name if state_dir != title_dir(state_dir) else "",
                    "cue_id": record.id,
                    "cue": record.source_number,
                    "timestamp": record.timestamp,
                    "output": str(output),
                    "applied": not record.drop and entry.target in record.text,
                    "ruleset": RULESET_VERSION,
                }
            )
    if profile.id == DEFAULT_PROFILE.id:
        episode_path = state_dir / "usage" / f"{video.stem}.jsonl"
    else:
        episode_path = state_dir / "usage" / profile.id / f"{video.stem}.jsonl"
    atomic_write(
        episode_path,
        "".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in usage),
    )
    root = title_dir(state_dir)
    if profile.id == DEFAULT_PROFILE.id:
        files = (
            sorted((root / "usage").glob("*.jsonl"))
            if root == state_dir
            else sorted(root.glob("S[0-9][0-9]/usage/*.jsonl"))
        )
    else:
        files = (
            sorted((root / "usage" / profile.id).glob("*.jsonl"))
            if root == state_dir
            else sorted(root.glob(f"S[0-9][0-9]/usage/{profile.id}/*.jsonl"))
        )
    lines: list[str] = []
    for path in files:
        lines.extend(line for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    atomic_write(
        usage_index_path(state_dir, profile),
        "\n".join(lines) + ("\n" if lines else ""),
    )
    return len(usage)
