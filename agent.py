import os
import base64
import datetime as dt
import requests

JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
CHAT_WEBHOOK_URL = os.environ["CHAT_WEBHOOK_URL"]
JIRA_PROJECT_KEY = os.environ["JIRA_PROJECT_KEY"]
STORY_POINTS_FIELD = os.environ["JIRA_STORY_POINTS_FIELD"]

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

def jql_search(jql: str, start_at: int = 0, max_results: int = 100):
    url = f"{JIRA_BASE_URL}/rest/api/3/search"
    payload = {
        "jql": jql,
        "startAt": start_at,
        "maxResults": max_results,
        "fields": ["summary", "description", "assignee", "updated", STORY_POINTS_FIELD],
    }
    r = requests.post(url, headers=jira_headers(), json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def get_all_issues(jql: str):
    issues = []
    start_at = 0
    while True:
        data = jql_search(jql, start_at=start_at)
        batch = data.get("issues", [])
        issues.extend(batch)
        start_at += len(batch)
        if start_at >= data.get("total", 0) or len(batch) == 0:
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

def build_digest(issues):
    missing_desc, missing_sp, unassigned, oversized, aging = [], [], [], [], []

    for it in issues:
        key = it["key"]
        f = it.get("fields", {})
        summary = (f.get("summary") or "").strip()

        desc_len = safe_len_description(f.get("description"))
        sp = f.get(STORY_POINTS_FIELD)
        assignee = f.get("assignee")
        updated = f.get("updated") or ""
        age_days = days_since_iso(updated) if updated else 0

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

    def bullets(rows, limit=15):
        if not rows:
            return "• None 🎉"
        lines = []
        for row in rows[:limit]:
            key = row[0]
            lines.append(f"• {key} – {row[1]} ({jira_issue_browse_url(key)})")
        if len(rows) > limit:
            lines.append(f"• +{len(rows)-limit} more…")
        return "\n".join(lines)

    msg = []
    msg.append("📚 *SCW2 Backlog Health Check*")
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
    msg.append(f"⚠ *Oversized (≥ {OVERSIZED_SP} SP ≈ 4+ days)* ({len(oversized)})")
    if oversized:
        lines = []
        for key, summary, sp in oversized[:15]:
            lines.append(f"• {key} – {summary} (SP: {sp}) ({jira_issue_browse_url(key)})")
        if len(oversized) > 15:
            lines.append(f"• +{len(oversized)-15} more…")
        msg.append("\n".join(lines))
    else:
        msg.append("• None 🎉")
    msg.append("")
    msg.append(f"🧟 *Aging (no updates ≥ {AGING_DAYS} days)* ({len(aging)})")
    if aging:
        lines = []
        for key, summary, d in aging[:15]:
            lines.append(f"• {key} – {summary} (last update: {d}d) ({jira_issue_browse_url(key)})")
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
    jql = f"project = {JIRA_PROJECT_KEY} AND sprint NOT IN openSprints() ORDER BY updated DESC"
    issues = get_all_issues(jql)
    post_to_chat(build_digest(issues))

if __name__ == "__main__":
    main()
