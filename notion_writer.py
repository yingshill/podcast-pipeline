"""
Notion output — writes AnalysisReport as a structured page matching report_template.md.

Key constraints:
- Notion text blocks have a 2,000-character limit → split long text at sentence boundaries
- Page creation accepts max 100 top-level children → append remaining blocks in batches
- Table rows and toggle children are nested inside their parent block (don't count toward the 100 limit)
"""
from __future__ import annotations
import logging
import os
from datetime import date

from notion_client import Client
from notion_client.errors import APIResponseError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from models import AnalysisReport, Chapter, ConceptCard, KeyInsight, HostQuestion

log = logging.getLogger(__name__)

BLOCK_CHAR_LIMIT = 1900


def _client() -> Client:
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise EnvironmentError("NOTION_TOKEN not set")
    return Client(auth=token)


# ── Primitive block builders ──────────────────────────────────────────────────

def _rt(text: str, url: str | None = None, bold: bool = False, italic: bool = False) -> dict:
    rt: dict = {"type": "text", "text": {"content": text}}
    if url:
        rt["text"]["link"] = {"url": url}
    if bold or italic:
        rt["annotations"] = {"bold": bold, "italic": italic}
    return rt


def _paragraph(rich_text: list[dict]) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text}}


def _h(text: str, level: int = 2) -> dict:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": [_rt(text)]}}


def _bullet(text: str, url: str | None = None) -> dict:
    return {
        "object": "block", "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [_rt(text, url=url)]},
    }


def _numbered(text: str) -> dict:
    return {
        "object": "block", "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": [_rt(text)]},
    }


def _todo(text: str, checked: bool = False) -> dict:
    return {
        "object": "block", "type": "to_do",
        "to_do": {"rich_text": [_rt(text)], "checked": checked},
    }


def _quote(text: str) -> dict:
    return {"object": "block", "type": "quote", "quote": {"rich_text": [_rt(text)]}}


def _callout(text: str, emoji: str = "💡") -> dict:
    return {
        "object": "block", "type": "callout",
        "callout": {
            "rich_text": [_rt(text)],
            "icon": {"type": "emoji", "emoji": emoji},
        },
    }


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _toggle(text: str, children: list[dict]) -> dict:
    return {
        "object": "block", "type": "toggle",
        "toggle": {"rich_text": [_rt(text)], "children": children},
    }


def _table(headers: list[str], rows: list[list[str]]) -> dict:
    def _cell(text: str) -> list[dict]:
        return [_rt(text)]

    header_row = {
        "object": "block", "type": "table_row",
        "table_row": {"cells": [_cell(h) for h in headers]},
    }
    data_rows = [
        {
            "object": "block", "type": "table_row",
            "table_row": {"cells": [_cell(c) for c in row]},
        }
        for row in rows
    ]
    return {
        "object": "block", "type": "table",
        "table": {
            "table_width": len(headers),
            "has_column_header": True,
            "has_row_header": False,
            "children": [header_row] + data_rows,
        },
    }


# ── Text splitting ────────────────────────────────────────────────────────────

def _split(text: str, limit: int = BLOCK_CHAR_LIMIT) -> list[str]:
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
            cut += 1
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def _paragraphs(text: str) -> list[dict]:
    return [_paragraph([_rt(chunk)]) for chunk in _split(text)]


# ── Section builders ──────────────────────────────────────────────────────────

def _consumption_guide() -> list[dict]:
    guide = (
        "Consumption Guide — Read in this order, not top-to-bottom\n\n"
        "Phase 1 — Rebuild Context (3–5 min, passive)\n"
        "1. Episode Summary → 30 sec to recover the arc\n"
        "2. Chapter Breakdown table → 2 min to reconstruct the structure\n"
        "3. Concept Map → 1 min to re-anchor the vocabulary\n\n"
        "Phase 2 — Engage with Insights (10–15 min, active)\n"
        "4. Key Insights table → Read Quote + Insight columns. Fill Your Take per row.\n"
        "5. Your Reflection section → Fill in order: Big Claim → Questions → What I'll Try → Pushback\n\n"
        "Phase 3 — Targeted Dive (only if needed)\n"
        "6. Chapter Detail Notes → Find the specific chapter's key quote + factual anchor\n"
        "7. Transcript → Ctrl+F for a specific phrase. This is a reference, never a starting point."
    )
    return [_callout(guide, "⏱️"), _divider()]


def _transcript_section(doc_url: str) -> list[dict]:
    return [
        _h("📋 Transcript", level=1),
        _paragraph([_rt("Open transcript: "), _rt(doc_url, url=doc_url)]),
        _divider(),
    ]


