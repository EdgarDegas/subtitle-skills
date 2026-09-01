# Target-language profiles

Read this reference before adding or changing a target language.

Runtime profiles live under `src/codex_subtitles/language_profiles/`. Generic workflow modules receive a `LanguageProfile`; they must not branch on a language ID. The registry in `language_profiles/__init__.py` is the only list exposed by `--profile`.

The current `zh-hans` profile owns:

- stable ID, human names, and `.zh-hans.srt` output tag
- Markdown glossary target header, persisted usage key, and scope labels
- embedded-track markers used to recognize an existing target SRT
- Iris translation instructions and Atlas curation instructions
- target punctuation normalization
- translated speaker-label, laughter, and foreign-text residue checks
- target-specific `pun_note` length and content rules

The protocol field for a proposed glossary translation is the language-neutral `target`. Translation records store the selected profile in their document header. Legacy records without that field mean the default `zh-hans` profile, so existing validated chunks remain reusable.

Default `zh-hans` artifact paths remain unchanged. A non-default profile receives a profile-qualified glossary, progress, records, retry, log, render-map, usage, preview, output, and cache namespace so target languages cannot reuse or overwrite each other's durable state.

To add another profile:

1. Implement every required behavior in one profile module and register it
2. Add its human translation policy reference
3. Verify output naming, existing-target detection, prompt assembly, normalization, validation, glossary parsing, state isolation, and legacy default compatibility
4. Run the complete test suite and the Skill validator

Do not add a partially configured language to the registry. In particular, changing only the Iris prompt or filename is not a complete profile.
