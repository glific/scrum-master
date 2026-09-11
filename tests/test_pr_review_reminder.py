"""Tests for pr_review_reminder.py"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from pr_review_reminder import (
    CORE_TEAM,
    DESCRIPTION_LIMIT,
    INCLUDED_REPOS,
    _age_label,
    _fetch_open_prs,
    _fetch_reviewers,
    _is_approved,
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

    def test_combines_pending_and_ever_requested(self):
        requested_resp = self._make_resp(200, {"users": [{"login": "alice"}]})
        timeline_resp = self._make_resp(200, [
            {"event": "review_requested", "requested_reviewer": {"login": "bob"}},
        ])
        with patch("pr_review_reminder.requests.get", side_effect=[requested_resp, timeline_resp]):
            result = _fetch_reviewers("glific", "glific", 1, {})
        assert set(result) == {"alice", "bob"}

    def test_deduplicates_reviewers(self):
        requested_resp = self._make_resp(200, {"users": [{"login": "alice"}]})
        timeline_resp = self._make_resp(200, [
            {"event": "review_requested", "requested_reviewer": {"login": "alice"}},
        ])
        with patch("pr_review_reminder.requests.get", side_effect=[requested_resp, timeline_resp]):
            result = _fetch_reviewers("glific", "glific", 1, {})
        assert result.count("alice") == 1

    def test_filters_coderabbit_bot(self):
        requested_resp = self._make_resp(200, {"users": []})
        timeline_resp = self._make_resp(200, [
            {"event": "review_requested", "requested_reviewer": {"login": "coderabbitai[bot]"}},
            {"event": "review_requested", "requested_reviewer": {"login": "bob"}},
        ])
        with patch("pr_review_reminder.requests.get", side_effect=[requested_resp, timeline_resp]):
            result = _fetch_reviewers("glific", "glific", 1, {})
        assert "coderabbitai[bot]" not in result
        assert "bob" in result

    def test_excludes_users_who_only_commented_without_being_requested(self):
        requested_resp = self._make_resp(200, {"users": []})
        timeline_resp = self._make_resp(200, [
            {"event": "review_requested", "requested_reviewer": {"login": "alice"}},
        ])
        with patch("pr_review_reminder.requests.get", side_effect=[requested_resp, timeline_resp]):
            result = _fetch_reviewers("glific", "glific", 1, {})
        assert set(result) == {"alice"}
        assert "bob" not in result

    def test_honors_review_request_removed(self):
        requested_resp = self._make_resp(200, {"users": []})
        timeline_resp = self._make_resp(200, [
            {"event": "review_requested", "requested_reviewer": {"login": "alice"}},
            {"event": "review_request_removed", "requested_reviewer": {"login": "alice"}},
        ])
        with patch("pr_review_reminder.requests.get", side_effect=[requested_resp, timeline_resp]):
            result = _fetch_reviewers("glific", "glific", 1, {})
        assert result == []

    def test_re_request_after_removal_keeps_reviewer(self):
        requested_resp = self._make_resp(200, {"users": []})
        timeline_resp = self._make_resp(200, [
            {"event": "review_requested", "requested_reviewer": {"login": "alice"}},
            {"event": "review_request_removed", "requested_reviewer": {"login": "alice"}},
            {"event": "review_requested", "requested_reviewer": {"login": "alice"}},
        ])
        with patch("pr_review_reminder.requests.get", side_effect=[requested_resp, timeline_resp]):
            result = _fetch_reviewers("glific", "glific", 1, {})
        assert result == ["alice"]

    def test_handles_api_errors_gracefully(self):
        requested_resp = self._make_resp(403, {})
        timeline_resp = self._make_resp(403, {})
        with patch("pr_review_reminder.requests.get", side_effect=[requested_resp, timeline_resp]):
            result = _fetch_reviewers("glific", "glific", 1, {})
        assert result == []


# ── _is_approved ──────────────────────────────────────────────────────────────

class TestIsApproved:
    def _make_resp(self, status, json_data):
        r = MagicMock()
        r.status_code = status
        r.json.return_value = json_data
        return r

    def _call(self, status, reviews):
        with patch("pr_review_reminder.requests.get", return_value=self._make_resp(status, reviews)):
            return _is_approved("glific", "glific", 1, {})

    def test_no_reviews_is_not_approved(self):
        assert self._call(200, []) is False

    def test_approval_counts(self):
        assert self._call(200, [
            {"state": "APPROVED", "user": {"login": "alice"}},
        ]) is True

    def test_comment_only_is_not_approved(self):
        assert self._call(200, [
            {"state": "COMMENTED", "user": {"login": "alice"}},
        ]) is False

    def test_changes_requested_is_not_approved(self):
        assert self._call(200, [
            {"state": "CHANGES_REQUESTED", "user": {"login": "alice"}},
        ]) is False

    def test_later_changes_requested_supersedes_approval(self):
        assert self._call(200, [
            {"state": "APPROVED", "user": {"login": "alice"}},
            {"state": "CHANGES_REQUESTED", "user": {"login": "alice"}},
        ]) is False

    def test_comment_after_approval_keeps_approval(self):
        assert self._call(200, [
            {"state": "APPROVED", "user": {"login": "alice"}},
            {"state": "COMMENTED", "user": {"login": "alice"}},
        ]) is True

    def test_dismissed_approval_is_not_approved(self):
        assert self._call(200, [
            {"state": "APPROVED", "user": {"login": "alice"}},
            {"state": "DISMISSED", "user": {"login": "alice"}},
        ]) is False

    def test_one_approval_among_several_reviewers_counts(self):
        assert self._call(200, [
            {"state": "CHANGES_REQUESTED", "user": {"login": "alice"}},
            {"state": "APPROVED", "user": {"login": "bob"}},
        ]) is True

    def test_handles_api_errors_gracefully(self):
        assert self._call(403, {}) is False


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
        lines = _pr_lines(prs)
        assert len(lines) == 1
        assert "Fix thing" in lines[0]
        assert "3 days" in lines[0]

    def test_returns_line_per_pr(self):
        prs = [self._make_pr(title=f"PR {i}") for i in range(25)]
        lines = _pr_lines(prs)
        assert len(lines) == 25

    def test_one_day_label(self):
        prs = [self._make_pr(age=1)]
        lines = _pr_lines(prs)
        assert "open for 1 day" in lines[0]
        assert "1 days" not in lines[0]

    def test_pr_opened_today(self):
        prs = [self._make_pr(age=0)]
        lines = _pr_lines(prs)
        assert "opened today" in lines[0]
        assert "0 days" not in lines[0]


# ── build_payload ─────────────────────────────────────────────────────────────

class TestBuildPayload:
    def _make_pr(self, author, age=3, reviewers=None):
        return {
            "title": "Some PR",
            "url": "https://github.com/glific/glific/pull/1",
            "age_days": age,
            "author": author,
            "reviewers": reviewers or [],
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
        assert "review call" in embed["description"]

    def test_groups_prs_by_reviewer(self):
        team_member = next(iter(CORE_TEAM))
        prs = [
            self._make_pr(team_member, reviewers=["alice"]),
            self._make_pr(team_member, reviewers=["bob"]),
        ]
        payload = build_payload(prs)
        description = payload["embeds"][0]["description"]
        assert "**alice**" in description
        assert "**bob**" in description

    def test_pr_with_no_reviewer_goes_to_unassigned_section(self):
        team_member = next(iter(CORE_TEAM))
        prs = [self._make_pr(team_member, reviewers=[])]
        payload = build_payload(prs)
        description = payload["embeds"][0]["description"]
        assert "**Unassigned**" in description

    def test_pr_with_multiple_reviewers_appears_in_each_section(self):
        team_member = next(iter(CORE_TEAM))
        prs = [self._make_pr(team_member, reviewers=["alice", "bob"])]
        payload = build_payload(prs)
        description = payload["embeds"][0]["description"]
        assert "**alice**" in description
        assert "**bob**" in description

    def test_unassigned_section_sorted_last(self):
        team_member = next(iter(CORE_TEAM))
        prs = [
            self._make_pr(team_member, reviewers=[]),
            self._make_pr(team_member, reviewers=["alice"]),
        ]
        payload = build_payload(prs)
        description = payload["embeds"][0]["description"]
        assert description.index("**alice**") < description.index("**Unassigned**")

    def test_lists_every_pr_when_it_fits(self):
        team_member = next(iter(CORE_TEAM))
        prs = [self._make_pr(team_member) for _ in range(25)]
        description = build_payload(prs)["embeds"][0]["description"]
        assert description.count("• **[Some PR]") == 25
        assert "…and" not in description

    def test_trims_and_reports_overflow_past_discord_limit(self):
        team_member = next(iter(CORE_TEAM))
        prs = [self._make_pr(team_member) for _ in range(300)]
        description = build_payload(prs)["embeds"][0]["description"]
        assert len(description) <= DESCRIPTION_LIMIT
        shown = description.count("• **[Some PR]")
        assert 0 < shown < 300
        assert f"…and {300 - shown} more" in description

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


# ── _fetch_open_prs ──────────────────────────────────────────────────────────

class TestFetchOpenPrs:
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

    def _mock_reviews(self, reviews):
        resp = MagicMock(status_code=200)
        resp.json.return_value = reviews
        return resp

    def _mock_reviewers(self):
        """The two responses _fetch_reviewers consumes: requested_reviewers, then timeline."""
        requested = MagicMock(status_code=200)
        requested.json.return_value = {"users": []}
        timeline = MagicMock(status_code=200)
        timeline.json.return_value = []
        return [requested, timeline]

    def test_search_query_has_no_age_cutoff(self):
        with patch("pr_review_reminder.requests.get", return_value=self._mock_search([])) as get:
            _fetch_open_prs("glific", "token")
        query = get.call_args.kwargs["params"]["q"]
        assert query == "is:pr is:open draft:false org:glific"
        assert "created:" not in query

    def test_includes_pr_opened_today(self):
        items = [self._make_item(created_days_ago=0)]

        with patch("pr_review_reminder.requests.get", side_effect=[
            self._mock_search(items),
            self._mock_reviews([]),
            *self._mock_reviewers(),
        ]):
            prs = _fetch_open_prs("glific", "token")

        assert len(prs) == 1
        assert prs[0]["age_days"] == 0

    def test_filters_out_excluded_repos(self):
        items = [
            self._make_item(repo="glific"),
            self._make_item(repo="some-other-repo", number=2),
        ]

        with patch("pr_review_reminder.requests.get", side_effect=[
            self._mock_search(items),
            self._mock_reviews([]),      # PR 1 not approved
            *self._mock_reviewers(),     # reviewers for PR 1
        ]):
            prs = _fetch_open_prs("glific", "token")

        assert len(prs) == 1
        assert prs[0]["repo"] == "glific"

    def test_includes_glific_frontend(self):
        items = [self._make_item(repo="glific-frontend", number=5)]

        with patch("pr_review_reminder.requests.get", side_effect=[
            self._mock_search(items),
            self._mock_reviews([]),
            *self._mock_reviewers(),
        ]):
            prs = _fetch_open_prs("glific", "token")

        assert len(prs) == 1
        assert prs[0]["repo"] == "glific-frontend"

    def test_skips_approved_prs(self):
        items = [self._make_item(repo="glific", number=1)]

        with patch("pr_review_reminder.requests.get", side_effect=[
            self._mock_search(items),
            self._mock_reviews([{"state": "APPROVED", "user": {"login": "alice"}}]),
        ]):
            prs = _fetch_open_prs("glific", "token")

        assert prs == []

    def test_keeps_unapproved_pr_alongside_approved_one(self):
        items = [
            self._make_item(repo="glific", number=1),
            self._make_item(repo="glific", number=2),
        ]

        with patch("pr_review_reminder.requests.get", side_effect=[
            self._mock_search(items),
            self._mock_reviews([{"state": "APPROVED", "user": {"login": "alice"}}]),
            self._mock_reviews([{"state": "COMMENTED", "user": {"login": "alice"}}]),
            *self._mock_reviewers(),
        ]):
            prs = _fetch_open_prs("glific", "token")

        assert [pr["number"] for pr in prs] == [2]
