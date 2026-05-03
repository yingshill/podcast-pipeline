"""
Integration test — runs the scraper against a real URL.

Usage:
    pytest tests/test_integration.py -m integration -v --url "https://..."

Or via env var:
    TEST_URL="https://..." pytest tests/test_integration.py -m integration -v
"""
import os
import pytest

from scraper import transcribe
from models import TranscriptSegment


@pytest.fixture
def test_url(request):
    url = request.config.getoption("--url") or os.environ.get("TEST_URL")
    if not url:
        pytest.skip("No URL provided. Pass --url or set TEST_URL env var.")
    return url


@pytest.mark.integration
class TestLiveTranscription:
    def test_returns_non_empty_segments(self, test_url):
        segments, source = transcribe(test_url)
        assert len(segments) > 0, "Expected at least one segment"
        print(f"\n  Source: {source}")
        print(f"  Segments: {len(segments)}")

    def test_all_segments_have_text(self, test_url):
        segments, _ = transcribe(test_url)
        empty = [i for i, s in enumerate(segments) if not s.text.strip()]
        assert not empty, f"Segments with empty text at indices: {empty}"

    def test_all_segments_have_speaker(self, test_url):
        segments, _ = transcribe(test_url)
        missing = [i for i, s in enumerate(segments) if not s.speaker]
        assert not missing, f"Segments missing speaker at indices: {missing}"

    def test_timestamps_are_non_negative(self, test_url):
        segments, _ = transcribe(test_url)
        bad = [i for i, s in enumerate(segments) if s.start_ms < 0 or s.end_ms < 0]
        assert not bad, f"Segments with negative timestamps at indices: {bad}"

    def test_timestamps_are_sequential(self, test_url):
        segments, _ = transcribe(test_url)
        violations = [
            i for i in range(1, len(segments))
            if segments[i].start_ms < segments[i - 1].start_ms
        ]
        assert not violations, f"Non-sequential timestamps at indices: {violations}"

    def test_no_media_player_noise_in_segments(self, test_url):
        """Scraper should filter out 'Current Time:', player labels, etc."""
        from scraper import _is_noise_block
        segments, _ = transcribe(test_url)
        noise = [s.text for s in segments if _is_noise_block(s.text)]
        assert not noise, f"Media-player noise leaked into segments: {noise[:3]}"

    def test_first_10_segments_preview(self, test_url):
        """Prints a preview for manual inspection — always passes if scraping succeeded."""
        segments, source = transcribe(test_url)
        print(f"\n\n── Transcript preview ({source}, {len(segments)} segments) ──")
        for seg in segments[:10]:
            from gdocs import _ms_to_label
            ts = _ms_to_label(seg.start_ms)
            print(f"  [{ts}] {seg.speaker}: {seg.text[:80]}{'…' if len(seg.text) > 80 else ''}")
        assert len(segments) > 0
