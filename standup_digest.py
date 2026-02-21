# standup_digest.py
#
# Secrets / env vars (EXACT):
#   JIRA_BASE_URL
#   JIRA_EMAIL
#   JIRA_API_TOKEN
#   STAND_UP
#
# Optional:
#   ENFORCE_9AM_LONDON=true|false  (default false)
#   JIRA_START_DATE_FIELD=customfield_XXXXX   (optional; if not set we’ll use due date only)
#   UPCOMING_DAYS=2                           (default 2)

import os
import sys
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

# ---- Config ----
DEV_ACCOUNT_ID = "5be5be3875085254a6a76016"
DEV_NAME = "Andy Edmonds"
PROJECT_KEY = "SPD"
QA_STATUS = "DEPLOYED TO QA"
# ---------------

LONDON = ZoneInfo("Europe/London")


def req_env(name: str) -> str:
    v = os.environ.get(name)
    if v is None or v == "":
        raise RuntimeError(f"Missing env var: {name}")
    return v


def present(name: str) -> str:
    v = os.environ.get(name)
    return "SET" if (v is not None and v != "") else "MISSING"


def enforce_9am_london() -> bool:
    raw = os.environ.get("ENFORCE_9AM_LONDON", "false").strip().lower()
    return raw in ("1", "true", "yes", "y", "on")


def should_run_now() -> bool:
    if not enforce_9am_london():
        return True
    now = datetime.now(LONDON)
    return now.weekday() < 5 and now.hour == 9


def _raise(r: requests.Response):
    if r.ok:
        return
    raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url} :: {(r.text or '')[:800]}")


def api_get(auth, base_url: str, path: str):
    url = f"{base_url}{path}"
    r = requests.get(url, auth=auth, headers={"Accept": "application/json"}, timeout=30)
    _raise(r)
    return r.json()


