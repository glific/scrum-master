#!/usr/bin/env python3
"""
Glific — Planned deliverables → Discord

Runs at 12 PM IST on alternate Mondays (first Monday of each iteration). Fetches the current
iteration and posts a Claude-generated overview of planned work to Discord.

Required env vars:
  GITHUB_TOKEN      - GitHub PAT with project read access
  ORG               - GitHub organisation name
  PROJECT_NUMBER    - GitHub Projects v2 project number
  ANTHROPIC_API_KEY - Anthropic API key
  DISCORD_WEBHOOK   - Discord webhook URL
"""

import os
import sys
from datetime import date

import anthropic
import requests
from dotenv import load_dotenv

from glific_iteration_update import _format_range, fetch_current_iteration

load_dotenv()


# ── Claude ────────────────────────────────────────────────────────────────────

def _generate_plan_summary(planned_items):
    if not planned_items:
        return "No planned tickets found for this iteration."
    titles = "\n".join(f"- {i['title']}" for i in planned_items)
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=(
            "You are a product writer summarising sprint plans for a Discord update. "
            "Given a list of planned engineering tickets, write 3-5 bullet points describing "
            "what the team aims to accomplish this sprint. Focus on user value and outcomes — "
            "not implementation details. Use plain language, no jargon. "
            "Start each bullet with '• '. No preamble, no closing sentence — bullets only."
        ),
        messages=[{"role": "user", "content": f"Planned tickets:\n{titles}"}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "Unable to generate plan.")
    return text.replace("\n•", "\n\n•")


# ── Discord ───────────────────────────────────────────────────────────────────

def _post_to_discord(webhook_url, data, planned_count, summary):
    date_range = _format_range(data["starts_at"], data["ends_at"])
    iter_label = f"{data['iteration_title']} ({date_range})"
    embed = {
        "title":       f"🗓️  Planned Deliverables  —  {iter_label}",
        "description": summary,
        "color":       0x3498DB,
        "footer":      {
            "text": f"Glific  •  {planned_count} tickets planned  •  {date.today().isoformat()}"
        },
    }
    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    resp.raise_for_status()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    token          = os.environ.get("GITHUB_TOKEN")
    org            = os.environ.get("ORG")
    project_number = os.environ.get("PROJECT_NUMBER")
    webhook        = os.environ.get("DISCORD_WEBHOOK")

    if not all([token, org, project_number, webhook]):
        sys.exit("Missing required env vars: GITHUB_TOKEN, ORG, PROJECT_NUMBER, DISCORD_WEBHOOK")

    print("Fetching current iteration from GitHub…")
    data = fetch_current_iteration(org, int(project_number), token)
    if not data:
        sys.exit("No active iteration found — nothing to post.")

    planned_items = [
        i for i in data["items"]
        if i["status"] in ("To Do", "In Progress", "In Review")
    ]

    print(f"Iteration : {data['iteration_title']}")
    print(f"Period    : {data['starts_at']} → {data['ends_at']}")
    print(f"Planned   : {len(planned_items)} tickets")

    print("Generating plan summary via Claude…")
    summary = _generate_plan_summary(planned_items)

    print("Posting to Discord…")
    _post_to_discord(webhook, data, len(planned_items), summary)

    print("Done.")


if __name__ == "__main__":
    main()
