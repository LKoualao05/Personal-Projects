"""GoogleSheetsStore against a fake Sheets API, including user-added columns."""

from tracker.config import Settings
from tracker.schemas import APPLICATION_HEADERS, ApplicationRow
from tracker.sheets_io import GoogleSheetsStore

from .fake_sheets import FakeSheetsService

SETTINGS = Settings(sheet_id="sid")
LEGACY = ["Company", "Role Title", "Job ID", "Date Applied", "Gmail Thread URL", "From", "Subject", "Message ID"]
USER_COLS = ["Technical Assessment", "Interviews", "Outcome"]


def legacy_sheet():
    """The real sheet's layout: 8 tracker columns + 3 columns the user added by hand."""
    return FakeSheetsService({
        "Applications": [
            LEGACY + USER_COLS,
            ["Acme", "SWE Intern", "", "2025-10-01", "url", "from", "subj", "m1", "No", "No", "Rejected"],
            ["Globex", "Data Intern", "", "2025-10-02", "url", "from", "subj", "m2"],
        ],
        "ProcessedMessageIds": [["Message ID"], ["m1"], ["m2"]],
        "CompanySummary": [["Company", "Applications", "Distinct Roles"], ["=QUERY(...)"]],
    })


def test_new_columns_are_appended_after_user_columns():
    svc = legacy_sheet()
    GoogleSheetsStore(svc, SETTINGS).ensure_schema()
    header = svc.tabs["Applications"][0]
    assert header[:11] == LEGACY + USER_COLS  # nothing renamed or moved
    assert header[11:] == [h for h in APPLICATION_HEADERS if h not in LEGACY]
    assert svc.tabs["ProcessedMessageIds"][0] == ["Message ID", "Category", "Processed At"]


def test_updates_never_touch_user_columns():
    svc = legacy_sheet()
    store = GoogleSheetsStore(svc, SETTINGS)
    store.ensure_schema()
    rows = store.list_applications()
    acme = rows[0]
    assert acme.extra == {"Technical Assessment": "No", "Interviews": "No", "Outcome": "Rejected"}
    acme.status, acme.summary = "Rejected", "Acme declined."
    acme.extra = {"Outcome": "SHOULD NOT BE WRITTEN"}
    store.update_applications([acme])

    line = svc.tabs["Applications"][1]
    assert line[8:11] == ["No", "No", "Rejected"]  # user cells untouched
    header = svc.tabs["Applications"][0]
    assert line[header.index("Status")] == "Rejected" and line[header.index("Summary")] == "Acme declined."
    # Writes were split into tracker-owned runs: A:H and L:Q, never I:K.
    ranges = [r for r, _ in svc.writes if r.startswith("Applications!") and r.endswith("2")]
    assert ranges == ["Applications!A2:H2", "Applications!L2:Q2"]


def test_append_and_round_trip_with_user_columns():
    svc = legacy_sheet()
    store = GoogleSheetsStore(svc, SETTINGS)
    store.ensure_schema()
    store.append_applications([ApplicationRow(company="Initech", role_title="PM Intern", status="Applied", message_id="m3")])
    rows = store.list_applications()
    assert [r.company for r in rows] == ["Acme", "Globex", "Initech"]
    assert rows[2].row_number == 4 and rows[2].status == "Applied" and rows[2].extra == {}
    assert rows[1].extra == {}


def test_update_before_schema_adds_columns_first():
    svc = legacy_sheet()
    store = GoogleSheetsStore(svc, SETTINGS)  # e.g. the MCP server, no sync run yet
    row = store.list_applications()[1]
    assert row.status == "" and row.company == "Globex"
    row.status = "Interview"
    store.update_applications([row])
    header = svc.tabs["Applications"][0]
    assert "Status" in header and svc.tabs["Applications"][2][header.index("Status")] == "Interview"


def test_grid_is_widened_when_needed():
    svc = FakeSheetsService({"Applications": [LEGACY + USER_COLS]}, columns=12)
    store = GoogleSheetsStore(svc, SETTINGS)
    store.ensure_schema()
    assert svc.columns["Applications"] >= len(LEGACY + USER_COLS) + 6


def test_fresh_sheet_and_summary_tab():
    svc = FakeSheetsService({"Applications": []})
    store = GoogleSheetsStore(svc, SETTINGS)
    store.ensure_schema()
    assert svc.tabs["Applications"][0] == APPLICATION_HEADERS
    store.append_applications([ApplicationRow(company="Acme", status="Interview", date_applied="2026-01-02")])
    store.refresh_summary()
    summary = svc.tabs["CompanySummary"]
    assert summary[0][:2] == ["Company", "Applications"] and summary[1][:2] == ["Acme", "1"]


# --- regression: live incident 2026-11-12 ------------------------------------------
# Sheets' append "table detection" ended the table mid-sheet and INSERTed new rows there;
# backfill updates then hit stale row numbers, overwriting 92 new rows and duplicating 76.

