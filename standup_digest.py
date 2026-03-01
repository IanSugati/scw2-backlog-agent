#!/usr/bin/env python3
"""
standup_digest.py

REQUIRED Secrets / env vars (EXACT):
  JIRA_BASE_URL
  JIRA_EMAIL
  JIRA_API_TOKEN
  ANDY_STANDUP
  STAND_UP
  SPRINT_ANCHOR_DATE (YYYY-MM-DD)

Optional:
  ENFORCE_9AM_LONDON=true|false   (default false)
  UPCOMING_DAYS=2                 (default 2)
  JIRA_STORY_POINTS_FIELD=customfield_10016 (default customfield_10016)

Behaviour:
- Yesterday time logged (BUG FIX: now uses worklogs filtered to yesterday, not timespent field)
- Capacity vs Estimate (Live Sprint)
- Live Sprint - In Progress
- Live Sprint - Up Next
"""

import os
import sys
import requests
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

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
    return now.weekday() < 5 and now.hour == 9


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
# Worklog fetch (the fix)
# -----------------------
def fetch_worklogs_for_issue(issue_key: str) -> List[Dict[str, Any]]:
    """Fetch all worklogs for a given issue."""
    base = req_env("JIRA_BASE_URL").rstrip("/")
    data = api_get(base, f"/rest/api/3/issue/{issue_key}/worklog", params={"maxResults": 5000})
    return data.get("worklogs", []) or []


def get_yesterday_seconds_for_issue(issue_key: str, yesterday_start: datetime, yesterday_end: datetime) -> int:
    """
    Fetch worklogs for an issue and sum seconds logged by DEV_ACCOUNT_ID
    within yesterday's window (London time).

    FIX: Previously used i["fields"]["timespent"] which is the TOTAL all-time
    seconds logged on the ticket. This now correctly filters to yesterday only.
    """
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

        if yesterday_start <= started_dt < yesterday_end:
            total += int(wl.get("timeSpentSeconds") or 0)

    return total


# -----------------------
# Sections
# -----------------------
def time_logged_yesterday() -> List[str]:
    """
    Return lines for time logged by DEV_ACCOUNT_ID yesterday.

    BUG FIX: The original implementation used i["fields"]["timespent"] which
    returns the TOTAL cumulative time ever logged on the ticket — not just
    yesterday's time. For example, "Morning Meetings February 2026" was showing
    18h 54m because that was the all-time total, not yesterday's contribution.

    This version fetches worklogs per issue and sums only entries where:
      - author == DEV_ACCOUNT_ID
      - started timestamp falls within yesterday (London time)
    """
    now_london = datetime.now(LONDON)
    today_london = now_london.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_london - timedelta(days=1)
    yesterday_end = today_london

    # Find issues that had worklogs yesterday (Jira JQL worklogDate filter)
    yesterday_date_str = yesterday_start.date().isoformat()
    jql = f"""
        project = {PROJECT_KEY}
        AND worklogAuthor = {DEV_ACCOUNT_ID}
        AND worklogDate = "{yesterday_date_str}"
    """
    issues = jira_search(jql, ["summary"])

    lines = []
    for i in issues:
        key = i["key"]
        summary = (i["fields"].get("summary") or "").strip()

        # Fetch actual worklogs and filter to yesterday + this dev only
        spent_seconds = get_yesterday_seconds_for_issue(key, yesterday_start, yesterday_end)

        if spent_seconds > 0:
            lines.append((spent_seconds, key, summary))

    # Sort by most time first
    lines.sort(reverse=True, key=lambda x: x[0])

    return [
        f"• {issue_link(key)} – {summary} ({seconds_to_pretty(secs)})"
        for secs, key, summary in lines[:MAX_LINES]
    ]


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
        spent = f.get("timespent") or 0  # total spent shown here for context only

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
    yesterday_lines = time_logged_yesterday()
    in_prog, next_up, total_sp = sprint_remaining()

    capacity = remaining_capacity_hours()
    overage = total_sp - capacity

    now_london = datetime.now(LONDON)
    yesterday = (now_london - timedelta(days=1)).strftime("%a %d %b")

    msg = f"🧑‍💻 Standup Prep – {DEV_NAME}\n\n"

    msg += f"⏱ Yesterday ({yesterday})\n"
    msg += "\n".join(yesterday_lines) if yesterday_lines else "• No time logged"
    msg += "\n\n"

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
