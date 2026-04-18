import os
import json
from datetime import datetime

import requests
import gspread
from secrets import S

TOKEN = S.telegram_token
CHAT_ID = S.telegram_chat_id
SHEET_ID = S.google_sheet_id
GOOGLE_CREDS = S.google_creds_json

COL_DATE = 1
COL_TIME = 2
COL_TITLE = 3
COL_REM_1DAY = 4
COL_REM_1HR = 5
COL_REM_15MIN = 6
COL_DONE = 7


def send_message(text: str):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)


def get_sheet():
    gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDS))
    return gc.open_by_key(SHEET_ID).sheet1


def main():
    sheet = get_sheet()
    records = sheet.get_all_records()
    now = datetime.now()

    for i, row in enumerate(records):
        row_idx = i + 2  # sheet row (header = row 1, data starts row 2)

        if str(row.get("Done", "")).upper() == "TRUE":
            continue

        try:
            event_dt = datetime.strptime(
                f"{row['Date']} {row['Time']}", "%Y-%m-%d %H:%M"
            )
        except (ValueError, KeyError):
            continue  # skip malformed rows

        delta_min = (event_dt - now).total_seconds() / 60

        # 1-day window: 1440 min ± 15 min
        if (1425 <= delta_min <= 1455
                and str(row.get("Reminded_1day", "")).upper() != "TRUE"):
            send_message(f"Tomorrow at {row['Time']}: {row['Title']}")
            sheet.update_cell(row_idx, COL_REM_1DAY, "TRUE")

        # 1-hour window: 60 min ± 15 min
        elif (45 <= delta_min <= 75
                and str(row.get("Reminded_1hr", "")).upper() != "TRUE"):
            send_message(f"In 1 hour: {row['Title']}")
            sheet.update_cell(row_idx, COL_REM_1HR, "TRUE")

        # 15-min window: 0–30 min out
        elif (0 <= delta_min <= 30
                and str(row.get("Reminded_15min", "")).upper() != "TRUE"):
            send_message(f"15 min: {row['Title']} — starting soon")
            sheet.update_cell(row_idx, COL_REM_15MIN, "TRUE")


if __name__ == "__main__":
    main()
