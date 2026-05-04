"""
Transcript cleaning protocols applied after scraping, before writing to Google Docs.

P1 — Remove caption noise tags  ([music], [laughter], etc.)
P2 — Collapse repeated words    (stutters: "and and" → "and")
P4 — Collapse back-channel affirmations  (>> Yeah. >> Yeah. → >> Yeah.)
P5 — Fix run-together number+unit words  (10ear → 10-year, 2year → 2-year)
P6 — Skip sponsor segments
"""
from __future__ import annotations
import re
from models import TranscriptSegment

# P1: YouTube auto-caption noise tags
_NOISE_TAG_RE = re.compile(
    r'\[(?:music|laughter|applause|noise|inaudible|cheering)[^\]]*\]',
    re.IGNORECASE,
)

# P2: Consecutive duplicate words (case-insensitive)
_STUTTER_RE = re.compile(r'\b(\w+)(\s+\1)+\b', re.IGNORECASE)

# P4: Consecutive back-channel affirmations — keep only the first occurrence
_BC_WORD = r'(?:Yeah|Mmhm|Mm-hmm|Okay|Right|Sure|Yep|Uh-huh)'
_BACKCHANNEL_RE = re.compile(
    r'(>>\s*' + _BC_WORD + r'\.?\s*)(?:>>\s*' + _BC_WORD + r'\.?\s*)+',
    re.IGNORECASE,
)

# P5: Number immediately joined to a time-unit word (no space or hyphen)
_NUMBER_UNIT_RE = re.compile(r'(\d+)(year|month|week|day|hour)s?\b', re.IGNORECASE)

# P6: Sponsor segment detection — phrases that reliably signal a sponsor read
_SPONSOR_PHRASES = [
    r'this episode is brought to you by',
    r'brought to you by',
    r'today\'?s? sponsor',
    r'promo code',
    r'use code .{2,20} (?:at|for)',
    r'apply online in minutes',
    r'no minimum balance',
    r'FDIC insured',
]
_SPONSOR_RE = re.compile('|'.join(_SPONSOR_PHRASES), re.IGNORECASE)


def clean_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    result = []
    for seg in segments:
        text = _strip_sponsor_tail(seg.text)
        if not text:
            continue  # entire segment was sponsor content
        cleaned = _clean_text(text)
        if cleaned:
            result.append(TranscriptSegment(
                start_ms=seg.start_ms,
                end_ms=seg.end_ms,
                speaker=seg.speaker,
                text=cleaned,
            ))
    return result


def _clean_text(text: str) -> str:
    text = _NOISE_TAG_RE.sub('', text)                      # P1
    text = _STUTTER_RE.sub(r'\1', text)                     # P2
    text = _BACKCHANNEL_RE.sub(r'\1', text)                 # P4
    text = _NUMBER_UNIT_RE.sub(r'\1-\2', text)              # P5
    text = re.sub(r'  +', ' ', text).strip()                # normalise whitespace
    return text


def _strip_sponsor_tail(text: str) -> str:
    """
    If a sponsor phrase appears in the first 120 chars, drop the whole segment.
    If it appears later, truncate at that point (real content precedes the ad read).
    """
    m = _SPONSOR_RE.search(text)
    if not m:
        return text
    if m.start() < 120:
        return ""   # whole segment is sponsor
    return text[:m.start()].strip()
