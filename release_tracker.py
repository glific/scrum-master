#!/usr/bin/env python3
"""
Glific — Weekly Release Tracker → Google Sheets

Runs every Saturday. Fetches all GitHub releases published in the org during
the current week (Monday–Saturday) and appends one row per week to a dedicated
'Releases' tab in the Google Sheet.  Weeks are grouped under their month so
the sheet stays easy to read at a glance.

Required env vars:
  GITHUB_TOKEN             - GitHub PAT with repo/read:org scope
  ORG                      - GitHub organisation name
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

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEET_NAME = "Releases"
HEADER = ["Month", "Week Start", "Week End", "Release Count", "Releases", "Last Updated"]
EXCLUDED_REPOS = {"support-process"}


# ── Date helpers ──────────────────────────────────────────────────────────────


def _week_bounds(reference: date) -> tuple[date, date]:
    """Return (monday, saturday) for the week containing *reference*."""
    monday = reference - timedelta(days=reference.weekday())       # weekday() 0=Mon
    saturday = monday + timedelta(days=5)
    return monday, saturday


def _month_label(d: date) -> str:
    return d.strftime("%B %Y")


# ── GitHub REST API ───────────────────────────────────────────────────────────


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _list_repos(org: str, token: str) -> list[str]:
    """Return all non-archived repo names in the org."""
    repos = []
    page = 1
    while True:
        resp = requests.get(
            f"https://api.github.com/orgs/{org}/repos",
            params={"per_page": 100, "page": page, "type": "all"},
            headers=_gh_headers(token),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for r in data:
            if not r.get("archived") and r["name"] not in EXCLUDED_REPOS:
                repos.append(r["name"])
        if len(data) < 100:
            break
        page += 1
    return repos


def _fetch_releases_in_range(
    org: str, repos: list[str], start: date, end: date, token: str
) -> list[dict]:
    """
    Return releases published between *start* (inclusive) and *end* (inclusive).
    Each entry: {"repo": str, "tag": str, "name": str, "published_at": str}
    """
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=timezone.utc)

    releases = []
    for repo in repos:
        page = 1
        while True:
            resp = requests.get(
                f"https://api.github.com/repos/{org}/{repo}/releases",
                params={"per_page": 100, "page": page},
                headers=_gh_headers(token),
                timeout=30,
            )
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            data = resp.json()
            found_any = False
            for r in data:
                pub = r.get("published_at")
                if not pub:
                    continue
                pub_dt = datetime.fromisoformat(pub.rstrip("Z")).replace(tzinfo=timezone.utc)
                if pub_dt < start_dt:
                    # Releases are newest-first; once we go past the window, stop.
                    found_any = False
                    break
                found_any = True
                if pub_dt <= end_dt:
                    releases.append({
                        "repo": repo,
                        "tag": r.get("tag_name", ""),
                        "name": r.get("name") or r.get("tag_name", ""),
                        "published_at": pub,
                    })
            if len(data) < 100 or not found_any:
                break
            page += 1
    return releases


# ── Google Sheets ─────────────────────────────────────────────────────────────


def _sheets_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        sys.exit("GOOGLE_CREDENTIALS_JSON not set")
    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()


def _ensure_sheet_tab(sheets, spreadsheet_id: str):
    meta = sheets.get(spreadsheetId=spreadsheet_id).execute()
    existing = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if SHEET_NAME not in existing:
        sheets.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]},
        ).execute()
        print(f"Created sheet tab '{SHEET_NAME}'.")


def _ensure_header(sheets, spreadsheet_id: str):
    result = sheets.values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_NAME}'!A1:{chr(64 + len(HEADER))}1",
    ).execute()
    if not result.get("values"):
        sheets.values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{SHEET_NAME}'!A1",
            valueInputOption="RAW",
            body={"values": [HEADER]},
        ).execute()
        print("Header row written.")


def _all_rows(sheets, spreadsheet_id: str) -> list[list[str]]:
    result = sheets.values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_NAME}'!A:F",
    ).execute()
    return result.get("values", [])


def _find_week_row(rows: list[list[str]], week_start_iso: str) -> int | None:
    """Return 1-based sheet row index where column B matches week_start_iso, or None."""
    for idx, row in enumerate(rows, start=1):
        if len(row) >= 2 and row[1] == week_start_iso:
            return idx
    return None


def _write_row(sheets, spreadsheet_id: str, row_index: int, values: list):
    sheets.values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_NAME}'!A{row_index}:{chr(64 + len(HEADER))}{row_index}",
        valueInputOption="RAW",
        body={"values": [values]},
    ).execute()


SUMMARY_COL_START = "H"   # Summary table starts at column H
SUMMARY_HEADER = ["Month", "Avg Releases/Week"]


def _rebuild_summary(sheets, spreadsheet_id: str, rows: list[list[str]]):
    """
    Write a dynamic summary table in columns H–I (starting at H1) showing each
    month and its average weekly release count, derived from the data in A:D.
    Rows is the current sheet data (including header at index 0).
    """
    # Collect ordered unique months from column A (skip header row)
    seen = []
    for row in rows[1:]:
        month = row[0] if row else ""
        if month and month not in seen:
            seen.append(month)

    summary_values = [SUMMARY_HEADER]
    for i, month in enumerate(seen, start=2):   # data starts at row 2
        # AVERAGEIF over column A (month) against column D (release count)
        formula = f'=AVERAGEIF(\'Releases\'!A:A,H{i},\'Releases\'!D:D)'
        summary_values.append([month, formula])

    # Clear old summary area first, then write fresh
    last_row = len(summary_values) + 5   # a little buffer for cleared leftovers
    sheets.values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"'{SHEET_NAME}'!H1:I{last_row}",
    ).execute()

    if summary_values:
        sheets.values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{SHEET_NAME}'!H1",
            valueInputOption="USER_ENTERED",
            body={"values": summary_values},
        ).execute()
    print(f"Summary table updated ({len(seen)} month(s)).")


# ── Main ──────────────────────────────────────────────────────────────────────


def main():
    token = os.environ.get("GITHUB_TOKEN")
    org = os.environ.get("ORG")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")

    if not all([token, org, spreadsheet_id]):
        sys.exit("Missing required env vars: GITHUB_TOKEN, ORG, SPREADSHEET_ID")

    saturday_input = os.environ.get("SATURDAY_DATE", "").strip()
    if saturday_input:
        try:
            reference = date.fromisoformat(saturday_input)
        except ValueError:
            sys.exit(f"Invalid SATURDAY_DATE '{saturday_input}' — expected YYYY-MM-DD.")
        if reference.weekday() != 5:  # 5 = Saturday
            sys.exit(
                f"SATURDAY_DATE '{saturday_input}' is not a Saturday "
                f"(it's a {reference.strftime('%A')})."
            )
        today = reference
    else:
        today = date.today()

    week_start, week_end = _week_bounds(today)
    month_label = _month_label(week_end)  # week belongs to the month it ends in

    print(f"Week      : {week_start} → {week_end}")
    print(f"Month     : {month_label}")
    print(f"Fetching repos for org '{org}'…")

    repos = _list_repos(org, token)
    print(f"Repos found: {len(repos)}")

    print(f"Fetching releases published {week_start} → {week_end}…")
    releases = _fetch_releases_in_range(org, repos, week_start, week_end, token)
    print(f"Releases found: {len(releases)}")
    for r in releases:
        print(f"  {r['repo']}  {r['tag']}  ({r['published_at']})")

    # Deduplicate by date: multiple repos releasing on the same day count as 1.
    release_dates = {
        datetime.fromisoformat(r["published_at"].rstrip("Z")).date()
        for r in releases
    }
    release_count = len(release_dates)
    print(f"Unique release days: {release_count}")

    # Build a compact summary string: "repo@tag, repo@tag, …"
    release_summary = ", ".join(f"{r['repo']}@{r['tag']}" for r in releases) or "—"

    sheets = _sheets_client()
    _ensure_sheet_tab(sheets, spreadsheet_id)
    _ensure_header(sheets, spreadsheet_id)

    rows = _all_rows(sheets, spreadsheet_id)

    row_values = [
        month_label,
        week_start.isoformat(),
        week_end.isoformat(),
        release_count,
        release_summary,
        today.isoformat(),
    ]

    existing_row = _find_week_row(rows, week_start.isoformat())
    if existing_row:
        print(f"Updating existing row {existing_row}…")
        _write_row(sheets, spreadsheet_id, existing_row, row_values)
    else:
        next_row = len(rows) + 1
        print(f"Appending new row {next_row}…")
        _write_row(sheets, spreadsheet_id, next_row, row_values)

    # Refresh rows after the write so the summary reflects the latest data
    updated_rows = _all_rows(sheets, spreadsheet_id)
    _rebuild_summary(sheets, spreadsheet_id, updated_rows)

    print("Done.")


if __name__ == "__main__":
    main()
