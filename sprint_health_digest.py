# sprint_health_digest.py
#
# REQUIRED Secrets / env vars:
#   JIRA_BASE_URL
#   JIRA_EMAIL
#   JIRA_API_TOKEN
#   CHAT_WEBHOOK_URL
#
# REQUIRED:
#   SPRINT_ANCHOR_DATE (YYYY-MM-DD)
#
# OPTIONAL:
#   JIRA_STORY_POINTS_FIELD (default customfield_10016)

import os
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

PROJECT_KEY = "SPD"

SPRINT_WORKDAYS = 9
SPRINT_GAP_DAYS = 1
HOURS_PER_DAY = 7

MAX_HISTORY_DAYS = 9
MAX_LINES = 6


def req_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v.strip()


def present(name: str) -> str:
    v = os.environ.get(name)
    return "SET" if (v is not None and v.strip() != "") else "MISSING"


def jira_auth():
    return (req_env("JIRA_EMAIL"), req_env("JIRA_API_TOKEN"))


def jira_get(url, **kwargs):
    r = requests.get(url, auth=jira_auth(), timeout=30, **kwargs)
    if not r.ok:
        raise requests.HTTPError(f"{r.status_code} {r.reason} :: {r.text[:500]}")
    return r.json()


def jira_post(url, payload):
    r = requests.post(url, auth=jira_auth(), json=payload, timeout=30)
    if not r.ok:
        raise requests.HTTPError(f"{r.status_code} {r.reason} :: {r.text[:500]}")
    return r.json()


def jira_search(jql: str, fields=None, max_results=200):
    base = req_env("JIRA_BASE_URL").rstrip("/")
    url = f"{base}/rest/api/3/search/jql"

    payload = {
        "jql": " ".join(jql.split()),
        "maxResults": max_results,
        "fields": fields or ["summary", "status", "duedate", "timespent"],
    }

    return jira_post(url, payload).get("issues", [])


def issue_link(key: str) -> str:
    base = req_env("JIRA_BASE_URL").rstrip("/")
    return f"<{base}/browse/{key}|{key}>"


def parse_jira_dt(dt_str: str):
    if len(dt_str) >= 5 and dt_str[-5] in ["+", "-"]:
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


# ----------------
# Sprint capacity
# ----------------
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


# ----------------
# Data collection
# ----------------
def sprint_issues():
    jql = f"""
        project = {PROJECT_KEY}
        AND sprint in openSprints()
        AND statusCategory != Done
        ORDER BY Rank ASC
    """
    return jira_search(jql)


def get_worklogs(issue_key: str):
    base = req_env("JIRA_BASE_URL").rstrip("/")
    url = f"{base}/rest/api/3/issue/{issue_key}/worklog"
    return jira_get(url, params={"maxResults": 5000}).get("worklogs", [])


# ----------------
# History builder
# ----------------
def build_daily_history(issues):
    today = datetime.now(LONDON).date()

    days = []
    d = today
    while len(days) < MAX_HISTORY_DAYS:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)

    history_blocks = []

    for day in days:
        start = datetime.combine(day, datetime.min.time(), tzinfo=LONDON)
        end = start + timedelta(days=1)

        worklog_totals = {}
        transitions = []

        for issue in issues:
            key = issue["key"]

            total_seconds = 0

            for wl in get_worklogs(key):
                started = wl.get("started")
                if not started:
                    continue

                dt_local = parse_jira_dt(started).astimezone(LONDON)

                if start <= dt_local < end:
                    total_seconds += int(wl.get("timeSpentSeconds", 0))

            if total_seconds:
                worklog_totals[key] = total_seconds

            # Status transitions from changelog
            histories = jira_get(
                f"{req_env('JIRA_BASE_URL').rstrip('/')}/rest/api/3/issue/{key}",
                params={"expand": "changelog"},
            ).get("changelog", {}).get("histories", [])

            for h in histories:
                created = parse_jira_dt(h["created"]).astimezone(LONDON)
                if not (start <= created < end):
                    continue

                for item in h.get("items", []):
                    if item.get("field") == "status":
                        transitions.append(
                            f"• {issue_link(key)}: {item.get('fromString')} → {item.get('toString')}"
                        )

        block = f"🗓 {day.strftime('%A %d %b')}\n\n"

        block += "⏱ Work logged\n"
        if worklog_totals:
            # sort by most time spent that day
            sorted_items = sorted(worklog_totals.items(), key=lambda x: x[1], reverse=True)
            for k, sec in sorted_items[:MAX_LINES]:
                block += f"• {issue_link(k)} – {seconds_to_pretty(sec)}\n"
            if len(sorted_items) > MAX_LINES:
                block += f"• +{len(sorted_items) - MAX_LINES} more…\n"
        else:
            block += "• None\n"

        block += "\n🔁 Status moves\n"
        if transitions:
            for line in transitions[:MAX_LINES]:
                block += f"{line}\n"
            if len(transitions) > MAX_LINES:
                block += f"• +{len(transitions) - MAX_LINES} more…\n"
        else:
            block += "• None\n"

        history_blocks.append(block.rstrip())

    return history_blocks


# ----------------
# Digest builder
# ----------------
def build_digest():
    issues = sprint_issues()

    sp_field = os.environ.get("JIRA_STORY_POINTS_FIELD", "customfield_10016").strip()

    total_sp = 0.0
    for i in issues:
        sp = (i.get("fields", {}) or {}).get(sp_field)
        if isinstance(sp, (int, float)):
            total_sp += float(sp)

    capacity = remaining_capacity_hours()
    overage = total_sp - capacity

    msg = "📊 *Sprint Health Digest*\n\n"
    msg += "🧮 Capacity vs Commitment\n"
    msg += f"• Remaining capacity: {capacity:.0f}h\n"
    msg += f"• Committed (SP): {total_sp:.1f}h\n"
    msg += (f"⚠ Over capacity by {overage:.1f}h\n" if overage > 0 else "✅ Within capacity\n")
    msg += "\n──────────────────────────────\n\n"
    msg += "\n\n".join(build_daily_history(issues))

    return msg


def send_chat(text: str):
    r = requests.post(req_env("CHAT_WEBHOOK_URL"), json={"text": text}, timeout=30)
    if not r.ok:
        raise requests.HTTPError(f"Chat error {r.status_code}: {r.text[:500]}")


if __name__ == "__main__":
    print("[env] SPRINT_ANCHOR_DATE =", present("SPRINT_ANCHOR_DATE"))

    digest = build_digest()
    send_chat(digest)

    print("Sprint digest sent ✅")
