# Atlas before each episode

Before Iris translates an episode or retries cues, Atlas reads the complete staged
source and shared glossary. It researches translation-relevant context using hosted
web search, then directly edits `glossary.md`. TMDB/TheTVDB/IMDb provide credits;
official show sources can establish character relationships. Local shell networking
stays disabled and only the glossary is writable.

Atlas owns the merging decisions. Reuse existing facts, add useful evidence or
clarifications, and consolidate duplicates. Keep approved translations and user
instructions. Background information can live in existing notes or concise Markdown
prose, with sources and episode/cue applicability. Do not create copies per episode.
An unchanged glossary is a valid result. Finish with a plain-language summary,
including uncertainty or research limitations; no structured receipt is required.

Iris receives the complete shared glossary and current filename. Its prompt handles
which background facts apply, speaker/referent ambiguity, and information-reveal
boundaries. These are model decisions, not Python episode filters. Keep notes concise
because the glossary is included in every chunk, including pronoun-only dialogue.

The pre-episode call uses the existing durable Atlas job, backup, diff and retry
mechanism. Successful calls are reused for the same episode/source/profile/ruleset;
failed calls or invalid Markdown edits are restored and remain pending before Iris
starts. Python validates Markdown structure during enrichment; it does not judge the
semantic merge or require a new row. `glossary retry` can retry failed Atlas calls
without Iris. Source-only, extraction, rendering, offset and sync do not call Atlas.
