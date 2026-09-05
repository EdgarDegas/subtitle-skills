from __future__ import annotations

import html
import re
from dataclasses import dataclass

from .config import PUN_NOTE_EXTRA_MS, TOP_POSITION_TAG
from .domain import RenderMapEntry, RenderResult, TranslationCue
from .errors import ValidationIssue, WorkflowError
from .language_profiles import DEFAULT_PROFILE, LanguageProfile


TIMESTAMP_LINE = re.compile(
    r"^\d{2}:\d{2}:\d{2},\d{3}\s+-->\s+\d{2}:\d{2}:\d{2},\d{3}(?:\s+.*)?$"
)


@dataclass(frozen=True)
class SrtCue:
    number: str
    timestamp: str
    text: str


def parse_srt(document: str) -> list[SrtCue]:
    normalized = document.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.lstrip("\ufeff").strip()
    if not normalized:
        raise ValidationIssue("structure", "subtitle document is empty", skippable=False)
    result: list[SrtCue] = []
    for ordinal, block in enumerate(re.split(r"\n{2,}", normalized), start=1):
        lines = block.splitlines()
        if (
            len(lines) < 3
            or not lines[0].strip().isdigit()
            or not TIMESTAMP_LINE.fullmatch(lines[1].strip())
        ):
            raise ValidationIssue(
                "structure",
                f"invalid SRT cue near block {ordinal}",
                skippable=False,
            )
        text = "\n".join(lines[2:]).strip()
        if not text:
            raise ValidationIssue(
                "structure",
                f"empty SRT text near block {ordinal}",
                skippable=False,
            )
        result.append(SrtCue(lines[0].strip(), lines[1].strip(), text))
    return result


def format_srt(cues: list[SrtCue]) -> str:
    return "\n\n".join(
        f"{cue.number}\n{cue.timestamp}\n{cue.text}" for cue in cues
    ) + "\n"


def normalize_text(text: str, profile: LanguageProfile = DEFAULT_PROFILE) -> str:
    return profile.normalize_text(text)


def normalize_srt(
    document: str, profile: LanguageProfile = DEFAULT_PROFILE
) -> str:
    return format_srt(
        [
            SrtCue(cue.number, cue.timestamp, normalize_text(cue.text, profile))
            for cue in parse_srt(document)
        ]
    )


def clean_source_srt(document: str) -> str:
    cues: list[SrtCue] = []
    for cue in parse_srt(document):
        top_positioned = cue.text.lstrip().startswith(TOP_POSITION_TAG)
        text = re.sub(r"<[^>]+>|\{\\[^}]+\}", "", cue.text)
        text = html.unescape(text).strip()
        if not text:
            raise WorkflowError(f"empty cue after styling cleanup: {cue.number}")
        if top_positioned:
            text = TOP_POSITION_TAG + text
        cues.append(SrtCue(cue.number, cue.timestamp, text))
    return format_srt(cues)


def timestamp_to_milliseconds(value: str) -> int:
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value)
    if not match:
        raise WorkflowError(f"invalid SRT timestamp value: {value}")
    hours, minutes, seconds, milliseconds = map(int, match.groups())
    return (((hours * 60) + minutes) * 60 + seconds) * 1_000 + milliseconds


def milliseconds_to_timestamp(value: int) -> str:
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def extend_timestamp(timestamp: str, extra_ms: int = PUN_NOTE_EXTRA_MS) -> str:
    start, separator, end_and_settings = timestamp.partition(" --> ")
    if not separator:
        raise WorkflowError(f"invalid SRT timestamp line: {timestamp}")
    end_parts = end_and_settings.split(maxsplit=1)
    end = milliseconds_to_timestamp(timestamp_to_milliseconds(end_parts[0]) + extra_ms)
    settings = f" {end_parts[1]}" if len(end_parts) == 2 else ""
    return f"{start} --> {end}{settings}"


def shift_timestamp(timestamp: str, offset_ms: int) -> str:
    """Shift one final-output timestamp, clamping values before zero."""
    start, separator, end_and_settings = timestamp.partition(" --> ")
    if not separator:
        raise WorkflowError(f"invalid SRT timestamp line: {timestamp}")
    end_parts = end_and_settings.split(maxsplit=1)
    shifted_start = milliseconds_to_timestamp(
        max(0, timestamp_to_milliseconds(start) + offset_ms)
    )
    shifted_end = milliseconds_to_timestamp(
        max(0, timestamp_to_milliseconds(end_parts[0]) + offset_ms)
    )
    settings = f" {end_parts[1]}" if len(end_parts) == 2 else ""
    return f"{shifted_start} --> {shifted_end}{settings}"


def shift_srt_timing(document: str, offset_ms: int) -> str:
    """Apply an episode offset to a rendered SRT without changing its text or IDs."""
    if offset_ms == 0:
        return document
    shifted = [
        SrtCue(cue.number, shift_timestamp(cue.timestamp, offset_ms), cue.text)
        for cue in parse_srt(document)
    ]
    result = format_srt(shifted)
    parse_srt(result)
    return result


def render_translation(
    records: list[TranslationCue],
    *,
    first_source_id: int = 1,
) -> RenderResult:
    """Render contiguous source IDs; previews may start later in the episode."""
    ordered = sorted(records, key=lambda record: record.id)
    expected_ids = list(range(first_source_id, first_source_id + len(ordered)))
    if first_source_id < 1 or [record.id for record in ordered] != expected_ids:
        raise WorkflowError(
            f"translation record IDs are not contiguous from source ID {first_source_id}"
        )
    output: list[tuple[SrtCue, int, str]] = []
    dropped = 0
    addition_counts: dict[str, int] = {}
    appeals: list[dict[str, object]] = []
    for record in ordered:
        if record.skip_checks:
            appeals.append(
                {
                    "cue_id": record.id,
                    "source_number": record.source_number,
                    "checks": list(record.skip_checks),
                }
            )
        if record.drop:
            dropped += 1
            continue
        for addition in record.additions:
            if addition.kind != "pun_note":
                raise WorkflowError(f"unsupported render addition: {addition.kind}")
            addition_counts[addition.kind] = addition_counts.get(addition.kind, 0) + 1
            output.append(
                (
                    SrtCue(
                        "0",
                        extend_timestamp(record.timestamp),
                        TOP_POSITION_TAG + addition.text,
                    ),
                    record.id,
                    addition.kind,
                )
            )
        output.append((SrtCue("0", record.timestamp, record.text), record.id, "main"))
    if not output:
        raise WorkflowError("all subtitle cues were dropped")
    renumbered: list[SrtCue] = []
    mapping: list[RenderMapEntry] = []
    for output_number, (cue, source_id, role) in enumerate(output, start=1):
        renumbered.append(SrtCue(str(output_number), cue.timestamp, cue.text))
        mapping.append(RenderMapEntry(output_number, source_id, role))
    document = format_srt(renumbered)
    parse_srt(document)
    return RenderResult(
        document=document,
        dropped_cues=dropped,
        addition_counts=addition_counts,
        appeals=tuple(appeals),
        mapping=tuple(mapping),
    )
