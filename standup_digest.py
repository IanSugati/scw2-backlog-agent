import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

DEV_NAME = "Andy Edmonds"
DEV_ACCOUNT_ID = "5be5be3875085254a6a76016"
PROJECT_KEY = "SPD"


def env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value.strip()


def jira_search(base_url, auth, jql):
    url = f"{base_url}/rest/api/3/search"
    payload = {
        "jql": " ".join(jql.split()),
        "maxResults": 50,
        "fields": ["summary", "status", "duedate"]
    }
    r = requests.post(url, json=payload, auth=auth, timeout=30)
    r.raise_for_status()
    return r.json().get("issues", [])


def jira_worklogs(base_url, auth, issue_key):
    url = f"{base_url}/rest/api/3/issue/{issue_key}/worklog"
    r = requests.get(url, auth=auth, timeout=30)
    r.raise_for_status()
    return r.json().get("worklogs", [])


def issue_link(base, key):
    return f"<{base}/browse/{key}|{key}>"


def yesterday_logged(base_url, auth):
    jql = f"""
        worklogAuthor = "{DEV_ACCOUNT_ID}"
        AND worklogDate >= startOfDay(-1)
        AND worklogDate < startOfDay()
    """
    issues = jira_search(base_url, auth, jql)

    yesterday = datetime.now(LONDON).date() - timedelta(days=1)
    lines = []

    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]

        total_seconds = 0
        for wl in jira_worklogs(base_url, auth, key):
            if wl["author"]["accountId"] != DEV_ACCOUNT_ID:
                continue

            started = datetime.fromisoformat(
                wl["started"].replace("Z", "+00:00")
            ).astimezone(LONDON).date()

            if started == yesterday:
                total_seconds += wl["timeSpentSeconds"]

        if total_seconds:
            mins = total_seconds // 60
            h, m = mins // 60, mins % 60
            duration = f"{h}h {m}m" if h else f"{m}m"

            lines.append(f"• {issue_link(base_url, key)} – {summary} ({duration})")

    return lines


def overdue_items(base_url, auth):
    jql = f"""
        assignee = "{DEV_ACCOUNT_ID}"
        AND duedate < startOfDay()
        AND statusCategory != Done
        ORDER BY duedate ASC
    """
    issues = jira_search(base_url, auth, jql)

    today = datetime.now(LONDON).date()
    lines = []

    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        duedate = issue["fields"]["duedate"]

        if not duedate:
            continue

        due = datetime.strptime(duedate, "%Y-%m-%d").date()
        days = (today - due).days

        lines.append(f"• {issue_link(base_url, key)} – {summary} ({days} days overdue)")

    return lines


def at_timeline(base_url, token, username):
    url = f"{base_url}/rest/api/1/timeline/{username}"
    headers = {
        "Accept": "application/json",
        "auth-token": token
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def scheduled_rest_of_week(base_url, token, username):
    today = datetime.now(LONDON).date()
    end_week = today + timedelta(days=(6 - today.weekday()))

    try:
        data = at_timeline(base_url, token, username)
    except Exception:
        return ["• Unable to read ActivityTimeline schedule"]

    lines = []

    for item in data if isinstance(data, list) else []:
        key = item.get("issueKey") or item.get("key")
        start = item.get("startDate")

        if not key or not start:
            continue

        start_day = datetime.fromisoformat(
            start.replace("Z", "+00:00")
        ).astimezone(LONDON).date()

        if today <= start_day <= end_week:
            day_label = start_day.strftime("%a %d %b")
            lines.append(f"{day_label} – {issue_link(base_url, key)}")

    return lines if lines else ["• Nothing scheduled"]


def post_to_chat(webhook, text):
    r = requests.post(webhook, json={"text": text}, timeout=30)
    r.raise_for_status()


if __name__ == "__main__":
    jira_base = env("JIRA_BASE_URL").rstrip("/")
    jira_email = env("JIRA_EMAIL")
    jira_token = env("JIRA_API_TOKEN")
    chat_webhook = env("STAND_UP")

    at_token = env("AT_API_TOKEN")
    at_username = env("AT_USERNAME_ANDY")

    auth = (jira_email, jira_token)

    msg = f"🧑‍💻 Standup Prep – {DEV_NAME}\n\n"

    msg += "⏱️ Yesterday (time logged)\n"
    msg += "\n".join(yesterday_logged(jira_base, auth)) + "\n\n"

    msg += "🗓 Scheduled – Rest of Week\n"
    msg += "\n".join(scheduled_rest_of_week(jira_base, at_token, at_username)) + "\n\n"

    msg += "⚠️ Overdue\n"
    msg += "\n".join(overdue_items(jira_base, auth)) + "\n"

    post_to_chat(chat_webhook, msg)

    print("Digest sent ✅")
