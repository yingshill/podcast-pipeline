"""
Claude analysis — operates on human annotations + chapter structure.
Uses prompt caching on the system prompt to reduce cost on re-runs.

Output: fully populated AnalysisReport matching report_template.md.
"""
from __future__ import annotations
import json
import logging
import os

import anthropic
from rich.console import Console as _Console
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

_console = _Console()

from models import (
    AnnotatedSegment, AnalysisReport, HeadingAnchor,
    SourceMetadata, ConceptCard, Chapter, ChapterTopic,
    KeyInsight, HostQuestion, Connection,
)

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8000

_anthropic: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _anthropic
    if _anthropic is None:
        _anthropic = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic

_SYSTEM = """\
You are an expert podcast analyst. You generate a structured notebook entry for a human reader.

You receive ONE of:
A) Annotation mode:
   1. Podcast title and URL
   2. Chapter structure — timestamp labels from the transcript (every ~10 min)
   3. Human-annotated passages — text the reader highlighted or underlined; these mark what personally resonated
   4. Context paragraphs surrounding each annotation
B) Full transcript mode:
   1. Podcast title and URL
   2. The complete transcript text (no annotations; analyze comprehensively)

━━━ SECTION RULES ━━━

source_metadata
  Use Episode Metadata (scraped from the source URL, provided below) as the primary source for
  show, guest(s), host, and episode title. Fall back to annotation content when metadata is absent.
  - episode_title: prefer jsonld_name or og_title; otherwise infer from annotations
  - show: prefer og_site_name or jsonld_series; otherwise infer from annotation content
  - guests: extract from jsonld_name / og_title (text before/after "with" or "|"); verify via annotations
  - tags: pick 1–3 from Available Notion Tags if they fit; otherwise create a new concise
          lowercase hyphenated tag (e.g. "product-strategy"). Notion creates new options automatically.
  - eval_metric: your honest assessment, pick exactly one from this exact list:
      🔥 Game Changer | 🧐 Interesting | 💤 Too Basic | 🗑️ Irrelevant | Good to know | ✅ Strong | 🛌 Sleep on it | ❓ Need more context | 👀 Watchlist
  - action: what the reader should do next, pick exactly one from this exact list:
      Try this week | Read deep | Bookmark | Skip
  - output: what this content could become, pick 1–3 from this exact list:
      LinkedIn Post | XHS Post | Turn into Project | Sunday Lab | Add to Zettelkasten | Share with Team | 🎨 Visualize | 📣 Project
  - topic_hub: 1–3 topic names from the provided Topic Hub list that best match this episode's content.
               Pick only from the exact names in the list. Empty list if none fit well.
  - core_insight: 1–2 sentences — the single most important takeaway from the episode
  - why_it_matters: 1–2 sentences — why a practitioner should care about this right now

episode_summary
  3–5 sentences covering: the arc of the conversation, the central argument or claim,
  and the conclusion or open question it leaves with. This is a passive re-orientation tool —
  the reader should be able to explain the episode to someone else after reading this.

concept_map
  3–5 key concepts introduced or heavily used in this episode.
  Each is a flashcard: concept = the term, explanation = plain-English 1–2 sentence
  definition a smart non-expert could immediately use.
  Draw from the annotations and context — do not invent concepts not discussed.

chapter_breakdown
  One entry per chapter using the provided timestamp structure.
  - title: a short descriptive title (not just the timestamp label)
  - summary: exactly 2 sentences describing what was discussed in this chapter
  - topics: 1–3 THEMES raised in this chapter — not one topic per annotation.
      If multiple annotations cluster around the same idea, merge them into one
      topic and pick the single most representative quote. Only split into separate
      topics if the annotations are genuinely about different ideas.
      Each topic has:
      key_quote: the single most precise or memorable quote representing this theme
                 (prefer annotated text verbatim; otherwise reconstruct from context)
      related_concept: the concept name from concept_map this topic connects to
                       (empty string if none)
      why_it_matters: one sentence — why a practitioner should care
      factual_anchor: any stat, study, benchmark, name-drop, or verifiable claim
                      mentioned (empty string if none)

key_insights
  3–7 insights driven exclusively by the human's annotations.
  - timestamp: the chapter label nearest to this annotation (e.g. "10:00")
  - quote: verbatim or near-verbatim text from the annotation
  - insight: 1–2 sentences interpreting why this specific passage matters
             and what it connects to in the broader conversation

host_questions
  Extract all notable questions the host asked during the interview.
  Assign each a category from this list:
      product | leadership | technical | personal | industry | process
  For each question, write a short answer (2–3 sentences) as the guest answered it,
  based strictly on what was discussed in the annotations and context.

connections
  If a list of recent entries is provided, identify 1–3 that genuinely connect to this episode.
  Only include real connections — skip if nothing meaningful links.
  - title: exact title of the related entry
  - relationship: one of "extends", "contradicts", "applies to", "background for", "compared with"
  - reason: one sentence explaining the specific connection

━━━ CONSTRAINTS ━━━
- Return ONLY valid JSON. No markdown fences, no commentary outside the JSON.
- eval_metric, action, and output MUST be chosen from the exact lists above.
- show and tags MAY use existing options from the provided lists OR new values — Notion creates them automatically.
- Do not fabricate quotes, stats, or claims not present in the provided text.
- Chapters with no annotations: infer topics from the context clues available.
- Keep all text concise — this is a reference document, not an essay.

━━━ JSON SCHEMA ━━━
{
  "source_metadata": {
    "episode_title": "",
    "show": "",
    "guests": [],
    "host": "",
    "topic_hub": [],
    "tags": [],
    "eval_metric": "",
    "action": "",
    "output": [],
    "core_insight": "",
    "why_it_matters": ""
  },
  "episode_summary": "",
  "concept_map": [
    {"concept": "", "explanation": ""}
  ],
  "chapter_breakdown": [
    {
      "start": "",
      "end": "",
      "title": "",
      "summary": "",
      "topics": [
        {
          "title": "",
          "key_quote": "",
          "related_concept": "",
          "why_it_matters": "",
          "factual_anchor": ""
        }
      ]
    }
  ],
  "key_insights": [
    {"timestamp": "", "quote": "", "insight": ""}
  ],
  "host_questions": [
    {"question": "", "category": "", "answer": ""}
  ],
  "connections": [
    {"title": "", "relationship": "", "reason": ""}
  ]
}
"""


