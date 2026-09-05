from __future__ import annotations

from pathlib import Path


RULESET_VERSION = "2026-09-05.1"
TRANSLATOR_NAME = "Iris"
TRANSLATOR_MODEL = "gpt-5.6-luna"
CURATOR_NAME = "Atlas"
CURATOR_MODEL = "gpt-5.6-terra"
CURATION_VERSION = "2026-09-01.1"

DEFAULT_CHUNK_CUES = 50
CONTEXT_CUES = 10
MAX_TRANSLATION_ATTEMPTS = 3
PUN_NOTE_EXTRA_MS = 1_500
TOP_POSITION_TAG = r"{\an8}"
SUPPORTED_ADDITION_KINDS = frozenset({"pun_note"})

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parents[1]
SKILL_DIR = PROJECT_DIR
TRANSLATION_SCHEMA = PACKAGE_DIR / "schemas" / "translation_output.schema.json"

VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})
SUBTITLE_EXTENSIONS = frozenset({".ass", ".srt", ".ssa", ".vtt"})

SKIPPABLE_CHECKS = frozenset({"speaker label"})
PROTOCOL_CHECKS = frozenset(
    {
        "appeal format",
        "appeal permission",
        "cue deletion",
        "cue id",
        "addition",
        "structure",
    }
)
