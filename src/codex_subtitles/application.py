from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from .codex_client import CodexClient, append_log
from .config import DEFAULT_CHUNK_CUES, RULESET_VERSION
from .domain import RenderResult, TranslationCue
from .errors import WorkflowError
from .glossary import ensure_glossary, write_usage
from .language_profiles import DEFAULT_PROFILE, LanguageProfile
from .media import (
    acquire_source,
    cached_source,
    destination_path,
    existing_target_subtitles,
    source_as_srt,
)
from .protocol import build_source_document
from .runtime import MediaTools
from .srt import (
    clean_source_srt,
    normalize_srt,
    parse_srt,
    render_translation,
    shift_srt_timing,
)
from .workflow import TranslationEngine, load_retry_patches, manual_retry, retry_fingerprint
from .workspace import (
    atomic_write,
    collection_dir,
    ensure_layout,
    log_path,
    load_records,
    output_path,
    preview_path,
    progress_path,
    read_json,
    records_path,
    retry_path,
    save_render_map,
    save_records,
    save_source_index,
    source_dir,
    subtitle_offset_ms,
    update_progress,
)


@dataclass(frozen=True)
class RunOptions:
    workspace_root: Path
    codex: str = "codex"
    chunk_cues: int = DEFAULT_CHUNK_CUES
    chunk_range: tuple[int, int | None] | None = None
    overwrite: bool = False
    stage_only: bool = False
    source_only: bool = False
    sync_only: bool = False
    normalize_only: bool = False
    explicit_source: Path | None = None
    requested_track: int | None = None
    retry_cues: str | None = None
    retry_reason: str | None = None
    profile: LanguageProfile = DEFAULT_PROFILE