def _legacy_sheet(n_rows: int, table_end: int):
    rows = [LEGACY + USER_COLS]
    for i in range(n_rows):
        extra = ["Yes", "No", "Rejected"] if i % 7 == 0 else []
        rows.append([f"Co{i}", f"Role {i}", "", "2025-01-01", f"url{i}", "from", f"subj{i}", f"old{i}", *extra])
    return FakeSheetsService({"Applications": rows, "ProcessedMessageIds": [["Message ID"]]},
                             append_table_end={"Applications": table_end})


def test_sync_is_correct_even_when_sheets_would_insert_mid_table():
    from tracker.pipeline import run_sync
    from tracker.schemas import Category

    from .fakes import FakeClassifier, FakeGmail, analysis, gmail_message

    svc = _legacy_sheet(30, table_end=15)
    store = GoogleSheetsStore(svc, SETTINGS)
    old = [gmail_message(f"old{i}", f"subj{i}", "Thanks for applying") for i in range(30)]
    new = [gmail_message(f"new{i}", f"New {i}", "Thanks for applying", day=i) for i in range(5)]
    results = {f"old{i}": analysis(Category.APPLICATION_CONFIRMATION, company=f"Co{i}", role=f"Role {i}") for i in range(30)}
    results |= {f"new{i}": analysis(Category.APPLICATION_CONFIRMATION, company=f"NewCo{i}", role="Intern") for i in range(5)}
    gmail = FakeGmail(new + old)
    # Old ids are already processed (legacy rows); new ones are not.
    svc.tabs["ProcessedMessageIds"] += [[f"old{i}"] for i in range(30)]
    res = run_sync(SETTINGS, gmail, store, FakeClassifier(results), backfill=True, log=lambda *_: None)

    assert svc.append_calls.count("Applications") == 0  # never relies on table detection
    rows = store.list_applications()
    ids = [r.message_id for r in rows]
    assert len(rows) == 35 and len(set(ids)) == 35  # nothing lost, nothing duplicated
    assert res.new_applications == 5 and res.backfilled == 30
    by_id = {r.message_id: r for r in rows}
    assert all(by_id[f"old{i}"].summary and by_id[f"old{i}"].company == f"Co{i}" for i in range(30))
    for i in range(0, 30, 7):  # user columns stay attached to the right application
        assert by_id[f"old{i}"].extra == {"Technical Assessment": "Yes", "Interviews": "No", "Outcome": "Rejected"}


def test_updates_follow_rows_the_user_moved():
    svc = _legacy_sheet(6, table_end=3)
    store = GoogleSheetsStore(svc, SETTINGS)
    store.ensure_schema()
    target = next(r for r in store.list_applications() if r.message_id == "old2")
    grid = svc.tabs["Applications"]
    grid[1:] = list(reversed(grid[1:]))  # user sorts the sheet between our read and write
    target.status, target.summary = "Interview", "Moved but still found."
    store.update_applications([target])
    row = next(r for r in store.list_applications() if r.message_id == "old2")
    assert (row.status, row.summary, row.company) == ("Interview", "Moved but still found.", "Co2")
    assert sum(1 for r in store.list_applications() if r.status == "Interview") == 1


def test_append_is_idempotent_and_grows_the_grid():
    svc = FakeSheetsService({"Applications": [list(APPLICATION_HEADERS)]})
    svc.row_counts["Applications"] = 3
    store = GoogleSheetsStore(svc, SETTINGS)
    store.ensure_schema()
    rows = [ApplicationRow(company=f"C{i}", message_id=f"m{i}") for i in range(5)]
    store.append_applications(rows)
    store.append_applications(rows)  # retry after a crash must not duplicate
    assert [r.message_id for r in store.list_applications()] == [f"m{i}" for i in range(5)]
    assert svc.row_counts["Applications"] >= 6


def test_audit_detects_duplicates_missing_rows_and_moved_user_columns():
    from tracker.audit import audit_rows

    backup = [LEGACY + USER_COLS,
              ["Acme", "", "", "", "", "", "", "m1", "Yes", "No", "Rejected"],
              ["Globex", "", "", "", "", "", "", "m2"]]
    good = [ApplicationRow(company="Acme", message_id="m1", status="Rejected",
                           extra={"Technical Assessment": "Yes", "Interviews": "No", "Outcome": "Rejected"}),
            ApplicationRow(company="Globex", message_id="m2", status="Applied")]
    assert audit_rows(good, backup)["ok"]
    bad = [ApplicationRow(company="Acme", message_id="m1", status="Rejected"),  # user columns detached
           ApplicationRow(company="Acme", message_id="m1", status="")]          # duplicate; m2 missing
    r = audit_rows(bad, backup)
    assert not r["ok"] and r["duplicate_message_ids"] == ["m1"] and r["original_rows_missing"] == ["m2"]
    assert r["user_columns_changed"] == ["m1"] and r["rows_without_status"] == 1
