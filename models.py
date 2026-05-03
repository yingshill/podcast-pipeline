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
    paragraph_index: int            # position in doc body
    nearest_anchor: Optional[HeadingAnchor] = None


@dataclass
class ReportSection:
    title: str
    body: str
    anchors: list[HeadingAnchor] = field(default_factory=list)


@dataclass
class AnalysisReport:
    podcast_title: str
    source_url: str
    doc_url: str
    executive_summary: str
    key_themes: list[ReportSection] = field(default_factory=list)
    resonance_points: list[ReportSection] = field(default_factory=list)  # from human annotations
    synthesis: str = ""
    action_items: list[str] = field(default_factory=list)


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
