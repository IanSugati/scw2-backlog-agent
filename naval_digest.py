import os
import base64
import datetime as dt
from zoneinfo import ZoneInfo
import requests
from collections import defaultdict

# ----------------------------
# Env
# ----------------------------
JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].strip().rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"].strip()
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"].strip()

# Naval-specific webhook
CHAT_WEBHOOK_URL = os.environ["NAVAL_TICKETS_CHAT_WEBHOOK_URL"].strip()

# Naval Jira accountId
NAVAL_ACCOUNT_ID = "5b45c29d20d02f2c16bcc37e"

LIST_LIMIT_ASSIGNED = int(os.environ.get("NAVAL_ASSIGNED_LIMIT", "25"))
LIST_LIMIT_TIMELOG = int(os.environ.get("NAVAL_TIMELOG_LIMIT", "75"))


def jira_headers():
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode("utf-8")).decode("utf-8")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def jira_issue_browse_url(key: str) -> str:
    return f"{JIRA_BASE_URL}/browse/{key}"


def post_to_chat(text: str):
    r = requests.post(CHAT_WEBHOOK_URL, json={"text": text}, timeout=30)
    r.raise_for_status()


def parse_jira_datetime(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        # Example: 2026-02-17T10:30:00.000+0000
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")
    except Exception:
        return None


def format_seconds(total_seconds: int) -> str:
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


def week_window_london(now_utc: dt.datetime):
    """
    Monday 00:00 (Europe/London) -> next Monday 00:00
    Returns:
      week_start_utc, week_end_utc, start_date_str, end_date_str, week_start_local, week_end_local
    """
    tz = ZoneInfo("Europe/London")
    now_local = now_utc.astimezone(tz)

    days_since_monday = now_local.weekday()  # Mon=0
    week_start_local = (now_local - dt.timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_end_local = week_start_local + dt.timedelta(days=7)

    week_start_date_str = week_start_local.date().isoformat()
    week_end_date_str = week_end_local.date().isoformat()

    week_start_utc = week_start_local.astimezone(dt.timezone.utc)
    week_end_utc = week_end_local.astimezone(dt.timezone.utc)

    return week_start_utc, week_end_utc, week_start_date_str, week_end_date_str, week_start_local, week_end_local


def jql_search(jql: str, next_page_token: str | None = None, max_results: int = 100):
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    params = {
        "jql": jql,
        "maxResults": max_results,
        "fields": ["summary", "status", "priority", "updated", "project"],
    }
    if next_page_token:
        params["nextPageToken"] = next_page_token

    r = requests.get(url, headers=jira_headers(), params=params, timeout=30)
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code} {r.reason}: {r.text}", response=r)
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


def fetch_worklogs_for_issue(issue_key: str):
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/worklog"
    r = requests.get(url, headers=jira_headers(), timeout=30)
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code} {r.reason}: {r.text}", response=r)
    return (r.json() or {}).get("worklogs", []) or []


def bullets(rows, limit: int):
    if not rows:
        return "• None 🎉"
    lines = []
    for k, summary, status, project_key, extra in rows[:limit]:
        suffix = f" — {extra}" if extra else ""
        lines.append(
            f"• {k} – {summary} *(Status: {status}, Project: {project_key})*{suffix} ({jira_issue_browse_url(k)})"
        )
    if len(rows) > limit:
        lines.append(f"• +{len(rows) - limit} more…")
    return "\n".join(lines)


def build_assigned_section_all_jira():
    # ✅ ALL PROJECTS: remove "project = X"
    jql = (
        f"assignee = {NAVAL_ACCOUNT_ID} "
        f"AND statusCategory != Done "
        f"ORDER BY priority DESC, updated DESC"
    )

    issues = get_all_issues(jql)

    rows = []
    for it in issues:
        key = it["key"]
        f = it.get("fields", {}) or {}
        summary = (f.get("summary") or "").strip()
        status = (f.get("status") or {}).get("name") or ""
        priority = (f.get("priority") or {}).get("name") or ""
        project_key = (f.get("project") or {}).get("key") or "?"
        extra = f"(Priority: {priority})" if priority else None
        rows.append((key, summary, status, project_key, extra))

    return rows


