"""Tests for glific_iteration_update.py"""

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest
import requests

from glific_iteration_update import (
    _bar_color,
    _display_status,
    _find_current_iteration,
    _find_previous_iteration,
    _format_range,
    _gql,
    _ordinal,
    _paginate_items,
    _progress_bar,
    _should_skip,
    build_messages,
    fetch_current_iteration,
)
from tests.conftest import (
    make_iteration,
    make_iteration_field,
    make_issue_item,
    make_pr_item,
    paginate_response,
)


# ── _ordinal ──────────────────────────────────────────────────────────────────

class TestOrdinal:
    def test_first(self):
        assert _ordinal(1) == "1st"

    def test_second(self):
        assert _ordinal(2) == "2nd"

    def test_third(self):
        assert _ordinal(3) == "3rd"

    def test_fourth(self):
        assert _ordinal(4) == "4th"

    def test_eleventh(self):
        assert _ordinal(11) == "11th"

    def test_twelfth(self):
        assert _ordinal(12) == "12th"

    def test_thirteenth(self):
        assert _ordinal(13) == "13th"

    def test_twenty_first(self):
        assert _ordinal(21) == "21st"

    def test_hundred_eleventh(self):
        assert _ordinal(111) == "111th"


# ── _format_range ─────────────────────────────────────────────────────────────

class TestFormatRange:
    def test_same_month(self):
        result = _format_range("2026-06-01", "2026-06-14")
        assert "1st June" in result
        assert "14th June" in result
        assert "–" in result

    def test_cross_month(self):
        result = _format_range("2026-05-26", "2026-06-08")
        assert "May" in result
        assert "June" in result


# ── _progress_bar ─────────────────────────────────────────────────────────────

class TestProgressBar:
    def test_all_done(self):
        bar = _progress_bar(100, 0)
        assert "🟩" in bar
        assert "🟨" not in bar
        assert "⬜" not in bar
        assert "100%" in bar

    def test_all_todo(self):
        bar = _progress_bar(0, 0)
        assert "⬜" in bar
        assert "🟩" not in bar
        assert "0%" in bar

    def test_mixed(self):
        bar = _progress_bar(50, 20)
        assert "🟩" in bar
        assert "🟨" in bar
        assert "⬜" in bar
        assert "50%" in bar

    def test_bar_length(self):
        bar = _progress_bar(0, 0, length=5)
        assert bar.count("⬜") == 5

    def test_inprogress_does_not_exceed_length(self):
        # 80% done + 80% inprogress would overflow without a clamp
        bar = _progress_bar(80, 80, length=10)
        total_blocks = bar.count("🟩") + bar.count("🟨") + bar.count("⬜")
        assert total_blocks == 10


# ── _bar_color ────────────────────────────────────────────────────────────────

class TestBarColor:
    def test_green_at_70(self):
        assert _bar_color(70) == 0x2ECC71

    def test_green_above_70(self):
        assert _bar_color(100) == 0x2ECC71

    def test_yellow_at_30(self):
        assert _bar_color(30) == 0xF1C40F

    def test_yellow_at_69(self):
        assert _bar_color(69) == 0xF1C40F

    def test_red_below_30(self):
        assert _bar_color(0) == 0xE74C3C

    def test_red_at_29(self):
        assert _bar_color(29) == 0xE74C3C


# ── _display_status ───────────────────────────────────────────────────────────

class TestDisplayStatus:
    @pytest.mark.parametrize("raw,expected", [
        ("done",        "Done"),
        ("Done",        "Done"),
        ("DONE",        "Done"),
        ("closed",      "Done"),
        ("in review",   "In Review"),
        ("In Review",   "In Review"),
        ("in progress", "In Progress"),
        ("to do",       "To Do"),
        ("todo",        "To Do"),
        (None,          "No status"),
        ("",            "No status"),
        ("unknown",     "unknown"),
    ])
    def test_mapping(self, raw, expected):
        assert _display_status(raw) == expected


# ── _should_skip ──────────────────────────────────────────────────────────────

class TestShouldSkip:
    def test_support_process_repo(self):
        content = {"repository": {"nameWithOwner": "glific/support-process"}, "title": "Normal"}
        assert _should_skip(content) is True

    def test_epic_title(self):
        content = {"repository": {"nameWithOwner": "glific/glific"}, "title": "[Epic] Big Feature"}
        assert _should_skip(content) is True

    def test_normal_item(self):
        content = {"repository": {"nameWithOwner": "glific/glific"}, "title": "Fix bug #123"}
        assert _should_skip(content) is False

    def test_no_repository_key(self):
        content = {"title": "Draft item"}
        assert _should_skip(content) is False


