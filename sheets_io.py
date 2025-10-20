from __future__ import annotations
from typing import List, Dict, Any, Tuple
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

APPLICATION_HEADERS = [
    "Company","Role Title","Job ID","Date Applied","Gmail Thread URL","From","Subject","Message ID"
]

def ensure_sheet_and_headers(sheets_service, spreadsheet_id: str, sheet_name: str, headers: List[str]) -> None:
    # create sheet if missing; ensure header row exists
    try:
        meta = sheets_service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
        sheet_titles = {s["properties"]["title"] for s in meta["sheets"]}
        if sheet_name not in sheet_titles:
            requests = [{
                "addSheet": {"properties": {"title": sheet_name}}
            }]
            sheets_service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()
    except HttpError as e:
        raise

    # check header row
    rng = f"{sheet_name}!1:1"
    res = sheets_service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=rng).execute()
    row = res.get("values", [])
    if not row or row[0] != headers:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=rng,
            valueInputOption="RAW",
            body={"values": [headers]}
        ).execute()

def get_processed_ids(sheets_service, spreadsheet_id: str, processed_sheet: str) -> set[str]:
    ensure_sheet_and_headers(sheets_service, spreadsheet_id, processed_sheet, ["Message ID"])
    res = sheets_service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{processed_sheet}!A2:A"
    ).execute()
    vals = res.get("values", [])
    return {v[0] for v in vals if v}

def append_processed_ids(sheets_service, spreadsheet_id: str, processed_sheet: str, message_ids: List[str]) -> None:
    if not message_ids:
        return
    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{processed_sheet}!A:A",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [[m] for m in message_ids]}
    ).execute()

def append_applications(sheets_service, spreadsheet_id: str, sheet_name: str, rows: List[List[str]]) -> None:
    if not rows:
        return
    sheets_service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A:A",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()

def refresh_company_summary(sheets_service, spreadsheet_id: str, source_sheet: str, summary_sheet: str) -> None:
    # recreate the summary sheet contents via a simple QUERY formula with GROUP BY Company
    ensure_sheet_and_headers(sheets_service, spreadsheet_id, summary_sheet, ["Company","Applications","Distinct Roles"])
    # Clear old (except header)
    sheets_service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=f"{summary_sheet}!A2:C"
    ).execute()
    # Insert a formula that calculates on the fly
    formula = (
        f'=QUERY({source_sheet}!A2:C, "select A, count(A), count(distinct C) where A is not null group by A label count(A) \'Applications\', count(distinct C) \'Distinct Roles\'", 0)'
    )
    sheets_service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"{summary_sheet}!A2",
        valueInputOption="USER_ENTERED",
        body={"values": [[formula]]}
    ).execute()
