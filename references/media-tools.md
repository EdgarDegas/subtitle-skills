# Agent-provisioned FFmpeg and ffprobe

The Skill contains no provider list, download URL, package-manager command, downloader, archive extractor, or automatic installer. The current Agent is responsible for finding and provisioning suitable FFmpeg and ffprobe executables when a media operation needs them.

## Installation prompt for the Agent

1. Run `python3 <skill-dir>/main.py tools status --workspace-dir <local-workspace>`. If both tools pass validation, reuse them and do not upgrade them without a reason.
2. If either tool is missing, inspect the current OS and CPU architecture. Find current installation guidance or download links from the FFmpeg project's current official information and trustworthy platform-native sources. Do not reuse a provider choice or direct URL merely because it appears in an old run, document, or cached manifest.
3. Prefer native executables for the detected architecture. Do not choose Rosetta, another emulator, or a compatibility build solely because it can run. If no suitable native option is available, explain the limitation and obtain the user's agreement before installing a non-native build.
4. Use the platform's normal package manager when appropriate, or download from a trustworthy publisher. Verify published checksums or signatures when available. Do not install PyPI packages named `ffmpeg` or `ffprobe`; those are not substitutes for the media executables.
5. Put regular executable files, or explicit links to a user-approved installation, at:

   ```text
   <workspace>/tools/ffmpeg/<os>-<architecture>/bin/ffmpeg[.exe]
   <workspace>/tools/ffmpeg/<os>-<architecture>/bin/ffprobe[.exe]
   ```

   The `<os>-<architecture>` value is determined by the Skill at runtime, such as `darwin-arm64`, `linux-x86_64`, or `windows-arm64`. Keep the tools outside the Skill directory.
6. Run `tools status` again. Both executables must be executable, respond to `-version`, and report the same FFmpeg build version before continuing.

The media resolver never falls back silently to PATH, application-private binaries, or Python package resources. Listing embedded tracks and extracting/converting subtitles require these workspace paths. Translating an already staged SRT, validating it, rendering it, and syncing it do not require FFmpeg.
