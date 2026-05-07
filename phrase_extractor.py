"""
Phrase extraction from underlined transcript annotations → Speaker Phrase Library.

Sends all underlines in a single Claude call to produce structured entries:
Phrase (clean), Type, Function, Register, Context quote, Meaning (EN), 解释（中文）.
"""
from __future__ import annotations
import json
import logging
import os

import anthropic
from rich.console import Console as _Console
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from models import AnnotatedSegment

log = logging.getLogger(__name__)
_console = _Console()

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8000
BATCH_SIZE = 10

_SYSTEM = """\
You are a phrase extraction and classification assistant for public speaking and interview preparation.

You receive underlined phrases from a podcast transcript with their surrounding context.
Return a JSON array — one object per phrase, in the same order as input.

━━━ TRUNCATION CLEANUP → phrase_clean ━━━
- Trim whitespace
- Fix obvious transcript artifacts (broken words) when unambiguous
- Remove leading cut-off fragments when the underline starts mid-word (keep the rest):
    "s two competing priorities"        → "two competing priorities"
    "o be the right amount of AGI pilled" → "be the right amount of AGI pilled"
    "e demos are amazing..."            → "the demos are amazing..."
    "code ca"                           → "code can"
    "add more safeguards so t"          → "add more safeguards so that"
- Do NOT normalize style or improve beyond fixing obvious cutoffs

━━━ CLASSIFICATION ━━━
type — exactly one:
  Verb phrase | Collocation | Fixed phrase | Discourse marker | Single word | Other

function — one or more:
  Prioritization & tradeoffs | Process & execution | Metrics & evaluation |
  Stakeholders & alignment | Strategy & vision | Risk & governance |
  Leadership tone / vibe | Interview-ready framing | Filler / optional

register — exactly one:
  Formal | Neutral | Casual

━━━ CONTEXT QUOTE ━━━
1–2 sentences including the speaker name and the phrase in context.
Format: Speaker Name: "...quote containing the phrase..."
Extract speaker from the context (look for patterns like "[timestamp]  Name" before the text).
If speaker cannot be determined, use "Speaker".

━━━ MEANING ━━━
meaning_en: 1–2 sentences, plain English, no jargon
meaning_zh: 1–2 sentences, concise accurate Chinese

━━━ CONSTRAINTS ━━━
- Return ONLY a valid JSON array. No markdown fences, no commentary.
- One object per phrase, same order as input.
- Do not fabricate content not present in the context.
- NEVER use ASCII double-quote characters (") inside string values — they break JSON.
  In meaning_zh, use 「」 for quoting terms (e.g. 「待完成工作」not "待完成工作").
  In context_quote, escape any double quotes that appear inside the string as \".

━━━ JSON SCHEMA ━━━
[
  {
    "phrase_clean": "",
    "type": "",
    "function": [],
    "register": "",
    "context_quote": "",
    "meaning_en": "",
    "meaning_zh": ""
  }
]
"""


def _build_user_message(
    underlines: list[AnnotatedSegment],
    context_map: dict[int, str],
    podcast_title: str,
) -> str:
    parts = [
        f"## Source: {podcast_title}",
        f"## Phrases to process: {len(underlines)}",
        "",
    ]
    for i, seg in enumerate(underlines, 1):
        anchor = seg.nearest_anchor.label if seg.nearest_anchor else "unknown"
        ctx = context_map.get(seg.paragraph_index, "")
        parts.append(f"### Phrase {i}  [{anchor}]")
        parts.append(f'Raw: "{seg.text}"')
        if ctx:
            parts.append(f"Context:\n{ctx}")
        parts.append("")
    return "\n".join(parts)


@retry(
    retry=retry_if_exception_type(anthropic.APIError),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(4),
    reraise=True,
)
def _call_claude(user_message: str) -> str:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def _parse_batch(raw: str, batch: list[AnnotatedSegment], doc_url: str) -> list[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    raw = raw.replace("\\'", "'")  # Claude sometimes escapes single quotes, invalid in JSON
    data = json.loads(raw)
    entries = []
    for i, item in enumerate(data):
        seg = batch[i] if i < len(batch) else None
        entries.append({
            "phrase_raw":    seg.text if seg else "",
            "phrase_clean":  item.get("phrase_clean", ""),
            "type":          item.get("type", ""),
            "function":      item.get("function", []),
            "register":      item.get("register", ""),
            "context_quote": item.get("context_quote", ""),
            "meaning_en":    item.get("meaning_en", ""),
            "meaning_zh":    item.get("meaning_zh", ""),
            "doc_url":       doc_url,
        })
    return entries


def extract_phrases(
    underlines: list[AnnotatedSegment],
    context_map: dict[int, str],
    podcast_title: str,
    doc_url: str,
) -> list[dict]:
    """Classify all underlined segments via Claude in batches. Returns one dict per phrase."""
    if not underlines:
        return []

    log.info("Extracting %d underlined phrases for '%s'", len(underlines), podcast_title)

    batches = [underlines[i: i + BATCH_SIZE] for i in range(0, len(underlines), BATCH_SIZE)]
    all_entries: list[dict] = []

    for idx, batch in enumerate(batches, 1):
        label = f"[dim]Extracting phrases batch {idx}/{len(batches)} ({len(batch)} phrases)…[/dim]"
        user_msg = _build_user_message(batch, context_map, podcast_title)
        with _console.status(label, spinner="dots"):
            raw = _call_claude(user_msg)
        all_entries.extend(_parse_batch(raw, batch, doc_url))

    log.info("Phrase extraction complete: %d entries", len(all_entries))
    return all_entries
