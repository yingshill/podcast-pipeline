"""Unit tests for notion_writer.py block logic and analyzer.py JSON parsing."""
import json
import pytest
from unittest.mock import MagicMock, patch

from notion_writer import (
    _split_text,
    _rich_text,
    _paragraph_block,
    _heading_block,
    _bullet_block,
    _build_all_blocks,
)
from analyzer import _build_user_message
from models import (
    AnalysisReport,
    AnnotatedSegment,
    HeadingAnchor,
    ReportSection,
)


# ── notion_writer: text splitting ────────────────────────────────────────────

class TestSplitText:
    def test_short_text_returns_as_is(self):
        assert _split_text("Hello world.") == ["Hello world."]

    def test_splits_at_sentence_boundary(self):
        # "First sentence." (15 chars) splits off first, then 1900 x's + tail splits again
        text = "First sentence. " + "x" * 1900 + " Second sentence."
        chunks = _split_text(text)
        assert len(chunks) == 3
        assert chunks[0] == "First sentence."

    def test_falls_back_to_space_when_no_period(self):
        text = "word " * 500  # 2500 chars, no period
        chunks = _split_text(text)
        assert all(len(c) <= 1900 for c in chunks)

    def test_long_text_all_chunks_under_limit(self):
        text = "This is a sentence. " * 200  # ~4000 chars
        chunks = _split_text(text, limit=1900)
        assert all(len(c) <= 1900 for c in chunks)

    def test_exact_limit_returns_single_chunk(self):
        text = "x" * 1900
        assert _split_text(text, limit=1900) == [text]

    def test_empty_string(self):
        assert _split_text("") == [""]


# ── notion_writer: block builders ────────────────────────────────────────────

class TestRichText:
    def test_plain_text(self):
        rt = _rich_text("hello")
        assert rt["type"] == "text"
        assert rt["text"]["content"] == "hello"
        assert "link" not in rt["text"]

    def test_with_url(self):
        rt = _rich_text("click here", url="https://example.com")
        assert rt["text"]["link"] == {"url": "https://example.com"}


class TestParagraphBlock:
    def test_structure(self):
        block = _paragraph_block([_rich_text("text")])
        assert block["type"] == "paragraph"
        assert block["object"] == "block"
        assert block["paragraph"]["rich_text"][0]["text"]["content"] == "text"


class TestHeadingBlock:
    def test_heading_2(self):
        block = _heading_block("My Title", level=2)
        assert block["type"] == "heading_2"
        assert block["heading_2"]["rich_text"][0]["text"]["content"] == "My Title"

    def test_heading_3(self):
        block = _heading_block("Sub", level=3)
        assert block["type"] == "heading_3"


class TestBulletBlock:
    def test_structure(self):
        block = _bullet_block("Do this thing")
        assert block["type"] == "bulleted_list_item"
        assert block["bulleted_list_item"]["rich_text"][0]["text"]["content"] == "Do this thing"


# ── notion_writer: full report block assembly ─────────────────────────────────

class TestBuildAllBlocks:
    def _make_report(self):
        anchor = HeadingAnchor(time_ms=600_000, label="10:00", heading_id="abc123", doc_id="docid")
        return AnalysisReport(
            podcast_title="Test Podcast",
            source_url="https://youtube.com/watch?v=test",
            doc_url="https://docs.google.com/document/d/docid/edit",
            executive_summary="This episode covers X and Y.",
            key_themes=[
                ReportSection(title="Theme One", body="Description of theme.", anchors=[anchor])
            ],
            resonance_points=[
                ReportSection(
                    title="Great insight",
                    body='> "The best quote."\n\nWhy it matters.',
                    anchors=[anchor],
                )
            ],
            synthesis="Overall this connects A to B.",
            action_items=["Read more about X", "Follow up on Y"],
        )

    def test_returns_list_of_blocks(self):
        blocks = _build_all_blocks(self._make_report())
        assert isinstance(blocks, list)
        assert len(blocks) > 0

    def test_all_blocks_have_type(self):
        blocks = _build_all_blocks(self._make_report())
        assert all("type" in b for b in blocks)

    def test_anchor_link_block_present(self):
        blocks = _build_all_blocks(self._make_report())
        # At least one block should reference the Google Doc anchor URL
        all_text = str(blocks)
        assert "heading=h.abc123" in all_text

    def test_executive_summary_in_blocks(self):
        blocks = _build_all_blocks(self._make_report())
        all_text = str(blocks)
        assert "This episode covers X and Y." in all_text

    def test_action_items_in_blocks(self):
        blocks = _build_all_blocks(self._make_report())
        all_text = str(blocks)
        assert "Read more about X" in all_text

    def test_quote_rendered_as_quote_block(self):
        blocks = _build_all_blocks(self._make_report())
        types = [b["type"] for b in blocks]
        assert "quote" in types


# ── analyzer: prompt building ─────────────────────────────────────────────────

class TestBuildUserMessage:
    def _make_annotated(self, text, annotation_type="highlight", time_ms=600_000):
        anchor = HeadingAnchor(time_ms=time_ms, label="10:00", heading_id="h1", doc_id="d1")
        return AnnotatedSegment(
            text=text,
            annotation_type=annotation_type,
            paragraph_index=5,
            nearest_anchor=anchor,
        )

    def test_contains_podcast_title(self):
        msg = _build_user_message("My Podcast", "https://url", [], {})
        assert "My Podcast" in msg

    def test_contains_annotation_text(self):
        seg = self._make_annotated("This is the highlighted text")
        msg = _build_user_message("Pod", "url", [seg], {})
        assert "This is the highlighted text" in msg

    def test_highlight_labeled_correctly(self):
        seg = self._make_annotated("text", annotation_type="highlight")
        msg = _build_user_message("Pod", "url", [seg], {})
        assert "HIGHLIGHT" in msg

    def test_underline_labeled_correctly(self):
        seg = self._make_annotated("text", annotation_type="underline")
        msg = _build_user_message("Pod", "url", [seg], {})
        assert "UNDERLINE" in msg

    def test_anchor_label_in_message(self):
        seg = self._make_annotated("text", time_ms=600_000)
        msg = _build_user_message("Pod", "url", [seg], {})
        assert "10:00" in msg

    def test_context_included_when_present(self):
        seg = self._make_annotated("annotated text")
        context = {5: "surrounding paragraph context here"}
        msg = _build_user_message("Pod", "url", [seg], context)
        assert "surrounding paragraph context here" in msg

    def test_annotation_count_in_message(self):
        segs = [self._make_annotated(f"text {i}") for i in range(5)]
        msg = _build_user_message("Pod", "url", segs, {})
        assert "5" in msg


# ── analyzer: JSON response parsing ──────────────────────────────────────────

class TestClaudeResponseParsing:
    """Test that the JSON-cleaning logic in analyzer.py handles edge cases."""

    def _strip_fences(self, raw: str) -> str:
        """Replicate the fence-stripping logic from analyzer.py."""
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return raw

    def test_clean_json_unchanged(self):
        raw = '{"key": "value"}'
        assert self._strip_fences(raw) == raw

    def test_strips_json_code_fence(self):
        raw = '```json\n{"key": "value"}\n```'
        result = self._strip_fences(raw)
        assert result == '{"key": "value"}'

    def test_strips_plain_code_fence(self):
        raw = '```\n{"key": "value"}\n```'
        result = self._strip_fences(raw)
        assert result == '{"key": "value"}'

    def test_strips_leading_and_trailing_whitespace(self):
        raw = '  \n  {"key": "value"}  \n  '
        result = self._strip_fences(raw)
        assert json.loads(result)["key"] == "value"