# ── _find_current_iteration ───────────────────────────────────────────────────

class TestFindCurrentIteration:
    def _fields_for(self, start_offset, duration=14):
        today = date.today()
        start = today + timedelta(days=start_offset)
        field = make_iteration_field(
            iterations=[make_iteration("iter-1", "Sprint 1", start.isoformat(), duration)]
        )
        return [field]

    def test_today_in_iteration(self):
        fields = self._fields_for(start_offset=-3)
        result = _find_current_iteration(fields)
        assert result is not None
        assert result["id"] == "iter-1"
        assert result["title"] == "Sprint 1"

    def test_today_is_start_date(self):
        fields = self._fields_for(start_offset=0)
        result = _find_current_iteration(fields)
        assert result is not None

    def test_today_past_end(self):
        fields = self._fields_for(start_offset=-20, duration=14)
        result = _find_current_iteration(fields)
        assert result is None

    def test_today_before_start(self):
        fields = self._fields_for(start_offset=5)
        result = _find_current_iteration(fields)
        assert result is None

    def test_non_iteration_field_skipped(self):
        fields = [{"__typename": "ProjectV2TextField", "name": "Title"}]
        result = _find_current_iteration(fields)
        assert result is None

    def test_days_elapsed_calculation(self):
        today = date.today()
        start = today - timedelta(days=3)
        field = make_iteration_field(
            iterations=[make_iteration("iter-1", "Sprint 1", start.isoformat(), 14)]
        )
        result = _find_current_iteration([field])
        assert result["days_elapsed"] == 4  # day 1 is the start day


# ── _find_previous_iteration ──────────────────────────────────────────────────

class TestFindPreviousIteration:
    def test_returns_most_recent_completed(self):
        fields = [
            make_iteration_field(completed=[
                make_iteration("iter-old", "Sprint 0", "2026-05-01", 14),
                make_iteration("iter-recent", "Sprint 1", "2026-05-15", 14),
            ])
        ]
        result = _find_previous_iteration(fields)
        assert result["id"] == "iter-recent"
        assert result["title"] == "Sprint 1"

    def test_no_completed_iterations(self):
        fields = [make_iteration_field(completed=[])]
        result = _find_previous_iteration(fields)
        assert result is None

    def test_dates_are_correct(self):
        fields = [
            make_iteration_field(completed=[
                make_iteration("iter-1", "Sprint 1", "2026-05-01", 14)
            ])
        ]
        result = _find_previous_iteration(fields)
        assert result["start"] == "2026-05-01"
        assert result["end"] == "2026-05-14"  # start + duration - 1 day


# ── _gql ─────────────────────────────────────────────────────────────────────

