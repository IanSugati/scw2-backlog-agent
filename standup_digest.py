# standup_digest.py
# Posts a 9am standup prep digest for one dev into a Google Chat space.
#
# REQUIRED ENV VARS:
#   JIRA_EMAIL
#   JIRA_API_TOKEN
#   GOOGLE_CHAT_STANDUP_WEBHOOK
#
# OPTIONAL ENV VARS:
#   ENFORCE_9AM_LONDON=true|false   (default true)  -> avoids double-run when you schedule 08:00+09:00 UTC for DST
#
# NOTES:
# - Jira Cloud /rest/api/3/search is called via POST (not GET) to avoid 410 Gone / long URL issues.
# - This MVP includes:
#   ✅ Time logged yesterday (per issue)
#   ✅ Pushed to QA yesterday (DEPLOYED TO QA)
#   ✅ Overdue list with 🧟 / 💀 indicators
#   ✅ Live sprint remaining (In Progress + Up Next)
#
# Next enhancements (when you're ready):
# - Status changes yesterday (by Andy) with from→to
# - Mentions yesterday + unanswered mentions

import os
import sys
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ---- Config (edit these if needed) ----
JIRA_BASE = "https://sugatitravel.atlassian.net"
DEV_ACCOUNT_ID = "5be5be3875085254a6a76016"
DEV_NAME = "Andy Edmonds"
PROJECT_KEY = "SPD"
QA_STATUS = "DEPLOYED TO QA"
# --------------------------------------

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
    """Used to avoid duplicate posts when GitHub cron runs at both 08:00 and 09:00 UTC for DST safety."""
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
    # 1 zombie per 10 days (cap at 100 days => 10 zombies)
    zombies = min(days_overdue // 10, 10)
    # skull per 100 days overdue (from 100 onward)
    skulls = days_overdue // 100 if days_overdue >= 100 else 0
    return ("🧟" * zombies) + ((" " + ("💀" * skulls)) if skulls else "")


def yesterday_window_london():
    now = datetime.now(LONDON)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_yesterday = start_today - timedelta(days=1)
    return start_yesterday, start_today


def jira_search(jql: str, fields="summary,status,duedate", max_results: int = 50):
    """
    Jira Cloud search via POST (fixes 410 Gone seen with GET query params).
    Returns issues list.
    """
    url = f"{JIRA_BASE}/rest/api/3/search"

    # Normalise multi-line / indented JQL into a single clean string
    jql_clean = " ".join([line.strip() for line in jql.splitlines() if line.strip()])

    payload = {
        "jql": jql_clean,
        "maxResults": max_results,
        "fields": fields.split(",") if isinstance(fields, str) else fields,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    r = requests.post(url, auth=AUTH, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json().get("issues", [])


def get_worklogs(issue_key: str):
    url = f"{JIRA_BASE}/rest/api/3/issue/{issue_key}/worklog"
    r = requests.get(url, auth=AUTH, params={"maxResults": 5000}, timeout=30)
    r.raise_for_status()
    return r.json().get("worklogs", [])


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

            # Jira returns offset like +0000; Python wants +00:00
            started_norm = started[:-2] + ":" + started[-2:] if started.endswith(("+0000", "+0100", "+0200", "+0300")) else started
            started_dt = datetime.fromisoformat(started_norm).astimezone(LONDON)

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
    issues = jira_search(jql)
    return [f"• {i['key']} – {i['fields']['summary']}" for i in issues]


def overdue_issues():
    jql = f"""
        project = {PROJECT_KEY}
        AND assignee = {DEV_ACCOUNT_ID}
        AND duedate < startOfDay()
        AND statusCategory != Done
        ORDER BY duedate ASC
    """
    issues = jira_search(jql)

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
    issues = jira_search(jql)

    in_progress = []
    up_next = []

    for i in issues:
        status_name = i["fields"]["status"]["name"].strip().lower()
        line = f"• {i['key']} – {i['fields']['summary']}"

        # tweak these buckets any time you want
        if status_name in {"in progress", "in review", "ready for integration"}:
            in_progress.append(line)
        else:
            up_next.append(line)

    return in_progress[:5], up_next[:5]


def build_digest():
    # Issues that have a worklog by Andy yesterday (issue set)
    yesterday_jql = f"""
        project = {PROJECT_KEY}
        AND worklogAuthor = {DEV_ACCOUNT_ID}
        AND worklogDate >= startOfDay(-1)
        AND worklogDate < startOfDay()
        ORDER BY updated DESC
    """
    yesterday_issues = jira_search(yesterday_jql)
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

    if in_prog:
        msg += "🔥 In Progress\n" + "\n".join(in_prog) + "\n\n"
    else:
        msg += "🔥 In Progress\n• None\n\n"

    if next_up:
        msg += "📋 Up Next\n" + "\n".join(next_up)
    else:
        msg += "📋 Up Next\n• None"

    return msg


def send_to_chat(text: str):
    r = requests.post(WEBHOOK_URL, json={"text": text}, timeout=30)
    r.raise_for_status()


if __name__ == "__main__":
    try:
        # required env
        JIRA_EMAIL = _require_env("JIRA_EMAIL")
        JIRA_API_TOKEN = _require_env("JIRA_API_TOKEN")
        WEBHOOK_URL = _require_env("GOOGLE_CHAT_STANDUP_WEBHOOK")
        AUTH = (JIRA_EMAIL, JIRA_API_TOKEN)

        if not should_run_now():
            print("Not 9am London (or weekend) — exiting without posting.")
            sys.exit(0)

        digest = build_digest()
        send_to_chat(digest)
        print("Digest sent ✅")

    except Exception as e:
        # Fail loud in Actions logs
        print(f"ERROR: {e}")
        raise
