"""
bot_persistent.py — Gary running persistently on the VPS.

Uses the same Jarvis brain as bot_once.py (tool use, Sonnet, history)
but loops forever with long-polling instead of exiting after one batch.

Run on VPS:
    nohup python bot_persistent.py >> /var/log/gary.log 2>&1 &

Or with systemd (recommended):
    see gary.service
"""

import os
import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta, date

import asyncio

import anthropic
import gspread
import requests
from secrets import S

TOKEN = S.telegram_token
ANTHROPIC_KEY = S.anthropic_api_key
SHEET_ID = S.google_sheet_id
GOOGLE_CREDS = S.google_creds_json
CHECKLIST_ID = S.spreadsheet_id
TAVILY_KEY = S.tavily_api_key
LINKEDIN_EMAIL = S.linkedin_email
LINKEDIN_PASSWORD = S.linkedin_password

COL_DATE, COL_TIME, COL_TITLE = 1, 2, 3
COL_REM_1DAY, COL_REM_1HR, COL_REM_15MIN, COL_DONE = 4, 5, 6, 7
MAX_HISTORY_SONNET = 8   # last N messages sent to Sonnet
MAX_HISTORY_HAIKU = 5    # last N messages sent to Haiku
MAX_HISTORY_SHEET = 8    # rows kept in the History sheet
POLL_TIMEOUT = 30  # seconds — Telegram long-poll window

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

_resume_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resume.txt")
_RESUME = open(_resume_path).read().strip() if os.path.exists(_resume_path) else ""

# Haiku: scheduling only — no resume, minimal instructions (~80 tokens)
HAIKU_SYSTEM = (
    "You are Gary, Jackson's scheduling assistant. Today is {TODAY}. "
    "Manage his calendar and CA move checklist using tools. Be brief."
)

# Sonnet: full Jarvis with resume — only used for job/LinkedIn/reasoning tasks (~400 tokens)
SONNET_SYSTEM = (
    "You are Gary, Jackson's personal AI assistant — think Jarvis. "
    "Warm, sharp, concise. Today is {TODAY}. Jackson is moving to CA, job hunting in tech.\n\n"
    "RESUME:\n" + _RESUME + "\n\n"
    "For job searches: ask target roles, cities, remote preference before calling search_jobs. "
    "For LinkedIn: only call optimize_linkedin when explicitly asked. "
    "Use tools freely for scheduling and checklist. You can talk about anything."
)

# Keywords that require Sonnet (job reasoning, LinkedIn, resume evaluation)
JOB_KEYWORDS = {
    "job", "jobs", "apply", "linkedin", "resume", "career",
    "hire", "hiring", "position", "positions", "opening", "openings",
    "salary", "interview", "optimize", "recruiter", "employed", "employment",
    "role", "roles", "opportunity", "opportunities", "work", "internship",
    "tech", "engineer", "engineering", "developer", "analyst",
}


def route_model(text: str) -> str:
    """Return 'sonnet' if the message needs reasoning/job context, else 'haiku'."""
    words = set(text.lower().split())
    return "sonnet" if words & JOB_KEYWORDS else "haiku"

# Scheduling-only tools sent to Haiku (no search_jobs, no optimize_linkedin)
HAIKU_TOOLS = [
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
]

