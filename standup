import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

JIRA_BASE = "https://sugatitravel.atlassian.net"
DEV_ACCOUNT_ID = "5be5be3875085254a6a76016"
DEV_NAME = "Andy Edmonds"
PROJECT_KEY = "SPD"
QA_STATUS = "DEPLOYED TO QA"

auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
webhook_url = os.environ["GOOGLE_CHAT_STANDUP_WEBHOOK"]

LONDON = ZoneInfo("Europe/London")


def seconds_to_pretty(seconds):
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    elif hours:
        return f"{hours}h"
    else:
        return f"{minutes}m"


def zombie_indicator(days):
    zombies = min(days // 10, 10)
    skulls = days // 100 if days >= 100 else 0
    return "🧟" * zombies + (" 💀" * skulls if skulls else "")


def yesterday_window():
    now = datetime.now(LONDON)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_yesterday = start_today - timedelta(days=1)
    return start_yesterday, start_today


def jira_search(jql):
    url = f"{JIRA_BASE}/rest/api/3/search"
    r = requests.get(url, auth=auth, params={"jql": jql, "maxResults": 50, "fields": "summary,status,duedate"})
    r.raise_for_status()
    return r.json().get("issues", [])


def get_worklogs(issue_key):
    url = f"{JIRA_BASE}/rest/api/3/issue/{issue_key}/worklog"
    r = requests.get(url, auth=auth, params={"maxResults": 5000})
    r.raise_for_status()
    return r.json().get("worklogs", [])


def time_logged_yesterday(issues):
    start_yesterday, start_today = yesterday_window()
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

            started_dt = datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(LONDON)

            if start_yesterday <= started_dt < start_today:
                total_seconds += wl.get("timeSpentSeconds", 0)

        if total_seconds:
            results.append(f"• {key} – {summary} ({seconds_to_pretty(total_seconds)})")

    return results


def overdue_issues():
    jql = f"""
    project = {PROJECT_KEY}
    AND assignee = {DEV_ACCOUNT_ID}
    AND duedate < startOfDay()
    AND statusCategory != Done
    """
    issues = jira_search(jql)

    results = []
    today = datetime.now(LONDON).date()

    for i in issues:
        due = i["fields"]["duedate"]
        if not due:
            continue

        due_date = datetime.fromisoformat(due).date()
        days = (today - due_date).days
        indicator = zombie_indicator(days)

        results.append(f"• {i['key']} – {i['fields']['summary']} ({days} days {indicator})")

    return results


def pushed_to_qa_yesterday():
    jql = f"""
    project = {PROJECT_KEY}
    AND assignee = {DEV_ACCOUNT_ID}
    AND status CHANGED TO "{QA_STATUS}"
    DURING (startOfDay(-1), startOfDay())
    """
    issues = jira_search(jql)
    return [f"• {i['key']} – {i['fields']['summary']}" for i in issues]


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
        status = i["fields"]["status"]["name"]

        line = f"• {i['key']} – {i['fields']['summary']}"

        if status.lower() in ["in progress", "in review", "ready for integration"]:
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
    """

    yesterday_issues = jira_search(yesterday_jql)
    time_lines = time_logged_yesterday(yesterday_issues)

    qa_lines = pushed_to_qa_yesterday()
    overdue_lines = overdue_issues()
    in_prog, next_up = sprint_remaining()

    message = f"🧑‍💻 Standup Prep – {DEV_NAME}\n\n"

    message += "⏱ Yesterday\n"
    message += "\n".join(time_lines) if time_lines else "• No time logged"
    message += "\n\n"

    if qa_lines:
        message += "🚀 Pushed to QA\n" + "\n".join(qa_lines) + "\n\n"

    if overdue_lines:
        message += "⚠️ Overdue\n" + "\n".join(overdue_lines) + "\n\n"

    message += "📌 Live Sprint – Remaining\n\n"

    if in_prog:
        message += "🔥 In Progress\n" + "\n".join(in_prog) + "\n\n"

    if next_up:
        message += "📋 Up Next\n" + "\n".join(next_up)

    return message


def send_to_chat(text):
    requests.post(webhook_url, json={"text": text})


if __name__ == "__main__":
    digest = build_digest()
    send_to_chat(digest)
    print("Digest sent ✅")
