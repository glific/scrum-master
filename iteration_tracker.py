#!/usr/bin/env python3
"""
Glific — Iteration completion rate → Google Sheets tracker + user stories → Discord

Runs every alternate Friday. Logs the done-ticket percentage for the current
iteration to a Google Sheet (each iteration gets one row; re-running updates
in place) and posts Claude-generated user stories to Discord.

Required env vars:
  GITHUB_TOKEN             - GitHub PAT with project read access
  ORG                      - GitHub organisation name
  PROJECT_NUMBER           - GitHub Projects v2 project number
  GOOGLE_CREDENTIALS_JSON  - Service account JSON key (full JSON string)
  SPREADSHEET_ID           - Target Google Sheet ID
  ANTHROPIC_API_KEY        - Anthropic API key for user story generation
  DISCORD_WEBHOOK          - Discord webhook URL
"""

import json
import os
import sys
from datetime import date

import anthropic
import requests
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

from glific_iteration_update import fetch_previous_iteration, _progress_bar

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER = [
    "Iteration",
    "Start Date",
    "End Date",
    "Total Tickets",
    "Done",
    "Completion %",
    "Last Updated",
]


# ── Claude ───────────────────────────────────────────────────────────────────


def _generate_user_stories(done_items):
    """Call Claude to produce user-facing stories for the completed tickets."""
    if not done_items:
        return "No completed tickets this iteration."
    titles = "\n".join(f"- {i['title']}" for i in done_items)
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=(
            "You are a product writer explaining sprint outcomes to non-technical stakeholders. "
            "Given a list of completed engineering ticket titles, write user stories that describe "
            "how each change improves the user experience. Use plain language — no jargon. "
            "Format: one bullet per story, starting with '• '. "
            "Frame each story as: what the user can now do, or what problem is now solved for them. "
            "No preamble, no closing sentence — bullets only."
        ),
        messages=[{"role": "user", "content": f"Completed tickets:\n{titles}"}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "Unable to generate stories.")
    return text.replace("\n•", "\n\n•")


# ── Google Sheets ─────────────────────────────────────────────────────────────


def _sheets_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        sys.exit("GOOGLE_CREDENTIALS_JSON not set")
    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build(
        "sheets", "v4", credentials=creds, cache_discovery=False
    ).spreadsheets()


def _ensure_header(sheets, spreadsheet_id):
    result = sheets.values().get(spreadsheetId=spreadsheet_id, range="A1:G1").execute()
    if not result.get("values"):
        sheets.values().update(
            spreadsheetId=spreadsheet_id,
            range="A1",
            valueInputOption="RAW",
            body={"values": [HEADER]},
        ).execute()
        print("Header row written.")


def _find_row(sheets, spreadsheet_id, iteration_title):
    """Return 1-based row index if iteration already exists, else None."""
    result = sheets.values().get(spreadsheetId=spreadsheet_id, range="A:A").execute()
    for idx, row in enumerate(result.get("values", []), start=1):
        if row and row[0] == iteration_title:
            return idx
    return None


def _write_row(sheets, spreadsheet_id, row_index, values):
    sheets.values().update(
        spreadsheetId=spreadsheet_id,
        range=f"A{row_index}:G{row_index}",
        valueInputOption="RAW",
        body={"values": [values]},
    ).execute()


# ── Discord ───────────────────────────────────────────────────────────────────


def _post_to_discord(webhook_url, data, pct, inprogress_pct, user_stories):
    from glific_iteration_update import _format_range

    date_range = _format_range(data["starts_at"], data["ends_at"])
    iter_label = f"{data['iteration_title']} ({date_range})"
    bar = _progress_bar(pct, inprogress_pct)
    embed = {
        "title": f"📖  User Stories  —  {iter_label}",
        "description": f"{bar}\n\n{user_stories}",
        "color": 0x5865F2,
        "footer": {
            "text": f"Glific  •  {pct}% complete  •  {date.today().isoformat()}"
        },
    }
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    resp.raise_for_status()


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    token = os.environ.get("GITHUB_TOKEN")
    org = os.environ.get("ORG")
    project_number = os.environ.get("PROJECT_NUMBER")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    webhook = os.environ.get("DISCORD_WEBHOOK")

    if not all([token, org, project_number, spreadsheet_id, webhook]):
        sys.exit(
            "Missing required env vars: "
            "GITHUB_TOKEN, ORG, PROJECT_NUMBER, SPREADSHEET_ID, DISCORD_WEBHOOK"
        )

    print("Fetching iteration data from GitHub…")
    data = fetch_previous_iteration(org, int(project_number), token)
    if not data:
        sys.exit("No active iteration found — nothing to log.")

    items = data["items"]
    total = len(items)
    done = sum(1 for i in items if i["status"] == "Done")
    in_progress = sum(1 for i in items if i["status"] in ("In Progress", "In Review"))
    pct = round(done / total * 100) if total else 0
    inprogress_pct = round(in_progress / total * 100) if total else 0

    print(f"Iteration : {data['iteration_title']}")
    print(f"Period    : {data['starts_at']} → {data['ends_at']}")
    print(f"Progress  : {done}/{total} done ({pct}%)")

    done_items = [
        i for i in items
        if i["status"] == "Done"
        and i.get("has_pr")
        and "investigation" not in [l.lower() for l in i.get("labels", [])]
    ]
    print("Generating user stories via Claude…")
    user_stories = _generate_user_stories(done_items)

    row_values = [
        data["iteration_title"],
        data["starts_at"],
        data["ends_at"],
        total,
        done,
        pct,
        date.today().isoformat(),
    ]

    sheets = _sheets_client()
    _ensure_header(sheets, spreadsheet_id)

    existing_row = _find_row(sheets, spreadsheet_id, data["iteration_title"])
    if existing_row:
        print(f"Updating existing row {existing_row}…")
        _write_row(sheets, spreadsheet_id, existing_row, row_values)
    else:
        result = (
            sheets.values().get(spreadsheetId=spreadsheet_id, range="A:A").execute()
        )
        next_row = len(result.get("values", [])) + 1
        print(f"Appending new row {next_row}…")
        _write_row(sheets, spreadsheet_id, next_row, row_values)

    print("Posting user stories to Discord…")
    _post_to_discord(webhook, data, pct, inprogress_pct, user_stories)

    print("Done.")


if __name__ == "__main__":
    main()
