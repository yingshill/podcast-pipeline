"""Unit tests for scraper.py — no external API calls."""
import pytest
from unittest.mock import MagicMock, patch

from scraper import (
    _youtube_video_id,
    _merge_youtube_chunks,
    _assign_speakers_youtube,
    _parse_blocks_to_segments,
    _parse_with_timestamps,
    _parse_with_speakers,
    _parse_plain,
    _ts_to_ms,
    _fill_end_times,
    _find_transcript_container,
    transcribe,
)
from models import TranscriptSegment


# ── URL routing ───────────────────────────────────────────────────────────────

class TestYoutubeVideoId:
    def test_standard_watch_url(self):
        assert _youtube_video_id("https://www.youtube.com/watch?v=abc1234XYZ_") == "abc1234XYZ_"

    def test_short_url(self):
        assert _youtube_video_id("https://youtu.be/abc1234XYZ_") == "abc1234XYZ_"

    def test_shorts_url(self):
        assert _youtube_video_id("https://youtube.com/shorts/abc1234XYZ_") == "abc1234XYZ_"

    def test_url_with_extra_params(self):
        vid = _youtube_video_id("https://www.youtube.com/watch?v=abc1234XYZ_&t=120s&list=PL123")
        assert vid == "abc1234XYZ_"

    def test_non_youtube_url_returns_none(self):
        assert _youtube_video_id("https://podcasts.example.com/episode/123") is None

    def test_empty_string_returns_none(self):
        assert _youtube_video_id("") is None


# ── YouTube chunk merging ─────────────────────────────────────────────────────

class TestMergeYoutubeChunks:
    def _make_entries(self, starts_and_texts):
        return [{"start": s, "end": s + 2.0, "text": t, "duration": 2.0}
                for s, t in starts_and_texts]

    def test_merges_short_chunks_into_window(self):
        entries = self._make_entries([(i * 2, f"word{i}") for i in range(20)])  # 40 seconds
        result = _merge_youtube_chunks(entries)
        assert len(result) == 2  # two 30-second windows

    def test_single_chunk_returns_one_group(self):
        entries = self._make_entries([(0, "hello world")])
        result = _merge_youtube_chunks(entries)
        assert len(result) == 1
        assert result[0]["text"] == "hello world"

    def test_empty_input_returns_empty(self):
        assert _merge_youtube_chunks([]) == []

    def test_merged_text_joins_with_spaces(self):
        entries = self._make_entries([(0, "hello"), (2, "world")])
        result = _merge_youtube_chunks(entries)
        assert result[0]["text"] == "hello world"

    def test_start_time_is_first_chunk_start(self):
        entries = self._make_entries([(5.0, "hello"), (7.0, "world")])
        result = _merge_youtube_chunks(entries)
        assert result[0]["start"] == pytest.approx(5.0)


# ── Speaker detection from YouTube captions ───────────────────────────────────

class TestAssignSpeakersYoutube:
    def _group(self, start, text):
        return {"start": start, "end": start + 5.0, "text": text}

    def test_detects_colon_speaker_label(self):
        groups = [self._group(0, "Alice: Hello everyone")]
        segs = _assign_speakers_youtube(groups)
        assert segs[0].speaker == "Alice"
        assert segs[0].text == "Hello everyone"

    def test_speaker_persists_across_unlabeled_groups(self):
        groups = [
            self._group(0, "Alice: Welcome to the show"),
            self._group(30, "This is a great topic"),
        ]
        segs = _assign_speakers_youtube(groups)
        assert segs[0].speaker == "Alice"
        assert segs[1].speaker == "Alice"

    def test_speaker_changes_on_new_label(self):
        groups = [
            self._group(0, "Alice: Hello"),
            self._group(30, "Bob: Thanks for having me"),
        ]
        segs = _assign_speakers_youtube(groups)
        assert segs[0].speaker == "Alice"
        assert segs[1].speaker == "Bob"

    def test_no_label_defaults_to_speaker(self):
        groups = [self._group(0, "No label here just plain text")]
        segs = _assign_speakers_youtube(groups)
        assert segs[0].speaker == "Speaker"

    def test_timestamps_converted_to_ms(self):
        groups = [self._group(65.5, "Alice: text")]
        segs = _assign_speakers_youtube(groups)
        assert segs[0].start_ms == 65500


# ── HTML format detection and parsing ────────────────────────────────────────

