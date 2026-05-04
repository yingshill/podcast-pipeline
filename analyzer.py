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
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from models import (
    AnnotatedSegment, AnalysisReport, HeadingAnchor,
    SourceMetadata, ConceptCard, Chapter, ChapterTopic,
    KeyInsight, HostQuestion,
)

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8000

_SYSTEM = """\
You are an expert podcast analyst. You generate a structured notebook entry for a human reader.

You receive:
1. Podcast title and URL
2. Chapter structure — timestamp labels from the transcript (every ~10 min)
3. Human-annotated passages — text the reader highlighted or underlined; these mark what personally resonated
4. Context paragraphs surrounding each annotation

━━━ SECTION RULES ━━━

source_metadata
  Infer show, guest(s), and host from the podcast title and annotation content.
  - source: match to the nearest option from this exact list:
      YouTube | Latent Space | TWIML | Lex Fridman | MLOps Community | Karpathy | Stanford | Data Engineering Podcast | Other
  - tags: pick 1–3 most relevant from this exact list:
      agents | llm-infra | rag | eval | data-tools | skills | observability
  - eval_metric: your honest assessment, pick exactly one from this exact list:
      🔥 Game Changer | 🧐 Interesting | 💤 Too Basic | 🗑️ Irrelevant | Good to know | ✅ Strong | 🛌 Sleep on it | ❓ Need more context | 👀 Watchlist
  - action: what the reader should do next, pick exactly one from this exact list:
      Try this week | Read deep | Bookmark | Skip
  - output: what this content could become, pick 1–3 from this exact list:
      LinkedIn Post | XHS Post | Turn into Project | Sunday Lab | Add to Zettelkasten | Share with Team | 🎨 Visualize | 📣 Project
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
  - topics: 1–3 key questions or themes raised in this chapter, each with:
      key_quote: the most precise or memorable quote from that topic
                 (use annotated text verbatim if it falls in this chapter;
                  otherwise reconstruct from the context provided)
      related_concept: the concept name from concept_map this topic connects to
                       (empty string if none)
      why_it_matters: one sentence — why a practitioner should care
      factual_anchor: any stat, study, benchmark, name-drop, or verifiable claim
                      mentioned (empty string if none)
  For chapters that contain human annotations, the annotated text MUST appear
  as the key_quote of the relevant topic.

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

━━━ CONSTRAINTS ━━━
- Return ONLY valid JSON. No markdown fences, no commentary outside the JSON.
- Values for source, tags, eval_metric, action, output MUST be chosen from the exact lists above.
- Do not fabricate quotes, stats, or claims not present in the provided text.
- Chapters with no annotations: infer topics from the context clues available.
- Keep all text concise — this is a reference document, not an essay.

━━━ JSON SCHEMA ━━━
{
  "source_metadata": {
    "show": "",
    "guests": [],
    "host": "",
    "source": "",
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
    {"question": "", "category": ""}
  ]
}
"""


def _build_user_message(
    podcast_title: str,
    source_url: str,
    annotated: list[AnnotatedSegment],
    context_map: dict[int, str],
    all_anchors: list[HeadingAnchor],
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
                "cache_control": {"type": "ephemeral"},
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
    if not annotated:
        raise ValueError(
            "No annotations found. Please highlight or underline text in the Google Doc before running analysis."
        )

    log.info("Sending %d annotations to Claude (%d chapters)", len(annotated), len(all_anchors))
    user_msg = _build_user_message(podcast_title, source_url, annotated, context_map, all_anchors)
    raw = _call_claude(user_msg)

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
    )

    concept_map = [
        ConceptCard(concept=c["concept"], explanation=c["explanation"])
        for c in data.get("concept_map", [])
    ]

    chapter_breakdown = [
        Chapter(
            start=ch["start"],
            end=ch["end"],
            title=ch["title"],
            summary=ch["summary"],
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
        KeyInsight(
            timestamp=i["timestamp"],
            quote=i["quote"],
            insight=i["insight"],
        )
        for i in data.get("key_insights", [])
    ]

    host_questions = [
        HostQuestion(question=q["question"], category=q["category"])
        for q in data.get("host_questions", [])
    ]

    report = AnalysisReport(
        podcast_title=podcast_title,
        source_url=source_url,
        doc_url=doc_url,
        source_metadata=source_metadata,
        episode_summary=data.get("episode_summary", ""),
        concept_map=concept_map,
        chapter_breakdown=chapter_breakdown,
        key_insights=key_insights,
        host_questions=host_questions,
    )

    log.info(
        "Analysis complete: %d chapters, %d insights, %d concepts, %d host questions",
        len(chapter_breakdown), len(key_insights), len(concept_map), len(host_questions),
    )
    return report
