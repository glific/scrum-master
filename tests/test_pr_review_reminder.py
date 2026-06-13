"""Tests for pr_review_reminder.py"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from pr_review_reminder import (
    CORE_TEAM,
    INCLUDED_REPOS,
    _age_label,
    _fetch_reviewers,
    _fetch_stale_prs,
    _pr_lines,
    build_payload,
)


# ── _age_label ────────────────────────────────────────────────────────────────

class TestAgeLabel:
    def test_one_day(self):
        assert _age_label(1) == "1 day"

    def test_multiple_days(self):
        assert _age_label(5) == "5 days"

    def test_zero_days(self):
        assert _age_label(0) == "0 days"


# ── _fetch_reviewers ──────────────────────────────────────────────────────────

class TestFetchReviewers:
    def _make_resp(self, status, json_data):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = json_data
        return r

    def test_combines_requested_and_submitted(self):
        requested_resp = self._make_resp(200, {"users": [{"login": "alice"}]})
        reviews_resp = self._make_resp(200, [
            {"user": {"login": "bob"}, "state": "APPROVED"},
        ])
        with patch("pr_review_reminder.requests.get", side_effect=[requested_resp, reviews_resp]):
            result = _fetch_reviewers("glific", "glific", 1, {})
        assert set(result) == {"alice", "bob"}

    def test_deduplicates_reviewers(self):
        requested_resp = self._make_resp(200, {"users": [{"login": "alice"}]})
        reviews_resp = self._make_resp(200, [
            {"user": {"login": "alice"}, "state": "APPROVED"},
        ])
        with patch("pr_review_reminder.requests.get", side_effect=[requested_resp, reviews_resp]):
            result = _fetch_reviewers("glific", "glific", 1, {})
        assert result.count("alice") == 1

    def test_filters_coderabbit_bot(self):
        requested_resp = self._make_resp(200, {"users": []})
        reviews_resp = self._make_resp(200, [
            {"user": {"login": "coderabbitai[bot]"}, "state": "APPROVED"},
            {"user": {"login": "bob"}, "state": "CHANGES_REQUESTED"},
        ])
        with patch("pr_review_reminder.requests.get", side_effect=[requested_resp, reviews_resp]):
            result = _fetch_reviewers("glific", "glific", 1, {})
        assert "coderabbitai[bot]" not in result
        assert "bob" in result

    def test_handles_api_errors_gracefully(self):
        requested_resp = self._make_resp(403, {})
        reviews_resp = self._make_resp(403, {})
        with patch("pr_review_reminder.requests.get", side_effect=[requested_resp, reviews_resp]):
            result = _fetch_reviewers("glific", "glific", 1, {})
        assert result == []


# ── _pr_lines ─────────────────────────────────────────────────────────────────

class TestPrLines:
    def _make_pr(self, title="Fix thing", age=3, reviewer=None, author="shijithkjayan"):
        return {
            "title": title,
            "url": "https://github.com/glific/glific/pull/1",
            "age_days": age,
            "author": author,
            "reviewers": [reviewer] if reviewer else [],
            "repo": "glific",
            "number": 1,
        }

    def test_formats_pr_line(self):
        prs = [self._make_pr()]
        lines = _pr_lines(prs, limit=10)
        assert len(lines) == 1
        assert "Fix thing" in lines[0]
        assert "3 days" in lines[0]

    def test_no_reviewer_assigned(self):
        prs = [self._make_pr(reviewer=None)]
        lines = _pr_lines(prs, limit=10)
        assert "No reviewer assigned" in lines[0]

    def test_shows_reviewer(self):
        prs = [self._make_pr(reviewer="alice")]
        lines = _pr_lines(prs, limit=10)
        assert "alice" in lines[0]

    def test_respects_limit(self):
        prs = [self._make_pr(title=f"PR {i}") for i in range(25)]
        lines = _pr_lines(prs, limit=20)
        assert len(lines) == 20

    def test_one_day_label(self):
        prs = [self._make_pr(age=1)]
        lines = _pr_lines(prs, limit=10)
        assert "1 day" in lines[0]
        assert "1 days" not in lines[0]


# ── build_payload ─────────────────────────────────────────────────────────────

class TestBuildPayload:
    def _make_pr(self, author, age=3):
        return {
            "title": "Some PR",
            "url": "https://github.com/glific/glific/pull/1",
            "age_days": age,
            "author": author,
            "reviewers": [],
            "repo": "glific",
            "number": 1,
        }

    def test_no_team_prs_returns_none(self):
        prs = [self._make_pr("external-user")]
        assert build_payload(prs) is None

    def test_team_pr_returns_payload(self):
        team_member = next(iter(CORE_TEAM))
        prs = [self._make_pr(team_member)]
        payload = build_payload(prs)
        assert payload is not None
        assert "embeds" in payload

    def test_embed_structure(self):
        team_member = next(iter(CORE_TEAM))
        prs = [self._make_pr(team_member)]
        payload = build_payload(prs)
        embed = payload["embeds"][0]
        assert "title" in embed
        assert "description" in embed
        assert "4:00 PM" in embed["description"]

    def test_overflow_text_shown(self):
        team_member = next(iter(CORE_TEAM))
        prs = [self._make_pr(team_member) for _ in range(25)]
        payload = build_payload(prs)
        assert "…and 5 more" in payload["embeds"][0]["description"]

    def test_no_overflow_when_under_limit(self):
        team_member = next(iter(CORE_TEAM))
        prs = [self._make_pr(team_member) for _ in range(5)]
        payload = build_payload(prs)
        assert "…and" not in payload["embeds"][0]["description"]

    def test_empty_pr_list_returns_none(self):
        assert build_payload([]) is None

    def test_mixes_team_and_non_team(self):
        team_member = next(iter(CORE_TEAM))
        prs = [self._make_pr("outsider"), self._make_pr(team_member)]
        payload = build_payload(prs)
        assert payload is not None
        # Only 1 team PR — no overflow
        assert "…and" not in payload["embeds"][0]["description"]


# ── _fetch_stale_prs ──────────────────────────────────────────────────────────

class TestFetchStalePrs:
    def _make_item(self, repo="glific", number=1, created_days_ago=5):
        created = (datetime.now(tz=timezone.utc) - timedelta(days=created_days_ago)).isoformat()
        return {
            "number": number,
            "title": "Test PR",
            "html_url": f"https://github.com/glific/{repo}/pull/{number}",
            "repository_url": f"https://api.github.com/repos/glific/{repo}",
            "created_at": created,
            "user": {"login": "alice"},
        }

    def _mock_search(self, items):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"items": items}
        resp.raise_for_status = MagicMock()
        return resp

    def test_filters_out_excluded_repos(self):
        items = [
            self._make_item(repo="glific"),
            self._make_item(repo="some-other-repo", number=2),
        ]
        reviewer_resp = MagicMock(status_code=200)
        reviewer_resp.json.return_value = {"users": []}

        review_resp = MagicMock(status_code=200)
        review_resp.json.return_value = []

        with patch("pr_review_reminder.requests.get", side_effect=[
            self._mock_search(items),
            reviewer_resp, review_resp,  # reviewers for PR 1
        ]):
            prs = _fetch_stale_prs("glific", "token")

        assert len(prs) == 1
        assert prs[0]["repo"] == "glific"

    def test_includes_glific_frontend(self):
        items = [self._make_item(repo="glific-frontend", number=5)]
        reviewer_resp = MagicMock(status_code=200)
        reviewer_resp.json.return_value = {"users": []}
        review_resp = MagicMock(status_code=200)
        review_resp.json.return_value = []

        with patch("pr_review_reminder.requests.get", side_effect=[
            self._mock_search(items),
            reviewer_resp, review_resp,
        ]):
            prs = _fetch_stale_prs("glific", "token")

        assert len(prs) == 1
        assert prs[0]["repo"] == "glific-frontend"
