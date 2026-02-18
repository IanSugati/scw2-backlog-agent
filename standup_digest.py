# standup_digest.py
#
# REQUIRED ENV VARS:
#   JIRA_EMAIL
#   JIRA_API_TOKEN
#   STAND_UP                      <-- Google Chat incoming webhook URL
#
# OPTIONAL ENV VARS:
#   ENFORCE_9AM_LONDON=true|false  (default true)

import os
import sys
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ---- Config (edit if needed) ----
JIRA_BASE = "https://sugatitravel.atlassian.net"
DEV_ACCOUNT_ID = "5be5be3875085254a6a76016"
DEV_NAME = "Andy Edmonds"
PROJECT_KEY = "SPD"
QA_STATUS = "DEPLOYED TO QA"
# --------------------------------

LONDON = ZoneInfo("Europe/London")


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


def enforce_9am_london() -> bool:
    raw = os.environ.get("ENFORCE_9AM_LONDON", "true").strip().lower()
    return raw in ("1", "true", "yes", "y", "on")


def should_run_now() -> bool:
    if not enforce_9am_london():
        return True
    now = datetime.now(LONDON)
    return now.weekday() < 5 and now.hour == 9  # Mon-Fri, 09:xx London


def seconds_to_pretty(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def zombie_indicator(days_overdue: int) -> str:
    zombies = min(days_overdue // 10, 10)  # 1 per 10 days, cap at 10
    skulls = (days_overdue // 100) if days_overdue >= 100 else 0
    return ("🧟" * zombies) + ((" " + ("💀" * skulls)) if skulls else "")


def yesterday_window_london():
    now = datetime.now(LONDON)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_yesterday = start_today - timedelta(days=1)
    return start_yesterday, start_today


def _raise_for_status_with_body(r: requests.Response):
    if r.ok:
        return
    snippet = (r.text or "")[:800]
    raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url} :: {snippet}")


def jira_myself():
    url = f"{JIRA_BASE}/rest/api/3/myself"
    r = requests.get(url, auth=AUTH, headers={"Accept": "application/json"}, timeout=30)
    _raise_for_status_with_body(r)
    return r.json()


def jira_search(jql: str, fields="summary,status,duedate", max_results: int = 100):
    """
    Uses /rest/api/3/search/jql (the legacy /search endpoint is removed in many tenants).
    """
    url = f"{JIRA_BASE}/rest/api/3/search/jql"
    jql_clean = " ".join([line.strip() for line in jql.splitlines() if line.strip()])

    payload = {
        "jql": jql_clean,
        "maxResults": max_results,
        "fields": fields.split(",") if isinstance(fields, str) else fields,
    }

    r = requests.post(
        url,
        auth=AUTH,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    _raise_for_status_with_body(r)
    data = r.json()
    return data.get("issues", []), data.get("total"), data.get("nextPageToken")


def debug_jql(label: str, jql: str):
    issues, total, next_token = jira_search(jql, fields="summary,status,duedate", max_results=20)
    keys = [i["key"] for i in issues[:10]]
    print(f"\n--- DEBUG: {label} ---")
    print(f"JQL: { ' '.join([line.strip() for line in jql.splitlines() if line.strip()]) }")
    print(f"Returned: {len(issues)} issues (total={total}, nextPageToken={next_token})")
    print(f"Sample keys: {keys if keys else '[]'}")
    return issues


def get_worklogs(issue_key: str):
    url = f"{JIRA_BASE}/rest/api/3/issue/{issue_key}/worklog"
    r = requests.get(url, auth=AUTH, params={"maxResults": 5000}, timeout=30)
    _raise_for_status_with_body(r)
    return r.json().get("worklogs", [])


def _parse_jira_datetime(started: str) -> datetime:
    # Jira often gives +0000; Python wants +00:00
    if len(started) >= 5 and (started[-5] in ["+", "-"]) and started[-2:].isdigit():
        started = started[:-2] + ":" + started[-2:]
    return datetime.fromisoformat(started)


def time_logged_yesterday(issues):
    start_yesterday, start_today = yesterday_window_london()
    results = []

    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        total_seconds = 0

        for wl in get_worklogs(key):
            author = wl.get("author", {}).get("accountId")
            started = wl.get("started")
            if author != DEV_ACCOUNT_ID or not started:
                continue

            started_dt = _parse_jira_datetime(started).astimezone(LONDON)

            if start_yesterday <= started_dt < start_today:
                total_seconds += int(wl.get("timeSpentSeconds", 0))

        if total_seconds:
            results.append(f"• {key} – {summary} ({seconds_to_pretty(total_seconds)})")

    return results


def pushed_to_qa_yesterday():
    jql = f"""
        project = {PROJECT_KEY}
        AND assignee = {DEV_ACCOUNT_ID}
        AND status CHANGED TO "{QA_STATUS}"
        DURING (startOfDay(-1), startOfDay())
        ORDER BY updated DESC
    """
    issues, _, _ = jira_search(jql, fields="summary,status,duedate", max_results=100)
    return [f"• {i['key']} – {i['fields']['summary']}" for i in issues]


def overdue_issues():
    jql = f"""
        project = {PROJECT_KEY}
        AND assignee = {DEV_ACCOUNT_ID}
        AND duedate < startOfDay()
        AND statusCategory != Done
        ORDER BY duedate ASC
    """
    issues, _, _ = jira_search(jql, fields="summary,status,duedate", max_results=200)

    today = datetime.now(LONDON).date()
    results = []

    for i in issues:
        due = i["fields"].get("duedate")
        if not due:
            continue

        due_date = datetime.fromisoformat(due).date()
        days = (today - due_date).days
        results.append(f"• {i['key']} – {i['fields']['summary']} ({days} days {zombie_indicator(days)})")

    return results


def sprint_remaining():
    jql = f"""
        project = {PROJECT_KEY}
        AND sprint in openSprints()
        AND assignee = {DEV_ACCOUNT_ID}
        AND statusCategory != Done
        ORDER BY Rank ASC
    """
    issues, _, _ = jira_search(jql, fields="summary,status,duedate", max_results=200)

    in_progress = []
    up_next = []

    for i in issues:
        status_name = i["fields"]["status"]["name"].strip().lower()
        line = f"• {i['key']} – {i['fields']['summary']}"

        if status_name in {"in progress", "in review", "ready for integration"}:
            in_progress.append(line)
        else:
            up_next.append(line)

    return in_progress[:5], up_next[:5]


def build_digest():
    yesterday_jql = f"""
        project = {PROJECT_KEY}
        AND worklogAuthor = {DEV_ACCOUNT_ID}
        AND worklogDate >= startOfDay(-1)
        AND worklogDate < startOfDay()
        ORDER BY updated DESC
    """
    yesterday_issues, _, _ = jira_search(yesterday_jql, fields="summary,status,duedate", max_results=200)
    time_lines = time_logged_yesterday(yesterday_issues)

    qa_lines = pushed_to_qa_yesterday()
    overdue_lines = overdue_issues()
    in_prog, next_up = sprint_remaining()

    msg = f"🧑‍💻 Standup Prep – {DEV_NAME}\n\n"

    msg += "⏱ Yesterday (time logged)\n"
    msg += "\n".join(time_lines) if time_lines else "• No time logged"
    msg += "\n\n"

    msg += "🚀 Pushed to QA (yesterday)\n"
    msg += "\n".join(qa_lines) if qa_lines else "• Nothing pushed to QA"
    msg += "\n\n"

    msg += "⚠️ Overdue\n"
    msg += "\n".join(overdue_lines) if overdue_lines else "• No overdue items"
    msg += "\n\n"

    msg += "📌 Live Sprint – Remaining\n\n"

    msg += "🔥 In Progress\n"
    msg += "\n".join(in_prog) if in_prog else "• None"
    msg += "\n\n"

    msg += "📋 Up Next\n"
    msg += "\n".join(next_up) if next_up else "• None"

    return msg


def send_to_chat(text: str):
    r = requests.post(WEBHOOK_URL, json={"text": text}, timeout=30)
    _raise_for_status_with_body(r)


if __name__ == "__main__":
    try:
        JIRA_EMAIL = _require_env("JIRA_EMAIL")
        JIRA_API_TOKEN = _require_env("JIRA_API_TOKEN")
        WEBHOOK_URL = _require_env("STAND_UP")
        AUTH = (JIRA_EMAIL, JIRA_API_TOKEN)

        # Always print who the API token user is
        me = jira_myself()
        print(f"\nAPI USER: {me.get('displayName')} | accountId={me.get('accountId')} | email={me.get('emailAddress')}\n")

        # Debug the exact queries
        debug_jql("Sanity: any SPD issues visible?", f"project = {PROJECT_KEY} ORDER BY updated DESC")
        debug_jql("Sprint remaining (Andy)", f"""
            project = {PROJECT_KEY}
            AND sprint in openSprints()
            AND assignee = {DEV_ACCOUNT_ID}
            AND statusCategory != Done
            ORDER BY Rank ASC
        """)
        debug_jql("Overdue (Andy)", f"""
            project = {PROJECT_KEY}
            AND assignee = {DEV_ACCOUNT_ID}
            AND duedate < startOfDay()
            AND statusCategory != Done
            ORDER BY duedate ASC
        """)
        debug_jql("Worklog yesterday (issue set)", f"""
            project = {PROJECT_KEY}
            AND worklogAuthor = {DEV_ACCOUNT_ID}
            AND worklogDate >= startOfDay(-1)
            AND worklogDate < startOfDay()
            ORDER BY updated DESC
        """)
        debug_jql("Pushed to QA yesterday", f"""
            project = {PROJECT_KEY}
            AND assignee = {DEV_ACCOUNT_ID}
            AND status CHANGED TO "{QA_STATUS}"
            DURING (startOfDay(-1), startOfDay())
            ORDER BY updated DESC
        """)

        if not should_run_now():
            print("Not 9am London (or weekend) — exiting without posting.")
            sys.exit(0)

        digest = build_digest()
        send_to_chat(digest)
        print("\nDigest sent ✅")

    except Exception as e:
        print(f"ERROR: {e}")
        raise
