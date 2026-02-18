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

CHAT_WEBHOOK_URL = os.environ["MELVIN_TIMELOG_WEBHOOK"].strip()

# Melvin Jira accountId
MELVIN_ACCOUNT_ID = "712020:9fe586ab-fc99-4260-8114-f55d35bcb262"

LIST_LIMIT_TIMELOG = int(os.environ.get("MELVIN_TIMELOG_LIMIT", "75"))
LIST_LIMIT_DUE_PER_DAY = int(os.environ.get("MELVIN_DUE_PER_DAY_LIMIT", "15"))
LIST_LIMIT_OTHER_NOT_THIS_WEEK = int(os.environ.get("MELVIN_OTHER_NOT_THIS_WEEK_LIMIT", "25"))


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
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")
    except Exception:
        return None


def format_seconds(total_seconds: int) -> str:
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


def is_weekday(d: dt.date) -> bool:
    return d.weekday() < 5  # Mon-Fri


def week_window_london(now_utc: dt.datetime):
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

    return week_start_utc, week_end_utc, week_start_date_str, week_end_date_str, week_start_local, week_end_local, tz


def jql_search(jql: str, next_page_token: str | None = None, max_results: int = 100):
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    params = {
        "jql": jql,
        "maxResults": max_results,
        "fields": ["summary", "status", "updated", "project", "duedate"],
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


def safe_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v)


def bullets(rows, limit: int):
    if not rows:
        return "• None ✅"

    lines = []
    for row in rows[:limit]:
        k, summary, status, project_key, extra = row
        suffix = f" — {extra}" if extra else ""
        lines.append(
            f"• {k} – {summary} *(Status: {status}, Project: {project_key})*{suffix} ({jira_issue_browse_url(k)})"
        )

    if len(rows) > limit:
        lines.append(f"• +{len(rows) - limit} more…")

    return "\n".join(lines)


