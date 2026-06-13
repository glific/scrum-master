"""Tests for iteration_planner.py"""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from iteration_planner import _generate_plan_summary, _post_to_discord


# ── _generate_plan_summary ────────────────────────────────────────────────────

class TestGeneratePlanSummary:
    def test_no_items_returns_message(self):
        result = _generate_plan_summary([])
        assert "No planned tickets" in result

    def test_calls_claude_with_titles(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(type="text", text="• Ship the feature\n• Fix the bugs")]
        mock_client.messages.create.return_value = mock_resp

        items = [{"title": "Build new dashboard"}, {"title": "Fix login timeout"}]
        with patch("iteration_planner.anthropic.Anthropic", return_value=mock_client):
            result = _generate_plan_summary(items)

        assert "Ship the feature" in result
        call_args = mock_client.messages.create.call_args
        user_content = call_args[1]["messages"][0]["content"]
        assert "Build new dashboard" in user_content
        assert "Fix login timeout" in user_content

    def test_bullet_separation(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(type="text", text="• Goal 1\n• Goal 2")]
        mock_client.messages.create.return_value = mock_resp

        with patch("iteration_planner.anthropic.Anthropic", return_value=mock_client):
            result = _generate_plan_summary([{"title": "Thing"}])

        assert "\n\n•" in result

    def test_fallback_when_no_text_block(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(type="tool_use")]  # no text block
        mock_client.messages.create.return_value = mock_resp

        with patch("iteration_planner.anthropic.Anthropic", return_value=mock_client):
            result = _generate_plan_summary([{"title": "Thing"}])

        assert result == "Unable to generate plan."


# ── _post_to_discord ──────────────────────────────────────────────────────────

class TestPostToDiscord:
    def _data(self):
        return {
            "iteration_title": "Sprint 3",
            "starts_at": "2026-06-15",
            "ends_at": "2026-06-28",
        }

    def test_posts_embed_to_webhook(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch("iteration_planner.requests.post", return_value=mock_resp) as mock_post:
            _post_to_discord("https://discord.com/api/webhooks/123/abc", self._data(), 10, "• Plan")
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert "embeds" in payload

    def test_embed_has_iteration_title(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch("iteration_planner.requests.post", return_value=mock_resp) as mock_post:
            _post_to_discord("https://discord.com/api/webhooks/123/abc", self._data(), 10, "• Plan")
        embed = mock_post.call_args[1]["json"]["embeds"][0]
        assert "Sprint 3" in embed["title"]

    def test_embed_footer_shows_ticket_count(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        with patch("iteration_planner.requests.post", return_value=mock_resp) as mock_post:
            _post_to_discord("https://discord.com/api/webhooks/123/abc", self._data(), 15, "• Plan")
        embed = mock_post.call_args[1]["json"]["embeds"][0]
        assert "15 tickets planned" in embed["footer"]["text"]

    def test_embed_description_contains_summary(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        summary = "• We will ship amazing things"
        with patch("iteration_planner.requests.post", return_value=mock_resp) as mock_post:
            _post_to_discord("https://discord.com/api/webhooks/123/abc", self._data(), 5, summary)
        embed = mock_post.call_args[1]["json"]["embeds"][0]
        assert summary in embed["description"]

    def test_raises_on_http_error(self):
        import requests as req_lib
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req_lib.HTTPError("403")
        with patch("iteration_planner.requests.post", return_value=mock_resp):
            with pytest.raises(req_lib.HTTPError):
                _post_to_discord("https://discord.com/api/webhooks/123/abc", self._data(), 5, "• Plan")
