import os
import json
import logging
from datetime import datetime, timedelta, date

import anthropic
import gspread
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

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


def call_claude(user_text: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=SYSTEM_PROMPT.format(TODAY=date.today().isoformat()),
        messages=[{"role": "user", "content": user_text}],
    )
    text = resp.content[0].text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def find_last_reminded_event(sheet) -> tuple:
    """Return (row_dict, row_idx) for the undone, reminded event closest to now."""
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
    lines = [f"- {r['Date']} {r['Time']}: {r['Title']}" for r in rows]
    return "\n".join(lines)


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
    # Clear reminder flags so remind.py re-fires at the new time
    sheet.update_cell(row_idx, COL_REM_1DAY, "")
    sheet.update_cell(row_idx, COL_REM_1HR, "")
    sheet.update_cell(row_idx, COL_REM_15MIN, "")
    return f"Snoozed '{row['Title']}' by {minutes} min → {new_dt.strftime('%Y-%m-%d %H:%M')}"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    lower = text.lower()

    sheet = get_sheet()

    # Pre-Claude shortcuts (no API call)
    if lower in ("today", "week"):
        reply = action_list(sheet, lower)
        await update.message.reply_text(reply)
        return

    if lower == "done":
        row, row_idx = find_last_reminded_event(sheet)
        if row is None:
            await update.message.reply_text("No recently reminded event found.")
            return
        sheet.update_cell(row_idx, COL_DONE, "TRUE")
        await update.message.reply_text(f"Marked done: {row['Title']}")
        return

    parts = lower.split()
    if len(parts) == 2 and parts[0] == "snooze" and parts[1].isdigit():
        reply = action_snooze(sheet, int(parts[1]))
        await update.message.reply_text(reply)
        return

    # Claude parse for everything else
    try:
        data = call_claude(text)
    except Exception as e:
        logger.error(f"Claude error: {e}")
        await update.message.reply_text("Sorry, I couldn't understand that. Try again.")
        return

    action = data.get("action", "unknown")

    if action == "add":
        reply = action_add(sheet, data)
    elif action == "list":
        reply = action_list(sheet, data.get("range", "today"))
    elif action == "cancel":
        reply = action_cancel(sheet, data.get("title", ""))
    elif action == "snooze":
        reply = action_snooze(sheet, int(data.get("minutes", 0)))
    else:
        reply = (
            "I can help you:\n"
            "- Schedule: 'Dentist tomorrow at 3pm'\n"
            "- View: 'today' or 'week'\n"
            "- Cancel: 'Cancel dentist'\n"
            "- Snooze: 'snooze 30'\n"
            "- Done: 'done'"
        )

    await update.message.reply_text(reply)


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Gary is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
