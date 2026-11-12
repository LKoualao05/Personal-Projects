# Eval report

Generated 2026-11-12 05:32 UTC · pricing as of 2026-11-11 · synthetic datasets in `eval/datasets/`

## Email classification (44 labeled emails)

| Model | Accuracy | Macro-F1 | Job vs not-job | Company | Role | p50 latency | p95 latency | Cost / request | Cost / 1k emails | Errors |
|---|---|---|---|---|---|---|---|---|---|---|
| `heuristic` | 70.5% | 0.654 | 75.0% | 60.6% | 12.1% | 0.00s | 0.00s | $0.000000 | $0.000 | 0 |
| `gemini-3.1-flash-lite` | 100.0% | 1.000 | 100.0% | 100.0% | 100.0% | 1.00s | 1.36s | $0.000377 | $0.377 | 0 |
| `gemini-3.5-flash-lite` | 100.0% | 1.000 | 100.0% | 100.0% | 100.0% | 1.01s | 1.41s | $0.000519 | $0.519 | 0 |
| `gemini-3.8-flash` | 100.0% | 1.000 | 100.0% | 100.0% | 100.0% | 3.39s | 30.61s | $0.001663 | $1.663 | 0 |
| `gemini-3.5-flash` | 100.0% | 1.000 | 100.0% | 100.0% | 100.0% | 4.47s | 18.49s | $0.006323 | $6.323 | 0 |
| `gemini-4-argon` | 100.0% | 1.000 | 100.0% | 100.0% | 100.0% | 4.86s | 50.39s | $0.003025 | $3.025 | 0 |

**Recall by category**

| Category | `heuristic` | `gemini-3.1-flash-lite` | `gemini-3.5-flash-lite` | `gemini-3.8-flash` | `gemini-3.5-flash` | `gemini-4-argon` |
|---|---|---|---|---|---|---|
| application_confirmation (10) | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| status_update (4) | 0.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| assessment (5) | 60.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| interview (5) | 40.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| offer (3) | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| rejection (6) | 66.7% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| not_job_related (11) | 81.8% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |

**Accuracy on hard-case subsets**

| Subset | `heuristic` | `gemini-3.1-flash-lite` | `gemini-3.5-flash-lite` | `gemini-3.8-flash` | `gemini-3.5-flash` | `gemini-4-argon` |
|---|---|---|---|---|---|---|
| ats-sender | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| full-time | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| injection | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| linkedin | 66.7% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| platform-sender | 66.7% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| tricky | 70.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |

**Tokens per request (mean)**: `gemini-3.1-flash-lite` 917 in / 98 out / 0 thinking; `gemini-3.5-flash-lite` 917 in / 97 out / 0 thinking; `gemini-3.8-flash` 917 in / 91 out / 169 thinking; `gemini-3.5-flash` 917 in / 87 out / 463 thinking; `gemini-4-argon` 1054 in / 92 out / 0 thinking

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
| `gemini-embedding-001` | 100.0% | 100.0% | 1.000 | 100.0% | 100.0% | 173 ms | $0.000121 |

## Takeaways

- **Gemini vs regex.** Every Gemini model gets 100% on the 44-email set, against 70.5% for the regex baseline (12% on role titles, 0% on status updates, fooled by a credit-card "application received").
- **Default model: `gemini-3.5-flash-lite`.** It ties for best accuracy at ~1.0 s p50 and $0.52 per 1,000 emails. `gemini-3.1-flash-lite` is ~27% cheaper but retires two months earlier (May 2027). The Flash and 4-series models are 3–17× more expensive and 3–5× slower here, with no accuracy gain.
- **The eval caught a real bug.** In the first live run, `gemini-3.5-flash` failed one email with a `ValidationError`. Thinking tokens (up to ~820 on that email) count against `max_output_tokens`, so the old 1,024 cap truncated the JSON. The cap is now 8,192 and covered by a regression test. The table above is from after the fix.
- **Dense embeddings matter for RAG.** `gemini-embedding-001` retrieves the right job description for 100% of paraphrased queries ("keep services running when they break at 3am" → SRE role), vs 25% for keyword matching, at ~$0.0000075 per query.
- **Limits.** The synthetic set is now saturated for Gemini, so it guards against regressions but won't separate models any further. Harder or real-world-style examples are the next step. The first real backlog run was also spot-checked by hand: on 40 real emails, the 38 labeled not-job-related were financial aid notices, promos and LinkedIn job alerts.
