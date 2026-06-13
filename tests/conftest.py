"""Shared fixtures and helpers for the scrum-master test suite."""

import pytest


# ── Iteration field fixtures ──────────────────────────────────────────────────

def make_iteration_field(iterations=None, completed=None):
    """Build a ProjectV2IterationField node suitable for _find_*_iteration helpers."""
    return {
        "__typename": "ProjectV2IterationField",
        "id": "field-1",
        "name": "Iteration",
        "configuration": {
            "iterations": iterations or [],
            "completedIterations": completed or [],
        },
    }


def make_iteration(id_, title, start_date, duration=14):
    return {"id": id_, "title": title, "startDate": start_date, "duration": duration}


# ── Project item fixtures ─────────────────────────────────────────────────────

def make_issue_item(
    title="Test Issue",
    repo="glific/glific",
    number=42,
    status="To Do",
    iteration_id="iter-1",
    labels=None,
    assignees=None,
    has_pr_events=0,
):
    return {
        "id": f"item-{number}",
        "content": {
            "__typename": "Issue",
            "number": number,
            "title": title,
            "url": f"https://github.com/{repo}/issues/{number}",
            "labels": {"nodes": [{"name": l} for l in (labels or [])]},
            "repository": {"nameWithOwner": repo},
            "assignees": {"nodes": [{"login": a, "name": None} for a in (assignees or [])]},
            "timelineItems": {"totalCount": has_pr_events},
        },
        "fieldValues": {
            "nodes": [
                {
                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                    "name": status,
                    "field": {"name": "Status"},
                },
                {
                    "__typename": "ProjectV2ItemFieldIterationValue",
                    "iterationId": iteration_id,
                    "title": "Iteration 1",
                    "startDate": "2026-06-01",
                    "duration": 14,
                    "field": {"name": "Iteration"},
                },
            ]
        },
    }


def make_pr_item(
    title="Test PR",
    repo="glific/glific",
    number=10,
    status="In Review",
    iteration_id="iter-1",
):
    return {
        "id": f"item-pr-{number}",
        "content": {
            "__typename": "PullRequest",
            "number": number,
            "title": title,
            "url": f"https://github.com/{repo}/pull/{number}",
            "labels": {"nodes": []},
            "repository": {"nameWithOwner": repo},
            "assignees": {"nodes": []},
        },
        "fieldValues": {
            "nodes": [
                {
                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                    "name": status,
                    "field": {"name": "Status"},
                },
                {
                    "__typename": "ProjectV2ItemFieldIterationValue",
                    "iterationId": iteration_id,
                    "title": "Iteration 1",
                    "startDate": "2026-06-01",
                    "duration": 14,
                    "field": {"name": "Iteration"},
                },
            ]
        },
    }


# ── Paginated items response ──────────────────────────────────────────────────

def paginate_response(items, has_next=False, cursor="cursor-abc"):
    return {
        "organization": {
            "projectV2": {
                "items": {
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": items,
                }
            }
        }
    }