class TestGql:
    def test_successful_query(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"data": {"organization": {}}}
        mock_resp.raise_for_status = MagicMock()
        with patch("glific_iteration_update.requests.post", return_value=mock_resp) as mock_post:
            result = _gql("query { }", {}, "token")
            assert result == {"organization": {}}
            mock_post.assert_called_once()

    def test_raises_on_graphql_errors(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"errors": [{"message": "Not found"}]}
        mock_resp.raise_for_status = MagicMock()
        with patch("glific_iteration_update.requests.post", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="GitHub GraphQL errors"):
                _gql("query { }", {}, "token")

    def test_raises_on_http_error(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = requests.HTTPError("401")
        with patch("glific_iteration_update.requests.post", return_value=mock_resp):
            with pytest.raises(requests.HTTPError):
                _gql("query { }", {}, "token")


# ── _paginate_items ───────────────────────────────────────────────────────────

class TestPaginateItems:
    def test_single_page(self):
        items = [make_issue_item()]
        with patch("glific_iteration_update._gql", return_value=paginate_response(items)):
            result = _paginate_items("org", 1, "token")
        assert len(result) == 1

    def test_multiple_pages(self):
        page1_items = [make_issue_item(number=i) for i in range(3)]
        page2_items = [make_issue_item(number=i + 10) for i in range(2)]

        responses = [
            paginate_response(page1_items, has_next=True, cursor="c1"),
            paginate_response(page2_items, has_next=False),
        ]
        with patch("glific_iteration_update._gql", side_effect=responses):
            result = _paginate_items("org", 1, "token")
        assert len(result) == 5

    def test_empty_project(self):
        with patch("glific_iteration_update._gql", return_value=paginate_response([])):
            result = _paginate_items("org", 1, "token")
        assert result == []


# ── fetch_current_iteration ───────────────────────────────────────────────────

class TestFetchCurrentIteration:
    def _make_meta(self, iteration_id="iter-1"):
        today = date.today()
        start = today - timedelta(days=3)
        return {
            "organization": {
                "projectV2": {
                    "id": "proj-1",
                    "title": "Glific Board",
                    "fields": {
                        "nodes": [
                            make_iteration_field(
                                iterations=[make_iteration(iteration_id, "Sprint 1", start.isoformat())]
                            )
                        ]
                    },
                }
            }
        }

    def test_filters_to_current_iteration(self):
        meta = self._make_meta("iter-1")
        items = [
            make_issue_item(number=1, iteration_id="iter-1", status="Done"),
            make_issue_item(number=2, iteration_id="iter-OTHER", status="To Do"),
        ]
        with patch("glific_iteration_update._gql", side_effect=[meta, paginate_response(items)]):
            result = fetch_current_iteration("glific", 8, "token")
        assert len(result["items"]) == 1
        assert result["items"][0]["status"] == "Done"

    def test_skips_support_process(self):
        meta = self._make_meta("iter-1")
        items = [
            make_issue_item(number=1, repo="glific/support-process", iteration_id="iter-1"),
            make_issue_item(number=2, repo="glific/glific", iteration_id="iter-1"),
        ]
        with patch("glific_iteration_update._gql", side_effect=[meta, paginate_response(items)]):
            result = fetch_current_iteration("glific", 8, "token")
        assert len(result["items"]) == 1
        assert result["support_skipped"] == 1

    def test_pr_has_pr_true(self):
        meta = self._make_meta("iter-1")
        items = [make_pr_item(number=5, iteration_id="iter-1")]
        with patch("glific_iteration_update._gql", side_effect=[meta, paginate_response(items)]):
            result = fetch_current_iteration("glific", 8, "token")
        assert result["items"][0]["has_pr"] is True

    def test_issue_without_pr_link(self):
        meta = self._make_meta("iter-1")
        items = [make_issue_item(number=3, iteration_id="iter-1", has_pr_events=0)]
        with patch("glific_iteration_update._gql", side_effect=[meta, paginate_response(items)]):
            result = fetch_current_iteration("glific", 8, "token")
        assert result["items"][0]["has_pr"] is False

    def test_returns_none_when_no_active_iteration(self):
        meta = {
            "organization": {
                "projectV2": {
                    "id": "proj-1",
                    "title": "Glific Board",
                    "fields": {"nodes": [make_iteration_field(iterations=[])]},
                }
            }
        }
        with patch("glific_iteration_update._gql", return_value=meta):
            result = fetch_current_iteration("glific", 8, "token")
        assert result is None


# ── build_messages ────────────────────────────────────────────────────────────

class TestBuildMessages:
    def _make_data(self, items):
        return {
            "project_title": "Glific Board",
            "iteration_title": "Sprint 1",
            "starts_at": "2026-06-01",
            "ends_at": "2026-06-14",
            "days_elapsed": 5,
            "total_days": 14,
            "items": items,
            "support_skipped": 0,
        }

    def test_returns_list_with_one_embed(self):
        items = [
            {"status": "Done", "identifier": "glific#1", "title": "Fix auth", "url": "http://x", "has_pr": True, "labels": [], "assignees": ["alice"]},
        ]
        with patch("glific_iteration_update._summarise_done", return_value="• Fixed auth"):
            payloads = build_messages(self._make_data(items))
        assert isinstance(payloads, list)
        assert len(payloads) == 1
        assert "embeds" in payloads[0]

    def test_completion_percentage_in_footer(self):
        items = [
            {"status": "Done", "identifier": "g#1", "title": "A", "url": "u", "has_pr": True, "labels": [], "assignees": []},
            {"status": "To Do", "identifier": "g#2", "title": "B", "url": "u", "has_pr": False, "labels": [], "assignees": []},
        ]
        with patch("glific_iteration_update._summarise_done", return_value="• A done"):
            payloads = build_messages(self._make_data(items))
        footer = payloads[0]["embeds"][0]["footer"]["text"]
        assert "50%" in payloads[0]["embeds"][0]["description"]

    def test_empty_items(self):
        with patch("glific_iteration_update._summarise_done", return_value="_None yet_"):
            payloads = build_messages(self._make_data([]))
        assert payloads[0]["embeds"][0] is not None

    def test_support_skipped_note_shown(self):
        data = self._make_data([])
        data["support_skipped"] = 3
        with patch("glific_iteration_update._summarise_done", return_value="_None yet_"):
            payloads = build_messages(data)
        desc = payloads[0]["embeds"][0]["description"]
        assert "3 item(s) excluded" in desc