def _build_user_message(
    podcast_title: str,
    source_url: str,
    annotated: list[AnnotatedSegment],
    context_map: dict[int, str],
    all_anchors: list[HeadingAnchor],
    topic_hub_names: list[str] | None = None,
    recent_entries: list[dict] | None = None,
    episode_metadata: dict | None = None,
    source_options: list[str] | None = None,
    tags_options: list[str] | None = None,
) -> str:
    parts = [
        f"## Podcast: {podcast_title}",
        f"URL: {source_url}",
        "",
        "## Chapter Structure",
    ]

    for i, anchor in enumerate(all_anchors):
        end_label = all_anchors[i + 1].label if i + 1 < len(all_anchors) else "end"
        parts.append(f"  {anchor.label} – {end_label}")

    if episode_metadata:
        parts += ["", "## Episode Metadata (scraped from source URL)"]
        for k, v in episode_metadata.items():
            parts.append(f"  {k}: {v}")

    if source_options:
        parts += ["", "## Available Notion Source Options (pick closest or use correct show name)"]
        parts.append(", ".join(source_options))

    if tags_options:
        parts += ["", "## Available Notion Tags (pick 1–3 or create new concise lowercase tag)"]
        parts.append(", ".join(tags_options))

    if topic_hub_names:
        parts += [
            "",
            "## Topic Hub (pick up to 3 that match this episode)",
            ", ".join(topic_hub_names),
        ]

    if recent_entries:
        parts += ["", "## Recent Entries (for Connects To — use exact titles)"]
        for e in recent_entries:
            line = f"- {e['title']}"
            if e.get("core_insight"):
                line += f" — {e['core_insight'][:120]}"
            parts.append(line)

    parts += [
        "",
        "## Human Annotations",
        f"Total: {len(annotated)}",
        "",
    ]

    for seg in annotated:
        anchor_label = seg.nearest_anchor.label if seg.nearest_anchor else "unknown"
        marker = "HIGHLIGHT" if seg.annotation_type == "highlight" else "UNDERLINE"
        parts.append(f"### [{anchor_label}] {marker}")
        parts.append(f'"{seg.text}"')
        ctx = context_map.get(seg.paragraph_index, "")
        if ctx:
            parts.append(f"Context:\n{ctx}")
        parts.append("")

    return "\n".join(parts)


