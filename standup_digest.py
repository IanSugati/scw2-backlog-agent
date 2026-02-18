import os
import sys
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# CONFIG (keep your hard-coded dev details for now)
DEV_ACCOUNT_ID = "5be5be3875085254a6a76016"
DEV_NAME = "Andy Edmonds"
PROJECT_KEY = "SPD"
QA_STATUS = "DEPLOYED TO QA"

LONDON = ZoneInfo("Europe/London")

def req_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v

def enforce_9am_london() -> bool:
    return os.environ.get("ENFORCE_9AM_LONDON", "true").lower() in ("1","true","yes","y","on")

def should_run_now() -> bool:
    if not enforce_9am_london():
        return True
    now = datetime.now(LONDON)
    return now.weekday() < 5 and now.hour == 9

def _raise(r: requests.Response):
    if r.ok:
        return
    raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url} :: {(r.text or '')[:400]}")

def jira_search(auth, jira_base, jql: str, fields=("summary","status","duedate"), max_results=200):
    url = f"{jira_base}/rest/api/3/search/jql"
    jql_clean = " ".join([line.strip() for line in jql.splitlines() if line.strip()])
    payload = {"jql": jql_clean, "maxResults": max_results, "fields": list(fields)}
    r = requests.post(url, auth=auth, headers={"Accept":"application/json","Content-Type":"application/json"}, json=payload, timeout=30)
    _raise(r)
    return r.json().get("issues", [])

def get_worklogs(auth, jira_base, issue_key: str):
    url = f"{jira_base}/rest/api/3/issue/{issue_key}/worklog"
    r = requests.get(url, auth=auth, params={"maxResults": 5000}, timeout=30)
    _raise(r)
    return r.json().get("worklogs", [])

def seconds_to_pretty(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m: return f"{h}h {m}m"
    if h: return f"{h}h"
    return f"{m}m"

def zombie_indicator(days: int) -> str:
    zombies = min(days // 10, 10)
    skulls = (days // 100) if days >= 100 else 0
    return ("🧟" * zombies) + ((" " + ("💀" * skulls)) if skulls else "")

def yesterday_window():
    now = datetime.now(LONDON)
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_today - timedelta(days=1), start_today

def parse_jira_dt(s: str) -> datetime:
    # convert +0000 to +00:00
    if len(s) >= 5 and (s[-5] in ["+","-"]) and s[-2:].isdigit():
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s)

def time_logged_yesterday(auth, jira_base, issues):
    start_y, start_t = yesterday_window()
    lines = []
    for issue in issues:
        key = issue["key"]
        summary = issue["fields"]["summary"]
        total = 0
        for wl in get_worklogs(auth, jira_base, key):
            if wl.get("author", {}).get("accountId") != DEV_ACCOUNT_ID:
                continue
            started = wl.get("started")
            if not started:
                continue
            dt = parse_jira_dt(started).astimezone(LONDON)
            if start_y <= dt < start_t:
                total += int(wl.get("timeSpentSeconds", 0))
        if total:
            lines.append(f"• {key} – {summary} ({seconds_to_pretty(total)})")
    return lines

def pushed_to_qa_yesterday(auth, jira_base):
    jql = f"""
      project = {PROJECT_KEY}
      AND assignee = {DEV_ACCOUNT_ID}
      AND status CHANGED TO "{QA_STATUS}"
      DURING (startOfDay(-1), startOfDay())
      ORDER BY updated DESC
    """
    issues = jira_search(auth, jira_base, jql)
    return [f"• {i['key']} – {i['fields']['summary']}" for i in issues]

def overdue_issues(auth, jira_base):
    jql = f"""
      project = {PROJECT_KEY}
      AND assignee = {DEV_ACCOUNT_ID}
      AND duedate < startOfDay()
      AND statusCategory != Done
      ORDER BY duedate ASC
    """
    issues = jira_search(auth, jira_base, jql)
    today = datetime.now(LONDON).date()
    out = []
    for i in issues:
        due = i["fields"].get("duedate")
        if not due:
            continue
        due_date = datetime.fromisoformat(due).date()
        days = (today - due_date).days
        out.append(f"• {i['key']} – {i['fields']['summary']} ({days} days {zombie_indicator(days)})")
    return out

def sprint_remaining(auth, jira_base):
    jql = f"""
      project = {PROJECT_KEY}
      AND sprint in openSprints()
      AND assignee = {DEV_ACCOUNT_ID}
      AND statusCategory != Done
      ORDER BY Rank ASC
    """
    issues = jira_search(auth, jira_base, jql)
    in_prog, up_next = [], []
    for i in issues:
        status = i["fields"]["status"]["name"].strip().lower()
        line = f"• {i['key']} – {i['fields']['summary']}"
        if status in {"in progress", "in review", "ready for integration"}:
            in_prog.append(line)
        else:
            up_next.append(line)
    return in_prog[:5], up_next[:5]

def build_digest(auth, jira_base):
    yesterday_jql = f"""
      project = {PROJECT_KEY}
      AND worklogAuthor = {DEV_ACCOUNT_ID}
      AND worklogDate >= startOfDay(-1)
      AND worklogDate < startOfDay()
      ORDER BY updated DESC
    """
    yesterday_issues = jira_search(auth, jira_base, yesterday_jql)
    time_lines = time_logged_yesterday(auth, jira_base, yesterday_issues)
    qa_lines = pushed_to_qa_yesterday(auth, jira_base)
    overdue_lines = overdue_issues(auth, jira_base)
    in_prog, next_up = sprint_remaining(auth, jira_base)

    msg = f"🧑‍💻 Standup Prep – {DEV_NAME}\n\n"
    msg += "⏱ Yesterday (time logged)\n" + ("\n".join(time_lines) if time_lines else "• No time logged") + "\n\n"
    msg += "🚀 Pushed to QA (yesterday)\n" + ("\n".join(qa_lines) if qa_lines else "• Nothing pushed to QA") + "\n\n"
    msg += "⚠️ Overdue\n" + ("\n".join(overdue_lines) if overdue_lines else "• No overdue items") + "\n\n"
    msg += "📌 Live Sprint – Remaining\n\n"
    msg += "🔥 In Progress\n" + ("\n".join(in_prog) if in_prog else "• None") + "\n\n"
    msg += "📋 Up Next\n" + ("\n".join(next_up) if next_up else "• None")
    return msg

def post_chat(webhook_url: str, text: str):
    r = requests.post(webhook_url, json={"text": text}, timeout=30)
    _raise(r)

if __name__ == "__main__":
    jira_base = req_env("JIRA_BASE_URL").rstrip("/")
    jira_email = req_env("JIRA_EMAIL")
    jira_token = req_env("JIRA_API_TOKEN")
    webhook = req_env("STAND_UP")
    auth = (jira_email, jira_token)

    if not should_run_now():
        print("Not 9am London (or weekend) — exiting.")
        sys.exit(0)

    digest = build_digest(auth, jira_base)
    post_chat(webhook, digest)
    print("Digest sent ✅")
