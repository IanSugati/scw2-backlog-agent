import os
import base64
import datetime as dt
from zoneinfo import ZoneInfo
import requests
from collections import Counter, defaultdict

# ----------------------------
# Env
# ----------------------------
JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].strip().rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"].strip()
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"].strip()

CHAT_WEBHOOK_URL = os.environ["CHAT_WEBHOOK_URL"].strip()

# SPD board + sprint (one-off test)
BOARD_ID = 5
SPRINT_ID = 418

# Custom fields (defaults based on your field dump)
# Story point estimate (used for "1 SP = 1 hour" estimate)
STORY_POINTS_FIELD = os.environ.get("JIRA_STORY_POINTS_FIELD", "customfield_10016").strip()
# Sprint Origin (dropdown: Planned / Added During Sprint)
SPRINT_ORIGIN_FIELD = os.environ.get("JIRA_SPRINT_ORIGIN_FIELD", "customfield_10104").strip()

TZ = ZoneInfo("Europe/London")


def jira_headers():
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode("utf-8")).decode("utf-8")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def post_to_chat(text: str):
    r = requests.post(CHAT_WEBHOOK_URL, json={"text": text}, timeout=30)
    r.raise_for_status()


def get_sprint(sprint_id: int) -> dict:
    url = f"{JIRA_BASE_URL}/rest/agile/1.0/sprint/{sprint_id}"
    r = requests.get(url, headers=jira_headers(), timeout=30)
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code} {r.reason}: {r.text}", response=r)
    return r.json() or {}


def parse_iso_z(s: str) -> dt.datetime | None:
    """
    Agile sprint dates come back like:
    2026-01-30T10:20:12.902Z
    """
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def format_date_local(d: dt.datetime | None) -> str:
    if not d:
        return "-"
    return d.astimezone(TZ).strftime("%a %d %b %Y")


def format_datetime_local(d: dt.datetime | None) -> str:
    if not d:
        return "-"
    return d.astimezone(TZ).strftime("%a %d %b %Y %H:%M")


def seconds_to_h_mm(seconds: int) -> str:
    seconds = int(seconds or 0)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


def sp_to_seconds(sp_value) -> int:
    """
    Your rule: 1 story point = 1 hour
    """
    try:
        if sp_value is None:
            return 0
        sp = float(sp_value)
        if sp <= 0:
            return 0
        return int(round(sp * 3600))
    except Exception:
        return 0


def get_sprint_issues(sprint_id: int):
    """
    Agile API pagination uses startAt/maxResults/total
    """
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
                "project",
                "timespent",  # kept for info, but we will use worklogs for sprint-window actuals
                STORY_POINTS_FIELD,
                SPRINT_ORIGIN_FIELD,
            ],
        }

        r = requests.get(url, headers=jira_headers(), params=params, timeout=30)
        if r.status_code >= 400:
            raise requests.HTTPError(f"{r.status_code} {r.reason}: {r.text}", response=r)

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


def fetch_worklogs_for_issue(issue_key: str):
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/worklog"
    r = requests.get(url, headers=jira_headers(), timeout=30)
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code} {r.reason}: {r.text}", response=r)
    return (r.json() or {}).get("worklogs", []) or []


def parse_worklog_started(s: str) -> dt.datetime | None:
    """
    Jira worklog.started often looks like:
    2026-02-03T12:34:56.789+0000
    """
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return dt.datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def sprint_origin_label(value) -> str:
    """
    Sprint Origin is a select field -> often returns dict like {"value":"Planned"}.
    """
    if value is None:
        return "Blank"
    if isinstance(value, dict):
        return (value.get("value") or value.get("name") or "Unknown").strip() or "Unknown"
    if isinstance(value, str):
        return value.strip() or "Blank"
    return str(value).strip() or "Unknown"


def display_name_from_worklog(wl: dict) -> str:
    author = wl.get("author") or {}
    # displayName is usually present
    dn = author.get("displayName")
    if dn:
        return str(dn).strip()
    # fallback to accountId if needed
    aid = author.get("accountId")
    return f"User {aid}" if aid else "Unknown"


