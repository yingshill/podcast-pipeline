"""
Transcript acquisition — two sources, zero AI tokens consumed here.

  YouTube URL  →  youtube-transcript-api  (captions, free, ~1 sec)
  Podcast URL  →  requests + BeautifulSoup (HTML transcript, free, ~2 sec)

Both return list[TranscriptSegment] so the rest of the pipeline is source-agnostic.
Timestamps:
  - YouTube: exact ms from the captions API
  - HTML: estimated from cumulative word count at 150 wpm (no audio data available)
Speaker labels:
  - YouTube: detected from embedded patterns (e.g. "[Alice]:" or "Alice:") when present;
             falls back to "Speaker" if captions have no labels
  - HTML: parsed from common podcast transcript formats (bold name, colon-delimited, etc.)
"""
from __future__ import annotations
import json
import logging
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup, Tag
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    NoTranscriptFound,
    TranscriptsDisabled,
)

from models import TranscriptSegment
from cleaner import clean_segments

log = logging.getLogger(__name__)

_YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/watch\?.*v=|youtu\.be/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})"
)

# Approximate speaking rate used for HTML timestamp estimation
_WORDS_PER_MS = 150 / 60_000   # 150 words/min → words per millisecond

# YouTube: merge raw caption chunks into groups of this many seconds
_YT_MERGE_WINDOW_S = 30.0

# HTML: try these CSS selectors in order to locate the transcript block
_TRANSCRIPT_SELECTORS = [
    ".transcript",
    "#transcript",
    '[class*="transcript"]',
    '[id*="transcript"]',
    '[class*="episode-content"]',
    '[class*="show-notes"]',
    "article",
    "main",
]

# Speaker detection regex — matches lines like:
#   "Alice:", "Alice (Host):", "[Alice]:", "ALICE:", "00:05 Alice:"
_SPEAKER_RE = re.compile(
    r"^\s*(?:\[?\d+:\d+(?::\d+)?\]?\s*)?"   # optional timestamp prefix
    r"(?:\[([A-Z][^]]{1,40})\]|([A-Z][A-Za-z .'-]{1,40}?))"  # [Name] or Name
    r"\s*(?:\([^)]{1,30}\))?"                # optional (role)
    r"\s*:\s+",                              # colon + space
    re.MULTILINE,
)

# Timestamp pattern embedded in HTML transcript text: [00:05:23] or (00:05:23)
_TS_RE = re.compile(r"[\[(](\d{1,2}):(\d{2})(?::(\d{2}))?[\])]")


# ── Public API ────────────────────────────────────────────────────────────────

def scrape_episode_metadata(url: str) -> dict:
    """Scrape episode title, show name, and guest info from page meta tags and JSON-LD.
    Returns a flat dict of whatever fields were found. Never raises.
    """
    if _youtube_video_id(url):
        return {}
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.warning("Metadata scrape failed for %s: %s", url, exc)
        return {}

    meta: dict = {}

    for og_prop, key in [
        ("og:title",       "og_title"),
        ("og:site_name",   "og_site_name"),
        ("og:description", "og_description"),
        ("og:image",       "og_image"),
    ]:
        tag = soup.find("meta", property=og_prop)
        if tag and tag.get("content"):
            meta[key] = tag["content"].strip()

    title_tag = soup.find("title")
    if title_tag:
        meta["page_title"] = title_tag.get_text(strip=True)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0] if data else {}
            dtype = data.get("@type", "")
            if dtype in ("PodcastEpisode", "Episode", "Article", "NewsArticle", "WebPage"):
                if data.get("name"):
                    meta["jsonld_name"] = data["name"]
                if data.get("description"):
                    meta["jsonld_description"] = str(data["description"])[:300]
                author = data.get("author") or data.get("creator")
                if isinstance(author, dict):
                    meta["jsonld_author"] = author.get("name", "")
                elif isinstance(author, list) and author:
                    meta["jsonld_author"] = ", ".join(
                        a.get("name", "") for a in author if isinstance(a, dict)
                    )
                series = data.get("partOfSeries") or data.get("partOf")
                if isinstance(series, dict) and series.get("name"):
                    meta["jsonld_series"] = series["name"]
                break
        except Exception:
            pass

    # Strip " | Site Name" / " - Site Name" suffix from og_title when we have og_site_name
    if meta.get("og_title") and meta.get("og_site_name"):
        site = meta["og_site_name"]
        for sep in (f" | {site}", f" - {site}", f" — {site}"):
            if meta["og_title"].endswith(sep):
                meta["og_title"] = meta["og_title"][: -len(sep)].strip()
                break

    log.info("Scraped metadata for %s: fields=%s", url, list(meta.keys()))
    return meta