def jira_search(auth, base_url: str, jql: str, fields=None, max_results: int = 200):
    url = f"{base_url}/rest/api/3/search/jql"
    jql_clean = " ".join(line.strip() for line in jql.splitlines() if line.strip())
    payload = {
        "jql": jql_clean,
        "maxResults": max_results,
        "fields": fields or ["summary", "status", "duedate"],
    }
    r = requests.post(
        url,
        auth=auth,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    _raise(r)
    return r.json().get("issues", [])


def issue_link(base_url: str, key: str) -> str:
    """
    Google Chat link format: <url|text>
    """
    return f"<{base_url}/browse/{key}|{key}>"


def parse_jira_dt(started: str) -> datetime:
    if len(started) >= 5 and (started[-5] in ["+", "-"]) and started[-2:].isdigit():
        started = started[:-2] + ":" + started[-2:]
    return datetime.fromisoformat(started)


def parse_jira_date(d: str) -> date | None:
    """
    Jira date fields (e.g. duedate, start date) often return 'YYYY-MM-DD'
    """
    if not d:
        return None
    try:
        return datetime.fromisoformat(d).date()
    except Exception:
        try:
            return datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            return None


def pretty_date(d: date) -> str:
    # e.g. Tue 23 Feb
    return d.strftime("%a %d %b")


def yesterday_window_london():
    now = datetime.now(LONDON)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_yesterday = start_today - timedelta(days=1)
    return start_yesterday, start_today


def get_worklogs(auth, base_url: str, issue_key: str):
    url = f"{base_url}/rest/api/3/issue/{issue_key}/worklog"
    r = requests.get(url, auth=auth, params={"maxResults": 5000}, timeout=30)
    _raise(r)
    return r.json().get("worklogs", [])


def seconds_to_pretty(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def zombie_indicator(days: int) -> str:
    zombies = min(days // 10, 10)  # cap at 100 days
    skulls = (days // 100) if days >= 100 else 0
    return ("🧟" * zombies) + ((" " + ("💀" * skulls)) if skulls else "")


def time_logged_yesterday(auth, base_url: str):
    jql = f"""
        project = {PROJECT_KEY}
        AND worklogAuthor = {DEV_ACCOUNT_ID}
        AND worklogDate >= startOfDay(-1)
        AND worklogDate < startOfDay()
        AND statusCategory != Done
        ORDER BY updated DESC
    """
    issues = jira_search(auth, base_url, jql, fields=["summary", "status"], max_results=200)

    start_y, start_t = yesterday_window_london()
    lines = []

    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        total = 0

        for wl in get_worklogs(auth, base_url, key):
            if wl.get("author", {}).get("accountId") != DEV_ACCOUNT_ID:
                continue
            started = wl.get("started")
            if not started:
                continue
            dt_local = parse_jira_dt(started).astimezone(LONDON)
            if start_y <= dt_local < start_t:
                total += int(wl.get("timeSpentSeconds", 0))

        if total:
            lines.append(f"• {issue_link(base_url, key)} – {summary} ({seconds_to_pretty(total)})")

    return lines


def pushed_to_qa_yesterday(auth, base_url: str):
    jql = f"""
        project = {PROJECT_KEY}
        AND assignee = {DEV_ACCOUNT_ID}
        AND status CHANGED TO "{QA_STATUS}"
        DURING (startOfDay(-1), startOfDay())
        AND statusCategory != Done
        ORDER BY updated DESC
    """
    issues = jira_search(auth, base_url, jql, fields=["summary"], max_results=200)
    return [f"• {issue_link(base_url, i['key'])} – {i['fields']['summary']}" for i in issues]


def overdue_all_projects(auth, base_url: str):
    jql = f"""
        assignee = {DEV_ACCOUNT_ID}
        AND duedate < startOfDay()
        AND statusCategory != Done
        ORDER BY duedate ASC
    """
    issues = jira_search(auth, base_url, jql, fields=["summary", "duedate"], max_results=200)

    today = datetime.now(LONDON).date()
    lines = []

    for i in issues:
        key = i["key"]
        summary = i["fields"]["summary"]
        due = i["fields"].get("duedate")
        if not due:
            continue
        due_date = parse_jira_date(due)
        if not due_date:
            continue
        days = (today - due_date).days
        lines.append(f"• {issue_link(base_url, key)} – {summary} ({days} days {zombie_indicator(days)})")

    return lines


def sprint_remaining(auth, base_url: str):
    jql = f"""
        project = {PROJECT_KEY}
        AND sprint in openSprints()
        AND assignee = {DEV_ACCOUNT_ID}
        AND statusCategory != Done
        ORDER BY Rank ASC
    """
    issues = jira_search(auth, base_url, jql, fields=["summary", "status"], max_results=500)

    in_progress = []
    up_next = []

    for i in issues:
        key = i["key"]
        summary = i["fields"]["summary"]
        status = i["fields"]["status"]["name"].strip().lower()
        line = f"• {issue_link(base_url, key)} – {summary}"

        if status in {"in progress", "in review", "ready for integration", "ready for package", "deployed to qa"}:
            in_progress.append(line)
        else:
            up_next.append(line)

    return in_progress[:8], up_next[:8]


def upcoming_next_days(auth, base_url: str):
    """
    Shows what's starting / due in the next N days (default 2), based on:
      - due date (system field: duedate)
      - optional start date (custom field id via JIRA_START_DATE_FIELD, e.g. customfield_10015)
    """
    upcoming_days = int(os.environ.get("UPCOMING_DAYS", "2").strip() or "2")
    start_field = os.environ.get("JIRA_START_DATE_FIELD", "").strip()

    # We’ll use startOfDay(+N+1) so “next 2 days” includes today + tomorrow (and filters cleanly).
    end_offset = upcoming_days + 1

    if start_field:
        jql = f"""
            project = {PROJECT_KEY}
            AND assignee = {DEV_ACCOUNT_ID}
            AND statusCategory != Done
            AND (
                (duedate >= startOfDay() AND duedate < startOfDay(+{end_offset}))
                OR
                ({start_field} >= startOfDay() AND {start_field} < startOfDay(+{end_offset}))
            )
            ORDER BY duedate ASC
        """
        fields = ["summary", "duedate", start_field]
    else:
        jql = f"""
            project = {PROJECT_KEY}
            AND assignee = {DEV_ACCOUNT_ID}
            AND statusCategory != Done
            AND duedate >= startOfDay()
            AND duedate < startOfDay(+{end_offset})
            ORDER BY duedate ASC
        """
        fields = ["summary", "duedate"]

    issues = jira_search(auth, base_url, jql, fields=fields, max_results=200)

    today = datetime.now(LONDON).date()
    end_day = today + timedelta(days=upcoming_days)

    lines = []
    for i in issues[:12]:
        key = i["key"]
        f = i.get("fields", {})
        summary = f.get("summary") or ""

        due_d = parse_jira_date(f.get("duedate"))
        start_d = parse_jira_date(f.get(start_field)) if start_field else None

        parts = []
        if start_d and today <= start_d <= end_day:
            parts.append(f"Start: {pretty_date(start_d)}")
        if due_d and today <= due_d <= end_day:
            parts.append(f"Due: {pretty_date(due_d)}")

        # If Jira returned something weird but issue matched JQL, still show it.
        suffix = f" ({', '.join(parts)})" if parts else ""

        lines.append(f"• {issue_link(base_url, key)} – {summary}{suffix}")

    return lines


def build_digest(auth, base_url: str):
    time_lines = time_logged_yesterday(auth, base_url)
    qa_lines = pushed_to_qa_yesterday(auth, base_url)
    in_prog, next_up = sprint_remaining(auth, base_url)
    upcoming_lines = upcoming_next_days(auth, base_url)
    overdue_lines = overdue_all_projects(auth, base_url)

    upcoming_days = int(os.environ.get("UPCOMING_DAYS", "2").strip() or "2")
    end_label = (datetime.now(LONDON).date() + timedelta(days=upcoming_days)).strftime("%a %d %b")

    msg = f"🧑‍💻 Standup Prep – {DEV_NAME}\n\n"

    msg += "⏱ Yesterday (time logged)\n"
    msg += "\n".join(time_lines) if time_lines else "• No time logged"
    msg += "\n\n"

    msg += "🚀 Pushed to QA (yesterday)\n"
    msg += "\n".join(qa_lines) if qa_lines else "• Nothing pushed to QA"
    msg += "\n\n"

    msg += f"📅 Next {upcoming_days} days (through {end_label})\n"
    msg += "\n".join(upcoming_lines) if upcoming_lines else "• Nothing scheduled (no start/due dates found)"
    msg += "\n\n"

    msg += "📌 Live Sprint – Remaining (SPD)\n\n"
    msg += "🔥 In Progress\n"
    msg += "\n".join(in_prog) if in_prog else "• None"
    msg += "\n\n"
    msg += "📋 Up Next\n"
    msg += "\n".join(next_up) if next_up else "• None"
    msg += "\n\n"

    msg += "⚠️ Overdue (all projects)\n"
    msg += "\n".join(overdue_lines) if overdue_lines else "• No overdue items"

    return msg


def send_chat(webhook_url: str, text: str):
    r = requests.post(webhook_url, json={"text": text}, timeout=30)
    _raise(r)


if __name__ == "__main__":
    # Safe env presence check (no values printed)
    print("[env] JIRA_BASE_URL =", present("JIRA_BASE_URL"))
    print("[env] JIRA_EMAIL =", present("JIRA_EMAIL"))
    print("[env] JIRA_API_TOKEN =", present("JIRA_API_TOKEN"))
    print("[env] STAND_UP =", present("STAND_UP"))
    print("[env] JIRA_START_DATE_FIELD =", present("JIRA_START_DATE_FIELD"))
    print("[env] UPCOMING_DAYS =", os.environ.get("UPCOMING_DAYS", "2"))

    base_url = req_env("JIRA_BASE_URL").strip().rstrip("/")
    email = req_env("JIRA_EMAIL").strip()
    token = req_env("JIRA_API_TOKEN").strip()
    webhook = req_env("STAND_UP").strip()

    auth = (email, token)

    if not should_run_now():
        print("Not 9am London (or weekend) — exiting.")
        sys.exit(0)

    me = api_get(auth, base_url, "/rest/api/3/myself")
    print(f"[sanity] Auth OK. API user={me.get('displayName')} accountId={me.get('accountId')}")

    digest = build_digest(auth, base_url)
    send_chat(webhook, digest)
    print("Digest sent ✅")
