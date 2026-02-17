import os
import base64
import datetime as dt
import requests

# ----------------------------
# Required env vars
# ----------------------------
JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].strip().rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"].strip()
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"].strip()
JIRA_PROJECT_KEY = os.environ["JIRA_PROJECT_KEY_TICKETS"].strip()
TICKETS_CHAT_WEBHOOK_URL = os.environ["TICKETS_CHAT_WEBHOOK_URL"].strip()

# ----------------------------
# Field IDs (confirmed from your /rest/api/3/field dump)
# ----------------------------
LABELS_FIELD = "labels"                 # system
PRODUCT_AREA_FIELD = "customfield_10150"
SUPPORT_CONTRACT_FIELD = "customfield_10098"

# ----------------------------
# Behaviour knobs
# ----------------------------
LIST_LIMIT = 10

WAITING_ON_CUSTOMER = {
    "Waiting for customer",
    "Pending Customer",
    "Customer Reply Needed",
}


def jira_headers():
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def jira_issue_browse_url(key):
    return f"{JIRA_BASE_URL}/browse/{key}"


def days_since(updated_iso):
    # Jira timestamps: 2026-02-17T12:34:56.000+0000
    base = updated_iso[:19]  # YYYY-MM-DDTHH:MM:SS
    updated_dt = dt.datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    return (dt.datetime.utcnow() - updated_dt).days


def is_blank(value) -> bool:
    """
    Treat as blank:
    - None
    - "" or whitespace
    - []
    - {} (or dict without a meaningful value/name)
    Jira select fields often return dicts with keys like: {"value": "..."} or {"name": "..."}
    """
    if value is None:
        return True
    if isinstance(value, str):
        return len(value.strip()) == 0
    if isinstance(value, list):
        return len(value) == 0
    if isinstance(value, dict):
        # Common select formats: {"value": "X"} or {"name": "X"}
        v = value.get("value") or value.get("name")
        if v is None:
            return True
        return len(str(v).strip()) == 0
    return False


def jql_search(jql: str, next_page_token: str | None = None, max_results: int = 100):
    """
    Jira Cloud: /rest/api/3/search/jql uses nextPageToken + isLast (not startAt/total).
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    params = {
        "jql": jql,
        "maxResults": max_results,
        "fields": [
            "summary",
            "status",
            "assignee",
            "priority",
            "updated",
            LABELS_FIELD,
            PRODUCT_AREA_FIELD,
            SUPPORT_CONTRACT_FIELD,
        ],
    }
    if next_page_token:
        params["nextPageToken"] = next_page_token

    r = requests.get(url, headers=jira_headers(), params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def get_all_issues(jql: str):
    issues = []
    token = None

    while True:
        data = jql_search(jql, next_page_token=token, max_results=100)
        batch = data.get("issues", []) or []
        issues.extend(batch)

        if data.get("isLast") is True:
            break

        token = data.get("nextPageToken")
        if not token or len(batch) == 0:
            break

    return issues


def bullets(rows, limit=LIST_LIMIT):
    if not rows:
        return "• None 🎉"

    lines = []
    for key, summary, extra in rows[:limit]:
        label = f" {extra}" if extra else ""
        lines.append(f"• {key} – {summary}{label} ({jira_issue_browse_url(key)})")

    if len(rows) > limit:
        lines.append(f"• +{len(rows) - limit} more…")

    return "\n".join(lines)


def post_to_chat(text):
    r = requests.post(TICKETS_CHAT_WEBHOOK_URL, json={"text": text}, timeout=30)
    r.raise_for_status()


def main():
    jql = f"""
        project = {JIRA_PROJECT_KEY}
        AND statusCategory != Done
        ORDER BY updated DESC
    """.strip()

    issues = get_all_issues(jql)

    unassigned = []
    high_priority = []
    waiting_customer = []

    # Missing-field buckets
    missing_labels = []
    missing_product_area = []
    missing_support_contract = []

    # Aging bands
    aging_5_9 = []
    aging_10_14 = []
    aging_15_19 = []
    aging_20_plus = []

    for issue in issues:
        key = issue["key"]
        f = issue.get("fields", {}) or {}

        summary = (f.get("summary") or "").strip()
        status = (f.get("status") or {}).get("name") or ""
        assignee = f.get("assignee")
        priority = (f.get("priority") or {}).get("name") or ""
        updated = f.get("updated") or ""

        labels = f.get(LABELS_FIELD)
        product_area = f.get(PRODUCT_AREA_FIELD)
        support_contract = f.get(SUPPORT_CONTRACT_FIELD)

        age_days = days_since(updated) if updated else 0

        # 🚨 Unassigned
        if assignee is None:
            unassigned.append((key, summary, None))

        # 🔥 High Priority (adjust later if your Jira uses P1/P2 naming)
        if priority.strip().lower() in {"high", "highest"}:
            high_priority.append((key, summary, f"(Priority: {priority})"))

        # 🔁 Waiting on Customer
        if status.strip() in WAITING_ON_CUSTOMER:
            waiting_customer.append((key, summary, f"(Status: {status})"))

        # 🏷 Missing Labels
        if is_blank(labels):
            missing_labels.append((key, summary, None))

        # 📦 Missing Product Area
        if is_blank(product_area):
            missing_product_area.append((key, summary, None))

        # 📜 Missing Support Contract
        if is_blank(support_contract):
            missing_support_contract.append((key, summary, None))

        # 🧟 Aging bands (based on "updated")
        if 5 <= age_days <= 9:
            aging_5_9.append((key, summary, f"(last update: {age_days}d)"))
        elif 10 <= age_days <= 14:
            aging_10_14.append((key, summary, f"(last update: {age_days}d)"))
        elif 15 <= age_days <= 19:
            aging_15_19.append((key, summary, f"(last update: {age_days}d)"))
        elif age_days >= 20:
            aging_20_plus.append((key, summary, f"(last update: {age_days}d)"))

    message = []
    message.append("🎫 *SSH Tickets Digest*")
    message.append("")

    # Missing-field hygiene first (so you can fix metadata quickly)
    message.append(f"🚨 *Missing Labels* ({len(missing_labels)})")
    message.append(bullets(missing_labels))
    message.append("")

    message.append(f"🚨 *Missing Product Area* ({len(missing_product_area)})")
    message.append(bullets(missing_product_area))
    message.append("")

    message.append(f"🚨 *Missing Support Contract* ({len(missing_support_contract)})")
    message.append(bullets(missing_support_contract))
    message.append("")

    # Operational sections
    message.append(f"🚨 *Unassigned* ({len(unassigned)})")
    message.append(bullets(unassigned))
    message.append("")

    message.append(f"🔥 *High Priority* ({len(high_priority)})")
    message.append(bullets(high_priority))
    message.append("")

    message.append(f"🔁 *Waiting on Customer* ({len(waiting_customer)})")
    message.append(bullets(waiting_customer))
    message.append("")

    # Aging bands
    message.append(f"🧟 *Aging 5–9 days (no updates)* ({len(aging_5_9)})")
    message.append(bullets(aging_5_9))
    message.append("")

    message.append(f"🧟 *Aging 10–14 days (no updates)* ({len(aging_10_14)})")
    message.append(bullets(aging_10_14))
    message.append("")

    message.append(f"🧟 *Aging 15–19 days (no updates)* ({len(aging_15_19)})")
    message.append(bullets(aging_15_19))
    message.append("")

    message.append(f"💀 *Aging 20+ days (no updates)* ({len(aging_20_plus)})")
    message.append(bullets(aging_20_plus))

    post_to_chat("\n".join(message))


if __name__ == "__main__":
    main()
