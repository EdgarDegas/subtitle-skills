"""List and extract embedded subtitles using the shared media implementation."""
from __future__ import annotations

import re
from pathlib import Path

from .domain import SubtitleTrack
from .errors import WorkflowError
from .media import TEXT_CODECS, demux_tracks, probe_tracks
from .runtime import MediaTools


def list_tracks(video: Path, tools: MediaTools) -> list[SubtitleTrack]:
    tracks = probe_tracks(video, tools)
    print(f"Embedded subtitle tracks in {video.name}:")
    for track in tracks:
        kind = "text" if track.codec.casefold() in TEXT_CODECS else "bitmap/unsupported"
        print(f"  {track.description} | {kind}")
    if not tracks:
        print("  No embedded subtitles")
    return tracks


def _component(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^\w.+@()\[\] -]+", "-", value, flags=re.UNICODE)
    return re.sub(r"\s+", "-", cleaned).strip(" .-_") or fallback


def extract_tracks(
    video: Path, tracks: list[SubtitleTrack], output_dir: Path, tools: MediaTools,
    *, requested: list[int] | None = None, overwrite: bool = False,
) -> list[Path]:
    requested_set = set(requested or [])
    unknown = requested_set - {track.index for track in tracks}
    if unknown:
        raise WorkflowError(f"subtitle stream indexes not found: {sorted(unknown)}")
    outputs = []
    for track in tracks:
        if requested_set and track.index not in requested_set:
            continue
        if track.codec.casefold() not in TEXT_CODECS:
            print(f"SKIP stream {track.index}: bitmap/unsupported ({track.codec}); no OCR")
            continue
        language = _component(track.language, "und")
        title = "." + _component(track.title, "untitled") if track.title else ""
        filename = f"{video.stem}.stream-{track.index}.{language}{title}.srt"
        outputs.append((track.index, output_dir / filename))
    if not outputs:
        if not tracks and not requested_set:
            return []
        raise WorkflowError("no selected text subtitle tracks can be converted to SRT")
    demux_tracks(video, outputs, tools, overwrite=overwrite)
    return [destination for _, destination in outputs]
