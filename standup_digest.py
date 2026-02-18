import os
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

JIRA_BASE_URL = os.environ["JIRA_BASE_URL"]
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
CHAT_WEBHOOK = os.environ["STAND_UP"]

DEV_NAME = "Andy Edmonds"
DEV_ACCOUNT_ID = "5be5be3875085254a6a76016"

auth = (JIRA_EMAIL, JIRA_API_TOKEN)

HEADERS = {
    "Accept": "application/json"
}


# --------------------------------------------------
# Jira Search (FIXED ENDPOINT)
# --------------------------------------------------
def jira_search(jql, max_results=50):
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"

    params = {
        "jql": jql,
        "maxResults": max_results,
        "fields": "summary,status,duedate"
    }

    response = requests.get(url, headers=HEADERS, params=params, auth=auth)

    print("\n--- DEBUG jira_search ---")
    print("URL:", url)
    print("JQL:", jql)
    print("Status:", response.status_code)
    print("Response:", response.text[:500])
    print("--- END DEBUG ---\n")

    response.raise_for_status()
    return response.json()["issues"]


def issue_link(key):
    return f"<{JIRA_BASE_URL}/browse/{key}|{key}>"


# --------------------------------------------------
# Yesterday Work Logged
# --------------------------------------------------
def yesterday_logged():
    yesterday = datetime.now(LONDON).date() - timedelta(days=1)

    jql = f"""
        worklogAuthor = "{DEV_ACCOUNT_ID}"
        AND worklogDate >= startOfDay(-1)
        AND worklogDate < startOfDay()
    """

    issues = jira_search(jql)

    if not issues:
        return ["• No time logged"]

    lines = []
    for issue in issues:
        lines.append(f"• {issue_link(issue['key'])} – {issue['fields']['summary']}")

    return lines


# --------------------------------------------------
# Pushed to QA Yesterday
# --------------------------------------------------
def pushed_to_qa():
    jql = f"""
        status changed TO "Deployed to QA"
        AFTER startOfDay(-1)
        BY "{DEV_ACCOUNT_ID}"
    """

    issues = jira_search(jql)

    if not issues:
        return ["• Nothing pushed to QA"]

    lines = []
    for issue in issues:
        lines.append(f"• {issue_link(issue['key'])} – {issue['fields']['summary']}")

    return lines


# --------------------------------------------------
# Overdue Items
# --------------------------------------------------
def overdue_items():
    jql = f"""
        assignee = "{DEV_ACCOUNT_ID}"
        AND duedate < now()
        AND statusCategory != Done
        ORDER BY duedate ASC
    """

    issues = jira_search(jql)

    if not issues:
        return ["• No overdue items"]

    today = datetime.now(LONDON).date()
    lines = []

    for issue in issues:
        duedate = issue["fields"]["duedate"]
        if not duedate:
            continue

        due = datetime.strptime(duedate, "%Y-%m-%d").date()
        days_overdue = (today - due).days

        lines.append(
            f"• {issue_link(issue['key'])} – {issue['fields']['summary']} "
            f"({days_overdue} days overdue)"
        )

    return lines if lines else ["• No overdue items"]


# --------------------------------------------------
# Live Sprint Remaining
# --------------------------------------------------
def sprint_remaining():
    jql = f"""
        assignee = "{DEV_ACCOUNT_ID}"
        AND sprint in openSprints()
        AND statusCategory != Done
    """

    issues = jira_search(jql)

    if not issues:
        return ["• None"]

    lines = []
    for issue in issues:
        status = issue["fields"]["status"]["name"]
        lines.append(
            f"• {issue_link(issue['key'])} – {issue['fields']['summary']} [{status}]"
        )

    return lines


# --------------------------------------------------
# Send to Google Chat
# --------------------------------------------------
def post_to_chat(message):
    payload = {"text": message}
    response = requests.post(CHAT_WEBHOOK, json=payload)

    print("\n--- DEBUG Google Chat ---")
    print("Status:", response.status_code)
    print("Response:", response.text)
    print("--- END DEBUG ---\n")

    response.raise_for_status()


# --------------------------------------------------
# Build Digest
# --------------------------------------------------
def build_digest():
    msg = f"📋 Standup Prep – {DEV_NAME}\n\n"

    msg += "⏱️ Yesterday (time logged)\n"
    msg += "\n".join(yesterday_logged()) + "\n\n"

    msg += "🚀 Pushed to QA (yesterday)\n"
    msg += "\n".join(pushed_to_qa()) + "\n\n"

    msg += "📌 Live Sprint – Remaining\n"
    msg += "\n".join(sprint_remaining()) + "\n\n"

    msg += "⚠️ Overdue\n"
    msg += "\n".join(overdue_items())

    return msg


# --------------------------------------------------
# MAIN
# --------------------------------------------------
if __name__ == "__main__":
    digest = build_digest()
    post_to_chat(digest)
    print("✅ Digest sent")
