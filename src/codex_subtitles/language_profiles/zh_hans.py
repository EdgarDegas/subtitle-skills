from __future__ import annotations

import re
import unicodedata

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


def _is_latin_word(token: str) -> bool:
    letters = [character for character in token if character.isalpha()]
    return bool(letters) and all(
        "LATIN" in unicodedata.name(character, "") for character in letters
    )


def _foreign_text_residue(text: str) -> str | None:
    plain = re.sub(r"\{\\[^}]+\}|<[^>]+>", " ", text)
    candidates = [
        token
        for token in re.findall(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*", plain, re.UNICODE)
        if _is_latin_word(token)
    ]
    meaningful: list[str] = []
    for token in candidates:
        letters = "".join(character for character in token if character.isalpha())
        if len(letters) <= 1 or (letters.isupper() and len(letters) <= 6):
            continue
        meaningful.append(token)
    if len(meaningful) >= 2:
        return " ".join(meaningful[:4])
    if meaningful and any(
        ord(character) > 127 for character in meaningful[0] if character.isalpha()
    ):
        return meaningful[0]
    return None


_TRANSLATED_SPEAKER_LABEL = re.compile(
    r"^(?:[-–—]\s*)?[^，,。.!！?？：:\n]{1,16}(?::|：)\s*"
)
_LAUGHTER = re.compile(
    r"(?:\b(?:heh(?:[ -]?heh)*|ha(?:[ -]?ha)+|ho(?:[ -]?ho)+|"
    r"chuckles?|chuckling|laughs?|laughter|giggles?)\b|"
    r"(?:^|[\s，,。.!！?？—–-])(?:哈哈+|呵呵+|嘿嘿+|嘻嘻+|咯咯+|哼哼+)"
    r"(?=$|[\s，,。.!！?？—–-]))",
    re.IGNORECASE,
)


class SimplifiedChineseProfile(LanguageProfile):
    def normalize_text(self, text: str) -> str:
        return _normalize_text(text)

    def foreign_text_residue(self, text: str) -> str | None:
        return _foreign_text_residue(text)

    def translated_speaker_label(self, text: str) -> bool:
        return bool(_TRANSLATED_SPEAKER_LABEL.match(text))

    def laughter_residue(self, text: str) -> str | None:
        match = _LAUGHTER.search(text)
        return match.group(0).strip() if match else None

    def validate_pun_note(self, text: str) -> str | None:
        if re.search(r"[A-Za-z]", text):
            return "contains source-language text"
        if re.match(r"^(?:双关|注|译注)\s*[:：]?", text):
            return "starts with a forbidden explanatory label"
        return None


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
- Remove laughter fragments such as `Heh`, `Ha-ha`, `Ho-ho`, `呵呵`, and `哈哈`; delete the whole cue only when it contains no dialogue or lyrics.
- Keep actual lyrics and visible on-screen words. Preserve an existing `{\\an8}` prefix exactly; add it to visible signs, papers, screens, labels, title cards, and messages after removing descriptive wrappers.
- If an intentional pun cannot survive natural Chinese, keep the literal translation in `text` and add one `pun_note`. Explain only the lost relationship, use no source-language words, keep it to one line and at most 32 Chinese characters, and add no `双关`, `注`, or `译注` prefix. Prefer `“芬兰语”与“完成”同音`.
- Preserve information-reveal order at every cue boundary. Example: `At the end of the corridor... / holding the gun... / was Anna` becomes `在走廊尽头... / 有个人拿着那把枪... / 她就是安娜`, without naming Anna early.
- Do not end lines with `。` or a single `.`, keep commas, and write ellipses as exactly `...`.""",
    curator_instructions="""- Maintain target terms in Simplified Chinese and preserve Iris's proposed target exactly when accepting a new entry.
- Never add commonplace standalone first names, real countries, or well-known real cities. Full names, uncommon names, fictional places, organizations, titles, nicknames, and story-specific terms may qualify.
- Finish with a short Chinese summary.""",
)
