import os
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta


# -----------------------------
# ENV VARIABLES
# -----------------------------
JIRA_BASE_URL = os.environ["JIRA_BASE_URL"]
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
STAND_UP = os.environ["STAND_UP"]


auth = HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN)


# -----------------------------
# HELPERS
# -----------------------------
def api_get(url, params=None):
    r = requests.get(url, headers={"Accept": "application/json"}, auth=auth, params=params)

    print("\n--- DEBUG API CALL ---")
    print("URL:", url)
    print("Params:", params)
    print("Status Code:", r.status_code)
    print("Response:", r.text[:500])
    print("--- END DEBUG ---\n")

    r.raise_for_status()
    return r.json()


# -----------------------------
# JIRA SEARCH
# -----------------------------
def jira_search(jql, max_results=50):
    url = f"{JIRA_BASE_URL}/rest/api/3/search"
    params = {
        "jql": jql,
        "maxResults": max_results
    }

    print("\n--- DEBUG jira_search ---")
    print("Base URL:", JIRA_BASE_URL)
    print("Search URL:", url)
    print("JQL:", jql)
    print("Params:", params)

    r = requests.get(url, headers={"Accept": "application/json"}, auth=auth, params=params)

    print("Status Code:", r.status_code)
    print("Response Body:", r.text[:500])
    print("--- END DEBUG ---\n")

    r.raise_for_status()
    return r.json()["issues"]


# -----------------------------
# YESTERDAY WORKLOGGED
# -----------------------------
def yesterday_logged():
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    jql = f'worklogDate = "{yesterday}" AND assignee = "{STAND_UP}"'

    issues = jira_search(jql)

    lines = []
    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        lines.append(f"• {key} – {summary}")

    return lines if lines else ["• No time logged"]


# -----------------------------
# PUSHED TO QA
# -----------------------------
def pushed_to_qa():
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

    jql = f'status changed TO "Deployed to QA" AFTER "{yesterday}" AND assignee = "{STAND_UP}"'

    issues = jira_search(jql)

    lines = []
    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        lines.append(f"• {key} – {summary}")

    return lines if lines else ["• Nothing pushed to QA"]


# -----------------------------
# OVERDUE
# -----------------------------
def overdue_items():
    jql = f'assignee = "{STAND_UP}" AND duedate < now() AND statusCategory != Done'

    issues = jira_search(jql)

    lines = []
    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        due = issue["fields"]["duedate"]
        lines.append(f"• {key} – {summary} (Due: {due})")

    return lines if lines else ["• No overdue items"]


# -----------------------------
# LIVE SPRINT
# -----------------------------
def live_sprint():
    jql = f'assignee = "{STAND_UP}" AND sprint in openSprints() AND statusCategory != Done'

    issues = jira_search(jql)

    lines = []
    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        status = issue["fields"]["status"]["name"]
        lines.append(f"• {key} – {summary} [{status}]")

    return lines if lines else ["• None"]


# -----------------------------
# SEND TO GOOGLE CHAT
# -----------------------------
def send_to_chat(message):
    r = requests.post(STAND_UP, json={"text": message})

    print("\n--- DEBUG CHAT POST ---")
    print("Webhook:", STAND_UP)
    print("Status Code:", r.status_code)
    print("Response:", r.text)
    print("--- END DEBUG ---\n")

    r.raise_for_status()


# -----------------------------
# BUILD DIGEST
# -----------------------------
def build_digest():
    msg = f"📋 Standup Prep – {STAND_UP}\n\n"

    msg += "⏱ Yesterday (time logged)\n"
    msg += "\n".join(yesterday_logged()) + "\n\n"

    msg += "🚀 Pushed to QA (yesterday)\n"
    msg += "\n".join(pushed_to_qa()) + "\n\n"

    msg += "⚠️ Overdue\n"
    msg += "\n".join(overdue_items()) + "\n\n"

    msg += "📌 Live Sprint – Remaining\n"
    msg += "\n".join(live_sprint())

    return msg


# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":
    digest = build_digest()
    send_to_chat(digest)
    print("✅ Digest sent")
