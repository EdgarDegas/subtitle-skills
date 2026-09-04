from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageProfile(ABC):
    """Target-language behavior injected into the otherwise language-neutral workflow."""

    id: str
    language_name: str
    native_name: str
    output_tag: str
    glossary_column: str
    glossary_value_key: str
    default_scope: str
    global_scope: str
    existing_track_markers: tuple[str, ...]
    translation_instructions: str
    curator_instructions: str
    pun_note_max_chars: int

    def __post_init__(self) -> None:
        token = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
        if not token.fullmatch(self.id) or not token.fullmatch(self.output_tag):
            raise ValueError("profile id and output_tag must be lowercase ASCII tokens")
        if not self.glossary_column.strip() or not self.glossary_value_key.strip():
            raise ValueError("profile glossary fields must not be empty")
        if self.pun_note_max_chars < 1:
            raise ValueError("profile pun_note_max_chars must be positive")

    @abstractmethod
    def normalize_text(self, text: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def foreign_text_residue(self, text: str) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def translated_speaker_label(self, text: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def validate_pun_note(self, text: str) -> str | None:
        raise NotImplementedError

    def output_filename(self, video_stem: str) -> str:
        return f"{video_stem}.{self.output_tag}.srt"

    def preview_filename(self, video_stem: str, chunk_start: int, chunk_end: int) -> str:
        return (
            f"{video_stem}.chunks-{chunk_start:03d}-{chunk_end:03d}."
            f"preview.{self.output_tag}.srt"
        )

    def is_explicit_target_track(self, identity: str) -> bool:
        folded = identity.casefold()
        return any(marker in folded for marker in self.existing_track_markers)
