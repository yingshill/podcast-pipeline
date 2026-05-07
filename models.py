from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PipelineStatus(str, Enum):
    PENDING = "pending"
    SCRAPING = "scraping"
    SCRAPED = "scraped"
    DOC_CREATING = "doc_creating"
    DOC_CREATED = "doc_created"
    AWAITING_ANNOTATION = "awaiting_annotation"
    ANALYZING = "analyzing"
    ANALYZED = "analyzed"
    REPORTING = "reporting"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class TranscriptSegment:
    start_ms: int
    end_ms: int
    speaker: str
    text: str


@dataclass
class HeadingAnchor:
    time_ms: int
    label: str          # e.g. "10:00"
    heading_id: str
    doc_id: str

    @property
    def url(self) -> str:
        return f"https://docs.google.com/document/d/{self.doc_id}/edit#heading=h.{self.heading_id}"


@dataclass
class AnnotatedSegment:
    text: str
    annotation_type: str            # "highlight" | "underline"
    paragraph_index: int
    nearest_anchor: Optional[HeadingAnchor] = None


# ── Report dataclasses ────────────────────────────────────────────────────────

@dataclass
class SourceMetadata:
    show: str = ""
    guests: list[str] = field(default_factory=list)
    host: str = ""
    source: str = ""                # matches Notion Source select
    tags: list[str] = field(default_factory=list)
    eval_metric: str = ""           # matches Notion Eval Metric select
    action: str = ""                # matches Notion Action select
    output: list[str] = field(default_factory=list)  # matches Notion Output multi_select
    core_insight: str = ""
    why_it_matters: str = ""
    topic_hub: list[str] = field(default_factory=list)


@dataclass
class ConceptCard:
    concept: str
    explanation: str


@dataclass
class ChapterTopic:
    title: str
    key_quote: str = ""
    related_concept: str = ""
    why_it_matters: str = ""
    factual_anchor: str = ""


@dataclass
class Chapter:
    start: str                      # e.g. "00:00"
    end: str                        # e.g. "10:00"
    title: str
    summary: str                    # 2 sentences
    topics: list[ChapterTopic] = field(default_factory=list)


@dataclass
class KeyInsight:
    timestamp: str
    quote: str
    insight: str


@dataclass
class HostQuestion:
    question: str
    category: str                   # product | leadership | technical | personal | industry | process
    answer: str = ""                # short AI-generated answer


@dataclass
class Connection:
    title: str
    relationship: str   # e.g. "extends", "contradicts", "applies to"
    reason: str         # one sentence


@dataclass
class AnalysisReport:
    podcast_title: str
    source_url: str
    doc_url: str
    source_metadata: SourceMetadata
    episode_summary: str
    concept_map: list[ConceptCard] = field(default_factory=list)
    chapter_breakdown: list[Chapter] = field(default_factory=list)
    key_insights: list[KeyInsight] = field(default_factory=list)
    host_questions: list[HostQuestion] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)


# ── Synthesis dataclasses ─────────────────────────────────────────────────────

@dataclass
class CrossTheme:
    theme: str
    episodes: list[str]
    synthesis: str


@dataclass
class RecurringClaim:
    claim: str
    episodes: list[str]
    why_it_matters: str


@dataclass
class OpenQuestion:
    question: str
    context: str


@dataclass
class SynthesisReport:
    date_range: str
    episode_count: int
    episodes_covered: list[str]
    throughline: str
    cross_themes: list[CrossTheme]
    recurring_claims: list[RecurringClaim]
    open_questions: list[OpenQuestion]
    distilled_actions: list[str]


@dataclass
class PipelineJob:
    id: str
    url: str
    status: PipelineStatus = PipelineStatus.PENDING
    assemblyai_id: Optional[str] = None
    doc_id: Optional[str] = None
    doc_url: Optional[str] = None
    notion_page_id: Optional[str] = None
    notion_page_url: Optional[str] = None
    error: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