def _build_user_message_from_transcript(
    podcast_title: str,
    source_url: str,
    transcript_text: str,
    topic_hub_names: list[str] | None = None,
    recent_entries: list[dict] | None = None,
    episode_metadata: dict | None = None,
    source_options: list[str] | None = None,
    tags_options: list[str] | None = None,
) -> str:
    parts = [
        f"## Podcast: {podcast_title}",
        f"URL: {source_url}",
        "",
    ]

    if episode_metadata:
        parts += ["## Episode Metadata (scraped from source URL)"]
        for k, v in episode_metadata.items():
            parts.append(f"  {k}: {v}")

    if source_options:
        parts += ["", "## Available Notion Source Options (pick closest or use correct show name)"]
        parts.append(", ".join(source_options))

    if tags_options:
        parts += ["", "## Available Notion Tags (pick 1–3 or create new concise lowercase tag)"]
        parts.append(", ".join(tags_options))

    if topic_hub_names:
        parts += [
            "",
            "## Topic Hub (pick up to 3 that match this episode)",
            ", ".join(topic_hub_names),
        ]

    if recent_entries:
        parts += ["", "## Recent Entries (for Connects To — use exact titles)"]
        for e in recent_entries:
            line = f"- {e['title']}"
            if e.get("core_insight"):
                line += f" — {e['core_insight'][:120]}"
            parts.append(line)

    parts += [
        "",
        "## Full Transcript (analyze comprehensively to generate all sections)",
        "",
        transcript_text,
    ]

    return "\n".join(parts)


@retry(
    retry=retry_if_exception_type(anthropic.APIError),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(4),
    reraise=True,
)
def _call_claude(user_message: str) -> str:
    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": _SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text


