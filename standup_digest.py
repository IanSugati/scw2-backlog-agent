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
  JIRA_START_DATE_FIELD=customfield_XXXXX   (optional)

Behaviour:
- Yesterday time logged
- Deployed to QA (previous 2 working days)
- Next N days
- Capacity vs Estimate (Live Sprint)
- Live Sprint – Remaining
"""

import os
import sys
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

# -----------------------
# Config
# -----------------------
DEV_ACCOUNT_ID = "5be5be3875085254a6a76016"
DEV_NAME = "Andy Edmonds"

PROJECT_KEY = "SPD"
QA_STATUS = "DEPLOYED TO QA"

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
# Sections
# -----------------------
def time_logged_yesterday() -> List[str]:
    jql = f"""
        project = {PROJECT_KEY}
        AND worklogAuthor = {DEV_ACCOUNT_ID}
        AND worklogDate >= startOfDay(-1)
        AND worklogDate < startOfDay()
    """
    issues = jira_search(jql, ["summary", "timespent"])

    lines = []
    for i in issues:
        key = i["key"]
        summary = i["fields"]["summary"]
        spent = i["fields"].get("timespent") or 0

        if spent:
            lines.append(f"• {issue_link(key)} – {summary} ({seconds_to_pretty(spent)})")

    return lines[:MAX_LINES]


def sprint_remaining():
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
        spent = f.get("timespent") or 0

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
    yesterday = time_logged_yesterday()
    in_prog, next_up, total_sp = sprint_remaining()

    capacity = remaining_capacity_hours()
    overage = total_sp - capacity

    msg = f"🧑‍💻 Standup Prep – {DEV_NAME}\n\n"

    msg += "⏱ Yesterday\n"
    msg += "\n".join(yesterday) if yesterday else "• No time logged"
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