class TestParseBlocksToSegments:
    def test_detects_timestamp_format(self):
        blocks = ["[00:01:30] Alice: Hello there", "[00:05:00] Bob: Great to be here"]
        segs = _parse_blocks_to_segments(blocks)
        assert segs[0].start_ms == 90_000
        assert segs[0].speaker == "Alice"

    def test_detects_speaker_only_format(self):
        blocks = ["Alice: First block of text", "Bob: Second block of text"]
        segs = _parse_blocks_to_segments(blocks)
        assert segs[0].speaker == "Alice"
        assert segs[1].speaker == "Bob"

    def test_detects_plain_format(self):
        blocks = ["Just plain text here", "Another paragraph with no labels"]
        segs = _parse_blocks_to_segments(blocks)
        assert all(s.speaker == "Speaker" for s in segs)
        assert len(segs) == 2

    def test_empty_blocks_return_empty(self):
        assert _parse_blocks_to_segments([]) == []


class TestParseWithTimestamps:
    def test_parses_mm_ss_format(self):
        segs = _parse_with_timestamps(["[01:30] Alice: Hello"])
        assert segs[0].start_ms == 90_000

    def test_parses_hh_mm_ss_format(self):
        segs = _parse_with_timestamps(["[01:02:03] Bob: Hi"])
        assert segs[0].start_ms == (3600 + 120 + 3) * 1000

    def test_parens_format_also_works(self):
        segs = _parse_with_timestamps(["(00:05) Speaker: text"])
        assert segs[0].start_ms == 5_000

    def test_fill_end_times_called(self):
        segs = _parse_with_timestamps([
            "[00:00] Alice: First",
            "[00:30] Bob: Second",
        ])
        assert segs[0].end_ms == 30_000


class TestParseWithSpeakers:
    def test_estimates_timestamps_from_word_count(self):
        segs = _parse_with_speakers(["Alice: " + "word " * 150])
        # 150 words at 150 wpm = 60 seconds = 60000 ms
        assert segs[0].start_ms == 0
        assert segs[0].end_ms == pytest.approx(60_000, rel=0.05)

    def test_speaker_persists_when_no_new_label(self):
        segs = _parse_with_speakers([
            "Alice: First paragraph",
            "Continuation without label",
        ])
        # No label on second block → last known speaker (Alice) carries forward
        assert segs[1].speaker == "Alice"

    def test_skips_empty_text(self):
        segs = _parse_with_speakers(["Alice: "])
        assert len(segs) == 0


class TestTsToMs:
    def test_mm_ss(self):
        import re
        m = re.search(r"\[(\d+):(\d+)(?::(\d+))?\]", "[05:30]")  # group(3) is None for MM:SS
        assert _ts_to_ms(m) == (5 * 60 + 30) * 1000

    def test_hh_mm_ss(self):
        import re
        m = re.search(r"\[(\d+):(\d+):(\d+)\]", "[01:02:03]")
        assert _ts_to_ms(m) == (3600 + 120 + 3) * 1000


class TestFillEndTimes:
    def test_fills_from_next_start(self):
        segs = [
            TranscriptSegment(0, 0, "A", "text"),
            TranscriptSegment(5000, 5000, "B", "text"),
        ]
        _fill_end_times(segs)
        assert segs[0].end_ms == 5000

    def test_last_segment_estimated_from_word_count(self):
        segs = [TranscriptSegment(0, 0, "A", "word " * 150)]
        _fill_end_times(segs)
        assert segs[0].end_ms > 0


# ── Mocked integration: transcribe() routing ─────────────────────────────────

class TestTranscribeRouting:
    @patch("scraper._scrape_youtube")
    def test_routes_youtube_url_to_youtube_scraper(self, mock_yt):
        mock_yt.return_value = [TranscriptSegment(0, 1000, "Speaker", "hello")]
        _, source = transcribe("https://www.youtube.com/watch?v=abc1234XYZ_")
        mock_yt.assert_called_once_with("abc1234XYZ_")
        assert source == "youtube"

    @patch("scraper._scrape_html")
    def test_routes_non_youtube_to_html_scraper(self, mock_html):
        mock_html.return_value = [TranscriptSegment(0, 1000, "Speaker", "hello")]
        _, source = transcribe("https://somecast.com/episode/1")
        mock_html.assert_called_once()
        assert source == "html"

    @patch("scraper.YouTubeTranscriptApi")
    def test_transcripts_disabled_raises_runtime_error(self, mock_api):
        from youtube_transcript_api import TranscriptsDisabled
        # v1.x: YouTubeTranscriptApi() is instantiated; mock the instance's .list()
        mock_api.return_value.list.side_effect = TranscriptsDisabled("vid")
        with pytest.raises(RuntimeError, match="disabled"):
            transcribe("https://www.youtube.com/watch?v=abc1234XYZ_")

    @patch("scraper.requests.get")
    def test_html_http_error_propagates(self, mock_get):
        from requests import HTTPError
        mock_get.return_value.raise_for_status.side_effect = HTTPError("404")
        with pytest.raises(HTTPError):
            transcribe("https://somecast.com/episode/1")
