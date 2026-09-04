# Retry and validation

Read this reference when a cue or chunk fails, a user requests a direct retry, or validation logic changes.

## Named checks

Every failure has one stable short name.

Appealable translation checks:

- `pun`
- `reveal order`
- `speaker label`
- `terminology`

Non-appealable protocol checks:

- `structure`
- `cue id`
- `cue deletion`
- `addition`
- `appeal format`
- `appeal permission`

Protocol checks protect safe mapping and rendering and therefore cannot be skipped.

## Deterministic validation

Require every target integer ID exactly once. Reject context IDs, unknown IDs, omissions, and duplicates. Rebuild timestamps, source text, and original SRT numbers from the local source index; Iris never controls them.

Iris owns the semantic decision that a cue is purely non-speech. Do not enumerate source-language sounds or reject a deletion based on source wording. Deterministically require every `drop=true` record to have empty `text` and `additions`. Validate addition kinds through a whitelist. A `pun_note` must be short, Chinese-only, and absent from an already top-positioned main cue.

Run the speaker-label residue check. Translation completeness, laughter removal, and omission of explanatory prefixes on pun notes are Iris prompt responsibilities, not deterministic checks. Do not run a separate duplicate semantic-review model pass.

## Automatic retry

On the first validation failure, explicitly tell Iris that the next call is a retry and include:

- the exact check name and reason
- the previous response
- each failing target ID
- up to 10 read-only source cues before and after each target
- the same relevant glossary selection

Use a local retry for mapped cue-level failures. Merge successful repairs by integer ID. Use a complete overlapping chunk retry for missing, duplicate, unknown, or otherwise structurally unmappable output. Allow at most three Iris attempts per chunk and never cache invalid records.

If a run stops at a chunk boundary or exhausts that chunk's attempts, preserve every earlier validated cache and the episode-wide `completed_chunks` list. Resume with `--chunks N-`, where `N` is the failed or next missing absolute chunk. Do not renumber the selected range from 1. After the remaining ranges succeed, run without `--chunks` for full source-ordered assembly and final rendering.

## Appeals

Only a retry response may list `skip_checks`. One target can appeal several named translation checks. The script validates every requested name and persists accepted appeals in the translation record. Iris may retain its original wording when a detector is a false positive.

Never accept an appeal for a protocol check. Additions and appeals are separate fields; an appeal does not change source identity or output timing.

## User-initiated retry

A user can begin directly with `--retry-cues` and a reason. The selector refers to source episode IDs, even if the episode has no complete translation yet. Save validated retry records in `retries/`; future chunk translation applies them by ID and gives the retry file its own source fingerprint. A fingerprint mismatch invalidates the patches.

## Atlas retry

Atlas editing is independent from Iris validation. A failed or interrupted Atlas edit
restores the exact previous glossary and leaves its durable job pending. Retry it with
`glossary retry`; tell Atlas this is an Atlas-only retry and include the stored error.
Do not call Iris, discard a validated chunk, or treat a pending terminology job as an
episode translation failure.
