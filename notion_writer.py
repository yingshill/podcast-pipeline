"""
Notion output — writes the AnalysisReport as a structured page in a Notion DB.

Key constraint: Notion text blocks have a 2,000-character limit.
We split long text at sentence boundaries before writing.

Each reference to a transcript section is written as an inline link:
  [MM:SS] → https://docs.google.com/document/d/{doc_id}/edit#heading=h.{heading_id}
"""
from __future__ import annotations
import logging
import os
import re
from datetime import date

from notion_client import Client
from notion_client.errors import APIResponseError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from models import AnalysisReport, HeadingAnchor, ReportSection

log = logging.getLogger(__name__)

BLOCK_CHAR_LIMIT = 1900  # conservative, under Notion's 2000


def _client() -> Client:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise EnvironmentError("NOTION_TOKEN not set")
    return Client(auth=token)


# ── Block builders ────────────────────────────────────────────────────────────

def _rich_text(text: str, url: str | None = None) -> dict:
    rt: dict = {"type": "text", "text": {"content": text}}
    if url:
        rt["text"]["link"] = {"url": url}
    return rt


def _paragraph_block(rich_text: list[dict]) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text}}


def _heading_block(text: str, level: int = 2) -> dict:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": [_rich_text(text)]}}


def _bullet_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [_rich_text(text)]},
    }


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _quote_block(text: str) -> dict:
    return {"object": "block", "type": "quote", "quote": {"rich_text": [_rich_text(text)]}}


def _callout_block(text: str, emoji: str = "🔗") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [_rich_text(text)],
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


# ── Text splitting ────────────────────────────────────────────────────────────

def _split_text(text: str, limit: int = BLOCK_CHAR_LIMIT) -> list[str]:
    """Split text into chunks under limit, breaking at sentence boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind(". ", 0, limit)
        if cut == -1:
            cut = text.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit
        else:
            cut += 1  # include the period
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


# ── Section renderer ──────────────────────────────────────────────────────────

def _render_section(section: ReportSection) -> list[dict]:
    blocks: list[dict] = [_heading_block(section.title, level=3)]

    # Body may contain a blockquote marker
    body = section.body
    quote_match = re.match(r'^> "(.+?)"(.*)$', body, re.DOTALL)
    if quote_match:
        blocks.append(_quote_block(quote_match.group(1)))
        remainder = quote_match.group(2).strip()
        if remainder:
            for chunk in _split_text(remainder):
                blocks.append(_paragraph_block([_rich_text(chunk)]))
    else:
        for chunk in _split_text(body):
            blocks.append(_paragraph_block([_rich_text(chunk)]))

    # Anchor link callout
    for anchor in section.anchors:
        label = f"↗ Transcript [{anchor.label}]"
        blocks.append(_callout_block(label, "🔗"))
        # Inline link paragraph
        blocks.append(_paragraph_block([
            _rich_text("Open in transcript: "),
            _rich_text(f"[{anchor.label}]", url=anchor.url),
        ]))

    return blocks


# ── Page creation ─────────────────────────────────────────────────────────────

@retry(
    retry=retry_if_exception_type(APIResponseError),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _create_page(client: Client, db_id: str, properties: dict, children: list[dict]) -> dict:
    return client.pages.create(
        parent={"database_id": db_id},
        properties=properties,
        children=children[:100],  # Notion accepts max 100 children on creation
    )


@retry(
    retry=retry_if_exception_type(APIResponseError),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _append_blocks(client: Client, page_id: str, children: list[dict]) -> None:
    client.blocks.children.append(block_id=page_id, children=children)


def write_report(report: AnalysisReport) -> tuple[str, str]:
    """
    Creates a Notion page in the configured DB and returns (page_id, page_url).
    Appends remaining blocks in batches if report exceeds 100 blocks.
    """
    db_id = os.environ.get("NOTION_DATABASE_ID")
    if not db_id:
        raise EnvironmentError("NOTION_DATABASE_ID not set")

    client = _client()

    properties = {
        "Name": {"title": [{"text": {"content": report.podcast_title}}]},
        "URL": {"url": report.source_url},
        "Analyzed": {"date": {"start": date.today().isoformat()}},
        "Transcript": {"url": report.doc_url},
    }

    all_blocks = _build_all_blocks(report)

    # Create page with first 100 blocks
    page = _create_page(client, db_id, properties, all_blocks[:100])
    page_id: str = page["id"]
    page_url: str = page.get("url", f"https://notion.so/{page_id.replace('-', '')}")

    # Append remaining blocks in batches of 100
    for i in range(100, len(all_blocks), 100):
        _append_blocks(client, page_id, all_blocks[i : i + 100])

    log.info("Notion page created: %s", page_url)
    return page_id, page_url


def _build_all_blocks(report: AnalysisReport) -> list[dict]:
    blocks: list[dict] = []

    # Header callout
    blocks.append(_callout_block(
        f"Source: {report.source_url}  |  Transcript: {report.doc_url}",
        emoji="🎙",
    ))
    blocks.append(_divider())

    # Executive summary
    blocks.append(_heading_block("Executive Summary", level=2))
    for chunk in _split_text(report.executive_summary):
        blocks.append(_paragraph_block([_rich_text(chunk)]))
    blocks.append(_divider())

    # Key themes
    if report.key_themes:
        blocks.append(_heading_block("Key Themes", level=2))
        for section in report.key_themes:
            blocks.extend(_render_section(section))
        blocks.append(_divider())

    # Resonance points (driven by human annotations)
    if report.resonance_points:
        blocks.append(_heading_block("What Resonated With You", level=2))
        blocks.append(_paragraph_block([_rich_text(
            "These sections are anchored to your highlights and underlines in the transcript."
        )]))
        for section in report.resonance_points:
            blocks.extend(_render_section(section))
        blocks.append(_divider())

    # Synthesis
    if report.synthesis:
        blocks.append(_heading_block("Synthesis", level=2))
        for chunk in _split_text(report.synthesis):
            blocks.append(_paragraph_block([_rich_text(chunk)]))
        blocks.append(_divider())

    # Action items
    if report.action_items:
        blocks.append(_heading_block("Follow-Ups & Open Questions", level=2))
        for item in report.action_items:
            blocks.append(_bullet_block(item))

    return blocks
