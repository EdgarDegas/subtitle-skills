"""Durable Atlas jobs. The agent edits Markdown; Python never applies term actions."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from difflib import unified_diff
from pathlib import Path

from .codex_client import CodexClient, append_log
from .config import (
    CURATION_VERSION,
    CURATOR_MODEL,
    CURATOR_NAME,
    RULESET_VERSION,
)
from .domain import GlossaryCandidate, SourceDocument
from .errors import WorkflowError
from .glossary import (
    ensure_glossary,
    updates_path,
    validate_glossary_edit,
)
from .language_profiles import DEFAULT_PROFILE, LanguageProfile
from .prompts import atlas_enrichment_prompt, atlas_prompt
from .workspace import append_jsonl, atomic_write, read_json, title_dir, update_progress, write_json


@contextmanager
def glossary_lock(
    state_dir: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
):
    """OS-owned lock: serialize editors and release even if the worker exits."""
    suffix = "" if profile.id == DEFAULT_PROFILE.id else f".{profile.id}"
    path = title_dir(state_dir) / f".glossary{suffix}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def jobs_for_episode(
    state_dir: Path,
    video: Path,
    fingerprint: str,
    *,
    request_id: str | None = None,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> list[tuple[Path, dict[str, object]]]:
    jobs = []
    for path in sorted((state_dir / "glossary-jobs" / video.stem).glob("*.json")):
        job = read_json(path)
        if job.get("source_fingerprint") != fingerprint or job.get("ruleset") != RULESET_VERSION:
            continue
        if str(job.get("profile") or DEFAULT_PROFILE.id) != profile.id:
            continue
        if request_id is None or job.get("request_id") == request_id:
            jobs.append((path, job))
    return jobs


def curation_status(
    state_dir: Path,
    video: Path,
    fingerprint: str,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> dict[str, object]:
    jobs = jobs_for_episode(state_dir, video, fingerprint, profile=profile)
    pending = [job for _, job in jobs if job.get("status") != "complete"]
    enrichment = [job for _, job in jobs if job.get("kind") == "episode-enrichment"]
    return {
        "enrichment_status": (
            "pending" if any(job.get("status") != "complete" for job in enrichment)
            else "complete" if enrichment else "none"
        ),
        "glossary_mode": "direct-edit",
        "glossary_profile": profile.id,
        "glossary_version": CURATION_VERSION,
        "glossary_status": "pending" if pending else ("complete" if jobs else "none"),
        "glossary_pending": len(pending),
        "glossary_last_error": next(
            (job.get("last_error") for job in reversed(pending) if job.get("last_error")), None
        ),
    }


def enqueue_curation(
    state_dir: Path,
    video: Path,
    fingerprint: str,
    candidates: list[GlossaryCandidate],
    *,
    request_id: str,
    profile: LanguageProfile = DEFAULT_PROFILE,
    episode: dict[str, object] | None = None,
) -> Path | None:
    if not candidates and episode is None:
        return None
    # Job identity belongs to the episode/request, not to individual cue IDs.
    payload = [asdict(candidate) for candidate in candidates]
    identity = json.dumps(
        [fingerprint, RULESET_VERSION, profile.id, request_id, payload, episode],
        ensure_ascii=False, sort_keys=True,
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    path = state_dir / "glossary-jobs" / video.stem / f"{digest}.json"
    if not path.exists():
        write_json(path, {
            "version": CURATION_VERSION,
            "video": video.name,
            "video_path": str(video),
            "source_fingerprint": fingerprint,
            "ruleset": RULESET_VERSION,
            "profile": profile.id,
            "request_id": request_id,
            "candidates": payload,
            **({"kind": "episode-enrichment", "episode": episode} if episode is not None else {}),
            "status": "pending",
            "attempts": 0,
            "last_error": None,
        })
    return path


def ensure_episode_enrichment(
    client: CodexClient,
    state_dir: Path,
    video: Path,
    source: SourceDocument,
    *,
    log_path: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> None:
    """Run Atlas before Iris, reusing the existing durable curation jobs."""
    path = enqueue_curation(
        state_dir, video, source.fingerprint, [],
        request_id="atlas-enrichment", profile=profile,
        episode={
            "title": title_dir(state_dir).name,
            "video": video.name,
            "source_cues": [{"id": cue.id, "text": cue.text} for cue in source.cues],
        },
    )
    assert path is not None
    if read_json(path).get("status") == "complete":
        return
    update_progress(state_dir, video, profile=profile, status="enriching")
    retry_pending(
        client, state_dir, video, source.fingerprint, log_path=log_path,
        request_id="atlas-enrichment", profile=profile,
    )
    job = read_json(path)
    if job.get("status") != "complete":
        raise WorkflowError(
            "Atlas episode enrichment is pending; Iris has not started. "
            + str(job.get("last_error") or "another Atlas editor holds the title lock")
        )


def _edit_job(
    client: CodexClient,
    state_dir: Path,
    path: Path,
    *,
    log_path: Path,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> None:
    job = read_json(path)
    if job.get("status") == "complete":
        return
    glossary = ensure_glossary(state_dir, profile)
    attempt = int(job.get("attempts") or 0) + 1
    backup = path.with_suffix(f".a{attempt:02d}.before.md")
    before = glossary.read_text(encoding="utf-8")
    atomic_write(backup, before)
    reason = job.get("last_error")
    if job.get("status") == "running":
        reason = "The previous Atlas call was interrupted. Check for already applied edits."
    job.update(status="running", attempts=attempt, backup=str(backup))
    write_json(path, job)
    summary = ""
    interrupted = False
    after = before
    try:
        if job.get("kind") == "episode-enrichment":
            prompt = atlas_enrichment_prompt(
                str(glossary.resolve()), job["episode"],
                retry_reason=str(reason) if reason else None,
                profile=profile,
            )
            edit = client.enrich_glossary
        else:
            candidates = []
            for item in job["candidates"]:
                candidate = dict(item)
                candidate.setdefault("target", candidate.get(profile.glossary_value_key, ""))
                candidates.append(candidate)
            prompt = atlas_prompt(
                str(glossary.resolve()), candidates,
                retry_reason=str(reason) if reason else None, profile=profile,
            )
            edit = client.edit_glossary
        summary = edit(
            prompt,
            glossary=glossary,
            request_id=f"{job['request_id']}-{path.stem}-a{attempt:02d}",
        )
        after = glossary.read_text(encoding="utf-8")
        validate_glossary_edit(
            before, after, profile,
            allow_merge=job.get("kind") == "episode-enrichment",
        )
        job.update(status="complete", last_error=None, summary=summary)
    except (OSError, UnicodeError, WorkflowError, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        # Recovery restores the exact previous document, not generated term actions.
        if glossary.exists():
            after = glossary.read_text(encoding="utf-8", errors="replace")
        else:
            after = ""
        if after != before:
            atomic_write(glossary, before)
        job.update(status="pending", last_error="interrupted" if interrupted else str(exc))
        append_log(log_path, f"GLOSSARY PENDING: {path.name}: {job['last_error']}")
    job["updated_at"] = datetime.now().isoformat(timespec="seconds")
    job["before_sha256"] = hashlib.sha256(before.encode("utf-8")).hexdigest()
    job["after_sha256"] = hashlib.sha256(after.encode("utf-8")).hexdigest()
    write_json(path, job)
    append_jsonl(updates_path(state_dir, profile), [{
        "version": CURATION_VERSION,
        "role": CURATOR_NAME,
        "model": CURATOR_MODEL,
        "mode": "direct-edit",
        "profile": profile.id,
        "job": str(path),
        "video": job["video"],
        "request_id": job["request_id"],
        "attempt": attempt,
        "status": job["status"],
        "summary": summary,
        "last_error": job.get("last_error"),
        "backup": str(backup),
        "before_sha256": job["before_sha256"],
        "after_sha256": job["after_sha256"],
        "diff": "".join(unified_diff(before.splitlines(True), after.splitlines(True), fromfile="before/glossary.md", tofile="after/glossary.md")),
    }])
    if job["status"] == "complete":
        append_log(log_path, f"GLOSSARY EDIT COMPLETE: {path.name}")
    if interrupted:
        raise KeyboardInterrupt


def retry_pending(
    client: CodexClient,
    state_dir: Path,
    video: Path,
    fingerprint: str,
    *,
    log_path: Path,
    request_id: str | None = None,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> dict[str, object]:
    """Try each matching job once, independently of Iris and translation caches."""
    pending = [
        path
        for path, job in jobs_for_episode(
            state_dir,
            video,
            fingerprint,
            request_id=request_id,
            profile=profile,
        )
        if job.get("status") != "complete"
    ]
    try:
        if pending:
            with glossary_lock(state_dir, profile):
                for path in pending:
                    _edit_job(
                        client,
                        state_dir,
                        path,
                        log_path=log_path,
                        profile=profile,
                    )
    except BlockingIOError:
        append_log(log_path, "GLOSSARY PENDING: another Atlas editor holds the title lock")
    finally:
        status = curation_status(state_dir, video, fingerprint, profile)
        update_progress(state_dir, video, profile=profile, **status)
    return status
