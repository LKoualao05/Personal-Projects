# Eval report

> Baseline run (regex heuristics + offline keyword embedder). Gemini on Vertex AI results are added by
> `python -m eval.run_eval all --models heuristic gemini-3.5-flash-lite gemini-3.8-flash gemini-3.5-flash --embedders hashing gemini-embedding-001`.

Generated 2026-11-12 04:57 UTC · pricing as of 2026-11-11 · synthetic datasets in `eval/datasets/`

## Email classification (44 labeled emails)

| Model | Accuracy | Macro-F1 | Job vs not-job | Company | Role | p50 latency | p95 latency | Cost / request | Cost / 1k emails | Errors |
|---|---|---|---|---|---|---|---|---|---|---|
| `heuristic` | 70.5% | 0.654 | 75.0% | 60.6% | 12.1% | 0.00s | 0.00s | $0.000000 | $0.000 | 0 |

**Recall by category**

| Category | `heuristic` |
|---|---|
| application_confirmation (10) | 100.0% |
| status_update (4) | 0.0% |
| assessment (5) | 60.0% |
| interview (5) | 40.0% |
| offer (3) | 100.0% |
| rejection (6) | 66.7% |
| not_job_related (11) | 81.8% |

**Accuracy on hard-case subsets**

| Subset | `heuristic` |
|---|---|
| ats-sender | 100.0% |
| full-time | 100.0% |
| injection | 100.0% |
| linkedin | 66.7% |
| platform-sender | 66.7% |
| tricky | 70.0% |

<details><summary>Misclassified by <code>heuristic</code> (13)</summary>

- `e11` Update on your application — expected **status_update**, got **not_job_related**
- `e12` Your application was viewed by Wide World Importers — expected **status_update**, got **application_confirmation**
- `e13` Your application has moved forward — expected **status_update**, got **not_job_related**
- `e14` Application status: In review — expected **status_update**, got **not_job_related**
- `e16` Margie's Travel invited you to take a General Coding Assessment — expected **assessment**, got **not_job_related**
- `e19` Action required: game-based assessment — expected **assessment**, got **not_job_related**
- `e21` Interview confirmation - Machine Learning Engineer Intern — expected **interview**, got **not_job_related**
- `e22` Invitation: Superday for Investment Banking Summer Analyst — expected **interview**, got **not_job_related**
- `e23` Quick call about the Frontend Intern role? — expected **interview**, got **not_job_related**
- `e28` Update on your Northwind Traders application — expected **rejection**, got **application_confirmation**
- `e32` Finance Rotational Intern Program — expected **rejection**, got **not_job_related**
- `e37` Your credit card application has been received — expected **not_job_related**, got **application_confirmation**
- `e41` Thank you for registering for our webinar — expected **not_job_related**, got **application_confirmation**

</details>

## Job-description retrieval (16 queries over 8 JDs, 8 chunks)

| Embedder | Recall@1 | Recall@3 | MRR | Recall@1 (keyword) | Recall@1 (paraphrase) | Query latency | Eval cost |
|---|---|---|---|---|---|---|---|
| `hashing` | 56.2% | 87.5% | 0.716 | 87.5% | 25.0% | 0 ms | $0.000000 |
