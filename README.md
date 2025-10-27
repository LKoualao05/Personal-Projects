# Gmail Internship Application Tracker

> **Part of [Personal-Projects](https://github.com/LKoualao05/Personal-Projects)** - A collection of general personal projects.

This program scans your Gmail for **application confirmation emails** and logs each application to a **Google Sheet**. It runs idempotently (won't duplicate entries), keeps roles distinct by **Job ID and Title**, **groups by company** in a summary tab, and **ignores** assessments/interviews/status updates.

## What it does
- Searches Gmail for confirmation emails (e.g., "Application received", "Thanks for applying", "Your application to …").
- Ignores non-confirmation updates (assessments, interviews, status updates).
- Extracts: **Company**, **Role Title**, **Job ID**, **Date Applied**, **Message ID**, **From**, **Subject**, and **Thread URL**.
- Writes to Google Sheets tab **`Applications`**, maintains a **`ProcessedMessageIds`** tab to avoid dupes.
- Creates/refreshes a **`CompanySummary`** tab with one row per company plus counts.
- Safe to run daily via GitHub Actions cron.

## Recent Improvements ✨
- **Significantly enhanced Gmail parsing robustness** - Increased from 0 to 29+ detected applications
- **Expanded confirmation keywords** from 16 to 40+ phrases including LinkedIn, ATS systems, and platform-specific patterns
- **Multiple search strategies** with 10 different query patterns for comprehensive email discovery
- **Two-stage content analysis** with full email body extraction when needed
- **Improved company and role extraction** with enhanced regex patterns
- **Smart duplicate prevention** using ProcessedMessageIds tracking

## Quick Start (Local)
1. Create a Google Cloud OAuth Client (Desktop app):
   - Go to Google Cloud Console → APIs & Services → Credentials → **Create Credentials → OAuth client ID**.
   - Application type: **Desktop app**.
   - Download the JSON and save it as **`credentials.json`** in the project folder.
2. Enable APIs:
   - **Gmail API**, **Google Sheets API**.
3. Create a Google Sheet:
   - Note its **Sheet ID** (the long ID in the URL) and put it in `config.json`.
   - The first run will auto-create the tabs if missing.
4. Install & run:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   python main.py
   ```
   - A browser will open to authorize once. A `token.json` will be saved for future runs.

## Run Daily (GitHub Actions)

### Step 1: Initial Local Setup
1. **First, run locally once** to generate `token.json` (it contains the refresh token):
   ```bash
   python main.py
   ```

### Step 2: Set up GitHub Repository Secrets
After your first successful local run, you'll need to add two secrets to your GitHub repository:

1. **Go to your GitHub repository → Settings → Secrets and variables → Actions → New repository secret**

2. **Add `GOOGLE_OAUTH_TOKEN_JSON`**:
   - Open `token.json` in your project directory
   - Copy the **entire JSON content** (everything from `{` to `}`)
   - Paste it as the secret value

3. **Add `GOOGLE_CREDENTIALS_JSON`**:
   - Open `credentials.json` in your project directory  
   - Copy the **entire JSON content** (everything from `{` to `}`)
   - Paste it as the secret value

### Step 3: Commit and Push
1. Ensure your `config.json` has the correct `SHEET_ID`
2. Commit all files (the `.gitignore` will exclude sensitive files):
   ```bash
   git add .
   git commit -m "Add GitHub Actions automation for daily Gmail tracking"
   git push
   ```

### Step 4: Verify Automation
- The workflow runs **daily at 9 AM EST (2 PM UTC)**
- You can also trigger it manually from the **Actions** tab in GitHub
- Check the **Actions** tab to see the workflow runs and any logs

> ⚠️ **Security Note**: The GitHub Actions workflow creates temporary credential files during execution and automatically deletes them afterward. Your secrets are only accessible during the workflow run.

> ⚠️ Gmail Service Accounts won't work for consumer accounts. Using your OAuth `token.json` as a secret is the simplest reliable approach for headless runs.

## Customizing the detection
- Edit keyword lists in `gmail_search.py` (`CONFIRMATION_KEYWORDS`, `EXCLUSION_KEYWORDS`) if needed.
- The parser is designed to be conservative—**high recall** for confirmations while excluding updates.

## Columns written to `Applications`
- `Company`
- `Role Title`
- `Job ID`
- `Date Applied` (YYYY-MM-DD)
- `Gmail Thread URL`
- `From`
- `Subject`
- `Message ID`

## Support tabs
- `ProcessedMessageIds` — stores message IDs that have been processed
- `CompanySummary` — aggregates applications by company and counts distinct roles

---

**Created:** 2025-10-19  
**Enhanced:** 2025-10-25
