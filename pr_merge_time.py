#!/usr/bin/env python3
"""
Glific — PR Merge Time → Google Sheets tracker

Runs every alternate Monday (start of new iteration). Fetches all PRs merged
during the previous iteration via the GitHub Search REST API, calculates the
average time from creation to merge in days, and logs one row per iteration to
a dedicated 'PR Merge Time' tab in the Google Sheet (re-running updates in place).

Required env vars:
  GITHUB_TOKEN             - GitHub PAT (public repo access is sufficient for public orgs)
  ORG                      - GitHub organisation name
  PROJECT_NUMBER           - GitHub Projects v2 project number
  GOOGLE_CREDENTIALS_JSON  - Service account JSON key (full JSON string)
  SPREADSHEET_ID           - Target Google Sheet ID
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

from glific_iteration_update import fetch_previous_iteration

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_NAME = "PR Merge Time"
HEADER = [
    "Iteration",
    "Start Date",
    "End Date",
    "PR Count",
    "Avg Days to Merge",
    "Last Updated",
]
EXCLUDED_REPOS = {"support-process"}
GITHUB_REST_URL = "https://api.github.com/search/issues"


# ── GitHub REST API ───────────────────────────────────────────────────────────


def _fetch_merged_prs(org, start_date_iso, end_date_iso, token):
    """Return list of PR dicts with created_at and merged_at for the given date range."""
    query = f"is:pr is:merged org:{org} merged:{start_date_iso}..{end_date_iso}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    prs = []
    page = 1
    while True:
        resp = requests.get(
            GITHUB_REST_URL,
            params={"q": query, "per_page": 100, "page": page},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        for item in items:
            repo_name = item.get("repository_url", "").split("/")[-1]
            if repo_name in EXCLUDED_REPOS:
                continue
            author = (item.get("user") or {}).get("login", "")
            if author == "dependabot[bot]":
                continue
            merged_at = (item.get("pull_request") or {}).get("merged_at")
            if not merged_at:
                continue
            prs.append({
                "created_at": item["created_at"],
                "merged_at": merged_at,
            })
        if len(items) < 100:
            break
        page += 1
    return prs


def _business_days(start: datetime, end: datetime) -> float:
    """Elapsed time between two datetimes counting only Mon–Fri hours."""
    if end <= start:
        return 0.0
    elapsed = 0.0
    cursor = start
    while cursor < end:
        next_midnight = (cursor + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        chunk_end = min(next_midnight, end)
        if cursor.weekday() < 5:  # Monday=0 … Friday=4
            elapsed += (chunk_end - cursor).total_seconds()
        cursor = next_midnight
    return elapsed / 86400


def _avg_days_to_merge(prs):
    """Average business days (Mon–Fri) from PR creation to merge, rounded to 1 decimal."""
    if not prs:
        return 0.0
    total = 0.0
    for pr in prs:
        created = datetime.fromisoformat(pr["created_at"].rstrip("Z")).replace(tzinfo=timezone.utc)
        merged = datetime.fromisoformat(pr["merged_at"].rstrip("Z")).replace(tzinfo=timezone.utc)
        total += _business_days(created, merged)
    return round(total / len(prs), 1)


# ── Google Sheets ─────────────────────────────────────────────────────────────


def _sheets_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        sys.exit("GOOGLE_CREDENTIALS_JSON not set")
    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()


def _ensure_sheet_tab(sheets, spreadsheet_id):
    meta = sheets.get(spreadsheetId=spreadsheet_id).execute()
    existing = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if SHEET_NAME not in existing:
        sheets.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]},
        ).execute()
        print(f"Created sheet tab '{SHEET_NAME}'.")


def _ensure_header(sheets, spreadsheet_id):
    result = sheets.values().get(
        spreadsheetId=spreadsheet_id, range=f"'{SHEET_NAME}'!A1:F1"
    ).execute()
    if not result.get("values"):
        sheets.values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{SHEET_NAME}'!A1",
            valueInputOption="RAW",
            body={"values": [HEADER]},
        ).execute()
        print("Header row written.")


def _find_row(sheets, spreadsheet_id, iteration_title):
    result = sheets.values().get(
        spreadsheetId=spreadsheet_id, range=f"'{SHEET_NAME}'!A:A"
    ).execute()
    for idx, row in enumerate(result.get("values", []), start=1):
        if row and row[0] == iteration_title:
            return idx
    return None


def _write_row(sheets, spreadsheet_id, row_index, values):
    sheets.values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_NAME}'!A{row_index}:F{row_index}",
        valueInputOption="RAW",
        body={"values": [values]},
    ).execute()


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    token = os.environ.get("GITHUB_TOKEN")
    org = os.environ.get("ORG")
    project_number = os.environ.get("PROJECT_NUMBER")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")

    if not all([token, org, project_number, spreadsheet_id]):
        sys.exit(
            "Missing required env vars: GITHUB_TOKEN, ORG, PROJECT_NUMBER, SPREADSHEET_ID"
        )

    print("Fetching previous iteration data from GitHub Projects…")
    data = fetch_previous_iteration(org, int(project_number), token)
    if not data:
        sys.exit("No completed iteration found — nothing to log.")

    start_date = data["starts_at"]
    end_date = data["ends_at"]
    iteration_title = data["iteration_title"]

    print(f"Iteration : {iteration_title}")
    print(f"Period    : {start_date} → {end_date}")
    print(f"Fetching PRs merged in {org} between {start_date} and {end_date}…")

    prs = _fetch_merged_prs(org, start_date, end_date, token)
    avg_days = _avg_days_to_merge(prs)

    print(f"PRs merged: {len(prs)}")
    print(f"Avg days to merge: {avg_days}")

    row_values = [
        iteration_title,
        start_date,
        end_date,
        len(prs),
        avg_days,
        date.today().isoformat(),
    ]

    sheets = _sheets_client()
    _ensure_sheet_tab(sheets, spreadsheet_id)
    _ensure_header(sheets, spreadsheet_id)

    existing_row = _find_row(sheets, spreadsheet_id, iteration_title)
    if existing_row:
        print(f"Updating existing row {existing_row}…")
        _write_row(sheets, spreadsheet_id, existing_row, row_values)
    else:
        result = sheets.values().get(
            spreadsheetId=spreadsheet_id, range=f"'{SHEET_NAME}'!A:A"
        ).execute()
        next_row = len(result.get("values", [])) + 1
        print(f"Appending new row {next_row}…")
        _write_row(sheets, spreadsheet_id, next_row, row_values)

    print("Done.")


if __name__ == "__main__":
    main()
