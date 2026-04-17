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
CHECKLIST_ID = os.environ["SPREADSHEET_ID"]
BRAVE_KEY = os.environ.get("BRAVE_API_KEY", "")

COL_DATE, COL_TIME, COL_TITLE = 1, 2, 3
COL_REM_1DAY, COL_REM_1HR, COL_REM_15MIN, COL_DONE = 4, 5, 6, 7
MAX_HISTORY = 20

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are Gary, Jackson's personal AI assistant — think Jarvis from Iron Man. \
Warm, sharp, proactive, concise. Today is {TODAY}. \
Jackson is a software professional moving to CA and actively job hunting in tech.

Use tools freely for scheduling, the move checklist, and general help. \
For job searches: gather target roles, preferred CA cities, and remote preference \
before calling search_jobs — ask 1-2 questions first if you don't have them. \
You can talk about anything, not just scheduling."""

TOOLS = [
    {
        "name": "add_event",
        "description": "Add a scheduled appointment to the calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD"},
                "time": {"type": "string", "description": "HH:MM (24-hour)"},
                "title": {"type": "string"},
            },
            "required": ["date", "time", "title"],
        },
    },
    {
        "name": "list_events",
        "description": "List scheduled events for today or the next 7 days.",
        "input_schema": {
            "type": "object",
            "properties": {"range": {"type": "string", "enum": ["today", "week"]}},
            "required": ["range"],
        },
    },
    {
        "name": "cancel_event",
        "description": "Cancel a scheduled event by partial title match.",
        "input_schema": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
    },
    {
        "name": "snooze_event",
        "description": "Snooze the most recently reminded event by N minutes.",
        "input_schema": {
            "type": "object",
            "properties": {"minutes": {"type": "integer"}},
            "required": ["minutes"],
        },
    },
    {
        "name": "complete_checklist_task",
        "description": "Mark a CA move checklist task as done.",
        "input_schema": {
            "type": "object",
            "properties": {"task": {"type": "string", "description": "Partial task name"}},
            "required": ["task"],
        },
    },
    {
        "name": "list_checklist",
        "description": "Show incomplete CA move checklist tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "urgency_filter": {"type": "string", "enum": ["urgent", "soon", "all"]}
            },
            "required": ["urgency_filter"],
        },
    },
    {
        "name": "search_jobs",
        "description": (
            "Search for tech job openings in California. "
            "Only call after gathering job_titles, cities, and remote preference."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_titles": {"type": "array", "items": {"type": "string"}},
                "cities": {"type": "array", "items": {"type": "string"}},
                "remote": {"type": "boolean"},
                "experience_level": {
                    "type": "string",
                    "enum": ["entry", "mid", "senior", "staff", "any"],
                },
            },
            "required": ["job_titles", "cities", "remote"],
        },
    },
]


# ── Sheet helpers ──────────────────────────────────────────────────────────────

def get_history_sheet(wb):
    try:
        return wb.worksheet("History")
    except gspread.WorksheetNotFound:
        ws = wb.add_worksheet(title="History", rows=100, cols=3)
        ws.append_row(["role", "content", "timestamp"])
        return ws


def load_history(ws):
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    return [
        {"role": r[0], "content": r[1]}
        for r in rows[1:]
        if len(r) >= 2 and r[0] in ("user", "assistant") and r[1]
    ]


def save_exchange(ws, user_text, reply):
    now = datetime.utcnow().isoformat()
    ws.append_row(["user", user_text, now])
    ws.append_row(["assistant", reply, now])
    all_rows = ws.get_all_values()
    excess = len(all_rows) - 1 - MAX_HISTORY
    if excess > 0:
        ws.delete_rows(2, 1 + excess)


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_message(chat_id, text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )


# ── Action functions ───────────────────────────────────────────────────────────

def find_last_reminded_event(sheet):
    now = datetime.now()
    best, best_idx, best_delta = None, None, None
    for i, row in enumerate(sheet.get_all_records()):
        if str(row.get("Done", "")).upper() == "TRUE":
            continue
        if not any(str(row.get(c, "")).upper() == "TRUE"
                   for c in ("Reminded_1day", "Reminded_1hr", "Reminded_15min")):
            continue
        try:
            dt = datetime.strptime(f"{row['Date']} {row['Time']}", "%Y-%m-%d %H:%M")
        except (ValueError, KeyError):
            continue
        delta = abs((dt - now).total_seconds())
        if best_delta is None or delta < best_delta:
            best, best_idx, best_delta = row, i + 2, delta
    return best, best_idx


def action_add(sheet, data):
    sheet.append_row([data["date"], data["time"], data["title"], "", "", "", ""])
    return f"Scheduled: {data['title']} on {data['date']} at {data['time']}"


