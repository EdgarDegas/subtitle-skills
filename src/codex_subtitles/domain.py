from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class SourceCue:
    """One immutable cue in the canonical episode source."""

    id: int
    source_number: str
    timestamp: str
    text: str

    @property
    def position(self) -> int:
        return self.id - 1

    def prompt_record(self, role: Literal["target", "context"]) -> dict[str, object]:
        return {"id": self.id, "role": role, "text": self.text}

    def to_dict(self) -> dict[str, object]:
        return {
            "type": "cue",
            "id": self.id,
            "source_number": self.source_number,
            "timestamp": self.timestamp,
            "text": self.text,
        }


@dataclass(frozen=True)
class SourceDocument:
    fingerprint: str
    cues: tuple[SourceCue, ...]


@dataclass(frozen=True)
class Addition:
    """A derived subtitle attached to, but distinct from, a source cue."""

    kind: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "text": self.text}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Addition":
        return cls(kind=str(value.get("kind") or ""), text=str(value.get("text") or ""))


@dataclass(frozen=True)
class IrisCue:
    id: int
    text: str
    drop: bool
    additions: tuple[Addition, ...] = ()
    skip_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class TranslationCue:
    id: int
    source_number: str
    timestamp: str
    source_text: str
    text: str
    drop: bool
    additions: tuple[Addition, ...] = ()
    skip_checks: tuple[str, ...] = ()

    @property
    def position(self) -> int:
        return self.id - 1

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["type"] = "cue"
        value["additions"] = [addition.to_dict() for addition in self.additions]
        value["skip_checks"] = list(self.skip_checks)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TranslationCue":
        raw_additions = value.get("additions", [])
        if not isinstance(raw_additions, list) or not all(
            isinstance(item, dict) for item in raw_additions
        ):
            raise TypeError("translation cue additions must be an array of objects")
        raw_skips = value.get("skip_checks", [])
        if not isinstance(raw_skips, list):
            raise TypeError("translation cue skip_checks must be an array")
        cue_id = value.get("id")
        if not isinstance(cue_id, int) or isinstance(cue_id, bool) or cue_id < 1:
            raise TypeError("translation cue id must be a positive integer")
        return cls(
            id=cue_id,
            source_number=str(value["source_number"]),
            timestamp=str(value["timestamp"]),
            source_text=str(value["source_text"]),
            text=str(value["text"]),
            drop=bool(value["drop"]),
            additions=tuple(Addition.from_dict(item) for item in raw_additions),
            skip_checks=tuple(str(item) for item in raw_skips),
        )


@dataclass(frozen=True)
class GlossaryCandidate:
    category: str
    source: str
    aliases: tuple[str, ...]
    target: str
    notes: str
    cue_ids: tuple[int, ...]


@dataclass(frozen=True)
class GlossaryEntry:
    category: str
    id: str
    aliases: tuple[str, ...]
    target: str
    notes: str = ""
    scope: str = "按需"

    @property
    def key(self) -> str:
        return f"{self.category}/{self.id}"


@dataclass(frozen=True)
class IrisResponse:
    cues: tuple[IrisCue, ...]
    glossary_candidates: tuple[GlossaryCandidate, ...] = ()


@dataclass
class ChunkResult:
    cues: list[TranslationCue]
    candidates: list[GlossaryCandidate] = field(default_factory=list)


@dataclass(frozen=True)
class RenderMapEntry:
    output_number: int
    source_id: int
    role: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class RenderResult:
    document: str
    dropped_cues: int
    addition_counts: dict[str, int]
    appeals: tuple[dict[str, object], ...]
    mapping: tuple[RenderMapEntry, ...]


@dataclass(frozen=True)
class TranslationRun:
    records: tuple[TranslationCue, ...]
    chunk_start: int
    chunk_end: int
    chunks_total: int

    @property
    def chunks_completed(self) -> int:
        return self.chunk_end - self.chunk_start + 1

    @property
    def complete(self) -> bool:
        return self.chunk_start == 1 and self.chunk_end == self.chunks_total


@dataclass(frozen=True)
class SubtitleTrack:
    index: int
    codec: str
    language: str
    title: str
    forced: bool
    hearing_impaired: bool

    @property
    def description(self) -> str:
        details = [f"embedded stream {self.index}", self.codec]
        if self.language:
            details.append(self.language)
        if self.title:
            details.append(self.title)
        return " | ".join(details)