_EXT_ALIASES = {".jpe": ".jpg", ".jpeg": ".jpg"}


def download_cover(url: str, dest_path: str) -> bool:
    """Download an image from url and save to dest_path. Returns True on success."""
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        ext = mimetypes.guess_extension(content_type) or ".jpg"
        ext = _EXT_ALIASES.get(ext, ext)
        final_path = dest_path if dest_path.endswith(ext) else dest_path + ext
        Path(final_path).write_bytes(resp.content)
        log.info("Cover downloaded: %s (%d bytes)", final_path, len(resp.content))
        return True
    except Exception as exc:
        log.warning("Cover download failed for %s: %s", url, exc)
        return False


def transcribe(url: str) -> tuple[list[TranscriptSegment], str]:
    """
    Route URL to the appropriate scraper.
    Returns (segments, source_tag) where source_tag is 'youtube' or 'html'.
    Raises RuntimeError if transcript cannot be retrieved.
    """
    vid = _youtube_video_id(url)
    if vid:
        log.info("YouTube URL detected — fetching captions for %s", vid)
        segments = _scrape_youtube(vid)
    else:
        log.info("Podcast URL detected — scraping HTML transcript from %s", url)
        segments = _scrape_html(url)

    source = "youtube" if vid else "html"
    cleaned = clean_segments(segments)
    log.info("Cleaning: %d → %d segments (removed %d)", len(segments), len(cleaned), len(segments) - len(cleaned))
    return cleaned, source


# ── YouTube ───────────────────────────────────────────────────────────────────

def _youtube_video_id(url: str) -> Optional[str]:
    m = _YOUTUBE_RE.search(url)
    return m.group(1) if m else None


def _scrape_youtube(video_id: str) -> list[TranscriptSegment]:
    try:
        entries = _fetch_youtube_transcript(video_id)
    except TranscriptsDisabled:
        raise RuntimeError(
            f"Transcripts are disabled for this YouTube video ({video_id}). "
            "Try a different episode or source."
        )
    except NoTranscriptFound:
        raise RuntimeError(
            f"No English transcript found for YouTube video ({video_id}). "
            "The video may not have captions."
        )

    merged = _merge_youtube_chunks(entries)
    segments = _assign_speakers_youtube(merged)
    log.info("YouTube: %d segments from %d raw caption chunks", len(segments), len(entries))
    return segments


def _fetch_youtube_transcript(video_id: str) -> list[dict]:
    """Prefer manual EN captions; fall back to auto-generated EN. v1.x API."""
    api = YouTubeTranscriptApi()
    transcript_list = api.list(video_id)

    for is_generated in (False, True):
        for t in transcript_list:
            if t.language_code.startswith("en") and t.is_generated == is_generated:
                return _snippets_to_dicts(t.fetch())

    # Last resort: any available language
    for t in transcript_list:
        return _snippets_to_dicts(t.fetch())

    raise NoTranscriptFound(video_id, ["en"], {})


def _snippets_to_dicts(fetched) -> list[dict]:
    """Convert FetchedTranscript snippets (v1.x objects) to plain dicts."""
    return [
        {"text": s.text, "start": s.start, "duration": s.duration}
        for s in fetched.snippets
    ]


