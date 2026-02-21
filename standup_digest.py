# standup_digest.py
#
# Secrets / env vars (EXACT):
#   JIRA_BASE_URL
#   JIRA_EMAIL
#   JIRA_API_TOKEN
#   STAND_UP
#
# Optional:
#   ENFORCE_9AM_LONDON=true|false   (default false)
#   JIRA_STORY_POINTS_FIELD         (default customfield_10016)
#   SPRINT_ANCHOR_DATE              (YYYY-MM-DD)  ← REQUIRED for capacity calc
#
# Capacity Model:
#   9 working days sprint
#   1 working day gap
#   7 hours per day

import os
import sys
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

# ---- Config ----
DEV_ACCOUNT_ID = "5be5be3875085254a6a76016"
DEV_NAME = "Andy Edmonds"
PROJECT_KEY = "SPD"
QA_STATUS = "DEPLOYED TO QA"

SPRINT_WORKDAYS = 9
SPRINT_GAP_DAYS = 1
HOURS_PER_DAY = 7
# ---------------

LONDON = ZoneInfo("Europe/London")


def req_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def present(name: str) -> str:
    return "SET" if os.environ.get(name) else "MISSING"


def enforce_9am_london() -> bool:
    raw = os.environ.get("ENFORCE_9AM_LONDON", "false").strip().lower()
    return raw in ("1", "true", "yes")


def should_run_now() -> bool:
    if not enforce_9am_london():
        return True
    now = datetime.now(LONDON)
    return now.weekday() < 5 and now.hour == 9


def _raise(r: requests.Response):
    if r.ok:
        return
    raise requests.HTTPError(f"{r.status_code} {r.reason} :: {(r.text or '')[:500]}")


def jira_search(auth, base_url: str, jql: str, fields=None, max_results: int = 200):
    url = f"{base_url}/rest/api/3/search/jql"
    payload = {
        "jql": " ".join(jql.split()),
        "maxResults": max_results,
        "fields": fields or ["summary"],
    }
    r = requests.post(url, auth=auth, json=payload, timeout=30)
    _raise(r)
    return r.json().get("issues", [])


def issue_link(base_url: str, key: str) -> str:
    return f"<{base_url}/browse/{key}|{key}>"


def parse_jira_date(d: str) -> date | None:
    if not d:
        return None
    try:
        return datetime.fromisoformat(d).date()
    except Exception:
        return None


def seconds_to_pretty(seconds: int) -> str:
    seconds = int(seconds or 0)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def seconds_to_hours(seconds: int) -> float:
    return (int(seconds or 0)) / 3600.0


def count_workdays(start: date, end: date) -> int:
    days = 0
    d = start
    while d < end:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def remaining_sprint_capacity() -> int:
    anchor_str = req_env("SPRINT_ANCHOR_DATE")
    anchor = date.fromisoformat(anchor_str)

    today = datetime.now(LONDON).date()
    workdays_since_anchor = count_workdays(anchor, today)

    cycle = SPRINT_WORKDAYS + SPRINT_GAP_DAYS
    pos = workdays_since_anchor % cycle

    if pos >= SPRINT_WORKDAYS:
        return 0  # gap day

    remaining_days = SPRINT_WORKDAYS - pos
    return remaining_days * HOURS_PER_DAY


