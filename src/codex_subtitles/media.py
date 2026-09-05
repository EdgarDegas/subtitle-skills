from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .codex_client import append_log
from .config import SUBTITLE_EXTENSIONS, VIDEO_EXTENSIONS
from .domain import SubtitleTrack
from .errors import WorkflowError
from .language_profiles import DEFAULT_PROFILE, LanguageProfile
from .srt import parse_srt
from .runtime import MediaTools


TEXT_CODECS = frozenset(
    {
        "ass",
        "eia_608",
        "eia_708",
        "jacosub",
        "microdvd",
        "mov_text",
        "mpl2",
        "pjs",
        "realtext",
        "sami",
        "srt",
        "ssa",
        "stl",
        "subrip",
        "subviewer",
        "subviewer1",
        "text",
        "vplayer",
        "webvtt",
    }
)
ENGLISH = frozenset({"en", "eng", "english", "en-us", "en-gb"})
def discover_videos(
    inputs: list[Path], *, recursive: bool, offline_workspace: Path | None = None,
) -> list[Path]:
    videos: list[Path] = []
    for raw in inputs:
        path = raw.expanduser().resolve()
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS:
            videos.append(path)
        elif path.is_dir():
            iterator = path.rglob("*") if recursive else path.iterdir()
            videos.extend(
                candidate.resolve()
                for candidate in iterator
                if candidate.is_file() and candidate.suffix.lower() in VIDEO_EXTENSIONS
            )
        elif offline_workspace is not None and path.suffix.lower() in VIDEO_EXTENSIONS:
            from .workspace import collection_dir, source_dir

            cached_source(path, source_dir(collection_dir(path, offline_workspace)))
            videos.append(path)
        else:
            raise WorkflowError(f"unsupported or missing video input: {raw}")
    return sorted(set(videos), key=lambda path: str(path).casefold())


def destination_path(
    video: Path, profile: LanguageProfile = DEFAULT_PROFILE
) -> Path:
    return video.with_name(profile.output_filename(video.stem))


