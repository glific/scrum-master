"""Tests for pr_merge_time.py"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pr_merge_time import (
    _avg_days_to_merge,
    _business_days,
    _ensure_header,
    _ensure_sheet_tab,
    _fetch_merged_prs,
    _find_row,
    _write_row,
    SHEET_NAME,
)


# ── _business_days ────────────────────────────────────────────────────────────

class TestBusinessDays:
    def _dt(self, iso):
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)

    def test_same_datetime_returns_zero(self):
        dt = self._dt("2026-06-01T10:00:00")
        assert _business_days(dt, dt) == 0.0

    def test_end_before_start_returns_zero(self):
        assert _business_days(self._dt("2026-06-02T10:00:00"), self._dt("2026-06-01T10:00:00")) == 0.0

    def test_one_full_weekday(self):
        # Monday midnight to Tuesday midnight = 1 business day
        start = self._dt("2026-06-01T00:00:00")  # Monday
        end   = self._dt("2026-06-02T00:00:00")  # Tuesday
        assert _business_days(start, end) == 1.0

    def test_weekend_not_counted(self):
        # Friday midnight to Monday midnight = 2 business days (Fri + ... wait)
        # Friday 00:00 to Monday 00:00: Fri=1 day, Sat=0, Sun=0 → 1 business day
        start = self._dt("2026-06-05T00:00:00")  # Friday
        end   = self._dt("2026-06-08T00:00:00")  # Monday
        assert _business_days(start, end) == 1.0

    def test_friday_to_monday_partial(self):
        # Created Friday 5pm, merged Monday 9am
        # Friday 5pm–midnight = 7h, Saturday skip, Sunday skip, Monday midnight–9am = 9h
        start = self._dt("2026-06-05T17:00:00")  # Friday 5pm
        end   = self._dt("2026-06-08T09:00:00")  # Monday 9am
        result = _business_days(start, end)
        expected = (7 + 9) / 24  # 16 business hours
        assert abs(result - expected) < 0.01

    def test_full_week(self):
        # Monday to next Monday = 5 business days
        start = self._dt("2026-06-01T00:00:00")  # Monday
        end   = self._dt("2026-06-08T00:00:00")  # next Monday
        assert _business_days(start, end) == 5.0

    def test_saturday_start_not_counted(self):
        # Saturday noon to Sunday noon = 0 business days
        start = self._dt("2026-06-06T12:00:00")  # Saturday
        end   = self._dt("2026-06-07T12:00:00")  # Sunday
        assert _business_days(start, end) == 0.0


# ── _avg_days_to_merge ────────────────────────────────────────────────────────

class TestAvgDaysToMerge:
    def test_empty_list(self):
        assert _avg_days_to_merge([]) == 0.0

    def test_single_pr_one_weekday(self):
        # Monday to Tuesday = 1 business day
        prs = [{"created_at": "2026-06-01T00:00:00Z", "merged_at": "2026-06-02T00:00:00Z"}]
        assert _avg_days_to_merge(prs) == 1.0

    def test_weekend_excluded(self):
        # Monday to Wednesday spanning a weekend-free week = 2 business days
        prs = [{"created_at": "2026-06-01T00:00:00Z", "merged_at": "2026-06-03T00:00:00Z"}]
        assert _avg_days_to_merge(prs) == 2.0

    def test_weekend_not_counted(self):
        # Friday midnight to Monday midnight = 1 business day (only Friday counts)
        prs = [{"created_at": "2026-06-05T00:00:00Z", "merged_at": "2026-06-08T00:00:00Z"}]
        assert _avg_days_to_merge(prs) == 1.0

    def test_rounds_to_one_decimal(self):
        prs = [
            {"created_at": "2026-06-01T00:00:00Z", "merged_at": "2026-06-02T08:00:00Z"},
            {"created_at": "2026-06-01T00:00:00Z", "merged_at": "2026-06-02T16:00:00Z"},
        ]
        result = _avg_days_to_merge(prs)
        assert result == round(result, 1)

    def test_same_day_merge(self):
        prs = [{"created_at": "2026-06-01T10:00:00Z", "merged_at": "2026-06-01T14:00:00Z"}]
        assert _avg_days_to_merge(prs) < 1.0


# ── _fetch_merged_prs ─────────────────────────────────────────────────────────

class TestFetchMergedPrs:
    def _make_item(self, repo="glific", merged_at="2026-06-10T12:00:00Z"):
        return {
            "repository_url": f"https://api.github.com/repos/glific/{repo}",
            "created_at": "2026-06-01T00:00:00Z",
            "pull_request": {"merged_at": merged_at},
        }

    def _search_resp(self, items, status=200):
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = {"items": items}
        resp.raise_for_status = MagicMock()
        return resp

    def test_excludes_support_process(self):
        items = [
            self._make_item(repo="glific"),
            self._make_item(repo="support-process"),
        ]
        with patch("pr_merge_time.requests.get", return_value=self._search_resp(items)):
            prs = _fetch_merged_prs("glific", "2026-06-01", "2026-06-14", "token")
        assert len(prs) == 1

    def test_excludes_dependabot(self):
        item = self._make_item()
        item["user"] = {"login": "dependabot[bot]"}
        with patch("pr_merge_time.requests.get", return_value=self._search_resp([item])):
            prs = _fetch_merged_prs("glific", "2026-06-01", "2026-06-14", "token")
        assert prs == []

    def test_skips_items_without_merged_at(self):
        item = self._make_item()
        item["pull_request"]["merged_at"] = None
        with patch("pr_merge_time.requests.get", return_value=self._search_resp([item])):
            prs = _fetch_merged_prs("glific", "2026-06-01", "2026-06-14", "token")
        assert prs == []

    def test_paginates(self):
        items_page1 = [self._make_item()] * 100
        items_page2 = [self._make_item()] * 5
        resp1 = self._search_resp(items_page1)
        resp2 = self._search_resp(items_page2)
        with patch("pr_merge_time.requests.get", side_effect=[resp1, resp2]):
            prs = _fetch_merged_prs("glific", "2026-06-01", "2026-06-14", "token")
        assert len(prs) == 105

    def test_returns_created_and_merged_at(self):
        items = [self._make_item()]
        with patch("pr_merge_time.requests.get", return_value=self._search_resp(items)):
            prs = _fetch_merged_prs("glific", "2026-06-01", "2026-06-14", "token")
        assert "created_at" in prs[0]
        assert "merged_at" in prs[0]


# ── _ensure_sheet_tab ─────────────────────────────────────────────────────────

class TestEnsureSheetTab:
    def test_creates_tab_if_missing(self):
        sheets = MagicMock()
        sheets.get().execute.return_value = {"sheets": [{"properties": {"title": "Sheet1"}}]}
        _ensure_sheet_tab(sheets, "sid")
        sheets.batchUpdate.assert_called_once()

    def test_skips_if_tab_exists(self):
        sheets = MagicMock()
        sheets.get().execute.return_value = {
            "sheets": [{"properties": {"title": SHEET_NAME}}]
        }
        _ensure_sheet_tab(sheets, "sid")
        sheets.batchUpdate.assert_not_called()


# ── _ensure_header ────────────────────────────────────────────────────────────

class TestEnsureHeader:
    def test_writes_header_when_missing(self):
        sheets = MagicMock()
        sheets.values().get().execute.return_value = {"values": []}
        _ensure_header(sheets, "sid")
        sheets.values().update.assert_called_once()

    def test_skips_header_when_present(self):
        sheets = MagicMock()
        sheets.values().get().execute.return_value = {"values": [["Iteration"]]}
        _ensure_header(sheets, "sid")
        sheets.values().update.assert_not_called()


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
        assert _find_row(sheets, "sid", "Sprint X") is None

    def test_empty_sheet(self):
        sheets = self._sheets([])
        assert _find_row(sheets, "sid", "Sprint 1") is None


# ── _write_row ────────────────────────────────────────────────────────────────

class TestWriteRow:
    def test_uses_correct_sheet_range(self):
        sheets = MagicMock()
        _write_row(sheets, "sid", 5, ["Sprint 1", "2026-06-01"])
        call_args = sheets.values().update.call_args
        assert f"'{SHEET_NAME}'!A5:F5" in str(call_args)
