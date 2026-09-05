from __future__ import annotations

import re

from .base import LanguageProfile


def _normalize_text(text: str) -> str:
    lines: list[str] = []
    closing = r'(?=["”’」』》）】]*$)'
    for raw_line in text.splitlines():
        line = re.sub(r"…+|\.{2,}", "...", raw_line.rstrip())
        line = re.sub(rf"。+{closing}", "", line)
        line = re.sub(rf"(?<!\.)\.{closing}", "", line)
        lines.append(line)
    return "\n".join(lines).strip()


_TRANSLATED_SPEAKER_LABEL = re.compile(
    r"^(?:[-–—]\s*)?[^，,。.!！?？：:\n]{1,16}(?::|：)\s*"
)


class SimplifiedChineseProfile(LanguageProfile):
    def normalize_text(self, text: str) -> str:
        return _normalize_text(text)

    def translated_speaker_label(self, text: str) -> bool:
        return bool(_TRANSLATED_SPEAKER_LABEL.match(text))

ZH_HANS = SimplifiedChineseProfile(
    id="zh-hans",
    language_name="Simplified Chinese",
    native_name="简体中文",
    output_tag="zh-hans",
    glossary_column="简中",
    glossary_value_key="zh_hans",
    default_scope="按需",
    global_scope="全局",
    existing_track_markers=(
        "zh-hans",
        "zh-cn",
        "chs",
        "simplified chinese",
        "简体",
        "简中",
    ),
    pun_note_max_chars=32,
    translation_instructions="""- Translate into natural Simplified Chinese. Use standard Simplified Chinese translations for commonplace standalone first names, real countries, and well-known real cities.
- English kinship words may omit distinctions Chinese requires. Use verified episode relationships and speaker/referent context for 哥哥/弟弟, 姐姐/妹妹, 堂/表 relatives and maternal/paternal relations. Never infer seniority from the word brother alone. If the relation or referent is unknown, use a natural neutral phrasing instead of guessing. A younger sibling saying 'I am a bad brother' refers to himself as 弟弟; accusing his older sibling refers to 哥哥.
- Remove laughter fragments such as `Heh`, `Ha-ha`, `Ho-ho`, `呵呵`, and `哈哈`; delete the whole cue only when it contains no dialogue or lyrics.
- Keep actual lyrics and visible on-screen words. Decide from context whether music-marked text is an actual lyric. Preserve an existing `{\\an8}` prefix exactly; add it to actual lyrics and to visible signs, papers, screens, labels, title cards, and messages after removing descriptive wrappers.
- If an intentional pun cannot survive natural Chinese, keep the literal translation in `text` and add one `pun_note`. Explain only the lost relationship, use whichever language makes it clearest, keep it to one line and at most 32 characters, and add no explanatory prefix such as `Pun:`, `双关`, `注`, or `译注`. Prefer `“芬兰语”与“完成”同音` when a concise Chinese explanation works.
- Preserve information-reveal order at every cue boundary. Example: `At the end of the corridor... / holding the gun... / was Anna` becomes `在走廊尽头... / 有个人拿着那把枪... / 她就是安娜`, without naming Anna early.
- Do not end lines with `。` or a single `.`, keep commas, and write ellipses as exactly `...`.""",
    curator_instructions="""- Maintain target terms in Simplified Chinese and preserve Iris's proposed target exactly when accepting a new entry.
- Never add commonplace standalone first names, real countries, or well-known real cities. Full names, uncommon names, fictional places, organizations, titles, nicknames, and story-specific terms may qualify.
- Finish with a short Chinese summary.""",
)
