from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from .config import CONTEXT_CUES, CURATOR_MODEL, RULESET_VERSION, TRANSLATOR_MODEL
from .domain import RenderMapEntry, SourceDocument, TranslationCue
from .errors import WorkflowError
from .language_profiles import DEFAULT_PROFILE, LanguageProfile
from .protocol import (
    parse_source_document,
    parse_translation_document,
    serialize_source_document,
    serialize_translation_document,
)


EPISODE = re.compile(r"s\d{1,2}e\d{1,3}", re.IGNORECASE)


def collection_dir(video: Path, workspace_root: Path) -> Path:
    season = video.parent.name
    if re.fullmatch(r"s\d{1,2}", season, re.IGNORECASE):
        return workspace_root / video.parent.parent.name / season.upper()
    match = EPISODE.search(video.stem)
    if match:
        season_name = re.match(r"s\d+", match.group(), re.IGNORECASE)
        assert season_name is not None
        return workspace_root / video.parent.name / season_name.group().upper()
    return workspace_root / video.stem


def title_dir(state_dir: Path) -> Path:
    return state_dir.parent if re.fullmatch(r"S\d{2}", state_dir.name) else state_dir


def ensure_layout(state_dir: Path) -> None:
    for name in (
        "chunks",
        "indexes",
        "logs",
        "outputs",
        "previews",
        "progress",
        "records",
        "retries",
        "sources",
        "usage",
    ):
        (state_dir / name).mkdir(parents=True, exist_ok=True)


def _profiled_stem(video: Path, profile: LanguageProfile) -> str:
    # Preserve every existing zh-Hans workspace path. Future profiles get their
    # own durable state without colliding with the legacy/default artifacts.
    if profile.id == DEFAULT_PROFILE.id:
        return video.stem
    return f"{video.stem}.{profile.output_tag}"


