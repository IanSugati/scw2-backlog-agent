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
JIRA_PROJECT_KEY = os.environ["JIRA_PROJECT_KEY_TICKETS"].strip()
TICKETS_CHAT_WEBHOOK_URL = os.environ["TICKETS_CHAT_WEBHOOK_URL"].strip()

LIST_LIMIT = int(os.environ.get("TIMELOG_LIST_LIMIT", "50"))  # max tickets shown overall (across all days)


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
    r = requests.post(TICKETS_CHAT_WEBHOOK_URL, json={"text": text}, timeout=30)
    r.raise_for_status()


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
        "fields": ["summary", "status"],
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


def parse_jira_datetime(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")
    except Exception:
        return None


def format_seconds(total_seconds: int) -> str:
    """
    Display as H:MM (hours and minutes)
    """
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


def main():
    tz = ZoneInfo("Europe/London")
    now_utc = dt.datetime.now(dt.timezone.utc)
    week_start_utc, week_end_utc, start_date, end_date, week_start_local, week_end_local = week_window_london(now_utc)

    # Find issues that have worklogs within the week (date based)
    jql = (
        f'project = {JIRA_PROJECT_KEY} '
        f'AND worklogDate >= "{start_date}" '
        f'AND worklogDate < "{end_date}" '
        f'ORDER BY updated DESC'
    )

    issues = get_all_issues(jql)

    if not issues:
        post_to_chat(
            "🕒 *SSH Weekly Worklog Digest*\n"
            f"Week: {week_start_local:%a %d %b} → {week_end_local:%a %d %b} (Mon→Mon)\n\n"
            "• No work logged this week 🎉"
        )
        return

    # day_key -> {"total": seconds, "items": [(ticket_seconds, key, summary, status)]}
    by_day = defaultdict(lambda: {"total": 0, "items": []})
    week_total_seconds = 0

    # We'll also keep an overall ticket tally (in case you want it later)
    ticket_totals = defaultdict(int)  # key -> total seconds across week
    ticket_meta = {}  # key -> (summary, status)

    for it in issues:
        key = it["key"]
        f = it.get("fields", {}) or {}
        summary = (f.get("summary") or "").strip()
        status = (f.get("status") or {}).get("name") or ""

        ticket_meta[key] = (summary, status)

        worklogs = fetch_worklogs_for_issue(key)

        # Sum per day for this ticket
        per_day_seconds = defaultdict(int)

        for wl in worklogs:
            started_raw = wl.get("started")
            started_dt = parse_jira_datetime(started_raw)
            if not started_dt:
                continue

            # Convert to UTC for window check, and to London for grouping-by-day
            started_utc = started_dt.astimezone(dt.timezone.utc)
            if not (week_start_utc <= started_utc < week_end_utc):
                continue

            secs = int(wl.get("timeSpentSeconds") or 0)
            if secs <= 0:
                continue

            started_local = started_dt.astimezone(tz)
            day_key = started_local.date().isoformat()  # YYYY-MM-DD
            per_day_seconds[day_key] += secs

        # Push into overall structures
        if per_day_seconds:
            ticket_week_seconds = sum(per_day_seconds.values())
            ticket_totals[key] += ticket_week_seconds
            week_total_seconds += ticket_week_seconds

            for day_key, secs in per_day_seconds.items():
                by_day[day_key]["total"] += secs
                by_day[day_key]["items"].append((secs, key, summary, status))

    # Sort days ascending
    sorted_days = sorted(by_day.keys())

    # Build message
    msg = []
    msg.append("🕒 *SSH Weekly Worklog Digest*")
    msg.append(f"Week: {week_start_local:%a %d %b} → {week_end_local:%a %d %b} (Mon→Mon)")
    msg.append(f"Total logged this week: *{format_seconds(week_total_seconds)}*")
    msg.append("")

    # Limit overall tickets shown (across all days), but still show per-day totals
    shown = 0

    for day_key in sorted_days:
        day_date = dt.date.fromisoformat(day_key)
        day_total = by_day[day_key]["total"]
        items = by_day[day_key]["items"]

        # Sort tickets within the day by time desc
        items.sort(reverse=True, key=lambda x: x[0])

        msg.append(f"📅 *{day_date:%a %d %b}* — *{format_seconds(day_total)}*")
        for secs, key, summary, status in items:
            if shown >= LIST_LIMIT:
                continue
            msg.append(
                f"• {key} – {summary} *(Status: {status})* — *{format_seconds(secs)}* ({jira_issue_browse_url(key)})"
            )
            shown += 1

        # If we hit the limit, add a friendly note once
        if shown >= LIST_LIMIT:
            msg.append(f"• +more tickets not shown (increase TIMELOG_LIST_LIMIT if needed)")
            msg.append("")
            break

        msg.append("")

    post_to_chat("\n".join(msg).strip())


if __name__ == "__main__":
    main()