def main():
    now_utc = dt.datetime.now(dt.timezone.utc)
    week_start_utc, week_end_utc, start_date, end_date, week_start_local, week_end_local, tz = week_window_london(now_utc)
    today_local = now_utc.astimezone(tz).date()

    # A) Assigned open issues (all projects)
    jql_assigned_open = (
        f"assignee = {MELVIN_ACCOUNT_ID} "
        f"AND statusCategory != Done "
        f"ORDER BY updated DESC"
    )
    assigned_open_issues = get_all_issues(jql_assigned_open)

    assigned_meta = {}
    for it in assigned_open_issues:
        key = it["key"]
        f = it.get("fields", {}) or {}
        assigned_meta[key] = {
            "summary": safe_str(f.get("summary")),
            "status": safe_str((f.get("status") or {}).get("name")),
            "project": safe_str((f.get("project") or {}).get("key")),
            "duedate": f.get("duedate"),
        }

    # B) Work logged this week (Mon->Mon)
    jql_worked_this_week = (
        f'worklogAuthor = {MELVIN_ACCOUNT_ID} '
        f'AND worklogDate >= "{start_date}" '
        f'AND worklogDate < "{end_date}" '
        f"ORDER BY updated DESC"
    )
    worked_issues = get_all_issues(jql_worked_this_week)

    by_day = defaultdict(lambda: {"total": 0, "items": []})
    weekend_total = 0
    naval_ticket_day_seconds = defaultdict(lambda: defaultdict(int))  # (kept name; harmless)

    worked_meta = {}

    def ensure_meta(issue_obj):
        key = issue_obj["key"]
        f = issue_obj.get("fields", {}) or {}
        if key not in worked_meta:
            worked_meta[key] = {
                "summary": safe_str(f.get("summary")),
                "status": safe_str((f.get("status") or {}).get("name")),
                "project": safe_str((f.get("project") or {}).get("key")),
            }

    for it in worked_issues:
        ensure_meta(it)
        key = it["key"]

        worklogs = fetch_worklogs_for_issue(key)
        per_day_seconds = defaultdict(int)

        for wl in worklogs:
            author = (wl.get("author") or {}).get("accountId")
            if author != MELVIN_ACCOUNT_ID:
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
            m = worked_meta[key]
            for day_key, secs in per_day_seconds.items():
                d_local = dt.date.fromisoformat(day_key)
                naval_ticket_day_seconds[key][day_key] += secs

                if is_weekday(d_local):
                    by_day[day_key]["total"] += secs
                    by_day[day_key]["items"].append((secs, key, m["summary"], m["status"], m["project"]))
                else:
                    weekend_total += secs

    for day_key in by_day:
        by_day[day_key]["items"].sort(reverse=True, key=lambda x: x[0])

    weekday_total = sum(by_day[d]["total"] for d in by_day)

    # C) Remaining weekdays (tomorrow -> end of week), due date + no time that day
    remaining_days = []
    d = today_local + dt.timedelta(days=1)
    while d < week_end_local.date():
        if is_weekday(d):
            remaining_days.append(d)
        d += dt.timedelta(days=1)

    due_day_no_time = defaultdict(list)
    for key, m in assigned_meta.items():
        due_str = m.get("duedate")
        if not due_str:
            continue

        try:
            due_date = dt.date.fromisoformat(due_str)
        except Exception:
            continue

        if due_date not in remaining_days:
            continue

        day_key = due_date.isoformat()
        secs_that_day = naval_ticket_day_seconds.get(key, {}).get(day_key, 0)
        if secs_that_day == 0:
            due_day_no_time[due_date].append((key, m["summary"], m["status"], m["project"], "(No time logged that day)"))

    for due_date in due_day_no_time:
        due_day_no_time[due_date].sort(key=lambda r: r[0])

    # D) Other open tasks not due this week (or no due date)
    week_start_date = week_start_local.date()
    week_end_date_exclusive = week_end_local.date()

    other_not_this_week = []
    for key, m in assigned_meta.items():
        due_str = m.get("duedate")

        if not due_str:
            other_not_this_week.append((key, m["summary"], m["status"], m["project"], "(No due date)"))
            continue

        try:
            due_date = dt.date.fromisoformat(due_str)
        except Exception:
            other_not_this_week.append((key, m["summary"], m["status"], m["project"], "(Bad due date)"))
            continue

        if not (week_start_date <= due_date < week_end_date_exclusive):
            other_not_this_week.append((key, m["summary"], m["status"], m["project"], f"(Due: {due_date:%a %d %b})"))

    other_not_this_week.sort(key=lambda r: r[0])

    # Message
    msg = []
    msg.append("🧑‍💻 *Melvin – Personal Digest (All Jira)*")
    msg.append("")

    msg.append("🕒 *Time logged by Melvin (this week)*")
    msg.append(f"Week: {week_start_local:%a %d %b} → {week_end_local:%a %d %b} (Mon→Mon)")
    msg.append(f"Total logged this week (Mon–Fri): *{format_seconds(weekday_total)}*")
    if weekend_total > 0:
        msg.append(f"Weekend time logged (Sat/Sun): *{format_seconds(weekend_total)}* (not shown day-by-day)")
    msg.append("")

    if not by_day:
        msg.append("• No time logged this week (Mon–Fri) ✅")
        msg.append("")
    else:
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
                msg.append("• +more tickets not shown (increase MELVIN_TIMELOG_LIMIT if needed)")
                msg.append("")
                break

            msg.append("")

    msg.append("📆 *Remaining days this week — due-date tasks with no time logged that day*")
    msg.append("(Weekdays only; uses *Due date* as “assigned for the day”)")
    msg.append("")

    if not remaining_days:
        msg.append("• No remaining weekdays in this week ✅")
        msg.append("")
    else:
        for due_date in remaining_days:
            items = due_day_no_time.get(due_date, [])
            msg.append(f"📅 *{due_date:%a %d %b}* ({len(items)})")
            if not items:
                msg.append("• None ✅")
            else:
                msg.append(bullets(items, limit=LIST_LIMIT_DUE_PER_DAY))
            msg.append("")

    msg.append("📦 *Other open tasks assigned to Melvin (not due this week)*")
    msg.append("(Due date outside this Mon→Mon week, or blank)")
    msg.append("")
    msg.append(bullets(other_not_this_week, limit=LIST_LIMIT_OTHER_NOT_THIS_WEEK))

    post_to_chat("\n".join(msg).strip())


if __name__ == "__main__":
    main()
