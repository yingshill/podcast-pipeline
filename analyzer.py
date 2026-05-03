"""
Claude analysis — operates only on annotated excerpts + context (~5K tokens).
Uses prompt caching on the system prompt to reduce cost on re-runs.

Output: structured AnalysisReport with per-section HeadingAnchor references.
"""
from __future__ import annotations
import json
import logging
import os

import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from models import AnnotatedSegment, AnalysisReport, HeadingAnchor, ReportSection

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096

_SYSTEM = """\
You are an expert podcast analyst. You receive:
1. Metadata about a podcast episode
2. Passages a human highlighted or underlined while reading the transcript — these are the moments that resonated most with them personally
3. Surrounding context (2 paragraphs before and after each annotation)

Your job is to produce a structured analytical report that:
- Centers on what the human found resonant (do NOT invent new "key points" the human didn't annotate)
- Synthesizes patterns across annotations (recurring themes, tensions, insights)
- Is concise and actionable — the human will use this as a reference, not re-read the whole thing
- Anchors each claim to a specific transcript moment using the provided anchor labels (e.g. "10:00")

Return ONLY valid JSON matching this exact schema:
{
  "executive_summary": "2-3 sentence overview of the episode through the lens of what the human found interesting",
  "key_themes": [
    {
      "title": "Theme title",
      "description": "1-2 sentences",
      "anchor_label": "MM:SS or HH:MM:SS of the most relevant annotation"
    }
  ],
  "resonance_points": [
    {
      "title": "Short label for this insight",
      "quote": "Exact or near-exact quote from the annotation",
      "analysis": "1-2 sentences: why this matters, what it connects to",
      "anchor_label": "MM:SS of this annotation"
    }
  ],
  "synthesis": "2-3 sentences connecting the resonance points into a bigger picture",
  "action_items": ["Specific follow-up or question worth exploring", "..."]
}
"""


def _build_user_message(
    podcast_title: str,
    source_url: str,
    annotated: list[AnnotatedSegment],
    context_map: dict[int, str],
) -> str:
    parts = [
        f"## Podcast: {podcast_title}",
        f"URL: {source_url}",
        "",
        "## Human Annotations",
        f"Total annotations: {len(annotated)}",
        "",
    ]

    for seg in annotated:
        anchor_label = seg.nearest_anchor.label if seg.nearest_anchor else "unknown"
        annotation_marker = "🟡 HIGHLIGHT" if seg.annotation_type == "highlight" else "__ UNDERLINE"
        parts.append(f"### [{anchor_label}] {annotation_marker}")
        parts.append(f'Annotated text: "{seg.text}"')
        ctx = context_map.get(seg.paragraph_index, "")
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
        system=[
            {
                "type": "text",
                "text": _SYSTEM,
                "cache_control": {"type": "ephemeral"},  # prompt caching
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def analyze(
    podcast_title: str,
    source_url: str,
    doc_url: str,
    annotated: list[AnnotatedSegment],
    context_map: dict[int, str],
    all_anchors: list[HeadingAnchor],
) -> AnalysisReport:
    """
    Runs Claude analysis. Returns a fully populated AnalysisReport.
    Raises ValueError if annotated is empty.
    """
    if not annotated:
        raise ValueError(
            "No annotations found. Please highlight or underline text in the Google Doc before running analysis."
        )

    log.info("Sending %d annotations to Claude (%d context entries)", len(annotated), len(context_map))
    user_msg = _build_user_message(podcast_title, source_url, annotated, context_map)
    raw = _call_claude(user_msg)

    # Strip markdown code fences if Claude wrapped the JSON
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    data = json.loads(raw)

    anchor_by_label = {a.label: a for a in all_anchors}

    def resolve_anchor(label: str) -> HeadingAnchor | None:
        if label in anchor_by_label:
            return anchor_by_label[label]
        # Fuzzy: find nearest label
        for a in all_anchors:
            if label in a.label or a.label in label:
                return a
        return all_anchors[0] if all_anchors else None

    key_themes = [
        ReportSection(
            title=t["title"],
            body=t["description"],
            anchors=[a for a in [resolve_anchor(t.get("anchor_label", ""))] if a],
        )
        for t in data.get("key_themes", [])
    ]

    resonance_points = [
        ReportSection(
            title=r["title"],
            body=f'> "{r["quote"]}"\n\n{r["analysis"]}',
            anchors=[a for a in [resolve_anchor(r.get("anchor_label", ""))] if a],
        )
        for r in data.get("resonance_points", [])
    ]

    report = AnalysisReport(
        podcast_title=podcast_title,
        source_url=source_url,
        doc_url=doc_url,
        executive_summary=data.get("executive_summary", ""),
        key_themes=key_themes,
        resonance_points=resonance_points,
        synthesis=data.get("synthesis", ""),
        action_items=data.get("action_items", []),
    )

    log.info(
        "Analysis complete: %d themes, %d resonance points",
        len(key_themes),
        len(resonance_points),
    )
    return report