def _merge_youtube_chunks(entries: list[dict]) -> list[dict]:
    """
    YouTube captions arrive as 1-3 second chunks.
    Merge into ~30-second windows for readability.
    """
    if not entries:
        return []

    groups: list[dict] = []
    window_start = entries[0]["start"]
    window_texts: list[str] = []
    window_end = 0.0

    for entry in entries:
        if entry["start"] - window_start > _YT_MERGE_WINDOW_S and window_texts:
            groups.append({
                "start": window_start,
                "end": window_end,
                "text": " ".join(window_texts),
            })
            window_start = entry["start"]
            window_texts = []

        window_texts.append(entry["text"].strip())
        window_end = entry["start"] + entry.get("duration", 0)

    if window_texts:
        groups.append({
            "start": window_start,
            "end": window_end,
            "text": " ".join(window_texts),
        })

    return groups


def _assign_speakers_youtube(groups: list[dict]) -> list[TranscriptSegment]:
    """
    Try to detect speaker labels from merged text.
    Many podcast YouTube uploads embed "Name: ..." patterns in their captions.
    Falls back to 'Speaker' if no labels found.
    """
    segments: list[TranscriptSegment] = []
    current_speaker = "Speaker"

    for g in groups:
        text = g["text"]
        start_ms = int(g["start"] * 1000)
        end_ms = int(g["end"] * 1000)

        # Check if this chunk starts with a speaker label
        m = _SPEAKER_RE.match(text)
        if m:
            current_speaker = (m.group(1) or m.group(2)).strip().title()
            text = text[m.end():].strip()

        if text:
            segments.append(TranscriptSegment(
                start_ms=start_ms,
                end_ms=end_ms,
                speaker=current_speaker,
                text=text,
            ))

    return segments


# ── HTML podcast sites ────────────────────────────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# Patterns that indicate media-player UI or page chrome — not transcript content
_NOISE_RE = re.compile(
    r"^(Current\s*Time|Total\s*[Tt]ime|Duration|Playback\s*Speed"
    r"|Subscribe|Share\s+this|Follow\s+on|Listen\s+on"
    r"|Apple\s+Podcasts?|Spotify|YouTube"
    r"|\d+:\d+\s*/\s*-?\d+:\d+)",  # "0:00 / -1:17:24" player display
    re.IGNORECASE,
)

# Minimum number of transcript-like blocks to consider the scrape valid.
# Show-notes pages typically have <20 substantial paragraphs; real transcripts have many more.
_MIN_TRANSCRIPT_BLOCKS = 30


def _scrape_html(url: str) -> list[TranscriptSegment]:
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    container = _find_transcript_container(soup)
    if not container:
        raise RuntimeError(
            f"Could not find a transcript section on {url}.\n"
            "The page may not have a posted transcript, or it may use a layout "
            "this scraper doesn't recognise. Check the URL or try a different source."
        )

    raw_blocks = _extract_text_blocks(container)
    segments = _parse_blocks_to_segments(raw_blocks)

    if not segments:
        raise RuntimeError(
            f"Found a transcript container on {url} but could not parse any segments. "
            "The transcript format may be unusual."
        )

    if len(segments) < _MIN_TRANSCRIPT_BLOCKS:
        log.warning(
            "Only %d segments found — this may be show notes rather than a full transcript. "
            "Check whether the transcript is behind a paywall or on a separate page.",
            len(segments),
        )

    log.info("HTML: %d segments parsed from %s", len(segments), url)
    return segments


def _find_transcript_container(soup: BeautifulSoup) -> Optional[Tag]:
    """Try selectors in priority order; return the first match with enough content."""
    for selector in _TRANSCRIPT_SELECTORS:
        el = soup.select_one(selector)
        if el and len(el.get_text(strip=True)) > 500:
            log.debug("Transcript container found via selector: %s", selector)
            return el
    return None


def _is_noise_block(text: str) -> bool:
    """Return True for media-player labels, navigation, and other non-transcript chrome."""
    return bool(_NOISE_RE.match(text.strip()))


