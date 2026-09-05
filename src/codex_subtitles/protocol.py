from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable

from .config import (
    RULESET_VERSION,
    SKIPPABLE_CHECKS,
    SUPPORTED_ADDITION_KINDS,
    TOP_POSITION_TAG,
)
from .domain import Addition, IrisCue, SourceCue, SourceDocument, TranslationCue
from .errors import ValidationIssue, WorkflowError
from .language_profiles import DEFAULT_PROFILE, LanguageProfile
from .srt import normalize_text, parse_srt


SPEAKER_LABEL = re.compile(r"^(?:[-–—]\s*)?[A-Z][A-Z0-9 .&'’\-]{0,36}(?::|：)\s*")
def _canonical_source(cues: Iterable[SourceCue]) -> str:
    return "\n".join(
        json.dumps(
            {"timestamp": cue.timestamp, "text": cue.text},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for cue in cues
    )


def build_source_document(document: str) -> SourceDocument:
    parsed = parse_srt(document)
    cues = tuple(
        SourceCue(
            id=cue_id,
            source_number=cue.number,
            timestamp=cue.timestamp,
            text=cue.text,
        )
        for cue_id, cue in enumerate(parsed, start=1)
    )
    fingerprint = hashlib.sha256(_canonical_source(cues).encode("utf-8")).hexdigest()
    return SourceDocument(fingerprint=fingerprint, cues=cues)


def source_fingerprint(document: str) -> str:
    return build_source_document(document).fingerprint


def serialize_source_document(source: SourceDocument) -> str:
    header = {
        "type": "source",
        "schema_version": 1,
        "source_fingerprint": source.fingerprint,
        "cue_count": len(source.cues),
    }
    values: list[dict[str, object]] = [header]
    values.extend(cue.to_dict() for cue in source.cues)
    return "".join(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        for value in values
    )


def parse_source_document(document: str) -> SourceDocument:
    values = _parse_jsonl(document, "source index")
    if not values or values[0].get("type") != "source":
        raise WorkflowError("source index has no source header")
    header = values[0]
    cues: list[SourceCue] = []
    for value in values[1:]:
        cue_id = value.get("id")
        if value.get("type") != "cue" or not _is_positive_int(cue_id):
            raise WorkflowError("source index contains an invalid cue record")
        cues.append(
            SourceCue(
                id=cue_id,
                source_number=str(value.get("source_number") or ""),
                timestamp=str(value.get("timestamp") or ""),
                text=str(value.get("text") or ""),
            )
        )
    expected_ids = list(range(1, len(cues) + 1))
    if [cue.id for cue in cues] != expected_ids:
        raise WorkflowError("source index cue IDs are not contiguous episode ordinals")
    fingerprint = hashlib.sha256(_canonical_source(cues).encode("utf-8")).hexdigest()
    if header.get("source_fingerprint") != fingerprint:
        raise WorkflowError("source index fingerprint does not match its cue content")
    if header.get("cue_count") != len(cues):
        raise WorkflowError("source index cue count does not match its header")
    return SourceDocument(fingerprint=fingerprint, cues=tuple(cues))


def source_window_jsonl(window: Iterable[SourceCue], target_ids: set[int]) -> str:
    return "\n".join(
        json.dumps(
            cue.prompt_record("target" if cue.id in target_ids else "context"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for cue in window
    )


def _validate_additions(
    source: SourceCue,
    additions: tuple[Addition, ...],
    profile: LanguageProfile,
) -> tuple[Addition, ...]:
    seen: set[str] = set()
    validated: list[Addition] = []
    for addition in additions:
        kind = addition.kind.strip()
        if kind not in SUPPORTED_ADDITION_KINDS:
            raise ValidationIssue(
                "addition",
                f"cue {source.id} has unsupported addition kind: {kind or '(empty)'}",
                cue_ids=(source.id,),
                skippable=False,
            )
        if kind in seen:
            raise ValidationIssue(
                "addition",
                f"cue {source.id} has duplicate addition kind: {kind}",
                cue_ids=(source.id,),
                skippable=False,
            )
        seen.add(kind)
        text = normalize_text(addition.text, profile)
        if not text or "\n" in text or len(text) > profile.pun_note_max_chars:
            raise ValidationIssue(
                "addition",
                f"{kind} for cue {source.id} must be one non-empty line and at most "
                f"{profile.pun_note_max_chars} characters",
                cue_ids=(source.id,),
                skippable=False,
            )
        if kind == "pun_note":
            if source.text.lstrip().startswith(TOP_POSITION_TAG):
                raise ValidationIssue(
                    "addition",
                    f"top-positioned cue {source.id} cannot also carry a pun_note",
                    cue_ids=(source.id,),
                    skippable=False,
                )
        validated.append(Addition(kind=kind, text=text))
    return tuple(validated)


def _validate_one(
    source: SourceCue,
    candidate: IrisCue,
    *,
    retry: bool,
    profile: LanguageProfile,
) -> TranslationCue:
    skipped = frozenset(check.strip().lower() for check in candidate.skip_checks if check.strip())
    if skipped and not retry:
        raise ValidationIssue(
            "appeal permission",
            f"skip_checks are allowed only in retry responses (cue {source.id})",
            cue_ids=(source.id,),
            skippable=False,
        )
    unknown = skipped - SKIPPABLE_CHECKS
    if unknown:
        raise ValidationIssue(
            "appeal format",
            f"unknown skip_checks for cue {source.id}: {', '.join(sorted(unknown))}",
            cue_ids=(source.id,),
            skippable=False,
        )
    if candidate.drop:
        if candidate.text.strip() or candidate.additions:
            raise ValidationIssue(
                "cue deletion",
                f"drop=true requires empty text and additions (cue {source.id})",
                cue_ids=(source.id,),
                skippable=False,
            )
        text = ""
        additions: tuple[Addition, ...] = ()
    else:
        if not candidate.text.strip():
            raise ValidationIssue(
                "structure",
                f"kept cue {source.id} has empty text",
                cue_ids=(source.id,),
                skippable=False,
            )
        text = normalize_text(candidate.text, profile)
        additions = _validate_additions(source, candidate.additions, profile)
        check_text = re.sub(r"^\{\\an8\}\s*", "", text.lstrip())
        if "speaker label" not in skipped:
            source_labeled = any(SPEAKER_LABEL.match(line.strip()) for line in source.text.splitlines())
            for line in check_text.splitlines():
                if SPEAKER_LABEL.match(line.strip()) or (
                    source_labeled and profile.translated_speaker_label(line.strip())
                ):
                    raise ValidationIssue(
                        "speaker label",
                        f"cue {source.id} retains a CC speaker label: {line.strip()[:48]}",
                        cue_ids=(source.id,),
                    )
        if source.text.lstrip().startswith(TOP_POSITION_TAG):
            text = TOP_POSITION_TAG + re.sub(r"^\{\\an8\}\s*", "", text.lstrip())
    return TranslationCue(
        id=source.id,
        source_number=source.source_number,
        timestamp=source.timestamp,
        source_text=source.text,
        text=text,
        drop=candidate.drop,
        additions=additions,
        skip_checks=tuple(sorted(skipped)),
    )


def validate_iris_cues(
    source_targets: list[SourceCue],
    candidates: Iterable[IrisCue],
    *,
    retry: bool,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> list[TranslationCue]:
    expected = {cue.id: cue for cue in source_targets}
    returned: dict[int, IrisCue] = {}
    for candidate in candidates:
        if candidate.id in returned:
            raise ValidationIssue(
                "cue id",
                f"Iris returned duplicate ID {candidate.id}",
                cue_ids=(candidate.id,),
                skippable=False,
            )
        if candidate.id not in expected:
            raise ValidationIssue(
                "cue id",
                f"Iris returned unknown or context-only ID {candidate.id}",
                cue_ids=(candidate.id,),
                skippable=False,
            )
        returned[candidate.id] = candidate
    missing = tuple(cue.id for cue in source_targets if cue.id not in returned)
    if missing:
        raise ValidationIssue(
            "cue id",
            "Iris omitted target ID(s): " + ", ".join(map(str, missing[:8])),
            cue_ids=missing,
            skippable=False,
        )

    validated: list[TranslationCue] = []
    failures: dict[str, list[ValidationIssue]] = defaultdict(list)
    for source in source_targets:
        try:
            validated.append(
                _validate_one(
                    source,
                    returned[source.id],
                    retry=retry,
                    profile=profile,
                )
            )
        except ValidationIssue as issue:
            failures[issue.check].append(issue)
    if failures:
        check, issues = next(iter(failures.items()))
        cue_ids = tuple(cue_id for issue in issues for cue_id in issue.cue_ids)
        raise ValidationIssue(
            check,
            f"{check} failed in {len(cue_ids)} cue(s): "
            + "; ".join(str(issue) for issue in issues),
            cue_ids=cue_ids,
            skippable=all(issue.skippable for issue in issues),
        )
    return validated


def serialize_translation_document(
    source_fingerprint: str,
    records: Iterable[TranslationCue],
    profile: LanguageProfile = DEFAULT_PROFILE,
    *,
    retry_fingerprint: str | None = None,
) -> str:
    values = list(records)
    header = {
        "type": "translation",
        "schema_version": 1,
        "ruleset_version": RULESET_VERSION,
        "profile": profile.id,
        "source_fingerprint": source_fingerprint,
        "cue_count": len(values),
    }
    if retry_fingerprint is not None:
        header["retry_fingerprint"] = retry_fingerprint
    documents: list[dict[str, object]] = [header]
    documents.extend(record.to_dict() for record in values)
    return "".join(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        for value in documents
    )


def parse_translation_document(
    document: str,
    *,
    expected_fingerprint: str | None = None,
    expected_retry_fingerprint: str | None = None,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> tuple[str, list[TranslationCue]]:
    values = _parse_jsonl(document, "translation records")
    if not values or values[0].get("type") != "translation":
        raise WorkflowError("translation records have no document header")
    header = values[0]
    fingerprint = str(header.get("source_fingerprint") or "")
    if header.get("ruleset_version") != RULESET_VERSION:
        raise WorkflowError("translation records use a stale ruleset")
    stored_profile = str(header.get("profile") or DEFAULT_PROFILE.id)
    if stored_profile != profile.id:
        raise WorkflowError(
            f"translation records use profile {stored_profile}, expected {profile.id}"
        )
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise WorkflowError("translation records use a different source fingerprint")
    # Legacy records without corrections remain usable. Legacy records with
    # patches must be assembled once to establish which corrections they contain.
    if (
        expected_retry_fingerprint is not None
        and header.get("retry_fingerprint", "") != expected_retry_fingerprint
    ):
        raise WorkflowError(
            "saved corrections have changed; rebuild with translate --stage-only "
            "--overwrite without --chunks, using the original --chunk-cues value"
        )
    try:
        records = [TranslationCue.from_dict(value) for value in values[1:]]
    except (KeyError, TypeError, ValueError) as exc:
        raise WorkflowError(f"invalid translation cue record: {exc}") from exc
    if header.get("cue_count") != len(records):
        raise WorkflowError("translation record count does not match its header")
    if len({record.id for record in records}) != len(records):
        raise WorkflowError("translation records contain duplicate cue IDs")
    return fingerprint, records


def records_as_previous_json(records: Iterable[TranslationCue]) -> str:
    return json.dumps(
        [
            {
                "id": record.id,
                "text": record.text,
                "drop": record.drop,
                "additions": [addition.to_dict() for addition in record.additions],
                "skip_checks": list(record.skip_checks),
            }
            for record in records
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _parse_jsonl(document: str, label: str) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    for line_number, line in enumerate(document.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkflowError(f"invalid {label} JSONL at line {line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise WorkflowError(f"{label} JSONL line {line_number} is not an object")
        values.append(value)
    return values


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