def _parse_response(raw: str, podcast_title: str, source_url: str, doc_url: str) -> AnalysisReport:
    """Parse a raw Claude JSON response into an AnalysisReport."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    data = json.loads(raw)

    meta = data.get("source_metadata", {})
    source_metadata = SourceMetadata(
        show=meta.get("show", ""),
        guests=meta.get("guests", []),
        host=meta.get("host", ""),
        source=meta.get("source", "Other"),
        tags=meta.get("tags", []),
        eval_metric=meta.get("eval_metric", ""),
        action=meta.get("action", ""),
        output=meta.get("output", []),
        core_insight=meta.get("core_insight", ""),
        why_it_matters=meta.get("why_it_matters", ""),
        topic_hub=meta.get("topic_hub", []),
    )
    concept_map = [
        ConceptCard(concept=c["concept"], explanation=c["explanation"])
        for c in data.get("concept_map", [])
    ]
    chapter_breakdown = [
        Chapter(
            start=ch["start"], end=ch["end"],
            title=ch["title"], summary=ch["summary"],
            topics=[
                ChapterTopic(
                    title=t["title"],
                    key_quote=t.get("key_quote", ""),
                    related_concept=t.get("related_concept", ""),
                    why_it_matters=t.get("why_it_matters", ""),
                    factual_anchor=t.get("factual_anchor", ""),
                )
                for t in ch.get("topics", [])
            ],
        )
        for ch in data.get("chapter_breakdown", [])
    ]
    key_insights = [
        KeyInsight(timestamp=i["timestamp"], quote=i["quote"], insight=i["insight"])
        for i in data.get("key_insights", [])
    ]
    host_questions = [
        HostQuestion(question=q["question"], category=q["category"], answer=q.get("answer", ""))
        for q in data.get("host_questions", [])
    ]
    connections = [
        Connection(title=c["title"], relationship=c["relationship"], reason=c["reason"])
        for c in data.get("connections", [])
    ]
    return AnalysisReport(
        podcast_title=meta.get("episode_title") or podcast_title,
        source_url=source_url,
        doc_url=doc_url,
        source_metadata=source_metadata,
        episode_summary=data.get("episode_summary", ""),
        concept_map=concept_map,
        chapter_breakdown=chapter_breakdown,
        key_insights=key_insights,
        host_questions=host_questions,
        connections=connections,
    )


def analyze(
    podcast_title: str,
    source_url: str,
    doc_url: str,
    annotated: list[AnnotatedSegment],
    context_map: dict[int, str],
    all_anchors: list[HeadingAnchor],
    topic_hub: dict[str, str] | None = None,
    recent_entries: list[dict] | None = None,
    episode_metadata: dict | None = None,
    source_options: list[str] | None = None,
    tags_options: list[str] | None = None,
) -> AnalysisReport:
    if not annotated:
        raise ValueError(
            "No annotations found. Please highlight or underline text in the Google Doc before running analysis."
        )

    topic_hub_names = sorted(topic_hub.keys()) if topic_hub else []
    log.info("Sending %d annotations to Claude (%d chapters)", len(annotated), len(all_anchors))
    user_msg = _build_user_message(
        podcast_title, source_url, annotated, context_map, all_anchors,
        topic_hub_names, recent_entries,
        episode_metadata=episode_metadata,
        source_options=source_options,
        tags_options=tags_options,
    )
    with _console.status("[dim]Analyzing with Claude…[/dim]", spinner="dots"):
        raw = _call_claude(user_msg)

    report = _parse_response(raw, podcast_title, source_url, doc_url)

    if not report.chapter_breakdown:
        log.warning("Claude returned 0 chapters — transcript may have no anchor timestamps or Claude truncated the response")

    log.info(
        "Analysis complete: %d chapters, %d insights, %d concepts, %d host questions",
        len(report.chapter_breakdown), len(report.key_insights), len(report.concept_map), len(report.host_questions),
    )
    return report


def analyze_from_transcript(
    podcast_title: str,
    source_url: str,
    doc_url: str,
    transcript_text: str,
    topic_hub: dict[str, str] | None = None,
    recent_entries: list[dict] | None = None,
    episode_metadata: dict | None = None,
    source_options: list[str] | None = None,
    tags_options: list[str] | None = None,
) -> AnalysisReport:
    """Analyze a full transcript without human annotations — auto-generates all notebook sections."""
    topic_hub_names = sorted(topic_hub.keys()) if topic_hub else []
    log.info("Analyzing transcript with Claude (%d characters)", len(transcript_text))

    user_msg = _build_user_message_from_transcript(
        podcast_title, source_url, transcript_text,
        topic_hub_names, recent_entries,
        episode_metadata=episode_metadata,
        source_options=source_options,
        tags_options=tags_options,
    )
    with _console.status("[dim]Analyzing with Claude…[/dim]", spinner="dots"):
        raw = _call_claude(user_msg)

    report = _parse_response(raw, podcast_title, source_url, doc_url)

    log.info(
        "Analysis complete: %d chapters, %d insights, %d concepts, %d host questions",
        len(report.chapter_breakdown), len(report.key_insights), len(report.concept_map), len(report.host_questions),
    )
    return report


def reconstruct_report(data: dict) -> AnalysisReport:
    """Reconstruct an AnalysisReport from a cached dataclasses.asdict() dict."""
    meta = data.get("source_metadata", {})
    source_metadata = SourceMetadata(
        show=meta.get("show", ""),
        guests=meta.get("guests", []),
        host=meta.get("host", ""),
        source=meta.get("source", ""),
        tags=meta.get("tags", []),
        eval_metric=meta.get("eval_metric", ""),
        action=meta.get("action", ""),
        output=meta.get("output", []),
        core_insight=meta.get("core_insight", ""),
        why_it_matters=meta.get("why_it_matters", ""),
        topic_hub=meta.get("topic_hub", []),
    )
    return AnalysisReport(
        podcast_title=data["podcast_title"],
        source_url=data["source_url"],
        doc_url=data["doc_url"],
        source_metadata=source_metadata,
        episode_summary=data.get("episode_summary", ""),
        concept_map=[ConceptCard(**c) for c in data.get("concept_map", [])],
        chapter_breakdown=[
            Chapter(
                start=ch["start"], end=ch["end"],
                title=ch["title"], summary=ch["summary"],
                topics=[ChapterTopic(**t) for t in ch.get("topics", [])],
            )
            for ch in data.get("chapter_breakdown", [])
        ],
        key_insights=[KeyInsight(**i) for i in data.get("key_insights", [])],
        host_questions=[HostQuestion(**q) for q in data.get("host_questions", [])],
        connections=[Connection(**c) for c in data.get("connections", [])],
    )