def _episode_summary_section(summary: str) -> list[dict]:
    return [_h("📖 Episode Summary", level=2)] + _paragraphs(summary) + [_divider()]


def _concept_map_section(concepts: list[ConceptCard]) -> list[dict]:
    rows = [[c.concept, c.explanation] for c in concepts]
    return [
        _h("🗺️ Concept Map", level=2),
        _paragraph([_rt("One flashcard per key concept. Front = concept name, Back = plain-English explanation.")]),
        _table(["Concept 🃏", "Explanation 🤖"], rows),
        _divider(),
    ]


def _chapter_breakdown_section(chapters: list[Chapter]) -> list[dict]:
    # Summary table
    table_rows = [
        [f"{ch.start} – {ch.end}", ch.title, ch.summary]
        for ch in chapters
    ]
    summary_table = _table(["Timestamp", "Chapter Title", "2-line Summary"], table_rows)

    # Toggle: Chapter Detail Notes
    detail_children: list[dict] = [
        _callout(
            "How to read this section: Each chapter contains all topics. "
            "Each topic has: key quote, related concept, why it matters, factual anchor. "
            "Use for Phase 3 targeted dives.",
            "📖",
        )
    ]
    for i, ch in enumerate(chapters, 1):
        detail_children.append(_h(f"Chapter {i} — {ch.title}", level=3))
        for j, topic in enumerate(ch.topics, 1):
            detail_children.append(_paragraph([_rt(f"Topic {i}.{j} — {topic.title}", bold=True)]))
            if topic.key_quote:
                detail_children.append(_bullet(f'Key quote: "{topic.key_quote}"'))
            if topic.related_concept:
                detail_children.append(_bullet(f"Related concept: {topic.related_concept}"))
            if topic.why_it_matters:
                detail_children.append(_bullet(f"Why it matters: {topic.why_it_matters}"))
            if topic.factual_anchor:
                detail_children.append(_bullet(f"Factual anchor: {topic.factual_anchor}"))

    toggle = _toggle("📌 Chapter & Topic Detail Notes (expand for structured drill-down)", detail_children)

    return [
        _h("📑 Chapter Breakdown", level=2),
        _paragraph([_rt("Podwise-style table of contents from transcript timestamps.")]),
        summary_table,
        toggle,
        _divider(),
    ]


def _key_insights_section(insights: list[KeyInsight]) -> list[dict]:
    rows = [[ki.timestamp, f'"{ki.quote}"', ki.insight, ""] for ki in insights]
    return [
        _h("💡 Key Insights", level=2),
        _paragraph([_rt(
            "Timestamp, Direct Quote, and Insight are auto-generated from your annotations. "
            "Fill Your Take ✏️ column only — then complete Your Reflection below."
        )]),
        _table(["Timestamp", "Direct Quote", "Insight 🤖", "Your Take ✏️"], rows),
        _divider(),
    ]


def _host_questions_section(questions: list[HostQuestion]) -> list[dict]:
    rows = [[q.question, q.category] for q in questions]
    return [
        _h("Host Question List and Category", level=2),
        _table(["Question", "Category"], rows),
        _divider(),
    ]


def _your_reflection_section(insights: list[KeyInsight]) -> list[dict]:
    blocks: list[dict] = [
        _h("✏️ Your Reflection", level=1),
        _callout(
            "This is the only section you need to write. "
            "5 things. Everything above was auto-generated — this is what makes it yours. "
            "Order: Big Claim → Your Take (in Key Insights above) → Questions → What I'll Try → Pushback",
            "✏️",
        ),
        _h("🎯 1. Big Claim", level=2),
        _paragraph([_rt("The episode's central argument in one sentence. Not the topic — the thesis.")]),
        _paragraph([_rt("Claim: ")]),
        _paragraph([_rt("Why this matters right now: ")]),
        _divider(),
        _h("💬 2. Your Take (on Key Insights)", level=2),
        _paragraph([_rt("Go back to the Key Insights table and fill the Your Take column for each row. Come back here when done.")]),
    ]
    for i in range(1, len(insights) + 1):
        blocks.append(_todo(f"Your Take filled for Insight {i}"))
    blocks += [
        _divider(),
        _h("❓ 3. Questions This Raised", level=2),
        _paragraph([_rt("What do you want to dig into further? These become future entries or research threads.")]),
        _numbered(""),
        _numbered(""),
        _numbered(""),
        _divider(),
        _h("🔁 4. What I'll Try", level=2),
        _paragraph([_rt("1–3 first-person commitments. Specific enough to act on this week.")]),
        _todo("Try 1: (what exactly) — by: (date) — connects to: (project/skill)"),
        _todo("Try 2: "),
        _todo("Try 3: (optional)"),
        _divider(),
        _h("⚡ 5. Pushback", level=2),
        _paragraph([_rt("Where do you disagree or see a gap? This is your signal that you're thinking, not just transcribing.")]),
        _paragraph([_rt("The claim I'm pushing back on: ")]),
        _paragraph([_rt("My counter-argument: ")]),
        _paragraph([_rt("Evidence or reasoning: ")]),
        _divider(),
    ]
    return blocks


