# Gmail Internship Application Tracker

> **Part of [Personal-Projects](https://github.com/LKoualao05/Personal-Projects)** - A collection of general personal projects.

Scans Gmail for job-application emails, uses **Gemini on Vertex AI** to classify and summarize each one, and keeps a **Google Sheet** of every application and its current status (Applied → Assessment → Interview → Offer / Rejected).

## Roadmap

- [x] **Gemini on Vertex AI** classifies and summarizes applications
- [x] **ADK agent** exposes Gmail and Sheets through an **MCP server**
- [x] **RAG** over job descriptions with a vector store (Firestore vector search)
- [x] **Eval script** tracks accuracy, latency and cost per request
- [x] Deployed on **Cloud Run**

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

## Evaluation

`eval/run_eval.py` measures **accuracy, latency and cost per request** for any set of classifiers, and **retrieval quality** for any set of embedders. Results go to [`eval/REPORT.md`](eval/REPORT.md).

```bash
python -m eval.run_eval classify --models heuristic gemini-3.5-flash-lite gemini-3.8-flash gemini-3.5-flash
python -m eval.run_eval retrieval --embedders hashing gemini-embedding-001
python -m eval.run_eval all --fail-under 0.9          # exits 1 if any model scores below 90%
```

| Suite | Data | Metrics |
|---|---|---|
| Classification | 44 labeled emails across all 7 categories, including hard cases: ATS senders, LinkedIn alerts vs. confirmations, a credit-card "application received", a rejection that opens with "thank you for applying", a prompt-injection email | accuracy, macro-F1, per-category recall, job-vs-not-job accuracy, field accuracy (company / role / job ID / internship), latency mean / p50 / p95, tokens (input / output / thinking), **cost per request and per 1,000 emails** |
| Retrieval | 8 job descriptions, 16 queries (half are paraphrases with no keyword overlap) | recall@1, recall@3, MRR, by query type, query latency, embedding cost |

- **Synthetic data only.** Fictional companies and `.example` domains, so no real email ever enters the repo. A test enforces this.
- **Cost** comes from token usage reported by the API × a dated price table (`tracker/pricing.py`) that handles introductory pricing windows and bills thinking tokens at the output rate. The daily sync report prints its estimated cost too.
- **CI** runs the offline suites (regex baseline + keyword embedder) on every PR as a regression gate.

Baseline (no LLM): regex heuristics reach **70.5% accuracy and 12% role-title accuracy**, and miss every status update. Keyword retrieval gets only **25% recall@1 on paraphrased queries**. Those are the numbers Gemini and the dense embeddings are measured against.

## Deployment (Cloud Run)

One **private** Cloud Run service runs everything:

```
                     ┌──────────────────── Cloud Run: gmail-tracker (IAM-only, max 1 instance) ─────────────────────┐
Cloud Scheduler ───► │ POST /tasks/sync   Gmail → Gemini → Sheets + JD indexing (≤500 emails/run, single writer) │
 (daily, OIDC)       │ /dev-ui, /run_sse  ADK agent API + dev UI ──stdio──► tracker MCP server                    │
you (gcloud proxy) ► │ /mcp/              tracker MCP server (streamable HTTP) for any MCP client                 │
                     │ /healthz                                                                                    │
                     └──────┬──────────────────┬───────────────────────┬─────────────────────┬────────────────────┘
                            ▼                  ▼                       ▼                     ▼
                     Secret Manager       Vertex AI              Firestore             Gmail / Sheets
                     (OAuth token,        (Gemini + embeddings,  (vector search)       APIs (user OAuth)
                      mounted read-only)   service account)
```

- **Private by default.** `--no-allow-unauthenticated` means every request needs a Google identity with `run.invoker`: your account through `gcloud run services proxy`, and the scheduler's service account through an OIDC token.
- **Least privilege.** The runtime service account has only `aiplatform.user`, `datastore.user`, `logging.logWriter`, and access to the single secret. Gmail/Sheets access uses your OAuth token from Secret Manager, which is mounted read-only and refreshed in memory.
- **Scales to zero.** With no minimum instances, it costs nothing while idle. Firestore and Vertex AI are pay-per-use.
- **Single writer.** `max-instances=1` plus an in-process lock (a second concurrent sync gets `409`). Each scheduled run handles at most 500 emails, so a big backlog is worked through over several days within Cloud Scheduler's 30-minute deadline.
- **CI/CD.** Every PR runs tests plus the offline eval. Once `deploy/setup_github_cd.sh` has run, each merge to `main` deploys automatically: GitHub authenticates through **Workload Identity Federation** (no service-account keys), only from `main` of this repo.

```bash
./deploy/setup_gcp.sh        # once: APIs, service account, Firestore + vector index, token → Secret Manager
./deploy/deploy.sh           # build (Cloud Build) + deploy + daily Cloud Scheduler job
./deploy/setup_github_cd.sh  # optional: keyless GitHub → Cloud Run deploys on merge

gcloud run services proxy gmail-tracker --region us-central1 --port 8080
open http://localhost:8080/dev-ui/                                   # chat with the agent
curl -X POST 'http://localhost:8080/tasks/sync?dry_run=true&limit=20' # preview a sync
```

Run the service locally with `uvicorn tracker.server:app --port 8080`.

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
  pricing.py            dated per-model token prices → cost per request
  server.py             Cloud Run app: ADK API + dev UI, /mcp, /tasks/sync, /healthz
eval/
  run_eval.py           accuracy / latency / cost + retrieval eval harness
  datasets/             synthetic labeled emails, JDs and retrieval queries
  REPORT.md             latest results
deploy/                 setup_gcp.sh · deploy.sh · setup_github_cd.sh
Dockerfile              Cloud Run image
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

The first eight match the original tracker, so existing sheets keep working.

**Your own columns are safe.** Tracker columns are located by header name. Missing ones are appended to the right of whatever is already there, existing headers are never renamed, and updates write only tracker-owned cells (split into contiguous ranges around your columns). Columns you add by hand (e.g. *Technical Assessment*, *Interviews*, *Outcome*) are never overwritten, and the agent can read them (`user_columns` in `list_applications`).

## Security notes

- Gmail access is **read-only** (`gmail.readonly`). The app can never send, delete or modify email.
- Email content is passed to Gemini as untrusted data, and the system prompt tells the model to ignore any instructions inside it. Output is constrained to a JSON schema.
- `credentials.json` and `token.json` are git-ignored.

## GitHub Actions

- `ci.yml`: tests + offline eval on every PR and push to `main`.
- `deploy.yml`: test → build → push to Artifact Registry → deploy to Cloud Run on every push to `main` (active once `setup_github_cd.sh` has run).
- `daily-tracker.yml`: **manual fallback only** (heuristic classifier, needs the `GOOGLE_OAUTH_TOKEN_JSON` / `GOOGLE_CREDENTIALS_JSON` secrets). The daily run is now Cloud Scheduler → Cloud Run.

---

**Created:** 2025-9-5
**Enhanced:** 2025-10-25 (parsing robustness) · 2026-11-11 (Gemini on Vertex AI, ADK agent + MCP server, RAG, eval harness, Cloud Run)