def _load_assembled_records(
    state_dir: Path,
    video: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> tuple[str, list[TranslationCue]]:
    """Check the saved artifact itself before reusing or publishing its output."""
    try:
        progress = _existing_progress(state_dir, video, profile)
        fingerprint = str(progress.get("source_fingerprint") or "")
        if not fingerprint:
            raise WorkflowError(f"episode has no source fingerprint: {video.name}")
        patches = load_retry_patches(state_dir, video, fingerprint, profile)
        records = load_records(
            state_dir,
            video,
            expected_fingerprint=fingerprint,
            expected_retry_fingerprint=retry_fingerprint(patches),
            profile=profile,
        )
        _require_full_assembly(progress)
    except WorkflowError:
        update_progress(
            state_dir, video, profile=profile, assembly_required=True,
            output_ready=False, records_ready=False, synced=False,
        )
        raise
    return fingerprint, records


def _require_full_assembly(progress: dict[str, object]) -> None:
    if progress.get("assembly_required"):
        raise WorkflowError(
            "full output assembly is required; rebuild with translate --stage-only "
            "--overwrite without --chunks, using the original --chunk-cues value"
        )


def _check_output_without_records(
    state_dir: Path, video: Path, profile: LanguageProfile,
) -> None:
    progress = _existing_progress(state_dir, video, profile)
    _require_full_assembly(progress)
    if retry_path(state_dir, video, profile).is_file():
        update_progress(
            state_dir, video, profile=profile, assembly_required=True,
            output_ready=False, records_ready=False, synced=False,
        )
        raise WorkflowError(
            "cannot verify saved corrections without translation records; "
            "rebuild with translate --stage-only --overwrite without --chunks, "
            "using the original --chunk-cues value"
        )


def _existing_progress(
    state_dir: Path,
    video: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> dict[str, object]:
    path = progress_path(state_dir, video, profile)
    if not path.is_file():
        return {}
    try:
        return read_json(path)
    except WorkflowError:
        return {}


def _compatible_completed_chunks(
    progress: dict[str, object],
    *,
    source_fingerprint: str,
    chunks_total: int,
    chunk_cues: int,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> set[int]:
    chunk_ruleset = progress.get("chunk_ruleset_version", progress.get("ruleset_version"))
    if (
        chunk_ruleset != RULESET_VERSION
        or progress.get("source_fingerprint") != source_fingerprint
        or int(progress.get("chunk_cues") or chunk_cues) != chunk_cues
        or str(progress.get("profile") or DEFAULT_PROFILE.id) != profile.id
    ):
        return set()
    raw = progress.get("completed_chunks")
    if isinstance(raw, list):
        return {
            value
            for value in raw
            if isinstance(value, int)
            and not isinstance(value, bool)
            and 1 <= value <= chunks_total
        }
    leading = min(int(progress.get("chunks_completed") or 0), chunks_total)
    return set(range(1, leading + 1))


def _next_chunk(completed: set[int], chunks_total: int) -> int | None:
    return next((chunk for chunk in range(1, chunks_total + 1) if chunk not in completed), None)


def _write_final_output(
    state_dir: Path,
    video: Path,
    source_fingerprint: str,
    records: list[TranslationCue],
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> tuple[RenderResult, Path, str, int]:
    """Render the canonical records, then apply the episode offset to final SRT only."""
    rendered = render_translation(records)
    offset_ms = subtitle_offset_ms(state_dir, video, profile)
    document = shift_srt_timing(rendered.document, offset_ms)
    render_map = save_render_map(
        state_dir,
        video,
        source_fingerprint,
        rendered.mapping,
        subtitle_offset_ms=offset_ms,
        profile=profile,
    )
    staged_output = output_path(state_dir, video, profile)
    atomic_write(staged_output, document)
    return rendered, render_map, document, offset_ms


def rerender_saved_output(
    video: Path,
    workspace_root: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> tuple[Path, int]:
    """Rebuild a final local SRT from records without any model or media call."""
    state_dir = collection_dir(video, workspace_root)
    ensure_layout(state_dir)
    fingerprint, records = _load_assembled_records(state_dir, video, profile)
    rendered, render_map, _, offset_ms = _write_final_output(
        state_dir, video, fingerprint, records, profile
    )
    staged_output = output_path(state_dir, video, profile)
    update_progress(
        state_dir,
        video,
        profile=profile,
        status="staged",
        ruleset_version=RULESET_VERSION,
        subtitle_offset_ms=offset_ms,
        output=str(staged_output),
        output_ready=True,
        records=str(records_path(state_dir, video, profile)),
        records_ready=True,
        render_map=str(render_map),
        sound_only_cues_removed=rendered.dropped_cues,
        additions_added=rendered.addition_counts,
        pun_notes_added=rendered.addition_counts.get("pun_note", 0),
        validation_appeals=list(rendered.appeals),
        synced=False,
    )
    return staged_output, offset_ms


def process_video(video: Path, options: RunOptions) -> str:
    profile = options.profile
    tools = MediaTools.in_workspace(options.workspace_root)
    state_dir = collection_dir(video, options.workspace_root)
    ensure_layout(state_dir)
    ensure_glossary(state_dir, profile)
    staged_output = output_path(state_dir, video, profile)
    destination = destination_path(video, profile)
    episode_log = log_path(state_dir, video, profile)
    episode_log.touch(exist_ok=True)
    prior_progress = _existing_progress(state_dir, video, profile)

    if options.source_only:
        if not options.overwrite:
            existing = existing_target_subtitles(video, tools, profile)
            if existing:
                update_progress(
                    state_dir,
                    video,
                    profile=profile,
                    status="existing_target",
                    existing_target=existing,
                    output=str(staged_output),
                    synced=destination.is_file(),
                )
                return f"EXISTING {profile.output_tag.upper()} {video.name}: {'; '.join(existing)}"
        append_log(episode_log, f"SOURCE-ONLY START: {video}")
        try:
            if options.explicit_source is not None or options.requested_track is not None:
                raise WorkflowError("explicit source selection bypasses the source cache")
            source = cached_source(video, source_dir(state_dir))
            description = f"durable local source {source.name}"
            append_log(episode_log, f"SOURCE CACHE HIT: {source}")
        except WorkflowError:
            update_progress(
                state_dir,
                video,
                profile=profile,
                status="copying_source",
                output=str(staged_output),
                synced=False,
            )
            source, description = acquire_source(
                video,
                source_dir(state_dir),
                episode_log,
                explicit_source=options.explicit_source,
                requested_track=options.requested_track,
                tools=tools,
                profile=profile,
            )
        if prior_progress.get("output_ready") and prior_progress.get("records_ready"):
            source_status = "synced" if prior_progress.get("synced") else "staged"
        else:
            source_status = "source_ready"
        update_progress(
            state_dir,
            video,
            profile=profile,
            status=source_status,
            source=description,
            source_file=str(source),
            output=str(staged_output),
            synced=bool(prior_progress.get("synced")),
        )
        append_log(episode_log, f"SOURCE-ONLY COMPLETE: {source}")
        return f"SOURCE READY {video.name}: {source}"

    if options.sync_only:
        if records_path(state_dir, video, profile).is_file():
            rerender_saved_output(video, options.workspace_root, profile)
        else:
            _check_output_without_records(state_dir, video, profile)
        if not staged_output.is_file():
            raise WorkflowError(f"no staged output: {staged_output}")
        document = normalize_srt(
            staged_output.read_text(encoding="utf-8-sig"), profile
        )
        atomic_write(staged_output, document)
        if destination.is_file() and not options.overwrite:
            try:
                existing = normalize_srt(
                    destination.read_text(encoding="utf-8-sig"), profile
                )
            except (OSError, UnicodeError, WorkflowError) as exc:
                raise WorkflowError(
                    f"destination exists and is not the same valid SRT: {destination}"
                ) from exc
            if existing != document:
                raise WorkflowError(
                    f"destination has different content; use --overwrite: {destination}"
                )
        else:
            atomic_write(destination, document, overwrite=options.overwrite)
        update_progress(
            state_dir,
            video,
            profile=profile,
            status="synced",
            output=str(staged_output),
            output_ready=True,
            records_ready=records_path(state_dir, video, profile).is_file(),
            synced=True,
        )
        return f"SYNCED {staged_output} -> {destination}"

    if options.normalize_only:
        if records_path(state_dir, video, profile).is_file():
            _load_assembled_records(state_dir, video, profile)
        else:
            _check_output_without_records(state_dir, video, profile)
        if not staged_output.is_file():
            raise WorkflowError(f"no staged output: {staged_output}")
        document = normalize_srt(
            staged_output.read_text(encoding="utf-8-sig"), profile
        )
        atomic_write(staged_output, document)
        update_progress(
            state_dir,
            video,
            profile=profile,
            status="staged",
            output=str(staged_output),
            output_ready=True,
            records_ready=records_path(state_dir, video, profile).is_file(),
            synced=False,
        )
        return f"NORMALIZED {staged_output}"

    if options.retry_cues:
        if not options.retry_reason:
            raise WorkflowError("retry_reason is required for retry_cues")
        source = cached_source(video, source_dir(state_dir))
        with tempfile.TemporaryDirectory(prefix="codex-subtitles-retry-") as temp_name:
            source_srt = clean_source_srt(source_as_srt(source, Path(temp_name), tools))
            source_document = build_source_document(source_srt)
            source_index = save_source_index(state_dir, video, source_document)
            client = CodexClient(
                executable=options.codex,
                work_dir=Path(temp_name),
                log_path=episode_log,
            )
            path = manual_retry(
                source_document,
                selector=options.retry_cues,
                reason=options.retry_reason,
                state_dir=state_dir,
                video=video,
                client=client,
                log_path=episode_log,
                profile=profile,
            )
            update_progress(
                state_dir,
                video,
                profile=profile,
                source_index=str(source_index),
            )
        return f"RETRY READY {video.name}: {path}"

    if staged_output.is_file() and not options.overwrite:
        try:
            _load_assembled_records(state_dir, video, profile)
        except WorkflowError as exc:
            raise WorkflowError(f"staged output is stale: {exc}; use --overwrite") from exc
        return f"SKIP {video.name}: current staged output exists"

    if not options.stage_only and not options.overwrite:
        existing = existing_target_subtitles(video, tools, profile)
        if existing:
            update_progress(
                state_dir,
                video,
                profile=profile,
                status="existing_target",
                existing_target=existing,
                output=str(staged_output),
                synced=destination.is_file(),
            )
            return f"EXISTING {profile.output_tag.upper()} {video.name}: {'; '.join(existing)}"

    append_log(episode_log, f"VIDEO START: {video}")
    update_progress(
        state_dir,
        video,
        profile=profile,
        status="starting",
        output=str(staged_output),
        synced=False,
    )
    try:
        if options.stage_only:
            source = cached_source(video, source_dir(state_dir))
            description = f"durable local source {source.name}"
        else:
            source, description = acquire_source(
                video,
                source_dir(state_dir),
                episode_log,
                explicit_source=options.explicit_source,
                requested_track=options.requested_track,
                tools=tools,
                profile=profile,
            )
        with tempfile.TemporaryDirectory(prefix="codex-subtitles-") as temp_name:
            work_dir = Path(temp_name)
            source_srt = clean_source_srt(source_as_srt(source, work_dir, tools))
            source_document = build_source_document(source_srt)
            source_index = save_source_index(state_dir, video, source_document)
            chunks_total = (
                len(source_document.cues) + options.chunk_cues - 1
            ) // options.chunk_cues
            completed_chunks = _compatible_completed_chunks(
                prior_progress,
                source_fingerprint=source_document.fingerprint,
                chunks_total=chunks_total,
                chunk_cues=options.chunk_cues,
                profile=profile,
            )
            if options.chunk_range is not None:
                selected_start = options.chunk_range[0]
                selected_end = options.chunk_range[1] or chunks_total
            else:
                selected_start = 1
                selected_end = chunks_total
            update_progress(
                state_dir,
                video,
                profile=profile,
                status="translating",
                chunk_ruleset_version=RULESET_VERSION,
                source=description,
                source_file=str(source),
                source_fingerprint=source_document.fingerprint,
                source_fingerprint_pending=False,
                source_index=str(source_index),
                cues_total=len(source_document.cues),
                chunk_cues=options.chunk_cues,
                chunks_completed=len(completed_chunks),
                completed_chunks=sorted(completed_chunks),
                chunks_total=chunks_total,
                next_chunk=_next_chunk(completed_chunks, chunks_total),
                active_chunk_range={"start": selected_start, "end": selected_end},
                output=str(staged_output),
                interruption_reason=None,
                synced=False,
            )
            client = CodexClient(
                executable=options.codex,
                work_dir=work_dir,
                log_path=episode_log,
            )
            engine = TranslationEngine(
                client=client,
                state_dir=state_dir,
                video=video,
                log_path=episode_log,
                chunk_cues=options.chunk_cues,
                profile=profile,
            )
            def mark_chunk_completed(chunk_index: int, total: int) -> None:
                completed_chunks.add(chunk_index)
                update_progress(
                    state_dir,
                    video,
                    profile=profile,
                    status="translating",
                    chunk_cues=options.chunk_cues,
                    chunks_completed=len(completed_chunks),
                    completed_chunks=sorted(completed_chunks),
                    chunks_total=total,
                    last_completed_chunk=chunk_index,
                    next_chunk=_next_chunk(completed_chunks, total),
                )

            run = engine.translate(
                source_document,
                chunk_range=options.chunk_range,
                progress_callback=mark_chunk_completed,
            )
            records = list(run.records)
            if not run.complete:
                rendered = render_translation(
                    records,
                    first_source_id=(run.chunk_start - 1) * options.chunk_cues + 1,
                )
                preview = preview_path(
                    state_dir,
                    video,
                    run.chunk_start,
                    run.chunk_end,
                    profile,
                )
                atomic_write(preview, rendered.document)
                append_log(
                    episode_log,
                    f"PARTIAL PREVIEW READY: range={run.chunk_start}-{run.chunk_end}/"
                    f"{run.chunks_total} "
                    f"preview={preview}",
                )
                update_progress(
                    state_dir,
                    video,
                    profile=profile,
                    status="partial",
                    source_fingerprint=source_document.fingerprint,
                    source_index=str(source_index),
                    chunk_cues=options.chunk_cues,
                    chunks_completed=len(completed_chunks),
                    completed_chunks=sorted(completed_chunks),
                    chunks_total=run.chunks_total,
                    last_completed_chunk=run.chunk_end,
                    next_chunk=_next_chunk(completed_chunks, run.chunks_total),
                    active_chunk_range={"start": run.chunk_start, "end": run.chunk_end},
                    chunks_processed_this_run=run.chunks_completed,
                    assembly_required=len(completed_chunks) == run.chunks_total,
                    preview=str(preview),
                    preview_cues=len(parse_srt(rendered.document)),
                    output_ready=False,
                    records_ready=False,
                    sound_only_cues_removed=rendered.dropped_cues,
                    additions_added=rendered.addition_counts,
                    pun_notes_added=rendered.addition_counts.get("pun_note", 0),
                    validation_appeals=list(rendered.appeals),
                    interruption_reason=None,
                    glossary_usage_records=None,
                    glossary_usage_file=None,
                    synced=False,
                )
                return (
                    f"PARTIAL {video.name}: range {run.chunk_start}-{run.chunk_end}/"
                    f"{run.chunks_total}; completed {len(completed_chunks)}/"
                    f"{run.chunks_total}; preview {preview}"
                )
            record_file = save_records(
                state_dir,
                video,
                source_document.fingerprint,
                records,
                profile,
                retry_fingerprint=run.retry_fingerprint,
            )
            rendered, render_map, final_document, offset_ms = _write_final_output(
                state_dir,
                video,
                source_document.fingerprint,
                records,
                profile,
            )
            usage_count = write_usage(
                state_dir,
                video,
                staged_output,
                records,
                profile,
            )
        append_log(
            episode_log,
            f"TRANSLATION VALIDATED: source={source_document.fingerprint} "
            f"dropped={rendered.dropped_cues} additions={rendered.addition_counts} "
            f"appeals={len(rendered.appeals)} offset_ms={offset_ms}",
        )
        update_progress(
            state_dir,
            video,
            profile=profile,
            status="staged",
            ruleset_version=RULESET_VERSION,
            source_fingerprint=source_document.fingerprint,
            source_index=str(source_index),
            chunks_completed=chunks_total,
            completed_chunks=list(range(1, chunks_total + 1)),
            chunks_total=chunks_total,
            next_chunk=None,
            active_chunk_range={"start": run.chunk_start, "end": run.chunk_end},
            chunks_processed_this_run=run.chunks_completed,
            assembly_required=False,
            output=str(staged_output),
            subtitle_offset_ms=offset_ms,
            output_ready=True,
            records=str(record_file),
            records_ready=True,
            render_map=str(render_map),
            sound_only_cues_removed=rendered.dropped_cues,
            additions_added=rendered.addition_counts,
            pun_notes_added=rendered.addition_counts.get("pun_note", 0),
            validation_appeals=list(rendered.appeals),
            glossary_usage_records=usage_count,
            synced=False,
        )
        if not options.stage_only:
            atomic_write(destination, final_document, overwrite=options.overwrite)
            update_progress(
                state_dir,
                video,
                profile=profile,
                status="synced",
                synced=True,
            )
            return f"SYNCED {staged_output} -> {destination}"
        return f"STAGED {staged_output}"
    except (OSError, WorkflowError) as exc:
        update_progress(
            state_dir,
            video,
            profile=profile,
            status="failed",
            last_error=str(exc),
            output=str(staged_output),
            synced=False,
        )
        raise