def _extract_text_blocks(container: Tag) -> list[str]:
    """
    Pull text blocks out of the container, filtering media-player noise.
    Respects paragraph / div boundaries so speaker labels stay with their text.
    """
    blocks: list[str] = []
    for el in container.find_all(["p", "div", "li", "span"], recursive=True):
        # Skip containers that hold other block elements (avoid double-counting)
        if el.find(["p", "div", "li"]):
            continue
        text = el.get_text(separator=" ", strip=True)
        if len(text) > 20 and not _is_noise_block(text):
            blocks.append(text)
    if not blocks:
        # Fallback: split by newlines from raw text
        raw = container.get_text(separator="\n", strip=True)
        blocks = [
            l.strip() for l in raw.splitlines()
            if len(l.strip()) > 20 and not _is_noise_block(l.strip())
        ]
    return blocks


def _parse_blocks_to_segments(blocks: list[str]) -> list[TranscriptSegment]:
    """
    Parse text blocks into TranscriptSegments.

    Handles three formats (detected automatically):
      A) Embedded timestamps: "[00:05:23] Alice: text..."
      B) Speaker labels only: "Alice: text..."
      C) Plain paragraphs with no labels
    """
    has_timestamps = any(_TS_RE.search(b) for b in blocks)
    has_speakers = any(_SPEAKER_RE.match(b) for b in blocks)

    if has_timestamps:
        return _parse_with_timestamps(blocks)
    elif has_speakers:
        return _parse_with_speakers(blocks)
    else:
        return _parse_plain(blocks)


def _parse_with_timestamps(blocks: list[str]) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    current_speaker = "Speaker"

    for block in blocks:
        ts_match = _TS_RE.search(block)
        start_ms = _ts_to_ms(ts_match) if ts_match else 0
        text = _TS_RE.sub("", block).strip()

        sp_match = _SPEAKER_RE.match(text)
        if sp_match:
            current_speaker = (sp_match.group(1) or sp_match.group(2)).strip().title()
            text = text[sp_match.end():].strip()

        if text:
            segments.append(TranscriptSegment(
                start_ms=start_ms,
                end_ms=start_ms,    # end unknown from HTML; filled by next segment later
                speaker=current_speaker,
                text=text,
            ))

    _fill_end_times(segments)
    return segments


def _parse_with_speakers(blocks: list[str]) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    current_speaker = "Speaker"
    cumulative_words = 0

    for block in blocks:
        sp_match = _SPEAKER_RE.match(block)
        if sp_match:
            current_speaker = (sp_match.group(1) or sp_match.group(2)).strip().title()
            text = block[sp_match.end():].strip()
        else:
            text = block

        if not text:
            continue

        start_ms = int(cumulative_words / _WORDS_PER_MS)
        word_count = len(text.split())
        end_ms = int((cumulative_words + word_count) / _WORDS_PER_MS)
        cumulative_words += word_count

        segments.append(TranscriptSegment(
            start_ms=start_ms,
            end_ms=end_ms,
            speaker=current_speaker,
            text=text,
        ))

    return segments


def _parse_plain(blocks: list[str]) -> list[TranscriptSegment]:
    """No labels, no timestamps — assign estimated times only."""
    segments: list[TranscriptSegment] = []
    cumulative_words = 0

    for block in blocks:
        word_count = len(block.split())
        start_ms = int(cumulative_words / _WORDS_PER_MS)
        end_ms = int((cumulative_words + word_count) / _WORDS_PER_MS)
        cumulative_words += word_count
        segments.append(TranscriptSegment(
            start_ms=start_ms,
            end_ms=end_ms,
            speaker="Speaker",
            text=block,
        ))

    return segments


def _ts_to_ms(m: re.Match) -> int:
    h_or_m, mins, secs = m.group(1), m.group(2), m.group(3)
    if secs is None:
        return (int(h_or_m) * 60 + int(mins)) * 1000
    return (int(h_or_m) * 3600 + int(mins) * 60 + int(secs)) * 1000


def _fill_end_times(segments: list[TranscriptSegment]) -> None:
    """Set each segment's end_ms to the start of the next segment."""
    for i in range(len(segments) - 1):
        if segments[i].end_ms == segments[i].start_ms:
            segments[i].end_ms = segments[i + 1].start_ms
    # Last segment: estimate from word count
    if segments:
        last = segments[-1]
        word_count = len(last.text.split())
        last.end_ms = last.start_ms + int(word_count / _WORDS_PER_MS)
