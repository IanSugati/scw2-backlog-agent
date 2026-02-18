import os
import base64
import datetime as dt
from zoneinfo import ZoneInfo
import requests
from collections import Counter

# ----------------------------
# Env
# ----------------------------
JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].strip().rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"].strip()
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"].strip()

CHAT_WEBHOOK_URL = os.environ["CHAT_WEBHOOK_URL"].strip()

BOARD_ID = 5
SPRINT_ID = 418

TZ = ZoneInfo("Europe/London")


def jira_headers():
    token = base64.b64encode(
        f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode("utf-8")
    ).decode("utf-8")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def post_to_chat(text: str):
    r = requests.post(CHAT_WEBHOOK_URL, json={"text": text}, timeout=30)
    r.raise_for_status()


def jira_issue_browse_url(key: str) -> str:
    return f"{JIRA_BASE_URL}/browse/{key}"


def get_sprint(sprint_id: int) -> dict:
    url = f"{JIRA_BASE_URL}/rest/agile/1.0/sprint/{sprint_id}"
    r = requests.get(url, headers=jira_headers(), timeout=30)
    if r.status_code >= 400:
        raise requests.HTTPError(
            f"{r.status_code} {r.reason}: {r.text}", response=r
        )
    return r.json() or {}


def parse_iso_z(s: str):
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def format_dt_local(d):
    if not d:
        return "-"
    return d.astimezone(TZ).strftime("%a %d %b %Y")


def seconds_to_h_mm(seconds: int) -> str:
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


def get_sprint_issues(sprint_id: int):
    issues = []
    start_at = 0

    while True:
        url = f"{JIRA_BASE_URL}/rest/agile/1.0/sprint/{sprint_id}/issue"
        params = {
            "startAt": start_at,
            "maxResults": 100,
            "fields": [
                "summary",
                "status",
                "issuetype",
                "assignee",
                "timespent",
                "timeoriginalestimate",
            ],
        }

        r = requests.get(url, headers=jira_headers(), params=params, timeout=30)

        if r.status_code >= 400:
            raise requests.HTTPError(
                f"{r.status_code} {r.reason}: {r.text}", response=r
            )

        data = r.json() or {}
        batch = data.get("issues", []) or []
        issues.extend(batch)

        if len(batch) == 0:
            break

        start_at += len(batch)

        total = data.get("total")
        if isinstance(total, int) and start_at >= total:
            break

    return issues


def main():
    sprint = get_sprint(SPRINT_ID)

    sprint_name = (sprint.get("name") or f"Sprint {SPRINT_ID}").strip()
    sprint_state = (sprint.get("state") or "").strip()

    start_dt = parse_iso_z(sprint.get("startDate"))
    end_dt = parse_iso_z(sprint.get("endDate"))
    complete_dt = parse_iso_z(sprint.get("completeDate"))

    issues = get_sprint_issues(SPRINT_ID)

    total = len(issues)
    done = 0
    not_done = 0

    by_type = Counter()
    by_assignee = Counter()

    total_timespent = 0
    total_original = 0
    has_time_data = False

    for it in issues:
        key = it.get("key")
        f = it.get("fields", {}) or {}

        status_cat = (
            ((f.get("status") or {}).get("statusCategory") or {}).get("key") or ""
        ).lower()

        if status_cat == "done":
            done += 1
        else:
            not_done += 1

        issue_type = ((f.get("issuetype") or {}).get("name") or "Unknown").strip()
        by_type[issue_type] += 1

        assignee_obj = f.get("assignee")
        assignee = (
            (assignee_obj or {}).get("displayName")
            if isinstance(assignee_obj, dict)
            else None
        ) or "Unassigned"

        by_assignee[assignee] += 1

        ts = f.get("timespent")
        oe = f.get("timeoriginalestimate")

        if isinstance(ts, int):
            total_timespent += ts
            has_time_data = True

        if isinstance(oe, int):
            total_original += oe
            has_time_data = True

    done_pct = (done / total * 100) if total else 0

    msg = []
    msg.append("📊 *SPD Sprint Digest (One-off)*")
    msg.append(f"*Sprint:* {sprint_name} *(state: {sprint_state})*")
    msg.append(f"*Dates:* {format_dt_local(start_dt)} → {format_dt_local(end_dt)}")

    if complete_dt:
        msg.append(f"*Completed:* {format_dt_local(complete_dt)}")

    msg.append("")
    msg.append(f"🎯 *Total Issues:* {total}")
    msg.append(f"✅ *Completed:* {done} ({done_pct:.0f}%)")
    msg.append(f"⏳ *Not Completed:* {not_done}")
    msg.append("")

    msg.append("🧩 *By Issue Type*")
    for name, count in sorted(by_type.items(), key=lambda x: (-x[1], x[0])):
        msg.append(f"• {name}: {count}")

    msg.append("")
    msg.append("👥 *By Assignee*")
    for name, count in sorted(by_assignee.items(), key=lambda x: (-x[1], x[0])):
        msg.append(f"• {name}: {count}")

    msg.append("")

    msg.append("⏱ *Time Tracking Totals*")
    if has_time_data:
        msg.append(f"• Time Spent: *{seconds_to_h_mm(total_timespent)}*")
        msg.append(f"• Original Estimate: *{seconds_to_h_mm(total_original)}*")
    else:
        msg.append("• No Jira time tracking data available 🙂")

    post_to_chat("\n".join(msg).strip())


if __name__ == "__main__":
    main()
