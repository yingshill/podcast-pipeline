#!/usr/bin/env python3
"""Quick smoke-test: create one minimal page in the Notion DB and print the URL."""
from dotenv import load_dotenv
load_dotenv()

from models import (
    AnalysisReport, SourceMetadata, ConceptCard, Chapter, ChapterTopic,
    KeyInsight, HostQuestion,
)
import notion_writer

report = AnalysisReport(
    podcast_title="[TEST PAGE] Notion writer smoke-test",
    source_url="https://example.com/test",
    doc_url="https://docs.google.com/document/d/test/edit",
    source_metadata=SourceMetadata(
        show="Test Show",
        guests=["Test Guest"],
        host="Test Host",
        core_insight="This is a test page created to verify the Notion writer works.",
        why_it_matters="Confirms the property name and API connection are correct.",
    ),
    episode_summary=(
        "This is a synthetic test episode created to verify the pipeline's "
        "Notion writer can successfully create a page in the target database."
    ),
    concept_map=[
        ConceptCard(concept="Test Concept", explanation="A placeholder concept for the smoke-test."),
    ],
    chapter_breakdown=[
        Chapter(
            start="00:00", end="05:00",
            title="Test Chapter",
            summary="A placeholder chapter. Two sentences of summary text here.",
            topics=[
                ChapterTopic(
                    title="Test Topic",
                    key_quote="This is a test quote.",
                    related_concept="Test Concept",
                    why_it_matters="Validates the toggle block structure.",
                    factual_anchor="Smoke-test run.",
                )
            ],
        )
    ],
    key_insights=[
        KeyInsight(timestamp="01:23", quote="Test quote text.", insight="Test insight."),
    ],
    host_questions=[
        HostQuestion(question="Is the Notion writer working?", category="technical"),
    ],
)

page_id, page_url = notion_writer.write_report(report)
print(f"\nSuccess! Page created:\n  {page_url}\n")
