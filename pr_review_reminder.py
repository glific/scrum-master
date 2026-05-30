#!/usr/bin/env python3
"""
Glific — Daily PR Review Reminder → Discord

Runs every weekday at 3:30 PM IST. Fetches all open PRs across the Glific org
that have been open for more than 1 day, and posts a Discord reminder for the
4:00 PM review meeting.

Required env vars:
  GITHUB_TOKEN    - GitHub PAT (public repo read access is sufficient)
  ORG             - GitHub organisation name
  DISCORD_WEBHOOK - Discord webhook URL
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

GITHUB_REST_URL = "https://api.github.com/search/issues"
INCLUDED_REPOS  = {"glific", "glific-frontend"}
CORE_TEAM       = {"priyanshu6238", "shijithkjayan", "akanshaaa19", "AmishaBisht", "rvignesh89", "dlobo", "mdshamoon"}


# ── GitHub helpers ────────────────────────────────────────────────────────────


def _fetch_reviewers(org, repo, number, headers):
    """Return all reviewer logins — both pending requests and submitted reviews."""
    reviewers = set()

    resp = requests.get(
        f"https://api.github.com/repos/{org}/{repo}/pulls/{number}/requested_reviewers",
        headers=headers, timeout=10,
    )
    if resp.status_code == 200:
        for u in resp.json().get("users", []):
            reviewers.add(u["login"])

    resp = requests.get(
        f"https://api.github.com/repos/{org}/{repo}/pulls/{number}/reviews",
        headers=headers, timeout=10,
    )
    if resp.status_code == 200:
        for r in resp.json():
            login = (r.get("user") or {}).get("login", "")
            if login and login != "coderabbitai[bot]":
                reviewers.add(login)

    return list(reviewers)


def _fetch_stale_prs(org, token):
    """Return open PRs that have been open for more than 1 day, oldest first."""
    cutoff  = (date.today() - timedelta(days=1)).isoformat()
    query   = f"is:pr is:open draft:false org:{org} created:<{cutoff}"
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
            created_at = datetime.fromisoformat(item["created_at"].rstrip("Z")).replace(tzinfo=timezone.utc)
            age_days   = (now - created_at).days
            author    = (item.get("user") or {}).get("login", "")
            reviewers = _fetch_reviewers(org, repo_name, item["number"], headers)
            prs.append({
                "number":     item["number"],
                "title":      item["title"],
                "url":        item["html_url"],
                "repo":       repo_name,
                "age_days":   age_days,
                "author":     author,
                "reviewers":  reviewers,
                "dependabot": author == "dependabot[bot]",
            })

        if len(items) < 100:
            break
        page += 1

    return prs


# ── Discord payload builder ───────────────────────────────────────────────────


def _age_label(days):
    return "1 day" if days == 1 else f"{days} days"


def _pr_lines(prs, limit):
    lines = []
    for pr in prs[:limit]:
        reviewer_str = ", ".join(pr["reviewers"]) if pr["reviewers"] else "No reviewer assigned"
        lines.append(
            f"• **[{pr['title']}]({pr['url']})** is open for more than {_age_label(pr['age_days'])}"
            f"\n  Author: {pr['author']}  •  Reviewer: {reviewer_str}"
        )
    return lines


def build_payload(prs):
    if not prs:
        return None

    team_prs      = [pr for pr in prs if not pr["dependabot"] and pr["author"] in CORE_TEAM]
    oss_prs       = [pr for pr in prs if not pr["dependabot"] and pr["author"] not in CORE_TEAM]
    dependabot    = [pr for pr in prs if pr["dependabot"]]

    sections = []

    if team_prs:
        lines    = _pr_lines(team_prs, 20)
        overflow = len(team_prs) - 20
        body     = "\n".join(lines)
        if overflow > 0:
            body += f"\n_…and {overflow} more_"
        sections.append(f"**👥 Team PRs**\n{body}")

    if oss_prs:
        lines    = _pr_lines(oss_prs, 20)
        overflow = len(oss_prs) - 20
        body     = "\n".join(lines)
        if overflow > 0:
            body += f"\n_…and {overflow} more_"
        sections.append(f"**🌍 Open Source Contributions**\n{body}")

    if dependabot:
        lines    = _pr_lines(dependabot, 10)
        overflow = len(dependabot) - 10
        body     = "\n".join(lines)
        if overflow > 0:
            body += f"\n_…and {overflow} more_"
        sections.append(f"**🤖 Dependabot PRs**\n{body}")

    description = "\n\n".join(sections)
    description += "\n\nPlease make sure to get on a call at **4:00 PM** for PR review and make sure these get reviewed! 🙏"

    embed = {
        "title":       "🔔  PR Review Reminder — 4:00 PM Today",
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

    print(f"Fetching open PRs in {org} that have been open for more than 1 day…")
    try:
        prs = _fetch_stale_prs(org, token)
    except requests.HTTPError as e:
        print(f"GitHub API error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(prs)} PR(s).")

    if not prs:
        print("No stale PRs — skipping Discord post.")
        return

    payload = build_payload(prs)

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
