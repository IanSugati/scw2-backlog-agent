import os
import base64
import datetime as dt
import requests

# Harden against secrets accidentally containing whitespace/newlines
JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].strip().rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"].strip()
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"].strip()
CHAT_WEBHOOK_URL = os.environ["CHAT_WEBHOOK_URL"].strip()
JIRA_PROJECT_KEY = os.environ["JIRA_PROJECT_KEY"].strip()
STORY_POINTS_FIELD = os.environ["JIRA_STORY_POINTS_FIELD"].strip()

# New required fields (Jira custom fields) for hygiene checks
PRO_SERVICES_WORK_TYPE_FIELD = os.environ["JIRA_PRO_SERVICES_WORK_TYPE_FIELD"].strip()
SPRINT_ORIGIN_FIELD = os.environ["JIRA_SPRINT_ORIGIN_FIELD"].strip()
SPRINT_FIELD = os.environ["JIRA_SPRINT_FIELD"].strip()  # e.g. customfield_10020

DESCRIPTION_MIN_CHARS = 30
OVERSIZED_SP = 28
AGING_DAYS = 30


def jira_headers() -> dict:
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode("utf-8")).decode("utf-8")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def jira_issue_browse_url(key: str) -> str:
    return f"{JIRA_BASE_URL}/browse/{key}"


