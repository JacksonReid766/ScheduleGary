import os
import json
import base64

import anthropic
import gspread
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
GOOGLE_CREDS_B64 = os.environ["GOOGLE_SHEETS_CREDENTIALS"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]


def get_sheet():
    creds_dict = json.loads(base64.b64decode(GOOGLE_CREDS_B64))
    gc = gspread.service_account_from_dict(creds_dict)
    return gc.open("CA Move Checklist").sheet1


def get_tasks(sheet):
    records = sheet.get_all_records()
    incomplete = [r for r in records if str(r.get("done", "")).upper() != "TRUE"]
    urgent = [r for r in incomplete if str(r.get("urgency", "")).lower() == "urgent"]
    if urgent:
        return urgent
    return [r for r in incomplete if str(r.get("urgency", "")).lower() == "soon"]


def build_nudge(tasks):
    task_list = "\n".join(f"- {r['task']} ({r['urgency']})" for r in tasks)
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=150,
        system=(
            "Pick the 1-2 most actionable tasks from the list and write a brief, "
            "encouraging nudge message. 2-3 sentences max, no fluff."
        ),
        messages=[{"role": "user", "content": task_list}],
    )
    return resp.content[0].text.strip()


def send_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)


def main():
    sheet = get_sheet()
    tasks = get_tasks(sheet)
    if not tasks:
        return  # Nothing urgent or soon — skip today
    nudge = build_nudge(tasks)
    send_message(nudge)


if __name__ == "__main__":
    main()
