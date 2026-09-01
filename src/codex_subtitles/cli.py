from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

from .application import RunOptions, process_video, rerender_saved_output
from .codex_client import CodexClient
from .config import DEFAULT_CHUNK_CUES, TRANSLATION_SCHEMA
from .curation import curation_status, retry_pending
from .errors import WorkflowError
from .language_profiles import DEFAULT_PROFILE, LanguageProfile, get_profile, profile_ids
from .media import discover_videos
from .runtime import MediaTools
from .extraction import extract_tracks, list_tracks
from .workspace import (
    collection_dir,
    ensure_layout,
    log_path,
    progress_path,
    read_json,
    records_path,
    subtitle_offset_ms,
    update_progress,
)


def parse_chunk_range(value: str) -> tuple[int, int | None]:
    match = re.fullmatch(r"([1-9]\d*)(?:-([1-9]\d*)?)?", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("use START, START-END, or START-")
    start = int(match.group(1))
    if "-" not in value:
        return (start, start)
    end = int(match.group(2)) if match.group(2) else None
    if end is not None and end < start:
        raise argparse.ArgumentTypeError("chunk range end must not precede start")
    return (start, end)


def _translation_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("inputs", nargs="+", type=Path, help="video files or directories")
    parser.add_argument("--recursive", action="store_true", help="scan directories recursively")
    parser.add_argument("--workspace-dir", required=True, type=Path, help="durable local workspace root")
    parser.add_argument("--source", type=Path, help="exact source subtitle for one video")
    parser.add_argument("--track", type=int, help="absolute embedded subtitle stream index")
    parser.add_argument("--overwrite", action="store_true", help="replace stale staged/destination output")
    parser.add_argument("--stage-only", action="store_true", help="translate from local source and do not sync")
    parser.add_argument("--source-only", action="store_true", help="copy/demux source locally and stop")
    parser.add_argument("--sync-only", action="store_true", help="sync staged output without translation")
    parser.add_argument("--normalize-only", action="store_true", help="normalize staged SRT punctuation")
    parser.add_argument("--retry-cues", help="episode cue IDs or ranges, for example 161-162,414")
    parser.add_argument("--retry-reason", help="required reason for retry mode")
    parser.add_argument("--codex", default="codex", help="codex executable name or path")
    parser.add_argument(
        "--profile",
        choices=profile_ids(),
        default=DEFAULT_PROFILE.id,
        help=f"target-language profile (default: {DEFAULT_PROFILE.id})",
    )
    parser.add_argument(
        "--chunk-cues",
        type=int,
        default=DEFAULT_CHUNK_CUES,
        help=f"target cues per Iris request (default: {DEFAULT_CHUNK_CUES})",
    )
    parser.add_argument(
        "--chunks",
        type=parse_chunk_range,
        metavar="START[-END]",
        help="translate an inclusive 1-based chunk range; START- continues to the end",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="video-subtitles",
        description="List, extract, and translate subtitles directly from this Skill; no pip installation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    translate = subparsers.add_parser("translate", help="translate local subtitle sources and sync SRT sidecars")
    _translation_arguments(translate)
    for command, help_text in (("list", "list embedded subtitle tracks"), ("extract", "extract selected or all text tracks to SRT")):
        media_parser = subparsers.add_parser(command, help=help_text)
        media_parser.add_argument("inputs", nargs="+", type=Path)
        media_parser.add_argument("--workspace-dir", required=True, type=Path)
        media_parser.add_argument("--recursive", action="store_true")
        if command == "extract":
            media_parser.add_argument("--track", action="append", type=int, help="absolute stream index; repeat for multiple tracks")
            media_parser.add_argument("--output-dir", type=Path, help="local output directory; defaults to workspace season extracted/<video>/")
            media_parser.add_argument("--overwrite", action="store_true")
    tooling = subparsers.add_parser("tools", help="inspect Agent-provisioned workspace-local FFmpeg binaries")
    tooling.add_argument("action", choices=("status",))
    tooling.add_argument("--workspace-dir", required=True, type=Path)
    glossary = subparsers.add_parser(
        "glossary", help="inspect or retry pending direct-edit Atlas jobs"
    )
    glossary.add_argument("action", choices=("status", "retry"))
    glossary.add_argument("inputs", nargs="+", type=Path, help="original video paths or mounted directories")
    glossary.add_argument("--workspace-dir", required=True, type=Path)
    glossary.add_argument("--recursive", action="store_true")
    glossary.add_argument("--codex", default="codex")
    glossary.add_argument(
        "--profile", choices=profile_ids(), default=DEFAULT_PROFILE.id
    )
    offset = subparsers.add_parser(
        "offset", help="show or set a persistent episode subtitle time offset"
    )
    offset.add_argument("action", choices=("show", "set"))
    offset.add_argument("inputs", nargs="+", type=Path, help="original video paths or mounted directories")
    offset.add_argument("--workspace-dir", required=True, type=Path)
    offset.add_argument("--recursive", action="store_true")
    offset.add_argument(
        "--profile", choices=profile_ids(), default=DEFAULT_PROFILE.id
    )
    offset.add_argument(
        "--milliseconds",
        type=int,
        help="signed offset: positive is later, negative is earlier",
    )
    return parser


def _glossary_command(
    args: argparse.Namespace,
    workspace: Path,
    profile: LanguageProfile,
) -> int:
    try:
        videos = discover_videos(
            args.inputs, recursive=args.recursive, offline_workspace=workspace
        )
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    failures = 0
    for video in videos:
        state_dir = collection_dir(video, workspace)
        try:
            progress = read_json(progress_path(state_dir, video, profile))
            fingerprint = str(progress.get("source_fingerprint") or "")
            if not fingerprint:
                raise WorkflowError(f"episode has no source fingerprint: {video.name}")
            if args.action == "retry":
                with tempfile.TemporaryDirectory(prefix="codex-subtitles-atlas-") as temp_name:
                    episode_log = log_path(state_dir, video, profile)
                    status = retry_pending(
                        CodexClient(
                            executable=args.codex,
                            work_dir=Path(temp_name),
                            log_path=episode_log,
                        ),
                        state_dir,
                        video,
                        fingerprint,
                        log_path=episode_log,
                        profile=profile,
                    )
            else:
                status = curation_status(state_dir, video, fingerprint, profile)
            print(
                f"GLOSSARY {video.name}: {status['glossary_status']} "
                f"pending={status['glossary_pending']}",
                flush=True,
            )
        except (OSError, WorkflowError) as exc:
            failures += 1
            print(f"FAILED {video.name}: {exc}", file=sys.stderr, flush=True)
    return 1 if failures else 0


def _offset_command(
    args: argparse.Namespace,
    workspace: Path,
    profile: LanguageProfile,
) -> int:
    try:
        videos = discover_videos(
            args.inputs, recursive=args.recursive, offline_workspace=workspace
        )
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    failures = 0
    for video in videos:
        state_dir = collection_dir(video, workspace)
        try:
            ensure_layout(state_dir)
            if args.action == "set":
                update_progress(
                    state_dir,
                    video,
                    profile=profile,
                    subtitle_offset_ms=args.milliseconds,
                    output_ready=False,
                    synced=False,
                )
                if records_path(state_dir, video, profile).is_file():
                    output, value = rerender_saved_output(video, workspace, profile)
                    print(
                        f"OFFSET {video.name}: {value:+d} ms; RENDERED {output}",
                        flush=True,
                    )
                else:
                    print(
                        f"OFFSET {video.name}: {args.milliseconds:+d} ms; output pending",
                        flush=True,
                    )
            else:
                value = subtitle_offset_ms(state_dir, video, profile)
                print(f"OFFSET {video.name}: {value:+d} ms", flush=True)
        except (OSError, WorkflowError) as exc:
            failures += 1
            print(f"FAILED {video.name}: {exc}", file=sys.stderr, flush=True)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace = args.workspace_dir.expanduser().resolve()
    profile = get_profile(getattr(args, "profile", DEFAULT_PROFILE.id))
    if args.command == "tools":
        try:
            tools = MediaTools.in_workspace(workspace)
            for name, version in tools.versions().items():
                print(f"{tools.path(name)}: {version}")
            return 0
        except (OSError, ValueError, WorkflowError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.command == "glossary":
        return _glossary_command(args, workspace, profile)
    if args.command == "offset":
        if args.action == "set" and args.milliseconds is None:
            parser.error("offset set requires --milliseconds")
        if args.action == "show" and args.milliseconds is not None:
            parser.error("--milliseconds is available only with offset set")
        return _offset_command(args, workspace, profile)
    if args.command in {"list", "extract"}:
        try:
            videos = discover_videos(args.inputs, recursive=args.recursive)
            if not videos:
                raise WorkflowError("no supported videos found")
            tools = MediaTools.in_workspace(workspace)
            for video in videos:
                tracks = list_tracks(video, tools)
                if args.command == "extract":
                    destination = (args.output_dir.expanduser().resolve() if args.output_dir
                                   else collection_dir(video, workspace) / "extracted" / video.stem)
                    for path in extract_tracks(video, tracks, destination, tools, requested=args.track, overwrite=args.overwrite):
                        print(f"EXTRACTED {path}")
            return 0
        except (OSError, WorkflowError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    missing = [
        str(path)
        for path in (TRANSLATION_SCHEMA,)
        if not path.is_file()
    ]
    if missing:
        parser.error("Skill schema missing: " + ", ".join(missing))
    modes = [
        args.stage_only,
        args.source_only,
        args.sync_only,
        args.normalize_only,
        args.retry_cues is not None,
    ]
    if sum(bool(mode) for mode in modes) > 1:
        parser.error("source/stage/sync/normalize/retry modes are mutually exclusive")
    if args.retry_cues and not args.retry_reason:
        parser.error("--retry-cues requires --retry-reason")
    if args.retry_reason and not args.retry_cues:
        parser.error("--retry-reason requires --retry-cues")
    if args.chunk_cues < 1:
        parser.error("--chunk-cues must be positive")
    if args.chunks is not None and any(
        (args.source_only, args.sync_only, args.normalize_only, args.retry_cues is not None)
    ):
        parser.error("--chunks is available only for direct or --stage-only translation")

    try:
        offline = args.stage_only or args.normalize_only or args.retry_cues is not None
        videos = discover_videos(args.inputs, recursive=args.recursive,
                                 offline_workspace=workspace if offline else None)
    except WorkflowError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not videos:
        print("error: no supported videos found", file=sys.stderr)
        return 2
    if args.source and len(videos) != 1:
        parser.error("--source requires exactly one video")
    if args.retry_cues and len(videos) != 1:
        parser.error("--retry-cues requires exactly one video")

    options = RunOptions(
        workspace_root=workspace,
        codex=args.codex,
        chunk_cues=args.chunk_cues,
        chunk_range=args.chunks,
        overwrite=args.overwrite,
        stage_only=args.stage_only,
        source_only=args.source_only,
        sync_only=args.sync_only,
        normalize_only=args.normalize_only,
        explicit_source=args.source,
        requested_track=args.track,
        retry_cues=args.retry_cues,
        retry_reason=args.retry_reason,
        profile=profile,
    )
    failures = 0
    existing: list[str] = []
    for video in videos:
        try:
            result = process_video(video, options)
            print(result, flush=True)
            if result.startswith(f"EXISTING {profile.output_tag.upper()}"):
                existing.append(video.name)
        except (OSError, WorkflowError) as exc:
            failures += 1
            print(f"FAILED {video.name}: {exc}", file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            update_progress(
                collection_dir(video, workspace),
                video,
                profile=profile,
                status="paused",
                interruption_reason="interrupted; validated chunks preserved",
            )
            print(f"PAUSED {video.name}: validated chunks preserved", file=sys.stderr, flush=True)
            return 130
    if existing:
        print(
            f"Existing explicit {profile.output_tag} SRTs were skipped: "
            + ", ".join(existing)
            + ". Rerun with --overwrite only when retranslation is desired.",
            flush=True,
        )
    return 1 if failures else 0