def main():
    sprint = get_sprint(SPRINT_ID)

    sprint_name = (sprint.get("name") or f"Sprint {SPRINT_ID}").strip()
    sprint_state = (sprint.get("state") or "").strip()

    sprint_start = parse_iso_z(sprint.get("startDate") or "")
    sprint_end = parse_iso_z(sprint.get("endDate") or "")
    sprint_complete = parse_iso_z(sprint.get("completeDate") or "")

    if not sprint_start or not sprint_end:
        raise RuntimeError("Sprint start/end dates missing — cannot calculate sprint-window worklogs reliably.")

    sprint_start_utc = sprint_start.astimezone(dt.timezone.utc)
    sprint_end_utc = sprint_end.astimezone(dt.timezone.utc)

    issues = get_sprint_issues(SPRINT_ID)

    total_issues = len(issues)
    done = 0
    not_done = 0

    by_type = Counter()
    by_assignee = Counter()
    by_origin_count = Counter()

    # Estimates (Story points => hours)
    total_sp_seconds = 0
    by_origin_sp_seconds = defaultdict(int)

    # Actuals (worklogs within sprint window)
    total_actual_seconds = 0
    actual_by_user_seconds = defaultdict(int)
    actual_by_origin_seconds = defaultdict(int)

    # Optional: actual by type if you want later
    # actual_by_type_seconds = defaultdict(int)

    for it in issues:
        key = it.get("key")
        f = it.get("fields", {}) or {}

        # Completion
        status_cat = (((f.get("status") or {}).get("statusCategory") or {}).get("key") or "").lower()
        if status_cat == "done":
            done += 1
        else:
            not_done += 1

        # Type / assignee
        issue_type = ((f.get("issuetype") or {}).get("name") or "Unknown").strip()
        by_type[issue_type] += 1

        assignee_obj = f.get("assignee")
        assignee_name = (assignee_obj or {}).get("displayName") if isinstance(assignee_obj, dict) else None
        assignee = (assignee_name or "Unassigned").strip()
        by_assignee[assignee] += 1

        # Sprint Origin
        origin = sprint_origin_label(f.get(SPRINT_ORIGIN_FIELD))
        by_origin_count[origin] += 1

        # Story point estimate => seconds
        sp_seconds = sp_to_seconds(f.get(STORY_POINTS_FIELD))
        total_sp_seconds += sp_seconds
        by_origin_sp_seconds[origin] += sp_seconds

        # Worklogs for this issue (filter within sprint window)
        # NOTE: this can be a few dozen HTTP calls; fine for a one-off sprint digest
        actual_issue_seconds = 0
        worklogs = fetch_worklogs_for_issue(key)
        for wl in worklogs:
            started_dt = parse_worklog_started(wl.get("started") or "")
            if not started_dt:
                continue

            started_utc = started_dt.astimezone(dt.timezone.utc)
            if not (sprint_start_utc <= started_utc < sprint_end_utc):
                continue

            secs = int(wl.get("timeSpentSeconds") or 0)
            if secs <= 0:
                continue

            actual_issue_seconds += secs

            user = display_name_from_worklog(wl)
            actual_by_user_seconds[user] += secs

        if actual_issue_seconds:
            total_actual_seconds += actual_issue_seconds
            actual_by_origin_seconds[origin] += actual_issue_seconds
            # actual_by_type_seconds[issue_type] += actual_issue_seconds

    done_pct = (done / total_issues * 100.0) if total_issues else 0.0

    # Sort breakdowns
    by_type_sorted = sorted(by_type.items(), key=lambda x: (-x[1], x[0].lower()))
    by_assignee_sorted = sorted(by_assignee.items(), key=lambda x: (-x[1], x[0].lower()))
    by_user_time_sorted = sorted(actual_by_user_seconds.items(), key=lambda x: (-x[1], x[0].lower()))
    by_origin_sorted = sorted(by_origin_count.items(), key=lambda x: (-x[1], x[0].lower()))

    # Variance
    variance_seconds = total_actual_seconds - total_sp_seconds

    # Build message
    msg = []
    msg.append("📊 *SPD Sprint Digest (One-off)*")
    msg.append(f"*Sprint:* {sprint_name} *(state: {sprint_state})*")
    msg.append(f"*Dates:* {format_datetime_local(sprint_start)} → {format_datetime_local(sprint_end)}")
    if sprint_complete:
        msg.append(f"*Completed:* {format_datetime_local(sprint_complete)}")
    msg.append("")

    msg.append(f"🎯 *Total Issues:* {total_issues}")
    msg.append(f"✅ *Completed:* {done} ({done_pct:.0f}%)")
    msg.append(f"⏳ *Not Completed:* {not_done}")
    msg.append("")

    msg.append("🧩 *By Issue Type*")
    if not by_type_sorted:
        msg.append("• None")
    else:
        for name, count in by_type_sorted:
            msg.append(f"• {name}: {count}")
    msg.append("")

    msg.append("👥 *By Assignee*")
    if not by_assignee_sorted:
        msg.append("• None")
    else:
        for name, count in by_assignee_sorted:
            msg.append(f"• {name}: {count}")
    msg.append("")

    msg.append("🧭 *By Sprint Origin*")
    msg.append("(Counts + actual time logged within sprint + SP estimate where 1 SP = 1 hour)")
    if not by_origin_sorted:
        msg.append("• None")
    else:
        for origin, count in by_origin_sorted:
            o_actual = actual_by_origin_seconds.get(origin, 0)
            o_est = by_origin_sp_seconds.get(origin, 0)
            msg.append(
                f"• {origin}: {count} — actual *{seconds_to_h_mm(o_actual)}* — est *{seconds_to_h_mm(o_est)}*"
            )
    msg.append("")

    msg.append("⏱ *Time Logged (within sprint dates)*")
    msg.append(f"• Total actual: *{seconds_to_h_mm(total_actual_seconds)}*")
    msg.append(f"• Total estimate (Story points): *{seconds_to_h_mm(total_sp_seconds)}*")
    sign = "+" if variance_seconds >= 0 else "-"
    msg.append(f"• Variance (actual - est): *{sign}{seconds_to_h_mm(abs(variance_seconds))}*")
    msg.append("")

    msg.append("👤 *Actual time by user (within sprint dates)*")
    if not by_user_time_sorted:
        msg.append("• No worklogs found in sprint window")
    else:
        for user, secs in by_user_time_sorted:
            msg.append(f"• {user}: *{seconds_to_h_mm(secs)}*")

    post_to_chat("\n".join(msg).strip())


if __name__ == "__main__":
    main()
