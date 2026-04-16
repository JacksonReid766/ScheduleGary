import os
import json
import logging
from datetime import datetime, timedelta, date

import anthropic
import gspread
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
SHEET_ID = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDS = os.environ["GOOGLE_CREDS_JSON"]

COL_DATE = 1
COL_TIME = 2
COL_TITLE = 3
COL_REM_1DAY = 4
COL_REM_1HR = 5
COL_REM_15MIN = 6
COL_DONE = 7

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a scheduling parser. Today is {TODAY}. Reply ONLY with JSON, no other text.

If user is scheduling: {{"action":"add","date":"YYYY-MM-DD","time":"HH:MM","title":"string"}}
If user wants to see schedule: {{"action":"list","range":"today"|"week"}}
If user is canceling: {{"action":"cancel","title":"string"}}
If user is snoozing: {{"action":"snooze","minutes":int}}
If unclear: {{"action":"unknown"}}"""


def get_sheet():
    gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDS))
    return gc.open_by_key(SHEET_ID).sheet1


def send_message(chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)


def call_claude(user_text: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM_PROMPT.format(TODAY=date.today().isoformat()),
        messages=[{"role": "user", "content": user_text}],
    )
    text = resp.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def find_last_reminded_event(sheet):
    records = sheet.get_all_records()
    now = datetime.now()
    best, best_idx, best_delta = None, None, None

    for i, row in enumerate(records):
        if str(row.get("Done", "")).upper() == "TRUE":
            continue
        reminded = any(
            str(row.get(c, "")).upper() == "TRUE"
            for c in ("Reminded_1day", "Reminded_1hr", "Reminded_15min")
        )
        if not reminded:
            continue
        try:
            event_dt = datetime.strptime(f"{row['Date']} {row['Time']}", "%Y-%m-%d %H:%M")
        except (ValueError, KeyError):
            continue
        delta = abs((event_dt - now).total_seconds())
        if best_delta is None or delta < best_delta:
            best, best_idx, best_delta = row, i + 2, delta

    return best, best_idx


def action_add(sheet, data: dict) -> str:
    sheet.append_row([data["date"], data["time"], data["title"], "", "", "", ""])
    return f"Scheduled: {data['title']} on {data['date']} at {data['time']}"


def action_list(sheet, range_str: str) -> str:
    records = sheet.get_all_records()
    today = date.today()

    if range_str == "today":
        target_dates = {today.strftime("%Y-%m-%d")}
    else:
        target_dates = {
            (today + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(7)
        }

    rows = [
        r for r in records
        if r.get("Date") in target_dates
        and str(r.get("Done", "")).upper() != "TRUE"
    ]
    rows.sort(key=lambda r: (str(r["Date"]), str(r["Time"])))

    if not rows:
        label = "today" if range_str == "today" else "this week"
        return f"No events scheduled for {label}."
    return "\n".join(f"- {r['Date']} {r['Time']}: {r['Title']}" for r in rows)


def action_cancel(sheet, title: str) -> str:
    records = sheet.get_all_records()
    for i, row in enumerate(records):
        if str(row.get("Done", "")).upper() == "TRUE":
            continue
        if title.lower() in str(row.get("Title", "")).lower():
            sheet.update_cell(i + 2, COL_DONE, "TRUE")
            return f"Canceled: {row['Title']}"
    return f"No active event found matching '{title}'."


def action_snooze(sheet, minutes: int) -> str:
    row, row_idx = find_last_reminded_event(sheet)
    if row is None:
        return "No recently reminded event to snooze."
    try:
        event_dt = datetime.strptime(f"{row['Date']} {row['Time']}", "%Y-%m-%d %H:%M")
    except ValueError:
        return "Could not parse event time."
    new_dt = event_dt + timedelta(minutes=minutes)
    sheet.update_cell(row_idx, COL_DATE, new_dt.strftime("%Y-%m-%d"))
    sheet.update_cell(row_idx, COL_TIME, new_dt.strftime("%H:%M"))
    sheet.update_cell(row_idx, COL_REM_1DAY, "")
    sheet.update_cell(row_idx, COL_REM_1HR, "")
    sheet.update_cell(row_idx, COL_REM_15MIN, "")
    return f"Snoozed '{row['Title']}' by {minutes} min → {new_dt.strftime('%Y-%m-%d %H:%M')}"


def process_message(text: str, sheet) -> str:
    lower = text.lower()

    if lower in ("today", "week"):
        return action_list(sheet, lower)

    if lower == "done":
        row, row_idx = find_last_reminded_event(sheet)
        if row is None:
            return "No recently reminded event found."
        sheet.update_cell(row_idx, COL_DONE, "TRUE")
        return f"Marked done: {row['Title']}"

    parts = lower.split()
    if len(parts) == 2 and parts[0] == "snooze" and parts[1].isdigit():
        return action_snooze(sheet, int(parts[1]))

    try:
        data = call_claude(text)
    except Exception as e:
        logger.error(f"Claude error: {e}")
        return "Sorry, I couldn't understand that. Try again."

    action = data.get("action", "unknown")

    if action == "add":
        return action_add(sheet, data)
    elif action == "list":
        return action_list(sheet, data.get("range", "today"))
    elif action == "cancel":
        return action_cancel(sheet, data.get("title", ""))
    elif action == "snooze":
        return action_snooze(sheet, int(data.get("minutes", 0)))
    else:
        return (
            "I can help you:\n"
            "- Schedule: 'Dentist tomorrow at 3pm'\n"
            "- View: 'today' or 'week'\n"
            "- Cancel: 'Cancel dentist'\n"
            "- Snooze: 'snooze 30'\n"
            "- Done: 'done'"
        )


def main():
    tg_url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    resp = requests.get(tg_url, params={"timeout": 0}, timeout=10)
    updates = resp.json().get("result", [])

    if not updates:
        logger.info("No new messages.")
        return

    sheet = get_sheet()

    for update in updates:
        if "message" not in update:
            continue
        msg = update["message"]
        if "text" not in msg:
            continue
        text = msg["text"].strip()
        chat_id = msg["chat"]["id"]
        logger.info(f"Processing message: {text!r}")
        reply = process_message(text, sheet)
        send_message(chat_id, reply)

    # Acknowledge all processed updates so they don't replay next run
    last_id = updates[-1]["update_id"]
    requests.get(tg_url, params={"offset": last_id + 1, "timeout": 0}, timeout=10)
    logger.info(f"Acknowledged up to update_id {last_id}")


if __name__ == "__main__":
    main()
