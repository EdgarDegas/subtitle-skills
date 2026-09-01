# Workflow and persistence architecture

The sole code source is this Skill's `src/codex_subtitles`, loaded by its `main.py`.
There is no installed Python distribution. Media binaries are resolved only from
the chosen workspace's `tools/ffmpeg/<platform>/bin`; see [media-tools.md](media-tools.md).
The `list` and `extract` subcommands share probing/demux code with `translate`.
Multi-track exports go to `extracted/<video-stem>/`, separate from `sources/` so
an arbitrary last-exported language cannot become the cached translation source.

Read this reference before changing source identity, chunking, retries, caches, or rendering.

## Episode source revision

The durable source is converted to clean SRT locally before any model call. Parse it into an immutable episode source index:

```json
{"type":"source","schema_version":1,"source_fingerprint":"<full sha256>","cue_count":737}
{"type":"cue","id":1,"source_number":"1","timestamp":"00:00:01,000 --> 00:00:03,000","text":"..."}
```

`id` is the one-based parsed order within this source revision. Do not use the original SRT number as identity; it may skip or repeat. The full fingerprint is SHA-256 over canonical `{timestamp,text}` records, so original numbering, BOM, and newline changes do not invalidate work, while timing or text changes do.

The fingerprint belongs to the episode document, not to each cue ID. If it changes, old caches, retry patches, records, and render maps are stale. A cue ID is stable only within one fingerprint.

## Translation records

Iris receives source records with only `id`, `role`, and `text`. Context records are read-only. A target response has this shape:

```json
{
  "id": 411,
  "text": "那是芬兰语吗？",
  "drop": false,
  "additions": [
    {"kind": "pun_note", "text": "“芬兰语”与“完成”同音"}
  ],
  "skip_checks": []
}
```

Keep one translation record per source cue. `drop` and `additions` may change the number of final SRT blocks but never insert into or renumber the source index. New addition kinds require an explicit schema enum, validator, prompt rule, renderer, and tests; Iris may not invent kinds.

Persist translation JSONL with one document header containing the ruleset and source fingerprint, followed by cue records. Do not repeat the fingerprint in every cue.

## Chunk ownership and alignment

Default core size is 50 target cues, with up to 10 context cues before and after. The target range gives each source cue exactly one owning chunk. Integer IDs then handle target-array reordering, omission, duplication, deletion, local retries, out-of-order completion, and cache resume.

For example, one request may contain context `41..50`, target `51..100`, and context `101..110`. The next request owns `101..150`. Only target records are accepted and merged.

Cache keys include the source fingerprint, ruleset, target-language profile, model policy, chunk size, context size, retry-patch digest, and relevant glossary digest. Cache contents also carry the full fingerprint, ruleset, and profile header. Missing profile fields in legacy documents mean the default `zh-hans` profile.

Persist validated Iris records before invoking Atlas. Atlas directly edits the shared
`glossary.md`; Python does not generate or apply term actions. Give the Atlas process
write access to that one file only, with the rest of the filesystem read-only and
network disabled. Store the candidates and episode fingerprint as a resumable job in
`glossary-jobs/`. A failed edit is restored from its exact pre-edit backup and leaves
the job pending; it never invalidates the already saved Iris chunk.

Use `--chunks START`, `--chunks START-END`, or `--chunks START-` to process an explicit inclusive one-based range. The absolute chunk number remains the cache owner and progress identity, even when the run starts after chunk 1. Persist `completed_chunks` as a sorted episode-wide list and keep the current `active_chunk_range` separate. A range run emits only a preview for that range. Run once without `--chunks` after the ranges finish to assemble the full source-ordered records and output from valid caches; if a relevant glossary digest changed, that final pass may refresh the affected chunk.

## Rendering

Rendering happens only after every translation record validates and merges in source-ID order. For each kept record:

1. Expand controlled additions before the main cue
2. Render `pun_note` at the top with `{\an8}` and extend its end by 1500 ms
3. Render the main translated cue with authoritative source timing
4. Renumber the complete output sequentially from 1
5. For a complete final SRT only, shift every rendered timestamp by the episode's
   persistent `subtitle_offset_ms`; positive is later, negative is earlier, and values
   before zero clamp to `00:00:00,000`

The offset lives in `progress/<episode>.json` and is also copied into the render-map
header for audit. It never participates in source identity, cache keys, translation
records, retries, or partial previews. Changing it re-renders from saved records and
does not invoke Iris or Atlas.

Persist a render map such as:

```json
{"output_number":411,"source_id":411,"role":"pun_note"}
{"output_number":412,"source_id":411,"role":"main"}
```

Dropped source cues remain visible in translation records but have no rendered output row.

## Workspace artifacts

Television uses `<workspace>/<show>/<season>/`; all seasons share `<workspace>/<show>/glossary.md`. A movie uses one title folder.

- `sources/`: untouched complete local subtitle copies or demuxed SRTs
- `indexes/*.source.jsonl`: immutable episode source index and fingerprint
- `chunks/`: validated resumable chunk records
- `retries/`: durable cue-level retry patches
- `records/`: complete validated source-aligned translations, isolated by non-default profile
- `indexes/*.render.jsonl`: final output-number to source-ID mapping
- `outputs/`: completed local profile-tagged SRTs
- `previews/`: explicitly requested chunk-range preview SRTs named with their absolute start and end; never treat them as complete outputs
- `usage/`: per-episode glossary occurrences
- `glossary-jobs/`: resumable Atlas direct-edit jobs and exact pre-edit backups
- `progress/`: compact machine-readable episode state, including the signed
  `subtitle_offset_ms` used only by final rendering
- `logs/`: compact stage log and separate full Codex JSONL
- `manifest.json`: season index
- `REPORT.md`: short user-facing report

`glossary-feedback.jsonl`, `glossary-updates.jsonl`, and `glossary-usage.jsonl` live at the show or movie root for direct `rg` queries.

## In-memory stages

The source document, current chunk window, relevant glossary subset, Iris response, validation failures, and merged translation records exist in memory. Every expensive validated boundary is persisted: local source, source index, successful chunks, retry patches, terminology feedback, Atlas jobs and edit audits, final records, output, mapping, progress, and logs.