def output_path(
    state_dir: Path,
    video: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> Path:
    return state_dir / "outputs" / profile.output_filename(video.stem)


def preview_path(
    state_dir: Path,
    video: Path,
    chunk_start: int,
    chunk_end: int,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> Path:
    return (
        state_dir
        / "previews"
        / profile.preview_filename(video.stem, chunk_start, chunk_end)
    )


def records_path(
    state_dir: Path,
    video: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> Path:
    return state_dir / "records" / f"{_profiled_stem(video, profile)}.jsonl"


def source_index_path(state_dir: Path, video: Path) -> Path:
    return state_dir / "indexes" / f"{video.stem}.source.jsonl"


def render_map_path(
    state_dir: Path,
    video: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> Path:
    return state_dir / "indexes" / f"{_profiled_stem(video, profile)}.render.jsonl"


def retry_path(
    state_dir: Path,
    video: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> Path:
    return state_dir / "retries" / f"{_profiled_stem(video, profile)}.json"


def progress_path(
    state_dir: Path,
    video: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> Path:
    return state_dir / "progress" / f"{_profiled_stem(video, profile)}.json"


def subtitle_offset_ms(
    state_dir: Path,
    video: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> int:
    """Return the persistent episode offset; missing legacy values mean zero."""
    path = progress_path(state_dir, video, profile)
    if not path.is_file():
        return 0
    value = read_json(path).get("subtitle_offset_ms", 0)
    if not isinstance(value, int) or isinstance(value, bool):
        raise WorkflowError(f"subtitle_offset_ms must be an integer in {path}")
    return value


def log_path(
    state_dir: Path,
    video: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> Path:
    return state_dir / "logs" / f"{_profiled_stem(video, profile)}.log"


def source_dir(state_dir: Path) -> Path:
    return state_dir / "sources"


def atomic_write(path: Path, content: str, *, overwrite: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise WorkflowError(f"target already exists: {path}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists() and not overwrite:
            raise WorkflowError(f"target appeared during write: {path}")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path: Path, value: dict[str, object]) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"JSON document is not an object: {path}")
    return value


def append_jsonl(path: Path, values: list[dict[str, object]]) -> None:
    if not values:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def save_source_index(state_dir: Path, video: Path, source: SourceDocument) -> Path:
    path = source_index_path(state_dir, video)
    document = serialize_source_document(source)
    atomic_write(path, document)
    loaded = parse_source_document(path.read_text(encoding="utf-8"))
    if loaded.fingerprint != source.fingerprint:
        raise WorkflowError(f"source index verification failed: {path}")
    return path


def save_records(
    state_dir: Path,
    video: Path,
    source_fingerprint: str,
    records: list[TranslationCue],
    profile: LanguageProfile = DEFAULT_PROFILE,
    *,
    retry_fingerprint: str | None = None,
) -> Path:
    path = records_path(state_dir, video, profile)
    atomic_write(
        path,
        serialize_translation_document(
            source_fingerprint, records, profile, retry_fingerprint=retry_fingerprint,
        ),
    )
    return path


def load_records(
    state_dir: Path,
    video: Path,
    *,
    expected_fingerprint: str | None = None,
    expected_retry_fingerprint: str | None = None,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> list[TranslationCue]:
    path = records_path(state_dir, video, profile)
    if not path.is_file():
        raise WorkflowError(f"translation record file is missing: {path}")
    _, records = parse_translation_document(
        path.read_text(encoding="utf-8"),
        expected_fingerprint=expected_fingerprint,
        expected_retry_fingerprint=expected_retry_fingerprint,
        profile=profile,
    )
    return records


def save_render_map(
    state_dir: Path,
    video: Path,
    source_fingerprint: str,
    mapping: tuple[RenderMapEntry, ...],
    *,
    subtitle_offset_ms: int = 0,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> Path:
    path = render_map_path(state_dir, video, profile)
    header = {
        "type": "render_map",
        "schema_version": 1,
        "ruleset_version": RULESET_VERSION,
        "profile": profile.id,
        "source_fingerprint": source_fingerprint,
        "output_cue_count": len(mapping),
        "subtitle_offset_ms": subtitle_offset_ms,
    }
    values: list[dict[str, object]] = [header]
    values.extend(entry.to_dict() for entry in mapping)
    atomic_write(
        path,
        "".join(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
            for value in values
        ),
    )
    return path


def update_progress(
    state_dir: Path,
    video: Path,
    *,
    profile: LanguageProfile = DEFAULT_PROFILE,
    **changes: object,
) -> None:
    path = progress_path(state_dir, video, profile)
    current: dict[str, object] = {}
    if path.is_file():
        try:
            current = read_json(path)
        except WorkflowError:
            current = {}
    current.update(
        {
            "video": video.name,
            "video_path": str(video),
            "runtime_ruleset_version": RULESET_VERSION,
            "translation_model": TRANSLATOR_MODEL,
            "glossary_model": CURATOR_MODEL,
            "profile": profile.id,
            "context_cues": CONTEXT_CUES,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    current.setdefault("subtitle_offset_ms", 0)
    current.update(changes)
    if current.get("status") != "failed":
        current.pop("last_error", None)
    write_json(path, current)
    rebuild_indexes(state_dir)


def _episode_key(entry: dict[str, object]) -> tuple[int, int, str]:
    match = re.search(r"S(\d+)E(\d+)", str(entry.get("video", "")), re.IGNORECASE)
    if not match:
        return (999, 999, str(entry.get("video", "")))
    return (int(match.group(1)), int(match.group(2)), str(entry.get("video", "")))


def _completed_chunk_numbers(episode: dict[str, object], total: int) -> list[int]:
    raw = episode.get("completed_chunks")
    if isinstance(raw, list):
        return sorted(
            {
                value
                for value in raw
                if isinstance(value, int)
                and not isinstance(value, bool)
                and 1 <= value <= total
            }
        )
    leading = min(int(episode.get("chunks_completed") or 0), total)
    return list(range(1, leading + 1))


def _compact_chunk_ranges(chunks: list[int]) -> str:
    if not chunks:
        return ""
    groups: list[str] = []
    start = previous = chunks[0]
    for value in chunks[1:]:
        if value == previous + 1:
            previous = value
            continue
        groups.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    groups.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(groups)


def _report_offset_ms(episode: dict[str, object]) -> int:
    value = episode.get("subtitle_offset_ms", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def rebuild_indexes(state_dir: Path) -> None:
    episodes: list[dict[str, object]] = []
    for path in sorted((state_dir / "progress").glob("*.json")):
        try:
            episodes.append(read_json(path))
        except WorkflowError:
            continue
    episodes.sort(key=_episode_key)
    manifest = {
        "collection": state_dir.name,
        "ruleset_version": RULESET_VERSION,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "episodes": episodes,
    }
    write_json(state_dir / "manifest.json", manifest)

    lines = [
        f"# {state_dir.name} 字幕进度",
        "",
        f"规则：{RULESET_VERSION}；上下文：前后各 {CONTEXT_CUES} 条",
        "",
        "| 视频 | 目标 | 状态 | 已完成块 | 下一块 | 偏移 | 术语 | 输出 | 记录 | 同步 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for episode in episodes:
        total = int(episode.get("chunks_total") or 0)
        completed = _completed_chunk_numbers(episode, total)
        chunk_ranges = _compact_chunk_ranges(completed)
        chunk_summary = f"{chunk_ranges or '-'} ({len(completed)}/{total})"
        next_chunk = episode.get("next_chunk")
        lines.append(
            "| "
            + " | ".join(
                (
                    str(episode.get("video", "")),
                    str(episode.get("profile") or DEFAULT_PROFILE.id),
                    str(episode.get("status", "")),
                    chunk_summary,
                    str(next_chunk) if next_chunk else "",
                    f"{_report_offset_ms(episode):+d} ms",
                    str(episode.get("glossary_status", "")),
                    "✓" if episode.get("output_ready") else "",
                    "✓" if episode.get("records_ready") else "",
                    "✓" if episode.get("synced") else "",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            f"- 术语表：`{title_dir(state_dir) / 'glossary.md'}`",
            f"- Iris 反馈：`{title_dir(state_dir) / 'glossary-feedback.jsonl'}`",
            f"- Atlas 更新：`{title_dir(state_dir) / 'glossary-updates.jsonl'}`",
            f"- Atlas 任务：`{state_dir / 'glossary-jobs'}`",
            f"- 术语引用：`{title_dir(state_dir) / 'glossary-usage.jsonl'}`",
        ]
    )
    atomic_write(state_dir / "REPORT.md", "\n".join(lines) + "\n")
