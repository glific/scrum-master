#!/usr/bin/env python3
"""
Glific — Iteration Completion Rate + Deliverables Summary → Discord
────────────────────────────────────────────────────────────────────
Fetches the current iteration from GitHub Projects v2, filters out
"support"-labelled tickets, calculates the completion rate, and posts
a rich summary to Discord.

Scheduled run : every Monday 9 AM IST via GitHub Actions cron
Manual run    : python glific_iteration_update.py [--dry-run]
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Constants ────────────────────────────────────────────────────────────────

GITHUB_API_URL   = "https://api.github.com/graphql"
SUPPORT_LABEL    = "support"          # tickets with this label are excluded

STATUS_ORDER     = ["Done", "In Review", "In Progress", "To Do"]
STATUS_CLOSED    = {"done", "closed"}
STATUS_ICONS     = {
    "Done":        "🟢",
    "In Review":   "🔍",
    "In Progress": "🟡",
    "To Do":       "⚪",
}
STATUS_NAME_MAP  = {
    "done":        "Done",
    "closed":      "Done",
    "in review":   "In Review",
    "in progress": "In Progress",
    "to do":       "To Do",
    "todo":        "To Do",
}

# ── GraphQL queries ──────────────────────────────────────────────────────────

PROJECT_META_QUERY = """
query ProjectMeta($org: String!, $number: Int!) {
  organization(login: $org) {
    projectV2(number: $number) {
      id
      title
      fields(first: 50) {
        nodes {
          __typename
          ... on ProjectV2IterationField {
            id
            name
            configuration {
              iterations {
                id title startDate duration
              }
              completedIterations {
                id title startDate duration
              }
            }
          }
        }
      }
    }
  }
}
"""

PROJECT_ITEMS_QUERY = """
query ProjectItems($org: String!, $number: Int!, $after: String) {
  organization(login: $org) {
    projectV2(number: $number) {
      items(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          content {
            __typename
            ... on Issue {
              number
              title
              url
              labels(first: 10) { nodes { name } }
              repository { nameWithOwner }
              assignees(first: 5) { nodes { login name } }
            }
            ... on PullRequest {
              number
              title
              url
              labels(first: 10) { nodes { name } }
              repository { nameWithOwner }
              assignees(first: 5) { nodes { login name } }
            }
            ... on DraftIssue {
              title
              assignees(first: 5) { nodes { login name } }
            }
          }
          fieldValues(first: 20) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2SingleSelectField { name } }
              }
              ... on ProjectV2ItemFieldIterationValue {
                title
                startDate
                duration
                iterationId
                field { ... on ProjectV2IterationField { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""

# ── GitHub helpers ───────────────────────────────────────────────────────────

def _gql(query, variables, token):
    resp = requests.post(
        GITHUB_API_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"GitHub GraphQL errors: {body['errors']}")
    return body["data"]


def _find_current_iteration(fields):
    """Return the iteration that contains today's date."""
    today = date.today()
    for field in fields:
        if field.get("__typename") != "ProjectV2IterationField":
            continue
        cfg = field.get("configuration") or {}
        for it in cfg.get("iterations", []):
            start = datetime.strptime(it["startDate"], "%Y-%m-%d").date()
            end   = start + timedelta(days=it["duration"])
            if start <= today < end:
                return {
                    "field_name":   field["name"],
                    "id":           it["id"],
                    "title":        it["title"],
                    "start":        start.isoformat(),
                    "end":          (end - timedelta(days=1)).isoformat(),
                    "total_days":   it["duration"],
                    "days_elapsed": (today - start).days + 1,
                }
    return None


def _paginate_items(org, number, token):
    all_items, cursor = [], None
    while True:
        variables = {"org": org, "number": number}
        if cursor:
            variables["after"] = cursor
        data = _gql(PROJECT_ITEMS_QUERY, variables, token)
        page = data["organization"]["projectV2"]["items"]
        all_items.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return all_items


def _is_support(content):
    """Return True if any label on the issue/PR is 'support' (case-insensitive)."""
    labels = (content.get("labels") or {}).get("nodes", [])
    return any(lbl.get("name", "").lower() == SUPPORT_LABEL for lbl in labels)


def _display_status(raw):
    return STATUS_NAME_MAP.get((raw or "").lower(), raw or "No status")


def fetch_current_iteration(org, project_number, token):
    meta    = _gql(PROJECT_META_QUERY, {"org": org, "number": project_number}, token)
    project = meta["organization"]["projectV2"]
    if not project:
        return None

    current = _find_current_iteration(project["fields"]["nodes"])
    if not current:
        return None

    raw_items = _paginate_items(org, project_number, token)

    items          = []
    support_count  = 0

    for item in raw_items:
        content = item.get("content") or {}

        # ── parse field values ──────────────────────────────────────────────
        status = iteration_id = None
        for fv in item["fieldValues"]["nodes"]:
            t = fv.get("__typename")
            if t == "ProjectV2ItemFieldSingleSelectValue":
                if (fv.get("field") or {}).get("name", "").lower() == "status":
                    status = fv.get("name")
            elif t == "ProjectV2ItemFieldIterationValue":
                iteration_id = fv.get("iterationId")

        if iteration_id != current["id"]:
            continue

        # ── skip support tickets ────────────────────────────────────────────
        if _is_support(content):
            support_count += 1
            continue

        typ = content.get("__typename", "")
        identifier = (
            f"{content['repository']['nameWithOwner'].split('/')[-1]}#{content['number']}"
            if typ in ("Issue", "PullRequest")
            else "Draft"
        )
        assignees = [
            a.get("name") or a.get("login")
            for a in (content.get("assignees") or {}).get("nodes", [])
        ]

        items.append({
            "identifier": identifier,
            "title":      content.get("title", "(untitled)"),
            "url":        content.get("url"),
            "status":     _display_status(status),
            "assignees":  assignees or ["Unassigned"],
        })

    return {
        "project_title":  project["title"],
        "iteration_title": current["title"],
        "starts_at":      current["start"],
        "ends_at":        current["end"],
        "days_elapsed":   current["days_elapsed"],
        "total_days":     current["total_days"],
        "items":          items,
        "support_skipped": support_count,
    }

# ── Message builder ──────────────────────────────────────────────────────────

def _ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1:"st",2:"nd",3:"rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _format_range(start_iso, end_iso):
    s = datetime.strptime(start_iso, "%Y-%m-%d").date()
    e = datetime.strptime(end_iso,   "%Y-%m-%d").date()
    return f"{_ordinal(s.day)} {s.strftime('%B')} – {_ordinal(e.day)} {e.strftime('%B')}"


def _progress_bar(done_pct, inprogress_pct, length=10):
    done_blocks   = round(done_pct / 100 * length)
    inprog_blocks = min(round(inprogress_pct / 100 * length), length - done_blocks)
    todo_blocks   = length - done_blocks - inprog_blocks
    return f"{'🟩' * done_blocks}{'🟨' * inprog_blocks}{'⬜' * todo_blocks} **{done_pct}%**"


def _bar_color(pct):
    if pct >= 70: return 0x2ECC71
    if pct >= 30: return 0xF1C40F
    return 0xE74C3C


def build_messages(data):
    items  = data["items"]
    total  = len(items)
    closed      = sum(1 for i in items if i["status"] == "Done")
    in_progress = sum(1 for i in items if i["status"] in ("In Progress", "In Review"))
    pct         = round(closed / total * 100) if total else 0
    inprog_pct  = round(in_progress / total * 100) if total else 0

    # ── status breakdown ─────────────────────────────────────────────────────
    status_counts = defaultdict(int)
    for i in items:
        status_counts[i["status"]] += 1

    breakdown_parts = []
    for s in STATUS_ORDER:
        if status_counts[s]:
            s_pct = round(status_counts[s] / total * 100) if total else 0
            breakdown_parts.append(f"{status_counts[s]} {s} {STATUS_ICONS.get(s,'')} ({s_pct}%)")
    breakdown = "  •  ".join(breakdown_parts) or "No items"

    # ── deliverables summary (Done items) ────────────────────────────────────
    done_items    = [i for i in items if i["status"] == "Done"]
    ongoing_items = [i for i in items if i["status"] in ("In Progress", "In Review")]

    def item_line(i):
        icon = STATUS_ICONS.get(i["status"], "⚪")
        link = f"[{i['identifier']}]({i['url']})" if i.get("url") else i["identifier"]
        title = i["title"][:60] + "…" if len(i["title"]) > 60 else i["title"]
        return f"{icon} {link} — {title}"

    done_lines    = "\n".join(item_line(i) for i in done_items)    or "_None yet_"
    ongoing_lines = "\n".join(item_line(i) for i in ongoing_items) or "_None_"

    date_range    = _format_range(data["starts_at"], data["ends_at"])
    iter_label    = f"{data['iteration_title']} ({date_range})"
    days_note     = f"Day {data['days_elapsed']} of {data['total_days']}"
    support_note  = (
        f"\n> _{data['support_skipped']} support ticket(s) excluded from calculations_"
        if data["support_skipped"] else ""
    )

    # ── summary embed ─────────────────────────────────────────────────────────
    summary_embed = {
        "title":       f"📋  {data['project_title']}  —  {iter_label}",
        "description": (
            f"{_progress_bar(pct, inprog_pct)}\n"
            f"{breakdown}\n"
            f"_{days_note}_"
            f"{support_note}"
        ),
        "color": _bar_color(pct),
        "fields": [
            {
                "name":   f"✅  Completed deliverables ({closed})",
                "value":  done_lines[:1020],
                "inline": False,
            },
            {
                "name":   f"🚧  In progress / In review ({len(ongoing_items)})",
                "value":  ongoing_lines[:1020],
                "inline": False,
            },
        ],
        "footer": {"text": f"Glific  •  Generated {date.today().isoformat()}"},
    }

    return [{"embeds": [summary_embed]}]

# ── Discord sender ───────────────────────────────────────────────────────────

def send_to_discord(webhook_url, payload):
    resp = requests.post(webhook_url, json=payload, timeout=10)
    resp.raise_for_status()

# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Post Glific GitHub iteration update to Discord."
    )
    parser.add_argument("--org",            type=str, help="GitHub org (overrides ORG env var)")
    parser.add_argument("--project-number", type=int, help="GitHub project number (overrides PROJECT_NUMBER)")
    parser.add_argument("--webhook-url",    type=str, help="Discord webhook URL (overrides DISCORD_WEBHOOK)")
    parser.add_argument("--dry-run", action="store_true", help="Print payload instead of posting to Discord")
    args = parser.parse_args()

    token   = os.environ.get("GITHUB_TOKEN")
    org     = args.org            or os.environ.get("ORG")
    num_raw = args.project_number or os.environ.get("PROJECT_NUMBER")
    webhook = args.webhook_url    or os.environ.get("DISCORD_WEBHOOK")

    errors = []
    if not token:   errors.append("GITHUB_TOKEN is not set.")
    if not org:     errors.append("ORG is not set (or use --org).")
    if not num_raw: errors.append("PROJECT_NUMBER is not set (or use --project-number).")
    if not webhook and not args.dry_run:
        errors.append("DISCORD_WEBHOOK is not set (or use --webhook-url).")
    if errors:
        for e in errors:
            print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    project_number = int(num_raw)

    print(f"Fetching current iteration for {org}/projects/{project_number} …")
    try:
        data = fetch_current_iteration(org, project_number, token)
    except requests.HTTPError as e:
        print(f"GitHub API error: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not data:
        print("No active iteration found — nothing to post.")
        sys.exit(0)

    total  = len(data["items"])
    closed = sum(1 for i in data["items"] if i["status"] == "Done")
    pct    = round(closed / total * 100) if total else 0
    print(f"Iteration : {data['iteration_title']}  ({data['starts_at']} → {data['ends_at']})")
    print(f"Items     : {total} total  |  {closed} done  |  {data['support_skipped']} support skipped")
    print(f"Completion: {pct}%")

    payloads = build_messages(data)

    if args.dry_run:
        print("\n── Dry-run payload ──────────────────────────────────────")
        print(json.dumps(payloads, indent=2))
        return

    try:
        for payload in payloads:
            send_to_discord(webhook, payload)
        print("Posted to Discord successfully.")
    except requests.HTTPError as e:
        print(f"Discord error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
