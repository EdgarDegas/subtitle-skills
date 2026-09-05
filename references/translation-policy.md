# zh-Hans language profile policy

Read this reference for Simplified Chinese translation or wording review. The runtime counterpart is `src/codex_subtitles/language_profiles/zh_hans.py`; generic workflow modules must not duplicate these language-specific rules.

## CC, SDH, lyrics, and visible text

Prefer a full non-forced embedded English CC/SDH text track. Fall back to a matching English sidecar, another matching sidecar, then the best embedded text track. Do not OCR bitmap subtitles or transcribe audio.

- Remove speaker labels such as `MAN 2:`, `BETSY:`, `LARS & CAL:`, and `BOTH:` while keeping the dialogue. Iris performs this semantic edit; the script only detects residue.
- Delete a pure non-speech cue with `drop=true`. In a mixed cue, remove only laughter or sound fragments such as `Heh`, `Ha-ha`, `Ho-ho`, `呵呵`, or `哈哈` and keep dialogue.
- Remove applause, footsteps, doors, ringing, and similar audio descriptions.
- Keep and translate actual song lyrics. Lyrics are often absent from ordinary dialogue tracks, so they are not disposable music descriptions. Position them at the top with `{\an8}`.
- Keep and translate words visibly shown on signs, papers, screens, labels, messages, and title cards. Remove wrappers such as `A sign reads` or `on the paper`, leave only the visible words, and position them at the top.

Lyrics and visible text already present in the source are ordinary source-aligned cue translations, not `additions`.

## Lost puns

When an intentional second meaning cannot survive natural Chinese, keep the ordinary or literal translation at the bottom and attach one `pun_note` addition. Its rendered top cue:

- explains only the lost relationship needed to understand the line or reaction
- may use any language when that explains the lost relationship most clearly
- is one line and at most 32 characters
- never starts with `Pun:`, `双关`, `注`, `译注`, or another explanatory label
- remains slightly longer than the main cue for reading time

For `Finnish` / `finish`, use:

```text
{\an8}“芬兰语”与“完成”同音
```

Do not invent a confusing Chinese pun and do not teach the source language. Iris handles laughter removal and omission of explanatory prefixes; these are prompt instructions, not word-list validators.

## Information-reveal order

At every playback moment, Chinese viewers should know no more key plot information than source-language viewers. Natural Chinese syntax may change, but never move a later name, relationship, action, evidence, or request into an earlier cue.

Example:

```text
At the end of the corridor...
holding the gun we had been looking for...
was Anna
```

Use:

```text
在走廊尽头...
有个人拿着我们一直在找的枪...
她就是安娜
```

Do not name Anna in the first cue.

Likewise:

```text
He gave me a key...
to the basement...
where they were holding your son
```

Use:

```text
他给了我一把钥匙...
是地下室的...
你儿子就被他们关在里面
```

Use temporary references such as `有人`, `有张照片`, `那个人`, or `里面` when Chinese front-loaded modifiers would reveal later information early.

## Relationships and reference context

Use Atlas's episode references to resolve distinctions English may omit, such as
哥哥/弟弟, 姐姐/妹妹, 堂/表 relatives and maternal/paternal relations. Identify the
speaker, addressee and referent before choosing the term. `Brother` alone does not
establish age order; neither do actor ages or cast ordering. A younger sibling saying
“I'm a bad brother” refers to himself as 弟弟, while accusing his older sibling refers
to 哥哥. Where the relationship or referent is unresolved, use natural neutral wording.
Background facts never authorize revealing a later identity or relationship early.

## Punctuation and readability

- Remove a trailing Chinese `。` or single `.` from each subtitle line
- Keep commas
- Normalize `……`, `…`, and repeated dots to exactly `...`
- Keep lines concise and natural for on-screen reading
- Do not leave bilingual duplication, protocol labels, commentary, or Markdown in subtitle text
