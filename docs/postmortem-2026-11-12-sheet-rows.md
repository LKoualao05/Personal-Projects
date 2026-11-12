# Postmortem: rows overwritten during the first live sync (2026-11-12)

**Impact:** 92 newly added application rows were overwritten and 76 legacy rows were duplicated in the tracker sheet. No original data and no user-maintained cells were lost. The sheet was restored from a pre-sync backup and re-synced with the fixed code the same night.
**Detection:** a post-sync audit found legacy-format rows *below* rows the sync had just added.
**Duration:** about 50 minutes from the first bad write to the restore.

## Timeline (UTC)

| Time | Event |
|---|---|
| 05:30 | Backup taken: duplicate tab `Applications (backup 2026-11-12)` + local JSON. First live sync starts. |
| 05:31–06:00 | Sync 1 adds 120 rows and backfills 314. Gmail rate limits stop ~1,900 fetches (fixed separately in #7). |
| 06:00–06:45 | Sync 2 adds 378 rows and backfills 279. |
| 06:47 | Audit: 76 duplicated Message IDs; tracker-written rows found sitting between untouched legacy blocks. |
| 06:50 | Scheduler paused. Root cause identified. Fix + regression tests (#8). |
| 07:10 | Fix deployed. Verified no user edits since the backup; tab restored from the backup (verified identical); full re-sync with the fixed code. |
| 07:55 | Re-sync done: 2,537 emails classified, 498 new rows, 523 status updates, 559 legacy rows backfilled, 0 errors. `python -m tracker.audit --against <backup>`: 0 duplicate IDs, 0 missing original rows, 0 user-column changes. Scheduler resumed. |

## Root cause

`GoogleSheetsStore.append_applications` used `spreadsheets.values.append` with `INSERT_ROWS`. Sheets locates the "table" to append to heuristically, and on this sheet it ended the table **mid-sheet** (row ~550 on the first pass, ~668 on the second). It *inserted* the new rows there, shifting every row below them.

The pipeline then applied backfill updates using row numbers read **before** the insert, so each update landed on whichever row had moved into that position. That was usually a just-inserted row, which got overwritten with legacy-row content, while the shifted legacy row stayed un-updated (the duplicate).

Two design flaws combined:
1. Relying on a heuristic (table detection) for *where* data goes.
2. Writing by position with positions that could go stale between read and write.

## Fix (#8)

- The Applications tab never uses `values.append`. New rows are written at explicit positions after the last used row, and appends are idempotent by Message ID.
- Every update re-resolves its row **by Message ID right before writing**. If the row can't be found, the write is refused rather than guessed.
- Updates run before appends.
- Regression tests replay the incident with a fake Sheets API that inserts mid-table, and also cover a user sorting the sheet mid-sync. They fail on the old code and pass on the new.

## What went right

- Backups (in-sheet tab + JSON) were taken *before* the first write, so the restore was exact and immediate.
- The user's hand-maintained columns were never written, because the header-aware store from #4 writes only tracker-owned cells. Their rows were all above the insertion point anyway.
- A post-run audit (Message-ID reconciliation against the backup) caught the problem in minutes, before the daily scheduled run.

## Follow-ups

- [x] Explicit-position writes + ID-located updates (#8)
- [x] Regression tests with a fake that reproduces the live behaviour
- [x] Post-sync audit, now a command: `python -m tracker.audit` (Message IDs unique, every original row present once, user columns attached to the same application)
- [x] Found during the audit: rows that only matched an *older* status email were skipped by the backfill (5 rows). Fixed + regression test.
- [ ] Optional: a scheduled integrity check that alerts if Message IDs are ever duplicated
