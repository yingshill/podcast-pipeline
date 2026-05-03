"""Unit tests for gdocs.py helpers and annotations.py detection logic."""
import pytest

from gdocs import _label_to_ms, _ms_to_label, _ms_to_inline_ts, nearest_anchor
from annotations import _is_highlighted, _is_underlined, _is_white
from models import HeadingAnchor


# ── gdocs helpers ─────────────────────────────────────────────────────────────

class TestLabelToMs:
    def test_mm_ss(self):
        assert _label_to_ms("05:30") == (5 * 60 + 30) * 1000

    def test_hh_mm_ss(self):
        assert _label_to_ms("01:02:03") == (3600 + 120 + 3) * 1000

    def test_zero(self):
        assert _label_to_ms("00:00") == 0

    def test_invalid_returns_none(self):
        assert _label_to_ms("not a time") is None

    def test_partial_invalid_returns_none(self):
        assert _label_to_ms("05:xx") is None

    def test_strips_whitespace(self):
        assert _label_to_ms("  10:00  ") == 600_000


class TestMsToLabel:
    def test_under_one_hour(self):
        assert _ms_to_label(5 * 60 * 1000 + 30 * 1000) == "05:30"

    def test_over_one_hour(self):
        assert _ms_to_label(3600 * 1000 + 2 * 60 * 1000 + 3 * 1000) == "01:02:03"

    def test_zero(self):
        assert _ms_to_label(0) == "00:00"

    def test_roundtrip(self):
        ms = 47 * 60 * 1000 + 15 * 1000  # 47:15
        assert _label_to_ms(_ms_to_label(ms)) == ms


class TestMsToInlineTs:
    def test_wraps_in_brackets(self):
        assert _ms_to_inline_ts(0) == "[00:00]"
        assert _ms_to_inline_ts(5 * 60 * 1000) == "[05:00]"


class TestNearestAnchor:
    def _anchor(self, time_ms, label="x"):
        return HeadingAnchor(time_ms=time_ms, label=label, heading_id="h1", doc_id="d1")

    def test_returns_anchor_at_or_before_time(self):
        anchors = [self._anchor(0), self._anchor(600_000), self._anchor(1_200_000)]
        result = nearest_anchor(700_000, anchors)
        assert result.time_ms == 600_000

    def test_returns_none_for_empty_list(self):
        assert nearest_anchor(5000, []) is None

    def test_exact_match(self):
        anchors = [self._anchor(600_000)]
        assert nearest_anchor(600_000, anchors).time_ms == 600_000

    def test_returns_none_when_all_anchors_are_after(self):
        anchors = [self._anchor(600_000), self._anchor(1_200_000)]
        assert nearest_anchor(100, anchors) is None

    def test_returns_latest_when_multiple_qualify(self):
        anchors = [self._anchor(0), self._anchor(300_000), self._anchor(600_000)]
        result = nearest_anchor(900_000, anchors)
        assert result.time_ms == 600_000


# ── annotation detection ──────────────────────────────────────────────────────

class TestIsWhite:
    def test_pure_white(self):
        assert _is_white({"red": 1.0, "green": 1.0, "blue": 1.0})

    def test_near_white_counts_as_white(self):
        assert _is_white({"red": 0.96, "green": 0.97, "blue": 0.98})

    def test_yellow_is_not_white(self):
        assert not _is_white({"red": 1.0, "green": 1.0, "blue": 0.0})

    def test_missing_keys_treated_as_zero(self):
        assert not _is_white({})  # 0,0,0 is black


class TestIsHighlighted:
    def _style(self, rgb):
        return {"backgroundColor": {"color": {"rgbColor": rgb}}}

    def test_yellow_highlight(self):
        assert _is_highlighted(self._style({"red": 1.0, "green": 1.0, "blue": 0.0}))

    def test_green_highlight(self):
        assert _is_highlighted(self._style({"red": 0.0, "green": 1.0, "blue": 0.0}))

    def test_white_background_not_highlighted(self):
        assert not _is_highlighted(self._style({"red": 1.0, "green": 1.0, "blue": 1.0}))

    def test_no_background_not_highlighted(self):
        assert not _is_highlighted({})

    def test_empty_background_color_not_highlighted(self):
        assert not _is_highlighted({"backgroundColor": {}})


class TestIsUnderlined:
    def test_underline_true(self):
        assert _is_underlined({"underline": True})

    def test_underline_false(self):
        assert not _is_underlined({"underline": False})

    def test_no_underline_key(self):
        assert not _is_underlined({})

    def test_underline_with_other_styles(self):
        assert _is_underlined({"bold": True, "underline": True, "italic": False})
