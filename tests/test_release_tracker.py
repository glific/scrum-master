"""Tests for release_tracker.py"""

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from release_tracker import (
    SHEET_NAME,
    _ensure_header,
    _ensure_sheet_tab,
    _fetch_releases_in_range,
    _find_week_row,
    _list_repos,
    _month_label,
    _rebuild_summary,
    _week_bounds,
    _write_row,
)


# ── _week_bounds ──────────────────────────────────────────────────────────────

class TestWeekBounds:
    def test_monday_is_start(self):
        monday = date(2026, 6, 1)  # confirmed Monday
        start, end = _week_bounds(monday)
        assert start == monday
        assert end == date(2026, 6, 6)  # Saturday

    def test_saturday_ends_same_week(self):
        saturday = date(2026, 6, 6)
        start, end = _week_bounds(saturday)
        assert start == date(2026, 6, 1)
        assert end == saturday

    def test_midweek(self):
        wednesday = date(2026, 6, 3)
        start, end = _week_bounds(wednesday)
        assert start == date(2026, 6, 1)
        assert end == date(2026, 6, 6)

    def test_sunday_belongs_to_next_week(self):
        sunday = date(2026, 6, 7)
        start, end = _week_bounds(sunday)
        # Sunday weekday() == 6, so monday = sunday - 6 days = 2026-06-01? No:
        # sunday - timedelta(days=6) = 2026-06-01
        assert start == date(2026, 6, 1)

    def test_end_is_always_saturday(self):
        for day_offset in range(7):
            d = date(2026, 6, 1) + __import__("datetime").timedelta(days=day_offset)
            _, end = _week_bounds(d)
            assert end.weekday() == 5, f"{d} gave end weekday {end.weekday()}"


# ── _month_label ──────────────────────────────────────────────────────────────

class TestMonthLabel:
    def test_format(self):
        assert _month_label(date(2026, 6, 1)) == "June 2026"

    def test_january(self):
        assert _month_label(date(2027, 1, 15)) == "January 2027"


# ── _find_week_row ────────────────────────────────────────────────────────────

class TestFindWeekRow:
    def test_found(self):
        rows = [
            ["June 2026", "2026-06-01", "2026-06-06"],
            ["June 2026", "2026-06-08", "2026-06-13"],
        ]
        assert _find_week_row(rows, "2026-06-08") == 2

    def test_not_found(self):
        rows = [["June 2026", "2026-06-01", "2026-06-06"]]
        assert _find_week_row(rows, "2026-06-15") is None

    def test_empty(self):
        assert _find_week_row([], "2026-06-01") is None

    def test_matches_column_b(self):
        # _find_week_row checks row[1] (column B = week start), not column A
        rows = [["June 2026", "2026-06-01", "2026-06-06", "2"]]
        assert _find_week_row(rows, "2026-06-01") == 1
        # column A value must NOT match when searching by week start
        assert _find_week_row(rows, "June 2026") is None


# ── _list_repos ───────────────────────────────────────────────────────────────

class TestListRepos:
    def _resp(self, repos, status=200):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = repos
        r.raise_for_status = MagicMock()
        return r

    def test_returns_non_archived(self):
        repos = [
            {"name": "glific", "archived": False},
            {"name": "archived-repo", "archived": True},
        ]
        with patch("release_tracker.requests.get", return_value=self._resp(repos)):
            result = _list_repos("glific", "token")
        assert "glific" in result
        assert "archived-repo" not in result

    def test_excludes_support_process(self):
        repos = [
            {"name": "support-process", "archived": False},
            {"name": "glific", "archived": False},
        ]
        with patch("release_tracker.requests.get", return_value=self._resp(repos)):
            result = _list_repos("glific", "token")
        assert "support-process" not in result

    def test_paginates(self):
        page1 = [{"name": f"repo-{i}", "archived": False} for i in range(100)]
        page2 = [{"name": "last-repo", "archived": False}]
        with patch("release_tracker.requests.get", side_effect=[
            self._resp(page1), self._resp(page2)
        ]):
            result = _list_repos("glific", "token")
        assert len(result) == 101


# ── _fetch_releases_in_range ──────────────────────────────────────────────────

