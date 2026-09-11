#!/usr/bin/env python3
"""
Glific — Daily PR Review Reminder → Discord

Runs every weekday at 9:00 AM IST. Fetches every open PR across the Glific org
that is still awaiting a review, and posts a Discord reminder to get them
reviewed. PRs that already have an approval are left out — they need a merge,
not a reviewer.

Required env vars:
  GITHUB_TOKEN    - GitHub PAT (public repo read access is sufficient)
  ORG             - GitHub organisation name
  DISCORD_WEBHOOK - Discord webhook URL
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

GITHUB_REST_URL = "https://api.github.com/search/issues"
INCLUDED_REPOS  = {"glific", "glific-frontend"}
CORE_TEAM       = {"priyanshu6238", "shijithkjayan", "akanshaaa19", "AmishaBisht", "rvignesh89"}


# ── GitHub helpers ────────────────────────────────────────────────────────────


def _fetch_reviewers(org, repo, number, headers):
    """Return logins formally assigned to review — pending requests plus anyone
    who was ever requested (per the issue timeline), honoring explicit removals.

    Submitted reviews are NOT used as a signal on their own: GitHub lets anyone
    leave a review (even just a comment) without being asked to, so treating
    every reviewer/commenter as "the reviewer" misattributes drive-by comments
    to people who were never assigned.
    """
    reviewers = set()

    resp = requests.get(
        f"https://api.github.com/repos/{org}/{repo}/pulls/{number}/requested_reviewers",
        headers=headers, timeout=10,
    )
    if resp.status_code == 200:
        for u in resp.json().get("users", []):
            reviewers.add(u["login"])

    resp = requests.get(
        f"https://api.github.com/repos/{org}/{repo}/issues/{number}/timeline",
        params={"per_page": 100}, headers=headers, timeout=10,
    )
    if resp.status_code == 200:
        assigned = {}
        for event in resp.json():
            login = (event.get("requested_reviewer") or {}).get("login", "")
            if not login or login == "coderabbitai[bot]":
                continue
            if event.get("event") == "review_requested":
                assigned[login] = True
            elif event.get("event") == "review_request_removed":
                assigned[login] = False
        reviewers.update(login for login, still_assigned in assigned.items() if still_assigned)

    return list(reviewers)


def _is_approved(org, repo, number, headers):
    """True when the PR already carries a standing approval.

    Only decisive reviews count: a plain comment leaves the verdict unchanged,
    while a later CHANGES_REQUESTED or a dismissal supersedes an earlier
    approval from the same person.
    """
    resp = requests.get(
        f"https://api.github.com/repos/{org}/{repo}/pulls/{number}/reviews",
        params={"per_page": 100}, headers=headers, timeout=10,
    )
    if resp.status_code != 200:
        return False

    verdicts = {}
    for review in resp.json():
        state = (review.get("state") or "").upper()
        if state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        login = (review.get("user") or {}).get("login", "")
        if login:
            verdicts[login] = state

    return "APPROVED" in verdicts.values()


def _fetch_open_prs(org, token):
    """Return every open, unapproved PR still awaiting review, oldest first."""
    query   = f"is:pr is:open draft:false org:{org}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    prs   = []
    page  = 1
    now   = datetime.now(tz=timezone.utc)

    while True:
        resp = requests.get(
            GITHUB_REST_URL,
            params={"q": query, "per_page": 100, "page": page, "sort": "created", "order": "asc"},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data  = resp.json()
        items = data.get("items", [])

        for item in items:
            repo_name = item.get("repository_url", "").split("/")[-1]
            if repo_name not in INCLUDED_REPOS:
                continue
            if _is_approved(org, repo_name, item["number"], headers):
                continue
            created_at = datetime.fromisoformat(item["created_at"].rstrip("Z")).replace(tzinfo=timezone.utc)
            age_days   = (now - created_at).days
            author    = (item.get("user") or {}).get("login", "")
            reviewers = _fetch_reviewers(org, repo_name, item["number"], headers)
            prs.append({
                "number":   item["number"],
                "title":    item["title"],
                "url":      item["html_url"],
                "repo":     repo_name,
                "age_days": age_days,
                "author":   author,
                "reviewers": reviewers,
            })

        if len(items) < 100:
            break
        page += 1

    return prs


# ── Discord payload builder ───────────────────────────────────────────────────


def _age_label(days):
    return "1 day" if days == 1 else f"{days} days"


def _pr_lines(prs):
    lines = []
    for pr in prs:
        age = pr["age_days"]
        age_text = "opened today" if age == 0 else f"open for {_age_label(age)}"
        lines.append(f"• **[{pr['title']}]({pr['url']})** — {age_text}")
    return lines


UNASSIGNED_LABEL  = "Unassigned"
HEADING           = "_Grouped by reviewer_\n\n"
SIGN_OFF          = "\n\nPlease get on a review call and make sure these get reviewed! 🙏"
DESCRIPTION_LIMIT = 4096   # Discord's hard cap on an embed description
OVERFLOW_RESERVE  = 40     # room for the "…and N more" note if we have to trim


def build_payload(prs):
    team_prs = [pr for pr in prs if pr["author"] in CORE_TEAM]

    if not team_prs:
        return None

    grouped = {}
    for pr in team_prs:
        for reviewer in pr["reviewers"] or [UNASSIGNED_LABEL]:
            grouped.setdefault(reviewer, []).append(pr)

    reviewers = sorted(r for r in grouped if r != UNASSIGNED_LABEL)
    if UNASSIGNED_LABEL in grouped:
        reviewers.append(UNASSIGNED_LABEL)

    # Every PR awaiting review gets listed; we only trim when the message would
    # otherwise exceed what Discord will accept.
    budget    = DESCRIPTION_LIMIT - len(HEADING) - len(SIGN_OFF) - OVERFLOW_RESERVE
    used      = 0
    shown     = 0
    sections  = []
    truncated = False

    for reviewer in reviewers:
        heading = f"**{reviewer}**"
        kept    = []
        for line in _pr_lines(grouped[reviewer]):
            cost = len(line) + 1 + (len(heading) + 2 if not kept else 0)
            if used + cost > budget:
                truncated = True
                break
            used  += cost
            shown += 1
            kept.append(line)
        if kept:
            sections.append(heading + "\n" + "\n".join(kept))
        if truncated:
            break

    overflow = sum(len(v) for v in grouped.values()) - shown
    body     = "\n\n".join(sections)
    if overflow > 0:
        body += f"\n_…and {overflow} more_"

    description = f"{HEADING}{body}{SIGN_OFF}"

    embed = {
        "title":       "🔔  PR Review Reminder",
        "description": description,
        "color":       0xE67E22,
        "footer":      {"text": f"Glific  •  {date.today().isoformat()}"},
    }
    return {"embeds": [embed]}


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Post PR review reminder to Discord.")
    parser.add_argument("--dry-run", action="store_true", help="Print payload instead of posting")
    args = parser.parse_args()

    token   = os.environ.get("GITHUB_TOKEN")
    org     = os.environ.get("ORG")
    webhook = os.environ.get("DISCORD_WEBHOOK")

    errors = []
    if not token:  errors.append("GITHUB_TOKEN is not set.")
    if not org:    errors.append("ORG is not set.")
    if not webhook and not args.dry_run:
        errors.append("DISCORD_WEBHOOK is not set.")
    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching open PRs in {org} that are awaiting review…")
    try:
        prs = _fetch_open_prs(org, token)
    except requests.HTTPError as e:
        print(f"GitHub API error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(prs)} PR(s).")

    payload = build_payload(prs)
    if payload is None:
        print("No team PRs awaiting review — skipping Discord post.")
        return

    if args.dry_run:
        print("\n── Dry-run payload ──────────────────────────────────────")
        print(json.dumps(payload, indent=2))
        return

    try:
        resp = requests.post(webhook, json=payload, timeout=10)
        resp.raise_for_status()
        print("Posted to Discord successfully.")
    except requests.HTTPError as e:
        print(f"Discord error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
