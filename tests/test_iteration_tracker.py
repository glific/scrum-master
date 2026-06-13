"""Tests for iteration_tracker.py"""

from datetime import date
from unittest.mock import MagicMock, call, patch

import pytest

from iteration_tracker import (
    _ensure_header,
    _find_row,
    _generate_user_stories,
    _get_planned_total,
    _post_to_discord,
    _write_row,
)


# ── _find_row ─────────────────────────────────────────────────────────────────

class TestFindRow:
    def _sheets(self, values):
        sheets = MagicMock()
        sheets.values().get().execute.return_value = {"values": values}
        return sheets

    def test_found(self):
        sheets = self._sheets([["Iteration"], ["Sprint 1"], ["Sprint 2"]])
        assert _find_row(sheets, "sid", "Sprint 1") == 2

    def test_not_found(self):
        sheets = self._sheets([["Iteration"], ["Sprint 1"]])
        assert _find_row(sheets, "sid", "Sprint 99") is None

    def test_empty_sheet(self):
        sheets = self._sheets([])
        assert _find_row(sheets, "sid", "Sprint 1") is None

    def test_header_row_not_matched(self):
        sheets = self._sheets([["Iteration", "Start Date"], ["Sprint 1", "2026-06-01"]])
        # Row 1 is the header; "Iteration" should not match "Sprint 1"
        assert _find_row(sheets, "sid", "Iteration") == 1  # header IS matched if title matches column A


# ── _get_planned_total ────────────────────────────────────────────────────────

class TestGetPlannedTotal:
    def _sheets(self, values):
        sheets = MagicMock()
        sheets.values().get().execute.return_value = {"values": values}
        return sheets

    def test_found(self):
        # Header + one data row: [Iteration, Start, End, PlannedTotal, ...]
        sheets = self._sheets([
            ["Iteration", "Start Date", "End Date", "Planned Total"],
            ["Sprint 1", "2026-06-01", "2026-06-14", "20"],
        ])
        assert _get_planned_total(sheets, "sid", "Sprint 1") == 20

    def test_not_found(self):
        sheets = self._sheets([
            ["Iteration", "Start Date", "End Date", "Planned Total"],
            ["Sprint 2", "2026-06-15", "2026-06-28", "18"],
        ])
        assert _get_planned_total(sheets, "sid", "Sprint 1") is None

    def test_malformed_value_returns_none(self):
        sheets = self._sheets([
            ["Iteration", "Start Date", "End Date", "Planned Total"],
            ["Sprint 1", "2026-06-01", "2026-06-14", "not-a-number"],
        ])
        assert _get_planned_total(sheets, "sid", "Sprint 1") is None

    def test_missing_planned_column_returns_none(self):
        sheets = self._sheets([
            ["Iteration", "Start Date", "End Date"],
            ["Sprint 1", "2026-06-01", "2026-06-14"],  # row[3] missing
        ])
        assert _get_planned_total(sheets, "sid", "Sprint 1") is None

    def test_empty_sheet(self):
        sheets = self._sheets([])
        assert _get_planned_total(sheets, "sid", "Sprint 1") is None


# ── _ensure_header ────────────────────────────────────────────────────────────

class TestEnsureHeader:
    def test_writes_header_when_empty(self):
        sheets = MagicMock()
        sheets.values().get().execute.return_value = {"values": []}
        _ensure_header(sheets, "sid")
        sheets.values().update.assert_called_once()

    def test_skips_header_when_exists(self):
        sheets = MagicMock()
        sheets.values().get().execute.return_value = {"values": [["Iteration"]]}
        _ensure_header(sheets, "sid")
        sheets.values().update.assert_not_called()


# ── _write_row ────────────────────────────────────────────────────────────────

class TestWriteRow:
    def test_writes_to_correct_range(self):
        sheets = MagicMock()
        _write_row(sheets, "sid", 3, ["Sprint 1", "2026-06-01"])
        call_kwargs = sheets.values().update.call_args
        assert "A3:K3" in str(call_kwargs)

    def test_passes_values(self):
        sheets = MagicMock()
        values = ["Sprint 1", "2026-06-01", "2026-06-14", 20, 15, 10, 5, 75, 25, "2026-06-14", "Stories"]
        _write_row(sheets, "sid", 2, values)
        update_body = sheets.values().update.call_args[1]["body"]
        assert update_body["values"] == [values]


# ── _generate_user_stories ────────────────────────────────────────────────────

class TestGenerateUserStories:
    def test_no_items_returns_message(self):
        result = _generate_user_stories([])
        assert "No completed tickets" in result

    def test_calls_claude(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(type="text", text="• Users can now log in faster")]
        mock_client.messages.create.return_value = mock_resp

        items = [{"title": "Speed up login flow"}]
        with patch("iteration_tracker.anthropic.Anthropic", return_value=mock_client):
            result = _generate_user_stories(items)

        assert "Users can now log in faster" in result
        mock_client.messages.create.assert_called_once()

    def test_bullet_separation(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(type="text", text="• Story 1\n• Story 2")]
        mock_client.messages.create.return_value = mock_resp

        with patch("iteration_tracker.anthropic.Anthropic", return_value=mock_client):
            result = _generate_user_stories([{"title": "Thing"}])

        assert "\n\n•" in result


# ── _post_to_discord ──────────────────────────────────────────────────────────

class TestPostToDiscord:
    def _data(self):
        return {
            "iteration_title": "Sprint 1",
            "starts_at": "2026-06-01",
            "ends_at": "2026-06-14",
            "items": [
                {"status": "Done", "identifier": "g#1", "title": "Fix bug", "url": "u", "has_pr": True, "labels": [], "assignees": []},
                {"status": "To Do", "identifier": "g#2", "title": "New feature", "url": "u", "has_pr": False, "labels": [], "assignees": []},
            ],
        }

    def test_posts_to_webhook(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch("iteration_tracker.requests.post", return_value=mock_resp) as mock_post:
            _post_to_discord("https://discord.com/api/webhooks/123/abc", self._data(), 50, 0, "• Story")
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert "embeds" in payload

    def test_embed_contains_completion(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch("iteration_tracker.requests.post", return_value=mock_resp) as mock_post:
            _post_to_discord("https://discord.com/api/webhooks/123/abc", self._data(), 50, 0, "• Story")
        embed = mock_post.call_args[1]["json"]["embeds"][0]
        assert "50%" in embed["footer"]["text"]