# Full tool set sent to Sonnet (scheduling tools + job search + LinkedIn)
SONNET_TOOLS = HAIKU_TOOLS + [
    {
        "name": "search_jobs",
        "description": "Search tech job openings in California. Only call after gathering job_titles, cities, and remote preference.",
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
    {
        "name": "optimize_linkedin",
        "description": "Scrape, rewrite, and apply optimized content to Jackson's LinkedIn profile. Takes 3-5 min. Only call when explicitly asked.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


# ── Sheet helpers ──────────────────────────────────────────────────────────────

def open_sheets():
    gc = gspread.service_account_from_dict(json.loads(GOOGLE_CREDS))
    wb = gc.open_by_key(SHEET_ID)
    schedule_sheet = wb.sheet1
    checklist_sheet = gc.open_by_key(CHECKLIST_ID).sheet1
    history_ws = get_history_sheet(wb)
    return schedule_sheet, checklist_sheet, history_ws


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
    excess = len(all_rows) - 1 - MAX_HISTORY_SHEET
    if excess > 0:
        ws.delete_rows(2, 1 + excess)


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_message(chat_id, text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )


def get_updates(offset=None):
    params = {"timeout": POLL_TIMEOUT, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/getUpdates",
        params=params,
        timeout=POLL_TIMEOUT + 10,
    )
    return resp.json().get("result", [])


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


def action_optimize_linkedin():
    if not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD:
        return "LINKEDIN_EMAIL or LINKEDIN_PASSWORD not set in .env — can't run."
    try:
        from linkedin.scraper import scrape_profile
        from linkedin.optimizer import optimize_profile
        from linkedin.editor import apply_edits
    except ImportError as e:
        return f"LinkedIn module not available: {e}"

    try:
        logger.info("LinkedIn: scraping profile...")
        profile = asyncio.run(scrape_profile(LINKEDIN_EMAIL, LINKEDIN_PASSWORD))

        logger.info("LinkedIn: optimizing with Claude...")
        optimized = optimize_profile(profile, api_key=ANTHROPIC_KEY)

        logger.info("LinkedIn: applying edits...")
        asyncio.run(apply_edits(optimized, LINKEDIN_EMAIL, LINKEDIN_PASSWORD))

        return (
            f"LinkedIn profile updated.\n"
            f"New headline: {optimized.get('headline', '')}\n"
            f"Rewrote {len(optimized.get('experience', []))} roles and "
            f"{len(optimized.get('skills', []))} skills."
        )
    except Exception as e:
        logger.error(f"LinkedIn optimization error: {e}")
        return f"LinkedIn optimization failed: {e}"


def action_search_jobs(params):
    if not TAVILY_KEY:
        return "Job search isn't set up yet — TAVILY_API_KEY is missing."
    titles = " OR ".join(params["job_titles"])
    cities = " OR ".join(params["cities"])
    remote_clause = " remote OR" if params.get("remote") else ""
    level = params.get("experience_level", "any")
    level_clause = f" {level}" if level not in ("any", None) else ""
    query = f"{remote_clause}{level_clause} {titles} jobs in {cities} California"
    resp = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": TAVILY_KEY,
            "query": query,
            "max_results": 5,
            "include_domains": [
                "linkedin.com", "lever.co", "greenhouse.io",
                "jobs.ashbyhq.com", "indeed.com",
            ],
        },
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if not results:
        return "No results — try broadening the search."
    lines = [
        f"• {r.get('title', '')}\n  {r.get('url', '')}\n  {r.get('content', '')[:150]}"
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
        "optimize_linkedin": lambda: action_optimize_linkedin(),
    }
    return dispatch.get(name, lambda: f"Unknown tool: {name}")()


def _tool_loop(client, model, max_tokens, system, tools, messages, schedule_sheet, checklist_sheet):
    """Shared tool-use loop for both Haiku and Sonnet."""
    for _ in range(5):
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
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


def call_gary_haiku(history, user_text, schedule_sheet, checklist_sheet):
    """Cheap path: scheduling/checklist tasks with no resume context."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    today = date.today().isoformat()
    trimmed = history[-MAX_HISTORY_HAIKU:] if len(history) > MAX_HISTORY_HAIKU else history
    messages = trimmed + [{"role": "user", "content": user_text}]
    system = HAIKU_SYSTEM.replace("{TODAY}", today)
    logger.info("Routing → Haiku")
    return _tool_loop(client, "claude-haiku-4-5-20251001", 512, system, HAIKU_TOOLS, messages, schedule_sheet, checklist_sheet)


def call_gary_sonnet(history, user_text, schedule_sheet, checklist_sheet):
    """Full-power path: job search, LinkedIn, reasoning, anything complex."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    today = date.today().isoformat()
    trimmed = history[-MAX_HISTORY_SONNET:] if len(history) > MAX_HISTORY_SONNET else history
    messages = trimmed + [{"role": "user", "content": user_text}]
    system = SONNET_SYSTEM.replace("{TODAY}", today)
    logger.info("Routing → Sonnet")
    return _tool_loop(client, "claude-sonnet-4-6", 1024, system, SONNET_TOOLS, messages, schedule_sheet, checklist_sheet)


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    logger.info("Gary (persistent) starting up...")

    # Open sheets once; reopen only on error
    schedule_sheet, checklist_sheet, history_ws = open_sheets()
    history = load_history(history_ws)

    offset = None
    running = True

    def handle_signal(sig, frame):
        nonlocal running
        logger.info(f"Signal {sig} received — shutting down cleanly.")
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while running:
        try:
            updates = get_updates(offset)
        except requests.exceptions.ReadTimeout:
            # Normal — long-poll timed out with no messages
            continue
        except Exception as e:
            logger.error(f"getUpdates error: {e} — retrying in 5s")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1  # advance regardless of whether we handle it

            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = msg.get("chat", {}).get("id")

            if not text or not chat_id:
                continue

            logger.info(f"Message: {text!r}")
            try:
                lower = text.strip().lower()
                parts = lower.split()

                # Layer 1: pre-Claude shortcuts — zero API tokens
                if lower in ("today", "week"):
                    reply = action_list(schedule_sheet, lower)
                elif lower in ("checklist", "tasks"):
                    reply = action_list_checklist(checklist_sheet, "all")
                elif lower == "done":
                    row, idx = find_last_reminded_event(schedule_sheet)
                    if row and idx:
                        schedule_sheet.update_cell(idx, COL_DONE, "TRUE")
                        reply = f"Marked done: {row['Title']}"
                    else:
                        reply = "No recently reminded event found."
                elif len(parts) == 2 and parts[0] == "snooze" and parts[1].isdigit():
                    reply = action_snooze(schedule_sheet, int(parts[1]))

                # Layer 2/3: route to Haiku or Sonnet
                elif route_model(text) == "sonnet":
                    reply = call_gary_sonnet(history, text, schedule_sheet, checklist_sheet)
                else:
                    reply = call_gary_haiku(history, text, schedule_sheet, checklist_sheet)

            except Exception as e:
                logger.error(f"Gary error: {e}")
                reply = "Something went wrong — try again in a moment."

            send_message(chat_id, reply)

            try:
                save_exchange(history_ws, text, reply)
            except Exception as e:
                logger.warning(f"History save error: {e}")

            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            # Keep in-memory history trimmed to Sonnet max (larger of the two)
            if len(history) > MAX_HISTORY_SONNET:
                history = history[-MAX_HISTORY_SONNET:]

        # Reopen sheets every ~30 min to refresh credentials
        # (gspread tokens expire; this is the simplest mitigation)

    logger.info("Gary shut down.")


if __name__ == "__main__":
    main()
