import os
import base64
import datetime as dt
import requests

JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].strip().rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"].strip()
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"].strip()
JIRA_PROJECT_KEY = os.environ["JIRA_PROJECT_KEY_TICKETS"].strip()

TICKETS_CHAT_WEBHOOK_URL = os.environ["TICKETS_CHAT_WEBHOOK_URL"].strip()

AGING_DAYS = 5
LIST_LIMIT = 10

WAITING_ON_CUSTOMER = {
    "Waiting for customer",
    "Pending Customer",
    "Customer Reply Needed"
}


def jira_headers():
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json"
    }


def jira_issue_browse_url(key):
    return f"{JIRA_BASE_URL}/browse/{key}"


def days_since(updated_iso):
    base = updated_iso[:19]
    updated_dt = dt.datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    return (dt.datetime.utcnow() - updated_dt).days


def get_issues():
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"

    jql = f'''
        project = {JIRA_PROJECT_KEY}
        AND statusCategory != Done
        ORDER BY updated DESC
    '''

    response = requests.get(
        url,
        headers=jira_headers(),
        params={
            "jql": jql,
            "maxResults": 100,
            "fields": ["summary", "status", "assignee", "priority", "updated"]
        }
    )

    response.raise_for_status()
    return response.json().get("issues", [])


def bullets(rows):
    if not rows:
        return "• None 🎉"

    lines = []
    for key, summary, extra in rows[:LIST_LIMIT]:
        label = f" {extra}" if extra else ""
        lines.append(f"• {key} – {summary}{label} ({jira_issue_browse_url(key)})")

    if len(rows) > LIST_LIMIT:
        lines.append(f"• +{len(rows) - LIST_LIMIT} more…")

    return "\n".join(lines)


def post_to_chat(text):
    requests.post(TICKETS_CHAT_WEBHOOK_URL, json={"text": text})


def main():
    issues = get_issues()

    unassigned = []
    high_priority = []
    waiting_customer = []
    aging = []

    for issue in issues:
        key = issue["key"]
        f = issue["fields"]

        summary = f.get("summary", "")
        status = f.get("status", {}).get("name", "")
        assignee = f.get("assignee")
        priority = f.get("priority", {}).get("name", "")
        updated = f.get("updated", "")

        age_days = days_since(updated) if updated else 0

        if assignee is None:
            unassigned.append((key, summary, None))

        if priority.lower() in {"high", "highest"}:
            high_priority.append((key, summary, f"(Priority: {priority})"))

        if status in WAITING_ON_CUSTOMER:
            waiting_customer.append((key, summary, None))

        if age_days >= AGING_DAYS:
            aging.append((key, summary, f"(last update: {age_days}d)"))

    message = []
    message.append("🎫 *SSH Tickets Digest*")
    message.append("")

    message.append(f"🚨 *Unassigned* ({len(unassigned)})")
    message.append(bullets(unassigned))
    message.append("")

    message.append(f"🔥 *High Priority* ({len(high_priority)})")
    message.append(bullets(high_priority))
    message.append("")

    message.append(f"🔁 *Waiting on Customer* ({len(waiting_customer)})")
    message.append(bullets(waiting_customer))
    message.append("")

    message.append(f"🧟 *Aging (≥ {AGING_DAYS} days)* ({len(aging)})")
    message.append(bullets(aging))

    post_to_chat("\n".join(message))


if __name__ == "__main__":
    main()