def action_list(sheet, range_str):
    today = date.today()
    target = (
        {today.strftime("%Y-%m-%d")}
        if range_str == "today"
        else {(today + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(7)}
    )
    rows = sorted(
        [r for r in sheet.get_all_records()
         if r.get("Date") in target and str(r.get("Done", "")).upper() != "TRUE"],
        key=lambda r: (str(r["Date"]), str(r["Time"])),
    )
    if not rows:
        return f"Nothing on for {'today' if range_str == 'today' else 'this week'}."
    return "\n".join(f"- {r['Date']} {r['Time']}: {r['Title']}" for r in rows)


def action_cancel(sheet, title):
    for i, row in enumerate(sheet.get_all_records()):
        if str(row.get("Done", "")).upper() == "TRUE":
            continue
        if title.lower() in str(row.get("Title", "")).lower():
            sheet.update_cell(i + 2, COL_DONE, "TRUE")
            return f"Canceled: {row['Title']}"
    return f"No active event matching '{title}'."


def action_snooze(sheet, minutes):
    row, idx = find_last_reminded_event(sheet)
    if row is None:
        return "No recently reminded event to snooze."
    try:
        dt = datetime.strptime(f"{row['Date']} {row['Time']}", "%Y-%m-%d %H:%M")
    except ValueError:
        return "Couldn't parse event time."
    new_dt = dt + timedelta(minutes=minutes)
    sheet.update_cell(idx, COL_DATE, new_dt.strftime("%Y-%m-%d"))
    sheet.update_cell(idx, COL_TIME, new_dt.strftime("%H:%M"))
    for col in (COL_REM_1DAY, COL_REM_1HR, COL_REM_15MIN):
        sheet.update_cell(idx, col, "")
    return f"Snoozed '{row['Title']}' by {minutes} min → {new_dt.strftime('%H:%M')}"


def action_complete_task(sheet, task_title):
    headers = sheet.row_values(1)
    done_col = headers.index("done") + 1
    for i, row in enumerate(sheet.get_all_records()):
        if str(row.get("done", "")).upper() == "TRUE":
            continue
        if task_title.lower() in str(row.get("task", "")).lower():
            sheet.update_cell(i + 2, done_col, "TRUE")
            return f"Checked off: {row['task']}"
    return f"No active task matching '{task_title}'."


def action_list_checklist(sheet, urgency_filter):
    incomplete = [r for r in sheet.get_all_records()
                  if str(r.get("done", "")).upper() != "TRUE"]
    if urgency_filter == "urgent":
        rows = [r for r in incomplete if r.get("urgency", "").lower() == "urgent"]
    elif urgency_filter == "soon":
        rows = [r for r in incomplete if r.get("urgency", "").lower() in ("urgent", "soon")]
    else:
        rows = incomplete
    if not rows:
        return "No incomplete tasks."
    return "\n".join(f"- [{r.get('urgency', '')}] {r['task']}" for r in rows[:20])


def action_search_jobs(params):
    if not BRAVE_KEY:
        return "Job search isn't set up yet — BRAVE_API_KEY is missing."
    titles = " OR ".join(f'"{t}"' for t in params["job_titles"])
    cities = " OR ".join(params["cities"])
    remote_clause = " OR remote" if params.get("remote") else ""
    level = params.get("experience_level", "any")
    level_clause = f" {level}" if level not in ("any", None) else ""
    query = (
        f"({titles}){level_clause} jobs ({cities}){remote_clause} "
        f"site:linkedin.com OR site:lever.co OR site:greenhouse.io OR site:jobs.ashbyhq.com"
    )
    resp = requests.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_KEY},
        params={"q": query, "count": 5},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json().get("web", {}).get("results", [])
    if not results:
        return "No results — try broadening the search."
    lines = [
        f"• {r.get('title', '')}\n  {r.get('url', '')}\n  {r.get('description', '')[:120]}"
        for r in results[:5]
    ]
    return "\n\n".join(lines)


# ── Claude tool-use loop ───────────────────────────────────────────────────────

def execute_tool(name, inp, schedule_sheet, checklist_sheet):
    dispatch = {
        "add_event": lambda: action_add(schedule_sheet, inp),
        "list_events": lambda: action_list(schedule_sheet, inp["range"]),
        "cancel_event": lambda: action_cancel(schedule_sheet, inp["title"]),
        "snooze_event": lambda: action_snooze(schedule_sheet, inp["minutes"]),
        "complete_checklist_task": lambda: action_complete_task(checklist_sheet, inp["task"]),
        "list_checklist": lambda: action_list_checklist(checklist_sheet, inp.get("urgency_filter", "all")),
        "search_jobs": lambda: action_search_jobs(inp),
    }
    return dispatch.get(name, lambda: f"Unknown tool: {name}")()


def call_gary(history, user_text, schedule_sheet, checklist_sheet):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    messages = history + [{"role": "user", "content": user_text}]
    system = SYSTEM_PROMPT.replace("{TODAY}", date.today().isoformat())

    for _ in range(5):
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            return next((b.text for b in resp.content if hasattr(b, "text")), "")

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = [
                {
                    "type": "tool_result",
                    "tool_use_id": b.id,
                    "content": execute_tool(b.name, b.input, schedule_sheet, checklist_sheet),
                }
                for b in resp.content if b.type == "tool_use"
            ]
            messages.append({"role": "user", "content": results})

    return "Something went wrong — try again."


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    tg_url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    updates = requests.get(tg_url, params={"timeout": 0}, timeout=10).json().get("result", [])

    if not updates:
        logger.info("No new messages.")
        return

    gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDS))
    wb = gc.open_by_key(SHEET_ID)
    schedule_sheet = wb.sheet1
    checklist_sheet = gc.open_by_key(CHECKLIST_ID).sheet1
    history_ws = get_history_sheet(wb)
    history = load_history(history_ws)

    last_id = None
    for update in updates:
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = msg.get("chat", {}).get("id")

        if not text or not chat_id:
            last_id = update["update_id"]
            continue

        logger.info(f"Message: {text!r}")
        try:
            reply = call_gary(history, text, schedule_sheet, checklist_sheet)
        except Exception as e:
            logger.error(e)
            reply = "Something went wrong — try again in a moment."

        send_message(chat_id, reply)
        save_exchange(history_ws, text, reply)
        history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        last_id = update["update_id"]

    if last_id is not None:
        requests.get(tg_url, params={"offset": last_id + 1, "timeout": 0}, timeout=10)
        logger.info(f"Acknowledged up to update_id {last_id}")


if __name__ == "__main__":
    main()
