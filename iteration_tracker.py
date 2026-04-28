#!/usr/bin/env python3
"""
Glific — Iteration completion rate → Google Sheets tracker

Runs every alternate Friday. Logs the done-ticket percentage for the current
iteration to a Google Sheet. Each iteration gets one row; re-running updates
the existing row in place.

Required env vars:
  GITHUB_TOKEN             - GitHub PAT with project read access
  ORG                      - GitHub organisation name
  PROJECT_NUMBER           - GitHub Projects v2 project number
  GOOGLE_CREDENTIALS_JSON  - Service account JSON key (full JSON string)
  SPREADSHEET_ID           - Target Google Sheet ID
"""

import json
import os
import sys
from datetime import date

from dotenv import load_dotenv
from google.oauth2 import service_account
from googleapiclient.discovery import build

from glific_iteration_update import fetch_current_iteration

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
HEADER = ["Iteration", "Start Date", "End Date", "Total Tickets", "Done", "Completion %", "Last Updated"]


def _sheets_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        sys.exit("GOOGLE_CREDENTIALS_JSON not set")
    info  = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()


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


def main():
    token          = os.environ.get("GITHUB_TOKEN")
    org            = os.environ.get("ORG")
    project_number = os.environ.get("PROJECT_NUMBER")
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")

    if not all([token, org, project_number, spreadsheet_id]):
        sys.exit("Missing required env vars: GITHUB_TOKEN, ORG, PROJECT_NUMBER, SPREADSHEET_ID")

    print("Fetching iteration data from GitHub…")
    data = fetch_current_iteration(org, int(project_number), token)
    if not data:
        sys.exit("No active iteration found — nothing to log.")

    items = data["items"]
    total = len(items)
    done  = sum(1 for i in items if i["status"] == "Done")
    pct   = round(done / total * 100) if total else 0

    row_values = [
        data["iteration_title"],
        data["starts_at"],
        data["ends_at"],
        total,
        done,
        f"{pct}%",
        date.today().isoformat(),
    ]

    print(f"Iteration : {data['iteration_title']}")
    print(f"Period    : {data['starts_at']} → {data['ends_at']}")
    print(f"Progress  : {done}/{total} done ({pct}%)")

    sheets = _sheets_client()
    _ensure_header(sheets, spreadsheet_id)

    existing_row = _find_row(sheets, spreadsheet_id, data["iteration_title"])
    if existing_row:
        print(f"Updating existing row {existing_row}…")
        _write_row(sheets, spreadsheet_id, existing_row, row_values)
    else:
        result   = sheets.values().get(spreadsheetId=spreadsheet_id, range="A:A").execute()
        next_row = len(result.get("values", [])) + 1
        print(f"Appending new row {next_row}…")
        _write_row(sheets, spreadsheet_id, next_row, row_values)

    print("Done.")


if __name__ == "__main__":
    main()
