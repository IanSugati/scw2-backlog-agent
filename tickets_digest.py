import os
import base64
import requests

JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].strip().rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"].strip()
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"].strip()
JIRA_PROJECT_KEY = os.environ["JIRA_PROJECT_KEY_TICKETS"].strip()

TICKETS_CHAT_WEBHOOK_URL = os.environ["TICKETS_CHAT_WEBHOOK_URL"].strip()


def jira_headers():
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json"
    }


def jira_issue_browse_url(key):
    return f"{JIRA_BASE_URL}/browse/{key}"


def get_issues():
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"

    jql = f'project = {JIRA_PROJECT_KEY} AND statusCategory != Done ORDER BY updated DESC'

    response = requests.get(
        url,
        headers=jira_headers(),
        params={
            "jql": jql,
            "maxResults": 5,
            "fields": ["summary", "status"]
        }
    )

    response.raise_for_status()
    return response.json().get("issues", [])


def post_to_chat(text):
    requests.post(TICKETS_CHAT_WEBHOOK_URL, json={"text": text})


def main():
    issues = get_issues()

    if not issues:
        post_to_chat("🎫 No open tickets 🎉")
        return

    lines = ["🎫 *Latest SSH Tickets*"]
    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        lines.append(f"• {key} – {summary} ({jira_issue_browse_url(key)})")

    post_to_chat("\n".join(lines))


if __name__ == "__main__":
    main()
