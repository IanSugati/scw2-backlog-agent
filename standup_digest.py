#!/usr/bin/env python3
"""
standup_digest.py

REQUIRED Secrets / env vars:
  JIRA_BASE_URL
  JIRA_EMAIL
  JIRA_API_TOKEN
  ANDY_STANDUP          (personal Google Chat webhook)
  SPRINT_ANCHOR_DATE    (YYYY-MM-DD)

Optional:
  ENFORCE_9AM_LONDON=true|false        (default false)
  JIRA_STORY_POINTS_FIELD              (default customfield_10016)
  STANDUP_LOOKBACK_DAYS=14             (default 14 - max days to look back for last worked day)

Fixes applied vs original:
  1. BUG FIX - "Yesterday" section now uses worklogs filtered to the specific day,
     not i["fields"]["timespent"] which is the all-time total for the ticket.
  2. SMART LOOKBACK - instead of always looking at "yesterday", the script finds
     the last day the developer actually logged time. This handles:
       - Weekends (Monday automatically shows Friday)
       - Public holidays
       - Days off / leave
     It walks backwards day by day (up to STANDUP_LOOKBACK_DAYS) until it finds
     a day with logged time, then shows that day with a clear label.
"""

import os
import sys
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Tuple

# -----------------------
# Config
# -----------------------
DEV_ACCOUNT_ID = "5be5be3875085254a6a76016"
DEV_NAME = "Andy Edmonds"

PROJECT_KEY = "SPD"

SPRINT_WORKDAYS = 9
SPRINT_GAP_DAYS = 1
HOURS_PER_DAY = 7

LONDON = ZoneInfo("Europe/London")

MAX_LINES = 12


# -----------------------
# Env helpers
# -----------------------
def req_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v.strip()


def present(name: str) -> str:
    return "SET" if (os.environ.get(name) or "").strip() else "MISSING"


def enforce_9am_london() -> bool:
    raw = (os.environ.get("ENFORCE_9AM_LONDON", "false") or "false").lower()
    return raw in ("1", "true", "yes")


def should_run_now() -> bool:
    if not enforce_9am_london():
        return True
    now = datetime.now(LONDON)
    return now.weekday() < 5 and now.hour == 8


# -----------------------
# HTTP helpers
# -----------------------
def _raise(r: requests.Response) -> None:
    if r.ok:
        return
    raise requests.HTTPError(f"{r.status_code} {r.reason} :: {(r.text or '')[:500]}")


def jira_auth() -> Tuple[str, str]:
    return (req_env("JIRA_EMAIL"), req_env("JIRA_API_TOKEN"))


def api_get(base_url: str, path: str, params=None) -> Dict[str, Any]:
    r = requests.get(f"{base_url}{path}", auth=jira_auth(), params=params, timeout=30)
    _raise(r)
    return r.json()


