from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .codex_client import CodexClient, append_log
from .config import (
    CONTEXT_CUES,
    DEFAULT_CHUNK_CUES,
    MAX_TRANSLATION_ATTEMPTS,
    RULESET_VERSION,
)
from .domain import (
    Addition,
    GlossaryCandidate,
    IrisCue,
    SourceCue,
    SourceDocument,
    TranslationCue,
    TranslationRun,
)
from .errors import ValidationIssue, WorkflowError
from .curation import enqueue_curation, ensure_episode_enrichment, retry_pending
from .glossary import glossary_context, record_feedback
from .language_profiles import DEFAULT_PROFILE, LanguageProfile
from .prompts import iris_chunk_prompt, iris_local_retry_prompt
from .protocol import (
    parse_translation_document,
    serialize_translation_document,
    source_window_jsonl,
    validate_iris_cues,
)
from .workspace import (
    atomic_write,
    read_json,
    retry_path,
    save_source_index,
    update_progress,
    write_json,
)


def _iris_json(cues: list[IrisCue]) -> str:
    return json.dumps(
        [
            {
                "id": cue.id,
                "text": cue.text,
                "drop": cue.drop,
                "additions": [addition.to_dict() for addition in cue.additions],
                "skip_checks": list(cue.skip_checks),
            }
            for cue in cues
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _translation_as_iris(cue: TranslationCue) -> IrisCue:
    return IrisCue(cue.id, cue.text, cue.drop, cue.additions, cue.skip_checks)


def _index_iris_cues(cues: tuple[IrisCue, ...]) -> dict[int, IrisCue]:
    indexed: dict[int, IrisCue] = {}
    for cue in cues:
        if cue.id in indexed:
            raise ValidationIssue(
                "cue id",
                f"Iris returned duplicate ID {cue.id}",
                cue_ids=(cue.id,),
                skippable=False,
            )
        indexed[cue.id] = cue
    return indexed


def _context_positions(target_positions: list[int], total: int) -> list[int]:
    positions: set[int] = set()
    for target in target_positions:
        positions.update(
            range(max(0, target - CONTEXT_CUES), min(total, target + CONTEXT_CUES + 1))
        )
    return sorted(positions)


def _candidate_ids_are_targets(
    candidates: list[GlossaryCandidate], target_ids: set[int]
) -> None:
    invalid = sorted(
        {
            cue_id
            for candidate in candidates
            for cue_id in candidate.cue_ids
            if cue_id not in target_ids
        }
    )
    if invalid:
        raise ValidationIssue(
            "cue id",
            "glossary candidates cite unknown or context-only IDs: "
            + ", ".join(map(str, invalid[:8])),
            cue_ids=tuple(invalid),
            skippable=False,
        )


def _empty_patch_document(
    source_fingerprint: str, profile: LanguageProfile,
) -> dict[str, object]:
    return {
        "version": 2,
        "ruleset_version": RULESET_VERSION,
        "source_fingerprint": source_fingerprint,
        "profile": profile.id,
        "patches": [],
    }


def _patch_document(
    state_dir: Path,
    video: Path,
    source_fingerprint: str,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> dict[str, object]:
    path = retry_path(state_dir, video, profile)
    if not path.is_file():
        return _empty_patch_document(source_fingerprint, profile)
    value = read_json(path)
    if (
        value.get("ruleset_version") != RULESET_VERSION
        or value.get("source_fingerprint") != source_fingerprint
        or str(value.get("profile") or DEFAULT_PROFILE.id) != profile.id
    ):
        raise WorkflowError(f"retry patches are stale for the current source: {path}")
    if not isinstance(value.get("patches"), list):
        raise WorkflowError(f"retry patch document has no patches array: {path}")
    return value


def _patches_by_id(document: dict[str, object]) -> dict[int, IrisCue]:
    patches: dict[int, IrisCue] = {}
    for item in document.get("patches", []):
        if not isinstance(item, dict):
            raise WorkflowError("retry patch is not an object")
        cue_id = item.get("id")
        additions = item.get("additions", [])
        skips = item.get("skip_checks", [])
        if (
            not isinstance(cue_id, int)
            or isinstance(cue_id, bool)
            or cue_id < 1
            or not isinstance(skips, list)
            or not isinstance(additions, list)
            or not all(isinstance(addition, dict) for addition in additions)
        ):
            raise WorkflowError(f"retry patch has an invalid record: {cue_id}")
        patches[cue_id] = IrisCue(
            cue_id,
            str(item.get("text") or ""),
            bool(item.get("drop")),
            tuple(Addition.from_dict(addition) for addition in additions),
            tuple(str(check) for check in skips),
        )
    return patches


class TranslationEngine:
    def __init__(
        self,
        *,
        client: CodexClient,
        state_dir: Path,
        video: Path,
        log_path: Path,
        chunk_cues: int = DEFAULT_CHUNK_CUES,
        profile: LanguageProfile = DEFAULT_PROFILE,
    ) -> None:
        if chunk_cues < 1:
            raise WorkflowError("chunk_cues must be positive")
        self.client = client
        self.state_dir = state_dir
        self.video = video
        self.log_path = log_path
        self.chunk_cues = chunk_cues
        self.profile = profile

    def _cache_path(
        self,
        source_fingerprint: str,
        *,
        legacy_patches: dict[str, object] | None = None,
    ) -> Path:
        patch_suffix = ""
        if legacy_patches is not None:
            digest = hashlib.sha256(
                json.dumps(legacy_patches, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:10]
            patch_suffix = f".p{digest}"
        profile_suffix = (
            "" if self.profile.id == DEFAULT_PROFILE.id else f".l{self.profile.id}"
        )
        return self.state_dir / "chunks" / (
            f"{self.video.stem}.src-{source_fingerprint[:12]}.r{RULESET_VERSION}."
            f"c{self.chunk_cues}.o{CONTEXT_CUES}{patch_suffix}{profile_suffix}"
        )

    def _translate_chunk(
        self,
        all_sources: list[SourceCue],
        *,
        core_start: int,
        core_end: int,
        chunk_index: int,
        chunk_total: int,
    ) -> tuple[list[TranslationCue], list[GlossaryCandidate]]:
        targets = all_sources[core_start:core_end]
        target_ids = {cue.id for cue in targets}
        window_start = max(0, core_start - CONTEXT_CUES)
        window_end = min(len(all_sources), core_end + CONTEXT_CUES)
        window = all_sources[window_start:window_end]
        source_window = source_window_jsonl(window, target_ids)
        glossary = glossary_context(self.state_dir, self.video, self.profile)

        raw_by_id: dict[int, IrisCue] | None = None
        validated: list[TranslationCue] | None = None
        candidates: list[GlossaryCandidate] = []
        retry_reason: str | None = None
        last_error: WorkflowError | None = None

        for attempt in range(1, MAX_TRANSLATION_ATTEMPTS + 1):
            request_kind = "initial" if attempt == 1 else "full-retry"
            local_ids: set[int] = set()
            if raw_by_id is not None and isinstance(last_error, ValidationIssue):
                local_ids = set(last_error.cue_ids) & target_ids
            try:
                if local_ids and raw_by_id is not None:
                    target_positions = [cue_id - 1 for cue_id in sorted(local_ids)]
                    positions = _context_positions(target_positions, len(all_sources))
                    retry_window = [all_sources[position] for position in positions]
                    retry_source = source_window_jsonl(retry_window, local_ids)
                    retry_targets = [cue for cue in targets if cue.id in local_ids]
                    previous = _iris_json([raw_by_id[cue.id] for cue in retry_targets])
                    prompt = iris_local_retry_prompt(
                        retry_source,
                        glossary_context(self.state_dir, self.video, self.profile),
                        reason=retry_reason or str(last_error),
                        previous=previous,
                        profile=self.profile,
                    )
                    request_kind = "local-retry"
                    response = self.client.translate(
                        prompt,
                        request_id=f"iris-c{chunk_index:03d}-a{attempt:02d}-local",
                    )
                    _candidate_ids_are_targets(list(response.glossary_candidates), local_ids)
                    record_feedback(
                        self.state_dir,
                        self.video,
                        list(response.glossary_candidates),
                        chunk=chunk_index,
                        chunks_total=chunk_total,
                        attempt=attempt,
                        request_kind=request_kind,
                        profile=self.profile,
                    )
                    repaired = validate_iris_cues(
                        retry_targets,
                        response.cues,
                        retry=True,
                        profile=self.profile,
                    )
                    for cue in repaired:
                        raw_by_id[cue.id] = _translation_as_iris(cue)
                    candidates.extend(response.glossary_candidates)
                else:
                    prompt = iris_chunk_prompt(
                        source_window,
                        glossary,
                        chunk_index=chunk_index,
                        chunk_total=chunk_total,
                        core_start=core_start,
                        core_end=core_end,
                        window_start=window_start,
                        window_end=window_end,
                        retry_reason=retry_reason if attempt > 1 else None,
                        previous=_iris_json(list(raw_by_id.values())) if raw_by_id else None,
                        profile=self.profile,
                    )
                    response = self.client.translate(
                        prompt,
                        request_id=f"iris-c{chunk_index:03d}-a{attempt:02d}-full",
                    )
                    _candidate_ids_are_targets(list(response.glossary_candidates), target_ids)
                    record_feedback(
                        self.state_dir,
                        self.video,
                        list(response.glossary_candidates),
                        chunk=chunk_index,
                        chunks_total=chunk_total,
                        attempt=attempt,
                        request_kind=request_kind,
                        profile=self.profile,
                    )
                    raw_by_id = _index_iris_cues(response.cues)
                    candidates.extend(response.glossary_candidates)

                assert raw_by_id is not None
                validated = validate_iris_cues(
                    targets,
                    list(raw_by_id.values()),
                    retry=attempt > 1,
                    profile=self.profile,
                )
                break
            except ValidationIssue as exc:
                last_error = exc
                retry_reason = f"{exc.check}: {exc}"
                append_log(
                    self.log_path,
                    f"CHUNK ATTEMPT FAILED: chunk={chunk_index}/{chunk_total} "
                    f"attempt={attempt}/{MAX_TRANSLATION_ATTEMPTS} check={exc.check}: {exc}",
                )
                if not exc.cue_ids or exc.check == "cue id":
                    raw_by_id = None
            except WorkflowError as exc:
                last_error = exc
                retry_reason = f"structure: {exc}"
                raw_by_id = None
                append_log(
                    self.log_path,
                    f"CHUNK ATTEMPT FAILED: chunk={chunk_index}/{chunk_total} "
                    f"attempt={attempt}/{MAX_TRANSLATION_ATTEMPTS}: {exc}",
                )

        if validated is None:
            raise WorkflowError(
                f"chunk {chunk_index}/{chunk_total} failed after "
                f"{MAX_TRANSLATION_ATTEMPTS} attempts: {last_error}"
            )
        return validated, candidates

    def translate(
        self,
        source: SourceDocument,
        *,
        chunk_range: tuple[int, int | None] | None = None,
        progress_callback=None,
    ) -> TranslationRun:
        save_source_index(self.state_dir, self.video, source)
        source_cues = list(source.cues)
        patch_document = _patch_document(
            self.state_dir,
            self.video,
            source.fingerprint,
            self.profile,
        )
        patches = _patches_by_id(patch_document)
        unknown_patches = sorted(set(patches) - {cue.id for cue in source_cues})
        if unknown_patches:
            raise WorkflowError(
                "retry patches contain unknown IDs: " + ", ".join(map(str, unknown_patches))
            )
        # Corrections are validated against the same immutable source as the
        # base records, but never become part of the chunk cache identity.
        corrected = {
            record.id: record
            for record in validate_iris_cues(
                [cue for cue in source_cues if cue.id in patches],
                patches.values(),
                retry=True,
                profile=self.profile,
            )
        }
        cache_dir = self._cache_path(source.fingerprint)
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Only an empty-patch legacy namespace is a trustworthy original. Other
        # old namespaces may have permanently baked corrections into their text.
        legacy_cache_dir = self._cache_path(
            source.fingerprint,
            legacy_patches=_empty_patch_document(source.fingerprint, self.profile),
        )
        ranges = [
            (start, min(start + self.chunk_cues, len(source_cues)))
            for start in range(0, len(source_cues), self.chunk_cues)
        ]
        if not ranges:
            raise WorkflowError("source has no subtitle cues")
        if chunk_range is not None:
            chunk_start, requested_end = chunk_range
            chunk_end = len(ranges) if requested_end is None else requested_end
            if chunk_start < 1 or chunk_end < chunk_start or chunk_end > len(ranges):
                raise WorkflowError(
                    f"chunk range {chunk_start}-{chunk_end} is outside 1-{len(ranges)}"
                )
        else:
            chunk_start = 1
            chunk_end = len(ranges)
        selected_ranges = list(enumerate(ranges, start=1))[chunk_start - 1 : chunk_end]
        ensure_episode_enrichment(
            self.client, self.state_dir, self.video, source,
            log_path=self.log_path, profile=self.profile,
        )
        update_progress(self.state_dir, self.video, profile=self.profile, status="translating")
        all_records: list[TranslationCue] = []
        for chunk_index, (core_start, core_end) in selected_ranges:
            targets = source_cues[core_start:core_end]
            window_start = max(0, core_start - CONTEXT_CUES)
            window_end = min(len(source_cues), core_end + CONTEXT_CUES)
            # Persist base translations independently of glossary edits and
            # saved corrections. Merge corrections only into this run's output.
            cache = cache_dir / f"chunk-{chunk_index:03d}.jsonl"
            cache_candidates = []
            for directory in (cache_dir, legacy_cache_dir):
                stable = directory / cache.name
                if stable.is_file():
                    cache_candidates.append(stable)
                cache_candidates.extend(sorted(
                    directory.glob(f"{cache.stem}.g*.jsonl"),
                    key=lambda path: (path.stat().st_mtime_ns, path.name),
                    reverse=True,
                ))
            records: list[TranslationCue] | None = None
            for cached_path in cache_candidates:
                try:
                    _, cached = parse_translation_document(
                        cached_path.read_text(encoding="utf-8"),
                        expected_fingerprint=source.fingerprint,
                        profile=self.profile,
                    )
                    records = validate_iris_cues(
                        targets,
                        [_translation_as_iris(cue) for cue in cached],
                        retry=True,
                        profile=self.profile,
                    )
                except (OSError, UnicodeError, WorkflowError):
                    append_log(
                        self.log_path,
                        f"CHUNK CACHE INVALID: {chunk_index}/{len(ranges)} file={cached_path.name}",
                    )
                    continue
                if cached_path != cache:
                    # Promote only validated, unpatched legacy records.
                    atomic_write(
                        cache,
                        serialize_translation_document(source.fingerprint, records, self.profile),
                    )
                append_log(
                    self.log_path,
                    f"CHUNK CACHE HIT: {chunk_index}/{len(ranges)} ruleset={RULESET_VERSION}",
                )
                break
            if records is None:
                records, candidates = self._translate_chunk(
                    source_cues,
                    core_start=core_start,
                    core_end=core_end,
                    chunk_index=chunk_index,
                    chunk_total=len(ranges),
                )
                request_id = f"atlas-c{chunk_index:03d}"
                job = enqueue_curation(
                    self.state_dir,
                    self.video,
                    source.fingerprint,
                    candidates,
                    request_id=request_id,
                    profile=self.profile,
                )
                # Persist validated Iris work before Atlas. Terminology failure must
                # never force the translation model to repeat this chunk.
                atomic_write(
                    cache,
                    serialize_translation_document(
                        source.fingerprint,
                        records,
                        self.profile,
                    ),
                )
                append_log(
                    self.log_path,
                    f"CHUNK VALIDATED: {chunk_index}/{len(ranges)} "
                    f"core={core_start + 1}-{core_end} window={window_start + 1}-{window_end}",
                )
                all_records.extend(corrected.get(record.id, record) for record in records)
                if progress_callback:
                    progress_callback(chunk_index, len(ranges))
                if job is not None:
                    retry_pending(
                        self.client,
                        self.state_dir,
                        self.video,
                        source.fingerprint,
                        log_path=self.log_path,
                        request_id=request_id,
                        profile=self.profile,
                    )
            else:
                all_records.extend(corrected.get(record.id, record) for record in records)
                if progress_callback:
                    progress_callback(chunk_index, len(ranges))
        expected_first_id = selected_ranges[0][1][0] + 1
        expected_last_id = selected_ranges[-1][1][1]
        if [record.id for record in all_records] != list(
            range(expected_first_id, expected_last_id + 1)
        ):
            raise WorkflowError("combined translation records are incomplete or out of order")
        return TranslationRun(
            records=tuple(all_records),
            chunk_start=chunk_start,
            chunk_end=chunk_end,
            chunks_total=len(ranges),
        )


def parse_retry_selector(selector: str, source_cues: list[SourceCue]) -> list[SourceCue]:
    ids: set[int] = set()
    for raw in selector.split(","):
        value = raw.strip()
        if not value:
            continue
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", value)
        if not match:
            raise WorkflowError(f"invalid retry selector: {value}")
        start, end = int(match.group(1)), int(match.group(2) or match.group(1))
        if start < 1 or end < start:
            raise WorkflowError(f"invalid retry range: {value}")
        ids.update(range(start, end + 1))
    selected = [cue for cue in source_cues if cue.id in ids]
    found = {cue.id for cue in selected}
    if found != ids:
        raise WorkflowError(
            "retry targets not found: " + ", ".join(map(str, sorted(ids - found)))
        )
    return selected


def save_retry_patches(
    state_dir: Path,
    video: Path,
    *,
    fingerprint: str,
    records: list[TranslationCue],
    reason: str,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> Path:
    document = _patch_document(state_dir, video, fingerprint, profile)
    existing = {
        int(item["id"]): item
        for item in document.get("patches", [])
        if isinstance(item, dict) and isinstance(item.get("id"), int)
    }
    for record in records:
        existing[record.id] = {
            "id": record.id,
            "text": record.text,
            "drop": record.drop,
            "additions": [addition.to_dict() for addition in record.additions],
            "skip_checks": list(record.skip_checks),
            "reason": reason,
        }
    document.update(
        {
            "version": 2,
            "ruleset_version": RULESET_VERSION,
            "source_fingerprint": fingerprint,
            "profile": profile.id,
            "patches": [existing[cue_id] for cue_id in sorted(existing)],
        }
    )
    path = retry_path(state_dir, video, profile)
    write_json(path, document)
    return path


def manual_retry(
    source: SourceDocument,
    *,
    selector: str,
    reason: str,
    state_dir: Path,
    video: Path,
    client: CodexClient,
    log_path: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> Path:
    save_source_index(state_dir, video, source)
    source_cues = list(source.cues)
    targets = parse_retry_selector(selector, source_cues)
    ensure_episode_enrichment(
        client, state_dir, video, source, log_path=log_path, profile=profile,
    )
    positions = _context_positions([cue.position for cue in targets], len(source_cues))
    window = [source_cues[position] for position in positions]
    target_ids = {cue.id for cue in targets}
    source_window = source_window_jsonl(window, target_ids)
    previous: str | None = None
    last_error: WorkflowError | None = None
    validated: list[TranslationCue] | None = None
    candidates: list[GlossaryCandidate] = []
    for attempt in range(1, MAX_TRANSLATION_ATTEMPTS + 1):
        prompt = iris_local_retry_prompt(
            source_window,
            glossary_context(state_dir, video, profile),
            reason=reason if attempt == 1 else f"{reason}; prior retry failed: {last_error}",
            previous=previous,
            profile=profile,
        )
        response = client.translate(prompt, request_id=f"iris-manual-a{attempt:02d}")
        previous = _iris_json(list(response.cues))
        _candidate_ids_are_targets(list(response.glossary_candidates), target_ids)
        record_feedback(
            state_dir,
            video,
            list(response.glossary_candidates),
            chunk=1,
            chunks_total=1,
            attempt=attempt,
            request_kind="manual-retry",
            profile=profile,
        )
        candidates.extend(response.glossary_candidates)
        try:
            validated = validate_iris_cues(
                targets,
                response.cues,
                retry=True,
                profile=profile,
            )
            break
        except WorkflowError as exc:
            last_error = exc
    if validated is None:
        raise WorkflowError(
            f"manual retry failed after {MAX_TRANSLATION_ATTEMPTS} attempts: {last_error}"
        )
    path = save_retry_patches(
        state_dir,
        video,
        fingerprint=source.fingerprint,
        records=validated,
        reason=reason,
        profile=profile,
    )
    job = enqueue_curation(
        state_dir,
        video,
        source.fingerprint,
        candidates,
        request_id="atlas-manual",
        profile=profile,
    )
    if job is not None:
        retry_pending(
            client,
            state_dir,
            video,
            source.fingerprint,
            log_path=log_path,
            request_id="atlas-manual",
            profile=profile,
        )
    update_progress(
        state_dir,
        video,
        profile=profile,
        status="retry_ready",
        source_fingerprint=source.fingerprint,
        retry_patches=str(path),
        retry_patches_saved=len(validated),
    )
    return path
