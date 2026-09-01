"""Media executables live in the user's workspace, never in the Skill or PATH."""
from __future__ import annotations

import os
import platform
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import SKILL_DIR
from .errors import WorkflowError


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    machine = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    if system not in {"darwin", "linux", "windows"} or machine not in {"x86_64", "arm64"}:
        raise WorkflowError(f"unsupported FFmpeg platform: {system}-{machine}; see references/media-tools.md")
    return f"{system}-{machine}"


@dataclass(frozen=True)
class MediaTools:
    directory: Path

    @classmethod
    def in_workspace(cls, workspace: Path) -> "MediaTools":
        workspace = workspace.expanduser().resolve()
        if workspace == SKILL_DIR or SKILL_DIR in workspace.parents:
            raise WorkflowError("workspace and media binaries must be outside the Skill directory")
        return cls(workspace / "tools" / "ffmpeg" / platform_key() / "bin")

    def path(self, name: str) -> Path:
        if name not in {"ffmpeg", "ffprobe"}:
            raise WorkflowError(f"unknown media executable: {name}")
        return self.directory / (name + (".exe" if os.name == "nt" else ""))

    def require(self, name: str) -> Path:
        path = self.path(name)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise WorkflowError(
                f"workspace media tool missing or not executable: {path}; "
                "ask the current Agent to provision ffmpeg and ffprobe as described in "
                "references/media-tools.md, then run main.py tools status --workspace-dir <local-workspace>"
            )
        if SKILL_DIR in path.resolve().parents:
            raise WorkflowError("media executables cannot resolve inside the Skill")
        return path

    def versions(self) -> dict[str, str]:
        versions = {}
        for name in ("ffmpeg", "ffprobe"):
            try:
                result = subprocess.run(
                    [str(self.require(name)), "-version"], capture_output=True,
                    text=True, check=False, timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise WorkflowError(f"cannot execute workspace {name}: {exc}") from exc
            if result.returncode or not result.stdout.startswith(name + " version "):
                raise WorkflowError(f"invalid or incompatible {name}: {result.stderr[-500:]}")
            versions[name] = result.stdout.splitlines()[0]
        if versions["ffmpeg"].split()[2] != versions["ffprobe"].split()[2]:
            raise WorkflowError("ffmpeg and ffprobe must come from the same build version")
        return versions