def api_post(base_url: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    r = requests.post(f"{base_url}{path}", auth=jira_auth(), json=payload, timeout=30)
    _raise(r)
    return r.json()


def jira_search(jql: str, fields: List[str]) -> List[Dict[str, Any]]:
    base = req_env("JIRA_BASE_URL").rstrip("/")
    payload = {
        "jql": " ".join(jql.split()),
        "maxResults": 200,
        "fields": fields,
    }
    return api_post(base, "/rest/api/3/search/jql", payload).get("issues", [])


def issue_link(key: str) -> str:
    base = req_env("JIRA_BASE_URL").rstrip("/")
    return f"<{base}/browse/{key}|{key}>"


# -----------------------
# Formatting helpers
# -----------------------
def parse_jira_dt(dt_str: str) -> datetime:
    if len(dt_str) >= 5 and dt_str[-5] in ("+", "-"):
        dt_str = dt_str[:-2] + ":" + dt_str[-2:]
    return datetime.fromisoformat(dt_str)


def seconds_to_pretty(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "0m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


# -----------------------
# Capacity
# -----------------------
def count_workdays(start: date, end: date) -> int:
    d = start
    days = 0
    while d < end:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def remaining_capacity_hours() -> int:
    anchor = date.fromisoformat(req_env("SPRINT_ANCHOR_DATE"))
    today = datetime.now(LONDON).date()
    cycle = SPRINT_WORKDAYS + SPRINT_GAP_DAYS
    pos = count_workdays(anchor, today) % cycle
    if pos >= SPRINT_WORKDAYS:
        return 0
    remaining_days = SPRINT_WORKDAYS - pos
    return remaining_days * HOURS_PER_DAY


# -----------------------
# Worklog helpers
# -----------------------
def fetch_worklogs_for_issue(issue_key: str) -> List[Dict[str, Any]]:
    base = req_env("JIRA_BASE_URL").rstrip("/")
    data = api_get(base, f"/rest/api/3/issue/{issue_key}/worklog", params={"maxResults": 5000})
    return data.get("worklogs", []) or []


def get_seconds_logged_on_day(issue_key: str, day_start: datetime, day_end: datetime) -> int:
    """Sum seconds logged by DEV_ACCOUNT_ID within a specific day window."""
    worklogs = fetch_worklogs_for_issue(issue_key)
    total = 0
    for wl in worklogs:
        author_id = (wl.get("author") or {}).get("accountId")
        if author_id != DEV_ACCOUNT_ID:
            continue
        started_raw = wl.get("started")
        if not started_raw:
            continue
        try:
            started_dt = parse_jira_dt(started_raw).astimezone(LONDON)
        except Exception:
            continue
        if day_start <= started_dt < day_end:
            total += int(wl.get("timeSpentSeconds") or 0)
    return total


def any_time_logged_on_date(check_date: date) -> bool:
    """Quick check via JQL: did DEV_ACCOUNT_ID log any time on check_date?"""
    jql = f"""
        project = {PROJECT_KEY}
        AND worklogAuthor = {DEV_ACCOUNT_ID}
        AND worklogDate = "{check_date.isoformat()}"
    """
    return len(jira_search(jql, ["summary"])) > 0


def find_last_worked_day(max_lookback: int = 14) -> Tuple[date, str]:
    """
    Walk backwards from yesterday until a day with logged time is found.

    Handles weekends, public holidays, and days off automatically.
    Returns (date, friendly label string).

    Examples:
      Monday morning  -> finds Friday, returns "Friday 28 Feb"
      After 3 days off -> returns "Wednesday 26 Feb (last worked day - 3 days ago)"
    """
    today = datetime.now(LONDON).date()
    max_lookback = int(os.environ.get("STANDUP_LOOKBACK_DAYS", str(max_lookback)))

    check = today - timedelta(days=1)

    for _ in range(max_lookback):
        if any_time_logged_on_date(check):
            days_ago = (today - check).days
            if days_ago == 1:
                label = check.strftime("%A %d %b")
            else:
                label = f"{check.strftime('%A %d %b')} (last worked day — {days_ago} days ago)"
            return check, label
        check -= timedelta(days=1)

    # Fallback — nothing found in lookback window
    yesterday = today - timedelta(days=1)
    return yesterday, f"{yesterday.strftime('%A %d %b')} (no time logged in last {max_lookback} days)"


def time_logged_on_day(target_date: date) -> List[Tuple[int, str, str]]:
    """Return (seconds, key, summary) tuples for all issues worked on target_date."""
    day_start = datetime(target_date.year, target_date.month, target_date.day, 0, 0, 0, tzinfo=LONDON)
    day_end = day_start + timedelta(days=1)

    jql = f"""
        project = {PROJECT_KEY}
        AND worklogAuthor = {DEV_ACCOUNT_ID}
        AND worklogDate = "{target_date.isoformat()}"
    """
    issues = jira_search(jql, ["summary"])

    lines = []
    for i in issues:
        key = i["key"]
        summary = (i["fields"].get("summary") or "").strip()
        spent_seconds = get_seconds_logged_on_day(key, day_start, day_end)
        if spent_seconds > 0:
            lines.append((spent_seconds, key, summary))

    lines.sort(reverse=True, key=lambda x: x[0])
    return lines[:MAX_LINES]


# -----------------------
# Sprint section
# -----------------------
def sprint_remaining() -> Tuple[List[str], List[str], float]:
    sp_field = os.environ.get("JIRA_STORY_POINTS_FIELD", "customfield_10016")

    issues = jira_search(
        f"project = {PROJECT_KEY} AND sprint in openSprints() AND statusCategory != Done",
        ["summary", "status", "duedate", "timespent", sp_field],
    )

    in_progress = []
    up_next = []
    total_sp = 0.0

    for i in issues:
        key = i["key"]
        f = i["fields"]
        summary = f["summary"]
        status = f["status"]["name"].lower()
        sp = f.get(sp_field) or 0
        spent = f.get("timespent") or 0  # all-time total, for context only

        if isinstance(sp, (int, float)):
            total_sp += float(sp)

        parts = [
            f"Due: {f.get('duedate')}" if f.get("duedate") else "Due: —",
            f"Est: {sp} SP" if sp else "Est: —",
            f"Spent: {seconds_to_pretty(spent)}",
        ]

        block = f"• {issue_link(key)} – {summary}\n  ({' | '.join(parts)})"

        if "in progress" in status:
            in_progress.append(block)
        else:
            up_next.append(block)

    return in_progress[:MAX_LINES], up_next[:MAX_LINES], total_sp


# -----------------------
# Build + send
# -----------------------
def build_digest() -> str:
    last_worked_date, day_label = find_last_worked_day()
    worked_lines = time_logged_on_day(last_worked_date)

    in_prog, next_up, total_sp = sprint_remaining()

    capacity = remaining_capacity_hours()
    overage = total_sp - capacity

    msg = f"🧑‍💻 Standup Prep – {DEV_NAME}\n\n"

    msg += f"⏱ Last worked: {day_label}\n"
    if worked_lines:
        for secs, key, summary in worked_lines:
            msg += f"• {issue_link(key)} – {summary} ({seconds_to_pretty(secs)})\n"
    else:
        msg += "• No time logged\n"
    msg += "\n"

    msg += "🧮 Capacity vs Estimate\n"
    msg += f"• Remaining capacity: {capacity}h\n"
    msg += f"• Committed (SP): {total_sp:.1f}h\n"
    msg += f"⚠ Over by {overage:.1f}h\n\n" if overage > 0 else "✅ Within capacity\n\n"

    msg += "🔥 In Progress\n"
    msg += "\n".join(in_prog) if in_prog else "• None"
    msg += "\n\n"

    msg += "📋 Up Next\n"
    msg += "\n".join(next_up) if next_up else "• None"

    return msg


def send_chat(text: str) -> None:
    webhook = req_env("ANDY_STANDUP")
    r = requests.post(webhook, json={"text": text}, timeout=30)
    _raise(r)


if __name__ == "__main__":
    print("[env] ANDY_STANDUP =", present("ANDY_STANDUP"))

    if not should_run_now():
        print("Not scheduled time — exiting.")
        sys.exit(0)

    digest = build_digest()
    send_chat(digest)

    print("Digest sent ✅")
