# glific-scrum-bot

Posts a weekly iteration update from Glific's GitHub Projects v2 board to Discord.

Runs every **Monday at 9 AM IST** via GitHub Actions, with a manual trigger option from the Actions tab.

---

## What it posts

A Discord embed containing:

- Iteration title and date range
- Colour-coded progress bar and overall completion rate
- Status breakdown — Done / In Review / In Progress / To Do
- Completed deliverables with links to each issue/PR
- Items currently in progress or in review
- Count of support tickets excluded from calculations

> Tickets labelled **`support`** are automatically filtered out from all calculations and the deliverables list.

**Example output**

```
📋  Glific  —  Iteration 12 (5th May – 18th May)
🟩🟩🟩🟩🟩🟩🟩⬜⬜⬜  70%
7 Done 🟢  •  2 In Review 🔍  •  3 In Progress 🟡  •  2 To Do ⚪
Day 8 of 14

✅  Completed deliverables (7)
🟢 glific#1234 — Add bulk contact export
🟢 glific#1201 — Fix flow timeout edge case
...

🚧  In progress / In review (5)
🟡 glific#1250 — Redesign onboarding flow
🔍 glific#1242 — Multilingual support improvements
...
```

---

## Setup

### 1. GitHub PAT

Create a fine-grained PAT at https://github.com/settings/personal-access-tokens/new

- **Resource owner:** `glific`
- **Organisation permissions → Projects:** Read-only

If the org enforces SAML SSO, click **Configure SSO** on the token after creating it and authorise for `glific`.

### 2. Discord webhook

In your target Discord channel:
**Edit Channel → Integrations → Webhooks → New Webhook** → copy the URL.

### 3. Repo secrets

Add these under **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `PROJECTS_READ_TOKEN` | PAT from step 1 |
| `DISCORD_WEBHOOK` | Webhook URL from step 2 |

### 4. Local setup

```bash
git clone https://github.com/glific/glific-scrum-bot
cd glific-scrum-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.template .env
# fill in GITHUB_TOKEN and DISCORD_WEBHOOK in .env
```

### 5. Test locally

```bash
# Preview payload — does not post to Discord
python glific_iteration_update.py --dry-run

# Post for real
python glific_iteration_update.py
```

---

## Manual trigger

Go to **Actions → Glific Iteration Update → Run workflow**.

Set `dry_run` to `true` to print the payload in the job logs without posting to Discord.

---

## Schedule

Runs on cron `30 3 * * 1` — every Monday at 03:30 UTC = **9:00 AM IST**.

GitHub Actions cron can drift by 5–15 minutes under platform load. If exact timing matters, use an external scheduler that calls `workflow_dispatch` via the API.

---

## Configuration

All config lives in environment variables (or `.env` for local runs):

| Variable | Description |
|---|---|
| `GITHUB_TOKEN` | PAT with Projects read access |
| `ORG` | GitHub organisation name (`glific`) |
| `PROJECT_NUMBER` | GitHub project number (`8`) |
| `DISCORD_WEBHOOK` | Discord channel webhook URL |

To change the support label filter, edit `SUPPORT_LABEL` at the top of `glific_iteration_update.py`.

---

## License

MIT
