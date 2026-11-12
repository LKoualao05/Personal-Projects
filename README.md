# Gmail Internship Application Tracker

> **Part of [Personal-Projects](https://github.com/LKoualao05/Personal-Projects)** - A collection of general personal projects.

Scans Gmail for job-application emails, uses **Gemini on Vertex AI** to classify and summarize each one, and keeps a **Google Sheet** of every application and its current status (Applied → Assessment → Interview → Offer / Rejected).

## Roadmap

- [x] **Gemini on Vertex AI** classifies and summarizes applications
- [x] **ADK agent** exposes Gmail and Sheets through an **MCP server**
- [x] **RAG** over job descriptions with a vector store (Firestore vector search)
- [ ] **Eval script** tracks accuracy, latency and cost per request
- [ ] Deployed on **Cloud Run**

## How it works

```
Gmail (read-only) ──► candidate search (14 queries: confirmations, rejections, interviews, OAs, offers)
                        │
                        ▼
              fetch full message (plain text, HTML → text)
                        │
                        ▼
      Gemini on Vertex AI  ── structured JSON output (Pydantic schema) ──►  EmailAnalysis
      (regex heuristics as fallback)                                         category, company, role,
                        │                                                    job id, location, internship?,
                        ▼                                                    one-line summary, confidence
     apply in chronological order
       • confirmation  → new row (deduped by company + role / job id)
       • assessment / interview / offer / rejection → update status of the matching row
       • not job-related (alerts, newsletters) → ignored
                        │
                        ▼
Google Sheets: Applications · ProcessedMessageIds · CompanySummary
```

Every classified message id is recorded, so emails are never re-processed or re-billed. Each Gemini call records latency and token usage (input, output, thinking), which the sync report prints.

### Email categories

| Category | Sheet status | Examples |
|---|---|---|
| `application_confirmation` | Applied | "Thanks for applying", LinkedIn "Your application was sent" |
| `status_update` | In Review | "Your application is under review" |
| `assessment` | Assessment | HackerRank / CodeSignal / HireVue invites |
| `interview` | Interview | "Let's schedule a phone screen" |
| `offer` | Offer | "We're excited to offer you…" |
| `rejection` | Rejected | "We've decided to move forward with other candidates" |
| `not_job_related` | — | Job alerts, newsletters, talent-community sign-ups |

## ADK agent + MCP server

Gmail and Sheets are exposed as **MCP tools** by `tracker/mcp_server.py` (MCP Python SDK 2.x). A **Google ADK** agent (`agents/tracker_agent`, Gemini on Vertex AI) connects to that server with `McpToolset` and answers questions in natural language. Any other MCP client (Claude Desktop, Claude Code, MCP Inspector) can use the same server.

```
 you ──► ADK agent (Gemini 3.5 Flash, Vertex AI)
            │  McpToolset (stdio, or streamable HTTP via TRACKER_MCP_URL)
            ▼
       tracker MCP server ──► Gmail API (read-only)
                          ──► Google Sheets (tracker)
                          ──► Gemini classifier / sync pipeline
```

| Tool | Access | What it does |
|---|---|---|
| `search_gmail` | read | Gmail query syntax search (`from:greenhouse.io newer_than:30d`) |
| `read_email` | read | Full text + links of one email |
| `classify_email` | read | Run the Gemini classifier on one email |
| `list_applications` | read | Sheet rows filtered by company / status / date / internship |
| `application_stats` | read | Totals by status, response rate, per-month counts, top companies |
| `search_job_descriptions` | read | Semantic search over indexed job descriptions (RAG) |
| `update_application` | **write** | Change status/summary of one row |
| `sync_applications` | **write** | Gmail → Gemini → Sheets sync (`dry_run=true` by default) |
| `add_job_description` | **write** | Index a JD from a job-board URL or pasted text |

Example questions: *"Which companies have I heard back from this month?"*, *"Do I have any assessments with deadlines coming up?"*, *"Check for new application emails"*, *"Mark the Globex application as Interview."*

### Run it

```bash
adk web agents                         # ADK dev UI → http://localhost:8000
adk run agents/tracker_agent           # terminal chat
python -m tracker.mcp_server           # MCP server alone (stdio)
python -m tracker.mcp_server --http    # … or streamable HTTP at http://127.0.0.1:8765/mcp
python -m agents.tracker_agent.serve_mcp   # the *agent itself* as a single MCP tool
```

Claude Desktop / Claude Code config (`.mcp.json`):

```json
{
  "mcpServers": {
    "internship-tracker": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["-m", "tracker.mcp_server"],
      "cwd": "/absolute/path/to/repo"
    }
  }
}
```

### Guardrails

- Gmail is read-only at the OAuth-scope level. The only writes go to the tracker sheet.
- The agent asks for **confirmation** (ADK tool confirmation) before `update_application` or a non-dry-run `sync_applications`. Disable it with `AGENT_CONFIRM_WRITES=false`.
- The agent exposed over MCP (`serve_mcp`) has the write tools filtered out, because an MCP host can't answer ADK confirmation prompts.
- Both the agent and the server instructions treat email bodies as untrusted. Expected failures (expired token, bad status, unknown row) come back as readable tool errors.

## RAG over job descriptions

Confirmation emails rarely say what the job actually involves, so the tracker indexes the **job description** behind each application and makes it searchable by meaning.

```
application email ─► job-board link? ─► public posting API ─┐
                    (Greenhouse, Lever, Workday, LinkedIn)  │
                 └► email body reads like a JD? ────────────┤
manual: URL or pasted text ─────────────────────────────────┤
                                                             ▼
             paragraph-aware chunks (1,200 chars, 150 overlap, "Company — Role" header)
                                                             ▼
             gemini-embedding-001 on Vertex AI (768-d, RETRIEVAL_DOCUMENT)
                                                             ▼
             Firestore native vector search (KNN, cosine)  ◄── query (RETRIEVAL_QUERY)
                                                             ▼
             top-k excerpts ─► Gemini grounded answer with [n] citations
```

- **Ingestion runs during every sync.** Each new job-related email is checked for a job-board link, and only links matching known job-board URL patterns are fetched. Tracking and redirect links (which can carry personal tokens) are never followed. Already-indexed jobs are skipped.
- **Why Firestore:** it's serverless, pay-per-use and scales to zero like Cloud Run. At a few hundred postings it costs pennies a month. Vertex AI Vector Search would need an always-on endpoint.
- **Offline mode:** `VECTOR_STORE=local EMBEDDING_MODEL=hashing` uses a JSON file and a bag-of-words embedder with no API calls (the tests use this).

```bash
# one-time Firestore setup
gcloud services enable firestore.googleapis.com
gcloud firestore databases create --location=nam5
gcloud firestore indexes composite create --collection-group=job_description_chunks \
  --query-scope=COLLECTION --field-config=field-path=embedding,vector-config='{"dimension":"768","flat":"{}"}'

python -m tracker.rag backfill                       # index JDs for applications already in the sheet
python -m tracker.rag add --company Acme --role "ML Intern" --url https://boards.greenhouse.io/acme/jobs/123
python -m tracker.rag search "computer vision with PyTorch"
python -m tracker.rag ask "Which of my roles mention Kubernetes, and at what level?"
```

The agent gets two more MCP tools, `search_job_descriptions` and `add_job_description`, so you can ask it things like *"What skills do the roles I interviewed for have in common?"*

## Project layout

```
main.py                 CLI: sync Gmail → Sheets
diagnose.py             side-by-side heuristic vs Gemini classification (read-only)
tracker/
  config.py             settings from config.json, overridable by env vars
  auth.py               Gmail/Sheets OAuth (file, env var, or Secret Manager mount)
  gmail_client.py       Gmail search + message parsing
  llm.py                Gemini classifier (Vertex AI, structured output, usage metering)
  heuristics.py         keyword/regex classifier (fallback + eval baseline)
  pipeline.py           sync logic: dedupe, status tracking, backfill
  sheets_io.py          Google Sheets store (+ in-memory and dry-run stores)
  schemas.py            Pydantic/dataclass types
  mcp_server.py         MCP server: Gmail + Sheets + RAG tools
  rag/                  job-description RAG: chunking, embeddings, Firestore/local stores, fetchers, CLI
agents/tracker_agent/
  agent.py              ADK agent (root_agent) using the MCP server via McpToolset
  serve_mcp.py          the agent itself exposed as an MCP server
tests/                  pytest suite with fake Gmail / Gemini / Sheets (no network)
```

## Setup

1. **Google Cloud project** with billing enabled. Enable the Gmail, Sheets and Vertex AI APIs:
   ```bash
   gcloud services enable gmail.googleapis.com sheets.googleapis.com aiplatform.googleapis.com
   gcloud auth application-default login     # Vertex AI uses Application Default Credentials
   ```
2. **OAuth client** (Desktop app) for Gmail/Sheets: APIs & Services → Credentials → Create OAuth client ID → Desktop app. Save it as `credentials.json`.
3. **Google Sheet**: put its ID in `config.json` (`SHEET_ID`). Tabs are created automatically.
4. **Install & run**:
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements-dev.txt
   python main.py --reauth      # one-time browser consent → token.json
   python main.py --dry-run     # preview changes
   python main.py --backfill    # sync + add summaries/status to rows from the old regex tracker
   python main.py               # normal daily sync
   python -m pytest             # tests
   ```

### Configuration

All keys in `config.json` can be overridden with environment variables of the same name.

| Key | Default | Notes |
|---|---|---|
| `SHEET_ID` | — | Target spreadsheet |
| `GCP_PROJECT` | — | Vertex AI project. If empty, the heuristic classifier is used |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Any Gemini model on Vertex AI |
| `GEMINI_LOCATION` | `global` | Vertex AI location |
| `GEMINI_THINKING_LEVEL` | *(model default)* | `MINIMAL` / `LOW` / `MEDIUM` / `HIGH` |
| `USE_LLM` | `true` | `false` forces the heuristic classifier |
| `GMAIL_SEARCH_AFTER` | `2025/01/01` | Gmail search lower bound |
| `LLM_MAX_WORKERS` | `6` | Parallel Gemini requests |
| `AGENT_MODEL` | `gemini-3.5-flash` | Model for the ADK agent |
| `AGENT_CONFIRM_WRITES` | `true` | Require confirmation before sheet writes |
| `TRACKER_MCP_URL` | *(stdio)* | Use a remote MCP server over streamable HTTP |
| `RAG_ENABLED` | `true` | Index job descriptions during sync |
| `EMBEDDING_MODEL` | `gemini-embedding-001` | `hashing` = offline embedder |
| `EMBEDDING_DIMS` | `768` | Must match the Firestore vector index |
| `VECTOR_STORE` | `firestore` | `local` = JSON file (`LOCAL_VECTOR_PATH`) |

## Columns written to `Applications`

`Company` · `Role Title` · `Job ID` · `Date Applied` · `Gmail Thread URL` · `From` · `Subject` · `Message ID` · `Status` · `Summary` · `Location` · `Internship` · `Last Update` · `Classified By`

The first eight match the original tracker, so existing sheets keep working. The new columns are added to the right.

## Security notes

- Gmail access is **read-only** (`gmail.readonly`). The app can never send, delete or modify email.
- Email content is passed to Gemini as untrusted data, and the system prompt tells the model to ignore any instructions inside it. Output is constrained to a JSON schema.
- `credentials.json` and `token.json` are git-ignored.

## Run daily (GitHub Actions)

`.github/workflows/daily-tracker.yml` runs the tracker every day at 9 AM ET with the heuristic classifier (the runner has no GCP credentials). Add the `GOOGLE_OAUTH_TOKEN_JSON` and `GOOGLE_CREDENTIALS_JSON` secrets (the contents of `token.json` and `credentials.json`). The Cloud Run deployment (see roadmap) replaces this with a Gemini-powered scheduled job.

---

**Created:** 2025-9-5
**Enhanced:** 2025-10-25 (parsing robustness) · 2026-11-11 (Gemini on Vertex AI, ADK agent + MCP server, RAG)