def probe_tracks(video: Path, tools: MediaTools) -> list[SubtitleTrack]:
    process = subprocess.run(
        [
            str(tools.require("ffprobe")),
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            "stream=index,codec_name:stream_tags=language,title:stream_disposition=forced,hearing_impaired",
            "-of",
            "json",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode:
        raise WorkflowError(f"ffprobe failed for {video.name}: {process.stderr.strip()}")
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"ffprobe returned invalid JSON for {video.name}") from exc
    tracks: list[SubtitleTrack] = []
    for stream in value.get("streams", []):
        if not isinstance(stream, dict):
            continue
        tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
        disposition = (
            stream.get("disposition")
            if isinstance(stream.get("disposition"), dict)
            else {}
        )
        tracks.append(
            SubtitleTrack(
                index=int(stream.get("index", -1)),
                codec=str(stream.get("codec_name") or ""),
                language=str(tags.get("language") or "").strip(),
                title=str(tags.get("title") or "").strip(),
                forced=bool(disposition.get("forced")),
                hearing_impaired=bool(disposition.get("hearing_impaired")),
            )
        )
    return tracks


def _english(track: SubtitleTrack) -> bool:
    return track.language.casefold() in ENGLISH or "english" in track.title.casefold()


def _sdh(track: SubtitleTrack) -> bool:
    value = f"{track.language} {track.title}".casefold()
    return track.hearing_impaired or any(term in value for term in ("sdh", "cc", "caption"))


def _text(track: SubtitleTrack) -> bool:
    return track.codec.casefold() in TEXT_CODECS


def choose_track(tracks: list[SubtitleTrack], requested: int | None = None) -> SubtitleTrack:
    if requested is not None:
        for track in tracks:
            if track.index == requested and _text(track):
                return track
        raise WorkflowError(f"embedded text subtitle stream not found: {requested}")
    candidates = [track for track in tracks if _text(track)]
    if not candidates:
        raise WorkflowError("no embedded text subtitle track")
    return max(
        candidates,
        key=lambda track: (
            _english(track) and not track.forced and _sdh(track),
            _english(track) and not track.forced,
            _english(track),
            not track.forced,
            -track.index,
        ),
    )


def _matching_sidecars(
    video: Path, profile: LanguageProfile = DEFAULT_PROFILE
) -> list[Path]:
    candidates = []
    for path in video.parent.iterdir():
        if not path.is_file() or path.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue
        if path.name == profile.output_filename(video.stem):
            continue
        if path.stem == video.stem or path.stem.startswith(video.stem + "."):
            candidates.append(path)
    return candidates


def choose_sidecar(
    video: Path, profile: LanguageProfile = DEFAULT_PROFILE
) -> Path | None:
    candidates = _matching_sidecars(video, profile)
    if not candidates:
        return None
    def score(path: Path) -> tuple[int, int, str]:
        name = path.name.casefold()
        english = any(token in name for token in (".en.", ".eng.", "english"))
        return (int(english), int(path.suffix.lower() == ".srt"), name)
    return max(candidates, key=score)


def existing_target_subtitles(
    video: Path,
    tools: MediaTools,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> list[str]:
    found: list[str] = []
    external = destination_path(video, profile)
    if external.is_file():
        try:
            parse_srt(external.read_text(encoding="utf-8-sig"))
            found.append(f"external {external.name}")
        except (OSError, UnicodeError, WorkflowError):
            pass
    for track in probe_tracks(video, tools):
        identity = f"{track.language} {track.title}".casefold()
        if (
            track.codec.casefold() in {"srt", "subrip"}
            and profile.is_explicit_target_track(identity)
        ):
            found.append(track.description)
    return found


def _copy_sidecar(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    temporary.unlink(missing_ok=True)
    shutil.copyfile(source, temporary)
    temporary.replace(destination)


def acquire_source(
    video: Path,
    sources: Path,
    log: Path,
    *,
    explicit_source: Path | None,
    requested_track: int | None,
    tools: MediaTools,
    profile: LanguageProfile = DEFAULT_PROFILE,
) -> tuple[Path, str]:
    """Copy or demux one complete source into the durable local workspace."""
    sources.mkdir(parents=True, exist_ok=True)
    if explicit_source is not None:
        source = explicit_source.expanduser().resolve()
        if not source.is_file():
            raise WorkflowError(f"explicit subtitle source is missing: {source}")
        digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
        destination = sources / f"{video.stem}.sidecar.{digest}{source.suffix.lower()}"
        _copy_sidecar(source, destination)
        append_log(log, f"SOURCE COPIED LOCALLY: {destination}")
        return destination, f"sidecar {source.name}"

    tracks = probe_tracks(video, tools)
    embedded_error: WorkflowError | None = None
    try:
        track = choose_track(tracks, requested_track)
        destination = sources / f"{video.stem}.embedded-stream-{track.index}.srt"
        demux_tracks(video, [(track.index, destination)], tools, overwrite=True)
        append_log(log, f"SOURCE DEMUXED LOCALLY: {destination}")
        return destination, track.description
    except WorkflowError as exc:
        if requested_track is not None:
            raise
        embedded_error = exc

    sidecar = choose_sidecar(video, profile)
    if sidecar is not None:
        digest = hashlib.sha256(str(sidecar).encode("utf-8")).hexdigest()[:12]
        destination = sources / f"{video.stem}.sidecar.{digest}{sidecar.suffix.lower()}"
        _copy_sidecar(sidecar, destination)
        append_log(log, f"SOURCE COPIED LOCALLY: {destination}")
        return destination, f"sidecar fallback {sidecar.name}"
    raise embedded_error or WorkflowError(f"no text subtitle source for {video.name}")


def cached_source(video: Path, sources: Path) -> Path:
    candidates = sorted(
        (
            path
            for path in sources.glob("*")
            if path.name.startswith(video.stem + ".")
            and path.is_file() and path.suffix.lower() in SUBTITLE_EXTENSIONS
            and ".partial" not in path.name and path.stat().st_size > 0
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not candidates:
        raise WorkflowError(
            f"no durable local source for {video.name}; run --source-only while media is mounted"
        )
    return candidates[0]


def source_as_srt(source: Path, temporary_dir: Path, tools: MediaTools) -> str:
    if source.suffix.lower() == ".srt":
        for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252"):
            try:
                document = source.read_text(encoding=encoding)
                parse_srt(document)
                return document
            except (UnicodeError, WorkflowError):
                continue
        raise WorkflowError(f"cannot decode valid SRT source: {source}")
    converted = temporary_dir / f"{source.stem}.converted.srt"
    process = subprocess.run(
        [str(tools.require("ffmpeg")), "-nostdin", "-y", "-i", str(source), "-f", "srt", str(converted)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if process.returncode:
        raise WorkflowError(f"subtitle conversion failed: {process.stderr[-1000:]}")
    return converted.read_text(encoding="utf-8-sig")


def demux_tracks(
    video: Path, outputs: list[tuple[int, Path]], tools: MediaTools, *, overwrite: bool = False,
) -> None:
    """Extract several text tracks in one scan, publish only complete nonempty files."""
    if not outputs:
        raise WorkflowError("no selected text tracks")
    for _, destination in outputs:
        if destination.exists() and not overwrite:
            raise WorkflowError(f"output exists: {destination}; use --overwrite")
        destination.parent.mkdir(parents=True, exist_ok=True)
    # A private temporary directory prevents concurrent demux calls sharing a partial file.
    with tempfile.TemporaryDirectory(prefix=".demux-", dir=outputs[0][1].parent) as temp:
        staged = [Path(temp) / f"track-{index}.srt" for index, _ in outputs]
        command = [str(tools.require("ffmpeg")), "-nostdin", "-hide_banner", "-v", "error", "-n", "-i", str(video)]
        for (index, _), path in zip(outputs, staged):
            command.extend(["-map", f"0:{index}", "-c:s", "srt", "-f", "srt", str(path)])
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, check=False)
        if result.returncode:
            raise WorkflowError(f"ffmpeg subtitle extraction failed: {result.stderr[-1000:]}")
        if any(not path.is_file() or path.stat().st_size == 0 for path in staged):
            raise WorkflowError("ffmpeg produced an empty subtitle track; no outputs were replaced")
        # No parsing, normalization, or model use here: source-first means copy only.
        for path, (_, destination) in zip(staged, outputs):
            if overwrite:
                path.replace(destination)
            else:
                # link is no-clobber even if another process published the same name.
                import os
                os.link(path, destination)
