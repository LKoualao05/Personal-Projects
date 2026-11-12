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