def sprint_remaining(auth, base_url: str):
    sp_field = os.environ.get("JIRA_STORY_POINTS_FIELD", "customfield_10016").strip()

    fields = ["summary", "status", "duedate", "timespent", sp_field]

    jql = f"""
        project = {PROJECT_KEY}
        AND sprint in openSprints()
        AND assignee = {DEV_ACCOUNT_ID}
        AND statusCategory != Done
        ORDER BY Rank ASC
    """

    issues = jira_search(auth, base_url, jql, fields=fields, max_results=500)

    active_statuses = {
        "in progress",
        "in review",
        "ready for integration",
        "ready for package",
        "deployed to qa",
        "deployed to package org",
    }

    in_progress = []
    up_next = []

    total_sp = 0

    for i in issues:
        key = i["key"]
        f = i["fields"]
        summary = f.get("summary") or ""
        status_name = (f.get("status", {}).get("name") or "").lower()

        sp = f.get(sp_field)
        spent_seconds = f.get("timespent") or 0

        if isinstance(sp, (int, float)):
            total_sp += float(sp)

        parts = []

        due_d = parse_jira_date(f.get("duedate"))
        if due_d:
            parts.append(f"Due: {due_d.strftime('%a %d %b')}")

        parts.append(f"Est: {sp:g} SP" if isinstance(sp, (int, float)) else "Est: —")
        parts.append(f"Spent: {seconds_to_pretty(spent_seconds)}")

        flag = ""
        if isinstance(sp, (int, float)) and sp > 0:
            spent_h = seconds_to_hours(spent_seconds)
            if spent_h > sp:
                flag = f" ⚠ Over +{spent_h - sp:.1f}h"
            elif spent_h / sp >= 0.8:
                flag = f" 🟠 {int((spent_h / sp)*100)}%"

        block = f"• {issue_link(base_url, key)} – {summary}\n  ({' | '.join(parts)}){flag}"

        if status_name in active_statuses:
            in_progress.append(block)
        else:
            up_next.append(block)

    return in_progress[:8], up_next[:8], total_sp


def build_digest(auth, base_url: str):
    in_prog, next_up, total_sp = sprint_remaining(auth, base_url)
    overdue_lines = overdue_all_projects(auth, base_url)

    capacity = remaining_sprint_capacity()
    overage = total_sp - capacity

    msg = f"🧑‍💻 Standup Prep – {DEV_NAME}\n\n"

    msg += "🧮 Capacity vs Estimate (Live Sprint)\n"
    msg += f"• Remaining capacity: {capacity:.0f}h\n"
    msg += f"• Committed (SP): {total_sp:.1f}h\n"

    if overage > 0:
        msg += f"⚠️ Over capacity by {overage:.1f}h\n\n"
    else:
        msg += "✅ Within capacity\n\n"

    msg += "📌 Live Sprint – Remaining (SPD)\n"
    msg += "──────────────────────────────\n\n"

    msg += "🔥 In Progress\n"
    msg += "\n".join(in_prog) if in_prog else "• None"
    msg += "\n\n"

    msg += "📋 Up Next\n"
    msg += "\n".join(next_up) if next_up else "• None"
    msg += "\n\n"

    msg += "⚠️ Overdue (all projects)\n"
    msg += "\n".join(overdue_lines) if overdue_lines else "• No overdue items"

    return msg


def overdue_all_projects(auth, base_url: str):
    jql = f"""
        assignee = {DEV_ACCOUNT_ID}
        AND duedate < startOfDay()
        AND statusCategory != Done
        ORDER BY duedate ASC
    """

    issues = jira_search(auth, base_url, jql, fields=["summary", "duedate"], max_results=200)

    today = datetime.now(LONDON).date()
    lines = []

    for i in issues:
        key = i["key"]
        summary = i["fields"]["summary"]
        due = parse_jira_date(i["fields"].get("duedate"))
        if not due:
            continue
        days = (today - due).days
        lines.append(f"• {issue_link(base_url, key)} – {summary} ({days} days)")

    return lines


def send_chat(webhook_url: str, text: str):
    r = requests.post(webhook_url, json={"text": text}, timeout=30)
    _raise(r)


if __name__ == "__main__":
    print("[env] JIRA_BASE_URL =", present("JIRA_BASE_URL"))
    print("[env] JIRA_EMAIL =", present("JIRA_EMAIL"))
    print("[env] JIRA_API_TOKEN =", present("JIRA_API_TOKEN"))
    print("[env] STAND_UP =", present("STAND_UP"))
    print("[env] SPRINT_ANCHOR_DATE =", present("SPRINT_ANCHOR_DATE"))

    base_url = req_env("JIRA_BASE_URL").strip().rstrip("/")
    email = req_env("JIRA_EMAIL").strip()
    token = req_env("JIRA_API_TOKEN").strip()
    webhook = req_env("STAND_UP").strip()

    auth = (email, token)

    if not should_run_now():
        print("Not scheduled run time — exiting.")
        sys.exit(0)

    digest = build_digest(auth, base_url)
    send_chat(webhook, digest)

    print("Digest sent ✅")
