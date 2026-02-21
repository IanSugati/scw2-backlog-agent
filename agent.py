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

STORY_POINTS_FIELD = os.environ.get("JIRA_STORY_POINTS_FIELD", "customfield_10016").strip()

SPRINT_FIELD = "customfield_10020"
SPRINT_ORIGIN_FIELD = "customfield_10104"
PRO_SERVICES_WORK_TYPE_FIELD = "customfield_10459"

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


def jql_search(jql: str, next_page_token=None, max_results: int = 100):
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"

    params = {
        "jql": jql,
        "maxResults": max_results,
        "fields": [
            "summary",
            "description",
            "assignee",
            "updated",
            "labels",
            STORY_POINTS_FIELD,
            SPRINT_FIELD,
            SPRINT_ORIGIN_FIELD,
            PRO_SERVICES_WORK_TYPE_FIELD,
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
        if not token or len(batch) == 0:
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

        return len(walk(desc).strip())

    return 0


def days_since_iso(updated_iso: str) -> int:
    base = updated_iso[:19]
    updated_dt = dt.datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
    return (dt.datetime.utcnow() - updated_dt).days


def has_any_sprint_value(val) -> bool:
    if val is None:
        return False
    if isinstance(val, list):
        return len(val) > 0
    if isinstance(val, str):
        return len(val.strip()) > 0
    return bool(val)


def is_blank(value) -> bool:
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

        if is_blank(ps_work_type):
            missing_ps_work_type.append((key, summary))
        if is_blank(labels):
            missing_labels.append((key, summary))
        if has_any_sprint_value(sprint_value) and is_blank(sprint_origin):
            missing_sprint_origin.append((key, summary))

    def bullets(rows, limit=15):
        if not rows:
            return "• None 🎉"
        lines = [f"• {k} – {s} ({jira_issue_browse_url(k)})" for k, s in rows[:limit]]
        if len(rows) > limit:
            lines.append(f"• +{len(rows)-limit} more…")
        return "\n".join(lines)

    msg = []
    msg.append("📚 *SCW2 Backlog Health Check*")
    msg.append("")

    msg.append(f"🚨 *Missing Pro Services Work Type* ({len(missing_ps_work_type)})")
    msg.append(bullets(missing_ps_work_type))
    msg.append("")
    msg.append(f"🚨 *Missing Labels* ({len(missing_labels)})")
    msg.append(bullets(missing_labels))
    msg.append("")
    msg.append(f"🚨 *Missing Sprint Origin (only when Sprint is set)* ({len(missing_sprint_origin)})")
    msg.append(bullets(missing_sprint_origin))
    msg.append("")
    msg.append(f"🚨 *Missing / thin description (<{DESCRIPTION_MIN_CHARS} chars)* ({len(missing_desc)})")
    msg.append(bullets(missing_desc))
    msg.append("")
    msg.append(f"🚨 *Missing estimate / story points* ({len(missing_sp)})")
    msg.append(bullets(missing_sp))
    msg.append("")
    msg.append(f"🚨 *Unassigned* ({len(unassigned)})")
    msg.append(bullets(unassigned))
    msg.append("")

    return "\n".join(msg)


def post_to_chat(text: str):
    r = requests.post(CHAT_WEBHOOK_URL, json={"text": text}, timeout=30)
    r.raise_for_status()


def main():
    jql = (
        f"project = {JIRA_PROJECT_KEY} "
        f"AND (sprint is EMPTY OR sprint NOT IN openSprints()) "
        f"AND statusCategory != Done "
        f"AND issuetype != Epic "
        f"AND issuetype != \"Sprint Meeting\" "
        f"ORDER BY updated DESC"
    )

    issues = get_all_issues(jql)
    post_to_chat(build_digest(issues))


if __name__ == "__main__":
    main()
