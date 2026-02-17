import os
import base64
import datetime as dt
from zoneinfo import ZoneInfo
import requests

# ----------------------------
# Env
# ----------------------------
JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].strip().rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"].strip()
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"].strip()
JIRA_PROJECT_KEY = os.environ["JIRA_PROJECT_KEY_TICKETS"].strip()
TICKETS_CHAT_WEBHOOK_URL = os.environ["TICKETS_CHAT_WEBHOOK_URL"].strip()

LIST_LIMIT = int(os.environ.get("TIMELOG_LIST_LIMIT", "30"))  # how many tickets to show


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


def start_of_week_london(now_utc: dt.datetime):
    """
    Return (week_start_utc, week_end_utc) for Monday->Monday,
    where 'Monday 00:00' is defined in Europe/London local time.
    """
    tz = ZoneInfo("Europe/London")
    now_local = now_utc.astimezone(tz)

    # Monday = 0 ... Sunday = 6
    days_since_monday = now_local.weekday()
    week_start_local = (now_local - dt.timedelta(days=days_since_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    week_end_local = week_start_local + dt.timedelta(days=7)

    week_start_utc = week_start_local.astimezone(dt.timezone.utc)
    week_end_utc = week_end_local.astimezone(dt.timezone.utc)
    return week_start_utc, week_end_utc


def jql_search(jql: str, next_page_token: str | None = None, max_results: int = 100):
    """
    Jira Cloud: /rest/api/3/search/jql uses nextPageToken + isLast
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
    params = {
        "jql": jql,
        "maxResults": max_results,
        "fields": ["summary", "status"],
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


def fetch_worklogs_for_issue(issue_key: str):
    """
    Fetch all worklogs for an issue.
    Note: Jira returns worklog.started as e.g. "2026-02-17T10:30:00.000+0000"
    """
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/worklog"
    r = requests.get(url, headers=jira_headers(), timeout=30)
    r.raise_for_status()
    return (r.json() or {}).get("worklogs", []) or []


def parse_jira_datetime(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        # Example: 2026-02-17T10:30:00.000+0000
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone(dt.timezone.utc)
    except Exception:
        return None


def format_seconds(total_seconds: int) -> str:
    # show as H:MM
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


def main():
    now_utc = dt.datetime.now(dt.timezone.utc)
    week_start_utc, week_end_utc = start_of_week_london(now_utc)

    # JQL finds issues with work logged in the current calendar week.
    # This is server-side filtering so we don’t loop every ticket in SSH.
    jql = (
        f"project = {JIRA_PROJECT_KEY} "
        f"AND worklogDate >= startOfWeek() "
        f"AND worklogDate < startOfWeek(+7d) "
        f"ORDER BY updated DESC"
    )

    issues = get_all_issues(jql)

    if not issues:
        post_to_chat("🕒 *SSH Weekly Worklog Digest*\n\n• No work logged this week 🎉")
        return

    rows = []
    for it in issues:
        key = it["key"]
        f = it.get("fields", {}) or {}
        summary = (f.get("summary") or "").strip()
        status = (f.get("status") or {}).get("name") or ""

        total_seconds = 0
        worklogs = fetch_worklogs_for_issue(key)

        for wl in worklogs:
            started = parse_jira_datetime(wl.get("started"))
            if not started:
                continue
            if week_start_utc <= started < week_end_utc:
                total_seconds += int(wl.get("timeSpentSeconds") or 0)

        # Worklog endpoint can include older logs too; if Jira/JQL returned it, we *should* have some,
        # but keep defensive:
        if total_seconds > 0:
            rows.append((total_seconds, key, summary, status))

    # Sort by most time logged
    rows.sort(reverse=True, key=lambda x: x[0])

    # Build message
    tz = ZoneInfo("Europe/London")
    week_start_local = week_start_utc.astimezone(tz)
    week_end_local = week_end_utc.astimezone(tz)

    msg = []
    msg.append("🕒 *SSH Weekly Worklog Digest*")
    msg.append(f"Week: {week_start_local:%a %d %b} → {week_end_local:%a %d %b} (Mon→Mon)")
    msg.append("")

    if not rows:
        msg.append("• No work logged this week 🎉")
        post_to_chat("\n".join(msg))
        return

    limit = min(LIST_LIMIT, len(rows))
    for total_seconds, key, summary, status in rows[:limit]:
        msg.append(f"• {key} – {summary} *(Status: {status})* — *{format_seconds(total_seconds)}* ({jira_issue_browse_url(key)})")

    if len(rows) > limit:
        msg.append(f"• +{len(rows) - limit} more…")

    post_to_chat("\n".join(msg))


if __name__ == "__main__":
    main()