def jql_search(jql: str, next_page_token: str | None = None, max_results: int = 100):
    """
    Jira Cloud: /rest/api/3/search is being removed; use /rest/api/3/search/jql instead.
    Pagination uses nextPageToken + isLast (not startAt/total).
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"

    params = {
        "jql": jql,
        "maxResults": max_results,
        "fields": [
            "summary",
            "description",
            "assignee",
            "updated",
            "status",
            "issuetype",
            "labels",
            SPRINT_FIELD,
            STORY_POINTS_FIELD,
            PRO_SERVICES_WORK_TYPE_FIELD,
            SPRINT_ORIGIN_FIELD,
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
        data = jql_search(jql, next_page_token=token)
        batch = data.get("issues", []) or []
        issues.extend(batch)

        if data.get("isLast") is True:
            break

        token = data.get("nextPageToken")
        if not token:
            break

        if len(batch) == 0:
            break

    return issues


def safe_len_description(desc) -> int:
    if desc is None:
        return 0
    if isinstance(desc, str):
        return len(desc.strip())
    if isinstance(desc, dict):

        def walk(node):
            if isinstance(node, dict):
                t = ""
                if node.get("type") == "text":
                    t += node.get("text", "")
                for c in node.get("content", []) or []:
                    t += walk(c)
                return t
            if isinstance(node, list):
                return "".join(walk(x) for x in node)
            return ""

        text = walk(desc)
        return len(text.strip())
    return 0


def days_since_iso(updated_iso: str) -> int:
    base = updated_iso[:19]  # YYYY-MM-DDTHH:MM:SS
    updated_dt = dt.datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    now = dt.datetime.utcnow()
    return (now - updated_dt).days


def has_any_sprint_value(sprint_field_value) -> bool:
    """
    Jira 'Sprint' custom field can come back as:
    - None
    - a list of sprint objects
    - occasionally a string (older formats)
    We treat "has a sprint" as any non-empty value/list.
    """
    if sprint_field_value is None:
        return False
    if isinstance(sprint_field_value, list):
        return len(sprint_field_value) > 0
    if isinstance(sprint_field_value, str):
        return len(sprint_field_value.strip()) > 0
    # fallback: some other truthy value
    return bool(sprint_field_value)


def is_blank(value) -> bool:
    """True if None, empty string, or empty list."""
    if value is None:
        return True
    if isinstance(value, str):
        return len(value.strip()) == 0
    if isinstance(value, list):
        return len(value) == 0
    return False


def build_digest(issues):
    missing_desc, missing_sp, unassigned, oversized, aging = [], [], [], [], []
    missing_ps_work_type, missing_labels, missing_sprint_origin = [], [], []

    for it in issues:
        key = it["key"]
        f = it.get("fields", {})
        summary = (f.get("summary") or "").strip()

        desc_len = safe_len_description(f.get("description"))
        sp = f.get(STORY_POINTS_FIELD)
        assignee = f.get("assignee")
        updated = f.get("updated") or ""
        age_days = days_since_iso(updated) if updated else 0

        labels = f.get("labels") or []
        ps_work_type = f.get(PRO_SERVICES_WORK_TYPE_FIELD)
        sprint_origin = f.get(SPRINT_ORIGIN_FIELD)
        sprint_value = f.get(SPRINT_FIELD)

        # Existing checks
        if desc_len < DESCRIPTION_MIN_CHARS:
            missing_desc.append((key, summary))
        if sp is None:
            missing_sp.append((key, summary))
        if assignee is None:
            unassigned.append((key, summary))
        if isinstance(sp, (int, float)) and sp >= OVERSIZED_SP:
            oversized.append((key, summary, sp))
        if age_days >= AGING_DAYS:
            aging.append((key, summary, age_days))

        # New checks
        if is_blank(ps_work_type):
            missing_ps_work_type.append((key, summary))
        if is_blank(labels):
            missing_labels.append((key, summary))
        # Sprint Origin only required IF Sprint field has a value
        if has_any_sprint_value(sprint_value) and is_blank(sprint_origin):
            missing_sprint_origin.append((key, summary))

    def bullets(rows, limit=15):
        if not rows:
            return "• None 🎉"
        lines = []
        for row in rows[:limit]:
            k = row[0]
            lines.append(f"• {k} – {row[1]} ({jira_issue_browse_url(k)})")
        if len(rows) > limit:
            lines.append(f"• +{len(rows)-limit} more…")
        return "\n".join(lines)

    msg = []
    msg.append("📚 *SCW2 Backlog Health Check*")
    msg.append("")

    # New sections first (so people see metadata hygiene quickly)
    msg.append(f"🚨 *Missing Pro Services Work Type* ({len(missing_ps_work_type)})")
    msg.append(bullets(missing_ps_work_type))
    msg.append("")
    msg.append(f"🚨 *Missing Labels* ({len(missing_labels)})")
    msg.append(bullets(missing_labels))
    msg.append("")
    msg.append(f"🚨 *Missing Sprint Origin (only when Sprint is set)* ({len(missing_sprint_origin)})")
    msg.append(bullets(missing_sprint_origin))
    msg.append("")

    # Existing sections
    msg.append(f"🚨 *Missing / thin description (<{DESCRIPTION_MIN_CHARS} chars)* ({len(missing_desc)})")
    msg.append(bullets(missing_desc))
    msg.append("")
    msg.append(f"🚨 *Missing estimate / story points* ({len(missing_sp)})")
    msg.append(bullets(missing_sp))
    msg.append("")
    msg.append(f"🚨 *Unassigned* ({len(unassigned)})")
    msg.append(bullets(unassigned))
    msg.append("")
    msg.append(f"⚠ *Oversized (≥ {OVERSIZED_SP} SP ≈ 4+ days)* ({len(oversized)})")

    if oversized:
        lines = []
        for k, summary, sp in oversized[:15]:
            lines.append(f"• {k} – {summary} (SP: {sp}) ({jira_issue_browse_url(k)})")
        if len(oversized) > 15:
            lines.append(f"• +{len(oversized)-15} more…")
        msg.append("\n".join(lines))
    else:
        msg.append("• None 🎉")

    msg.append("")
    msg.append(f"🧟 *Aging (no updates ≥ {AGING_DAYS} days)* ({len(aging)})")

    if aging:
        lines = []
        for k, summary, d in aging[:15]:
            lines.append(f"• {k} – {summary} (last update: {d}d) ({jira_issue_browse_url(k)})")
        if len(aging) > 15:
            lines.append(f"• +{len(aging)-15} more…")
        msg.append("\n".join(lines))
    else:
        msg.append("• None 🎉")

    return "\n".join(msg)


def post_to_chat(text: str):
    r = requests.post(CHAT_WEBHOOK_URL, json={"text": text}, timeout=30)
    r.raise_for_status()


def main():
    jql = (
        f"project = {JIRA_PROJECT_KEY} "
        f"AND sprint NOT IN openSprints() "
        f"AND statusCategory != Done "
        f"AND issuetype != \"Sprint Meeting\" "
        f"ORDER BY updated DESC"
    )

    issues = get_all_issues(jql)
    post_to_chat(build_digest(issues))


if __name__ == "__main__":
    main()