class TestFetchReleasesInRange:
    def _release(self, tag="v1.0", published="2026-06-03T12:00:00Z"):
        return {"tag_name": tag, "name": tag, "published_at": published}

    def _resp(self, releases, status=200):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = releases
        r.raise_for_status = MagicMock()
        return r

    def test_returns_releases_in_range(self):
        releases = [self._release("v1.0", "2026-06-03T12:00:00Z")]
        with patch("release_tracker.requests.get", return_value=self._resp(releases)):
            result = _fetch_releases_in_range(
                "glific", ["glific"], date(2026, 6, 1), date(2026, 6, 6), "token"
            )
        assert len(result) == 1
        assert result[0]["tag"] == "v1.0"

    def test_excludes_releases_outside_range(self):
        releases = [self._release("v0.9", "2026-05-25T12:00:00Z")]  # before window
        with patch("release_tracker.requests.get", return_value=self._resp(releases)):
            result = _fetch_releases_in_range(
                "glific", ["glific"], date(2026, 6, 1), date(2026, 6, 6), "token"
            )
        assert result == []

    def test_handles_404(self):
        r = MagicMock()
        r.status_code = 404
        with patch("release_tracker.requests.get", return_value=r):
            result = _fetch_releases_in_range(
                "glific", ["no-releases-repo"], date(2026, 6, 1), date(2026, 6, 6), "token"
            )
        assert result == []

    def test_multiple_repos(self):
        release_a = self._release("v1.0", "2026-06-02T00:00:00Z")
        release_b = self._release("v2.0", "2026-06-04T00:00:00Z")
        resp_a = self._resp([release_a])
        resp_b = self._resp([release_b])
        with patch("release_tracker.requests.get", side_effect=[resp_a, resp_b]):
            result = _fetch_releases_in_range(
                "glific", ["repo-a", "repo-b"], date(2026, 6, 1), date(2026, 6, 6), "token"
            )
        assert len(result) == 2

    def test_skips_release_without_published_at(self):
        releases = [{"tag_name": "v1.0", "name": "v1.0", "published_at": None}]
        with patch("release_tracker.requests.get", return_value=self._resp(releases)):
            result = _fetch_releases_in_range(
                "glific", ["glific"], date(2026, 6, 1), date(2026, 6, 6), "token"
            )
        assert result == []


# ── _ensure_sheet_tab ─────────────────────────────────────────────────────────

class TestEnsureSheetTab:
    def test_creates_when_missing(self):
        sheets = MagicMock()
        sheets.get().execute.return_value = {"sheets": [{"properties": {"title": "Sheet1"}}]}
        _ensure_sheet_tab(sheets, "sid")
        sheets.batchUpdate.assert_called_once()

    def test_skips_when_exists(self):
        sheets = MagicMock()
        sheets.get().execute.return_value = {
            "sheets": [{"properties": {"title": SHEET_NAME}}]
        }
        _ensure_sheet_tab(sheets, "sid")
        sheets.batchUpdate.assert_not_called()


# ── _ensure_header ────────────────────────────────────────────────────────────

class TestEnsureHeader:
    def test_writes_when_empty(self):
        sheets = MagicMock()
        sheets.values().get().execute.return_value = {"values": []}
        _ensure_header(sheets, "sid")
        sheets.values().update.assert_called_once()

    def test_skips_when_exists(self):
        sheets = MagicMock()
        sheets.values().get().execute.return_value = {"values": [["Month"]]}
        _ensure_header(sheets, "sid")
        sheets.values().update.assert_not_called()


# ── _write_row ────────────────────────────────────────────────────────────────

class TestWriteRow:
    def test_correct_range(self):
        sheets = MagicMock()
        _write_row(sheets, "sid", 3, ["June 2026", "2026-06-01"])
        call_args = sheets.values().update.call_args
        assert f"'{SHEET_NAME}'!A3:F3" in str(call_args)


# ── _rebuild_summary ──────────────────────────────────────────────────────────

class TestRebuildSummary:
    def test_clears_and_writes_summary(self):
        sheets = MagicMock()
        rows = [
            ["Month", "Week Start", "Week End", "Count"],
            ["June 2026", "2026-06-01", "2026-06-06", "2"],
            ["June 2026", "2026-06-08", "2026-06-13", "3"],
            ["July 2026", "2026-07-06", "2026-07-11", "1"],
        ]
        _rebuild_summary(sheets, "sid", rows)
        sheets.values().clear.assert_called_once()
        sheets.values().update.assert_called_once()
        update_values = sheets.values().update.call_args[1]["body"]["values"]
        # Header + 2 month rows
        assert update_values[0] == ["Month", "Avg Releases/Week"]
        months = [row[0] for row in update_values[1:]]
        assert "June 2026" in months
        assert "July 2026" in months

    def test_formula_references_correct_column(self):
        sheets = MagicMock()
        rows = [
            ["Month", "Week Start"],
            ["June 2026", "2026-06-01"],
        ]
        _rebuild_summary(sheets, "sid", rows)
        update_values = sheets.values().update.call_args[1]["body"]["values"]
        formula = update_values[1][1]
        assert "AVERAGEIF" in formula
        assert "D:D" in formula

    def test_empty_data_writes_only_header(self):
        sheets = MagicMock()
        _rebuild_summary(sheets, "sid", [["Month", "Week Start"]])
        update_values = sheets.values().update.call_args[1]["body"]["values"]
        assert update_values == [["Month", "Avg Releases/Week"]]