def _connects_to_section() -> list[dict]:
    return [
        _h("🔗 Connects To", level=2),
        _callout("Agent-filled section. Brainstorm will propose connections after reading transcript. Do not fill manually.", "🤖"),
        _paragraph([_rt("Pending review — agent will propose connections after reading transcript.")]),
        _bullet("Related entry in this DB: (pending)"),
        _bullet("Related Project: (pending)"),
        _bullet("Contradicts or extends: (pending)"),
        _bullet("Updates mental model: (pending)"),
        _divider(),
    ]


def _source_metadata_section(report: AnalysisReport) -> list[dict]:
    meta = report.source_metadata
    guests_str = ", ".join(meta.guests) if meta.guests else "—"
    tags_str = ", ".join(meta.tags) if meta.tags else "—"
    return [
        _h("📊 Source Metadata", level=2),
        _paragraph([_rt(f"Episode: {report.podcast_title}  —  Show: {meta.show}  —  Guest(s): {guests_str}  —  Host: {meta.host}")]),
        _paragraph([_rt(f"Tags: {tags_str}  —  Eval: {meta.eval_metric}  —  Action: {meta.action}")]),
        _paragraph([_rt(f"Core Insight: {meta.core_insight}")]),
        _paragraph([_rt(f"Why It Matters: {meta.why_it_matters}")]),
    ]


# ── Page creation ─────────────────────────────────────────────────────────────

def _build_properties(report: AnalysisReport) -> dict:
    meta = report.source_metadata
    props: dict = {
        "Title": {"title": [{"text": {"content": report.podcast_title}}]},
        "URL": {"url": report.source_url},
        "Status": {"status": {"name": "Queued"}},
    }
    if meta.source:
        props["Source"] = {"select": {"name": meta.source}}
    if meta.tags:
        props["Tags"] = {"multi_select": [{"name": t} for t in meta.tags]}
    if meta.eval_metric:
        props["Eval Metric"] = {"select": {"name": meta.eval_metric}}
    if meta.action:
        props["Action"] = {"select": {"name": meta.action}}
    if meta.output:
        props["Output"] = {"multi_select": [{"name": o} for o in meta.output]}
    if meta.core_insight:
        props["Core Insight"] = {"rich_text": [{"text": {"content": meta.core_insight[:2000]}}]}
    if meta.why_it_matters:
        props["Why It Matters"] = {"rich_text": [{"text": {"content": meta.why_it_matters[:2000]}}]}
    return props


def _build_all_blocks(report: AnalysisReport) -> list[dict]:
    blocks: list[dict] = []
    blocks += _consumption_guide()
    blocks += _transcript_section(report.doc_url)
    blocks += _episode_summary_section(report.episode_summary)
    if report.concept_map:
        blocks += _concept_map_section(report.concept_map)
    if report.chapter_breakdown:
        blocks += _chapter_breakdown_section(report.chapter_breakdown)
    if report.key_insights:
        blocks += _key_insights_section(report.key_insights)
    if report.host_questions:
        blocks += _host_questions_section(report.host_questions)
    blocks += _your_reflection_section(report.key_insights)
    blocks += _connects_to_section()
    blocks += _source_metadata_section(report)
    return blocks


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
        children=children[:100],
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
    """Creates a Notion page and returns (page_id, page_url)."""
    db_id = os.environ.get("NOTION_DATABASE_ID")
    if not db_id:
        raise EnvironmentError("NOTION_DATABASE_ID not set")

    client = _client()
    properties = _build_properties(report)
    all_blocks = _build_all_blocks(report)

    page = _create_page(client, db_id, properties, all_blocks[:100])
    page_id: str = page["id"]
    page_url: str = page.get("url", f"https://notion.so/{page_id.replace('-', '')}")

    for i in range(100, len(all_blocks), 100):
        _append_blocks(client, page_id, all_blocks[i: i + 100])

    log.info("Notion page created: %s (%d blocks)", page_url, len(all_blocks))
    return page_id, page_url