def build_timelog_section_all_jira():
    tz = ZoneInfo("Europe/London")
    now_utc = dt.datetime.now(dt.timezone.utc)
    week_start_utc, week_end_utc, start_date, end_date, week_start_local, week_end_local = week_window_london(now_utc)

    # ✅ ALL PROJECTS: remove "project = X"
    jql = (
        f'worklogAuthor = {NAVAL_ACCOUNT_ID} '
        f'AND worklogDate >= "{start_date}" '
        f'AND worklogDate < "{end_date}" '
        f'ORDER BY updated DESC'
    )

    issues = get_all_issues(jql)

    by_day = defaultdict(lambda: {"total": 0, "items": []})
    week_total = 0

    for it in issues:
        key = it["key"]
        f = it.get("fields", {}) or {}
        summary = (f.get("summary") or "").strip()
        status = (f.get("status") or {}).get("name") or ""
        project_key = (f.get("project") or {}).get("key") or "?"

        worklogs = fetch_worklogs_for_issue(key)
        per_day_seconds = defaultdict(int)

        for wl in worklogs:
            author = (wl.get("author") or {}).get("accountId")
            if author != NAVAL_ACCOUNT_ID:
                continue

            started_dt = parse_jira_datetime(wl.get("started"))
            if not started_dt:
                continue

            started_utc = started_dt.astimezone(dt.timezone.utc)
            if not (week_start_utc <= started_utc < week_end_utc):
                continue

            secs = int(wl.get("timeSpentSeconds") or 0)
            if secs <= 0:
                continue

            started_local = started_dt.astimezone(tz)
            day_key = started_local.date().isoformat()
            per_day_seconds[day_key] += secs

        if per_day_seconds:
            for day_key, secs in per_day_seconds.items():
                by_day[day_key]["total"] += secs
                by_day[day_key]["items"].append((secs, key, summary, status, project_key))
            week_total += sum(per_day_seconds.values())

    # sort each day's items by time desc
    for day_key in by_day:
        by_day[day_key]["items"].sort(reverse=True, key=lambda x: x[0])

    return by_day, week_total, week_start_local, week_end_local


def main():
    assigned_rows = build_assigned_section_all_jira()

    by_day, week_total, week_start_local, week_end_local = build_timelog_section_all_jira()

    msg = []
    msg.append("🧑‍💻 *Naval – Personal Digest (All Jira)*")
    msg.append("")

    msg.append(f"📌 *Assigned to Naval (Open)* ({len(assigned_rows)})")
    msg.append(bullets(assigned_rows, limit=LIST_LIMIT_ASSIGNED))
    msg.append("")

    msg.append("🕒 *Time logged by Naval (this week)*")
    msg.append(f"Week: {week_start_local:%a %d %b} → {week_end_local:%a %d %b} (Mon→Mon)")
    msg.append(f"Total logged this week: *{format_seconds(week_total)}*")
    msg.append("")

    if not by_day:
        msg.append("• No time logged this week 🎉")
        post_to_chat("\n".join(msg))
        return

    shown = 0
    for day_key in sorted(by_day.keys()):
        day_date = dt.date.fromisoformat(day_key)
        day_total = by_day[day_key]["total"]
        items = by_day[day_key]["items"]

        msg.append(f"📅 *{day_date:%a %d %b}* — *{format_seconds(day_total)}*")
        for secs, key, summary, status, project_key in items:
            if shown >= LIST_LIMIT_TIMELOG:
                continue
            msg.append(
                f"• {key} – {summary} *(Status: {status}, Project: {project_key})* — *{format_seconds(secs)}* ({jira_issue_browse_url(key)})"
            )
            shown += 1

        if shown >= LIST_LIMIT_TIMELOG:
            msg.append("• +more tickets not shown (increase NAVAL_TIMELOG_LIMIT if needed)")
            msg.append("")
            break

        msg.append("")

    post_to_chat("\n".join(msg).strip())


if __name__ == "__main__":
    main()
