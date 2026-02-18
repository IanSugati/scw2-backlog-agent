import os
import sys
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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


def _raise(r: requests.Response):
    if r.ok:
        return
    raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url} :: {(r.text or '')[:800]}")


def should_run_now() -> bool:
    # keep disabled for testing in workflow by ENFORCE_9AM_LONDON=false
    return True


def api_get(auth, base_url, path):
    url = f"{base_url}{path}"
    r = requests.get(url, auth=auth, headers={"Accept": "application/json"}, timeout=30)
    _raise(r)
    return r.json()


def jira_search(auth, base_url: str, jql: str, fields=None, max_results: int = 200):
    """
    Try POST /rest/api/3/search first (body-based).
    If tenant blocks it, fall back to POST /rest/api/3/search/jql.
    Always prints endpoint + totals.
    """
    jql_clean = " ".join(line.strip() for line in jql.splitlines() if line.strip())
    payload = {
        "jql": jql_clean,
        "maxResults": max_results,
        "fields": fields or ["summary", "status", "duedate"],
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    # 1) Preferred: /search (POST with body)
    url1 = f"{base_url}/rest/api/3/search"
    r1 = requests.post(url1, auth=auth, headers=headers, json=payload, timeout=30)

    if r1.status_code not in (404, 410):
        _raise(r1)
        data = r1.json()
        issues = data.get("issues", [])
        total = data.get("total", "n/a")
        print(f"[jira_search] endpoint=/search total={total} returned={len(issues)} jql={jql_clean}")
        return issues

    # 2) Fallback: /search/jql
    url2 = f"{base_url}/rest/api/3/search/jql"
    r2 = requests.post(url2, auth=auth, headers=headers, json=payload, timeout=30)
    _raise(r2)
    data = r2.json()
    issues = data.get("issues", [])
    total = data.get("total", data.get("numberOfSearchResults", "n/a"))
    print(f"[jira_search] endpoint=/search/jql total={total} returned={len(issues)} jql={jql_clean}")
    return issues


def debug_sample(label, issues):
    keys = [i["key"] for i in issues[:10]]
    print(f"[debug] {label}: {len(issues)} issues. sample={keys}")


def parse_jira_dt(started: str) -> datetime:
    if len(started) >= 5 and (started[-5] in ["+", "-"]) and started[-2:].isdigit():
        started = started[:-2] + ":" + started[-2:]
    return datetime.fromisoformat(started)


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
    debug_sample("YESTERDAY issue set (worklog JQL)", issues)

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
            dt = parse_jira_dt(started).astimezone(LONDON)
            if start_y <= dt < start_t:
                total += int(wl.get("timeSpentSeconds", 0))

        if total:
            lines.append(f"• {key} – {summary} ({seconds_to_pretty(total)})")

    return lines


def build_and_send(auth, base_url: str, webhook_url: str):
    # --- SANITY CHECKS (critical) ---
    me = api_get(auth, base_url, "/rest/api/3/myself")
    print(f"[sanity] API user displayName={me.get('displayName')} accountId={me.get('accountId')}")

    proj = api_get(auth, base_url, f"/rest/api/3/project/{PROJECT_KEY}")
    print(f"[sanity] Project {PROJECT_KEY} name={proj.get('name')} id={proj.get('id')}")

    # Can we see ANY issues in SPD?
    any_spd = jira_search(auth, base_url, f"project = {PROJECT_KEY} ORDER BY updated DESC", fields=["summary"], max_results=5)
    debug_sample("SANITY: any issues visible in SPD", any_spd)

    # --- MAIN QUERIES ---
    time_lines = time_logged_yesterday(auth, base_url)

    qa_issues = jira_search(auth, base_url, f"""
        project = {PROJECT_KEY}
        AND assignee = {DEV_ACCOUNT_ID}
        AND status CHANGED TO "{QA_STATUS}"
        DURING (startOfDay(-1), startOfDay())
        AND statusCategory != Done
        ORDER BY updated DESC
    """, fields=["summary"], max_results=200)
    debug_sample("PUSHED TO QA yesterday", qa_issues)

    overdue_issues = jira_search(auth, base_url, f"""
        assignee = {DEV_ACCOUNT_ID}
        AND duedate < startOfDay()
        AND statusCategory != Done
        ORDER BY duedate ASC
    """, fields=["summary", "duedate"], max_results=200)
    debug_sample("OVERDUE all projects", overdue_issues)

    sprint_issues = jira_search(auth, base_url, f"""
        project = {PROJECT_KEY}
        AND sprint in openSprints()
        AND assignee = {DEV_ACCOUNT_ID}
        AND statusCategory != Done
        ORDER BY Rank ASC
    """, fields=["summary", "status"], max_results=500)
    debug_sample("SPRINT remaining (SPD)", sprint_issues)

    # --- MESSAGE ---
    msg = f"🧑‍💻 Standup Prep – {DEV_NAME}\n\n"
    msg += "⏱ Yesterday (time logged)\n"
    msg += "\n".join(time_lines) if time_lines else "• No time logged"
    msg += "\n\n"

    msg += "🚀 Pushed to QA (yesterday)\n"
    msg += "\n".join([f"• {i['key']} – {i['fields']['summary']}" for i in qa_issues]) if qa_issues else "• Nothing pushed to QA"
    msg += "\n\n"

    msg += "⚠️ Overdue (all projects)\n"
    msg += "\n".join([f"• {i['key']} – {i['fields']['summary']}" for i in overdue_issues]) if overdue_issues else "• No overdue items"
    msg += "\n\n"

    msg += "📌 Live Sprint – Remaining (SPD)\n"
    msg += "\n".join([f"• {i['key']} – {i['fields']['summary']}" for i in sprint_issues[:25]]) if sprint_issues else "• None"

    r = requests.post(webhook_url, json={"text": msg}, timeout=30)
    _raise(r)
    print("Digest sent ✅")


if __name__ == "__main__":
    base_url = req_env("JIRA_BASE_URL").rstrip("/")
    email = req_env("JIRA_EMAIL")
    token = req_env("JIRA_API_TOKEN")
    webhook = req_env("STAND_UP")

    auth = (email, token)

    if not should_run_now():
        print("Not scheduled time — exiting.")
        sys.exit(0)

    build_and_send(auth, base_url, webhook)
