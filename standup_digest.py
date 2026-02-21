# standup_digest.py
#
# Secrets / env vars (EXACT):
#   JIRA_BASE_URL
#   JIRA_EMAIL
#   JIRA_API_TOKEN
#   STAND_UP
#
# Required (for capacity calc):
#   SPRINT_ANCHOR_DATE           (YYYY-MM-DD) - start of a sprint-cycle anchor
#
# Optional:
#   ENFORCE_9AM_LONDON=true|false   (default false)
#   UPCOMING_DAYS=2                 (default 2)
#   JIRA_STORY_POINTS_FIELD         (default customfield_10016)
#   JIRA_START_DATE_FIELD           (optional customfield id; if set, upcoming uses it too)
#
# Behaviour included (as discussed):
# - Yesterday: time logged by Andy (worklog scan)
# - Deployed to QA: status changed to QA_STATUS in previous 2 WORKING days, with deployed timestamp
# - Next N days: due date + optional start date
# - Capacity vs Estimate (Live Sprint): capacity from cadence model, estimate from SP totals
# - Live Sprint: list items split into In Progress / Up Next, with Due | Est (SP) | Spent and flags:
#     • ⚠ Over when spent > SP
#     • 🟠 80%+ when spent >= 80% of SP (and not Done)
# - Divider under Live Sprint heading (Option 1)
# - Overdue: SPD ONLY (as requested)
#
# Notes:
# - Estimate = Story Points (1 SP = 1 hour)
# - Capacity = remaining sprint working days * 7h
# - Sprint cadence model: 9 working days ON + 1 working day gap (repeat)

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

# Capacity model (agreed)
SPRINT_WORKDAYS = 9
SPRINT_GAP_DAYS = 1
HOURS_PER_DAY = 7

# Output limits (avoid Google Chat truncation)
MAX_TIMELOG_LINES = 8
MAX_QA_LINES = 10
MAX_UPCOMING_LINES = 10
MAX_SPRINT_IN_PROGRESS = 8
MAX_SPRINT_UP_NEXT = 8
MAX_OVERDUE_LINES = 10
# ----------------

LONDON = ZoneInfo("Europe/London")


# ----------------
# Helpers
# ----------------
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


def api_get(auth, base_url: str, path: str, params=None):
    url = f"{base_url}{path}"
    r = requests.get(url, auth=auth, headers={"Accept": "application/json"}, params=params, timeout=30)
    _raise(r)
    return r.json()


def jira_search(auth, base_url: str, jql: str, fields=None, max_results: int = 200):
    """
    Jira Cloud: /rest/api/3/search/jql (POST).
    """
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
    """Google Chat link format: <url|text>"""
    return f"<{base_url}/browse/{key}|{key}>"


def parse_jira_dt(dt_str: str) -> datetime:
    """
    Jira timestamps can be like:
      2026-02-21T10:15:30.123+0000
    Normalise +0000 -> +00:00 for Python.
    """
    if not dt_str:
        raise ValueError("Empty datetime string")
    if len(dt_str) >= 5 and (dt_str[-5] in ["+", "-"]) and dt_str[-2:].isdigit():
        dt_str = dt_str[:-2] + ":" + dt_str[-2:]
    return datetime.fromisoformat(dt_str)


def parse_jira_date(d: str) -> date | None:
    """Jira date fields often return YYYY-MM-DD."""
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
    return d.strftime("%a %d %b")


def pretty_dt_london(d: datetime) -> str:
    return d.astimezone(LONDON).strftime("%a %d %b %H:%M")


def seconds_to_pretty(seconds: int) -> str:
    seconds = int(seconds or 0)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def seconds_to_hours_float(seconds: int) -> float:
    return (int(seconds or 0)) / 3600.0


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


def zombie_indicator(days: int) -> str:
    zombies = min(days // 10, 10)
    skulls = (days // 100) if days >= 100 else 0
    return ("🧟" * zombies) + ((" " + ("💀" * skulls)) if skulls else "")


# ----------------------------
# Capacity calc (cadence model)
# ----------------------------
def count_workdays(start: date, end: date) -> int:
    """Count Mon–Fri in [start, end)."""
    days = 0
    d = start
    while d < end:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def remaining_sprint_capacity_hours() -> int:
    """
    Cadence model:
      - 9 working days sprint
      - 1 working day gap
      - repeats every 10 working days
    Includes 'today' as remaining capacity (suitable for 9am runs).
    """
    anchor_str = req_env("SPRINT_ANCHOR_DATE").strip()
    anchor = date.fromisoformat(anchor_str)

    today = datetime.now(LONDON).date()
    cycle = SPRINT_WORKDAYS + SPRINT_GAP_DAYS

    workdays_since_anchor = count_workdays(anchor, today)
    pos = workdays_since_anchor % cycle

    if pos >= SPRINT_WORKDAYS:
        return 0  # gap day

    remaining_days = SPRINT_WORKDAYS - pos
    return remaining_days * HOURS_PER_DAY


# ----------------------------
# Digest sections
# ----------------------------
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
        summary = issue["fields"].get("summary") or ""
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

    return lines[:MAX_TIMELOG_LINES]


def get_issue_changelog(auth, base_url: str, issue_key: str):
    """
    Fetch changelog via expand=changelog.
    Usually enough for “recent” transitions (previous 2 workdays).
    """
    data = api_get(auth, base_url, f"/rest/api/3/issue/{issue_key}", params={"expand": "changelog"})
    return (data.get("changelog", {}) or {}).get("histories", []) or []


def previous_working_days(today_local: date, n: int = 2) -> list[date]:
    """Return previous N working days before today (Mon–Fri), most-recent-first."""
    out: list[date] = []
    d = today_local - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def find_status_change_to(issue_histories, target_status_name: str, window_start: datetime, window_end: datetime) -> datetime | None:
    target_lower = target_status_name.strip().lower()
    best: datetime | None = None

    for h in issue_histories:
        created = h.get("created")
        if not created:
            continue

        try:
            created_dt = parse_jira_dt(created).astimezone(LONDON)
        except Exception:
            continue

        if not (window_start <= created_dt < window_end):
            continue

        for item in h.get("items", []) or []:
            if (item.get("field") or "").lower() != "status":
                continue
            to_str = (item.get("toString") or "").strip().lower()
            if to_str == target_lower:
                if best is None or created_dt > best:
                    best = created_dt

    return best


def deployed_to_qa_previous_2_workdays(auth, base_url: str):
    """
    Find issues deployed to QA during the previous 2 working days (London),
    and show when they were deployed.
    """
    today = datetime.now(LONDON).date()
    prev_days = previous_working_days(today, n=2)  # [most recent, older]
    oldest = prev_days[-1]

    window_start = datetime.combine(oldest, datetime.min.time(), tzinfo=LONDON)
    window_end = datetime.combine(today, datetime.min.time(), tzinfo=LONDON)

    start_str = oldest.isoformat()
    end_str = today.isoformat()

    # Use explicit dates to avoid JQL '+N' issues.
    jql = f"""
        project = {PROJECT_KEY}
        AND assignee = {DEV_ACCOUNT_ID}
        AND status CHANGED TO "{QA_STATUS}"
        AND statusCategory != Done
        AND status CHANGED TO "{QA_STATUS}" AFTER "{start_str}" BEFORE "{end_str}"
        ORDER BY updated DESC
    """

    issues = jira_search(auth, base_url, jql, fields=["summary"], max_results=200)

    rows = []
    for i in issues:
        key = i["key"]
        summary = i["fields"].get("summary") or ""

        histories = get_issue_changelog(auth, base_url, key)
        deployed_dt = find_status_change_to(histories, QA_STATUS, window_start, window_end)

        if deployed_dt:
            rows.append((deployed_dt, f"• {issue_link(base_url, key)} – {summary} (Deployed: {pretty_dt_london(deployed_dt)})"))
        else:
            rows.append((window_start, f"• {issue_link(base_url, key)} – {summary} (Deployed: within last 2 working days)"))

    rows.sort(key=lambda x: x[0], reverse=True)
    return [r[1] for r in rows[:MAX_QA_LINES]]


def upcoming_next_days(auth, base_url: str):
    """
    Shows items with a due date in the next N days (default 2).
    If JIRA_START_DATE_FIELD is set, includes items starting in that window too.
    """
    upcoming_days = int(os.environ.get("UPCOMING_DAYS", "2").strip() or "2")
    start_field = os.environ.get("JIRA_START_DATE_FIELD", "").strip()

    # Avoid startOfDay(+N) which can trigger '+' reserved char problems in some JQL contexts;
    # use numeric offset without '+'.
    end_offset = upcoming_days + 1

    if start_field:
        jql = f"""
            project = {PROJECT_KEY}
            AND assignee = {DEV_ACCOUNT_ID}
            AND statusCategory != Done
            AND (
                (duedate >= startOfDay() AND duedate < startOfDay({end_offset}))
                OR
                ({start_field} >= startOfDay() AND {start_field} < startOfDay({end_offset}))
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
            AND duedate < startOfDay({end_offset})
            ORDER BY duedate ASC
        """
        fields = ["summary", "duedate"]

    issues = jira_search(auth, base_url, jql, fields=fields, max_results=200)

    today = datetime.now(LONDON).date()
    end_day = today + timedelta(days=upcoming_days)

    lines = []
    for i in issues:
        key = i["key"]
        f = i.get("fields", {}) or {}
        summary = f.get("summary") or ""

        due_d = parse_jira_date(f.get("duedate"))
        start_d = parse_jira_date(f.get(start_field)) if start_field else None

        parts = []
        if start_d and today <= start_d <= end_day:
            parts.append(f"Start: {pretty_date(start_d)}")
        if due_d and today <= due_d <= end_day:
            parts.append(f"Due: {pretty_date(due_d)}")

        suffix = f" ({' | '.join(parts)})" if parts else ""
        lines.append(f"• {issue_link(base_url, key)} – {summary}{suffix}")

        if len(lines) >= MAX_UPCOMING_LINES:
            break

    return lines


def sprint_remaining(auth, base_url: str):
    """
    Live sprint items for Andy (SPD), split into:
      - In Progress
      - Up Next
    Adds: Due | Est (SP) | Spent plus flags (Over / 80%).
    Also returns total SP committed (for capacity).
    """
    sp_field = os.environ.get("JIRA_STORY_POINTS_FIELD", "customfield_10016").strip()

    fields = ["summary", "status", "duedate", "timespent", sp_field]

    jql = f"""
        project = {PROJECT_KEY}
        AND sprint in openSprints()
        AND assignee = {DEV_ACCOUNT_ID}
        AND statusCategory != Done
        AND issuetype != Epic
        AND issuetype != "Sprint Meeting"
        ORDER BY Rank ASC
    """

    issues = jira_search(auth, base_url, jql, fields=fields, max_results=500)

    # "In Progress" mapping (as tuned from your workflow)
    active_statuses = {
        "in progress",
        "in review",
        "ready for integration",
        "ready for package",
        "deployed to qa",
        "deployed to package org",
    }

    in_progress = []
    up_next = []
    total_sp = 0.0

    for i in issues:
        key = i["key"]
        f = i.get("fields", {}) or {}
        summary = (f.get("summary") or "").strip()
        status_name = ((f.get("status") or {}).get("name") or "").strip().lower()

        sp = f.get(sp_field)
        spent_seconds = f.get("timespent") or 0
        due_d = parse_jira_date(f.get("duedate"))

        # Committed SP (estimate)
        if isinstance(sp, (int, float)):
            total_sp += float(sp)

        parts = []
        if due_d:
            parts.append(f"Due: {pretty_date(due_d)}")

        parts.append(f"Est: {sp:g} SP" if isinstance(sp, (int, float)) else "Est: —")
        parts.append(f"Spent: {seconds_to_pretty(spent_seconds)}")

        # Threshold flags (live sprint only)
        flag = ""
        if isinstance(sp, (int, float)) and sp > 0:
            spent_h = seconds_to_hours_float(spent_seconds)
            est_h = float(sp)

            if spent_h > est_h:
                over_by = spent_h - est_h
                flag = f" ⚠ Over +{over_by:.1f}h"
            else:
                ratio = spent_h / est_h if est_h else 0.0
                if ratio >= 0.80:
                    flag = f" 🟠 {int(ratio * 100)}%"

        block = f"• {issue_link(base_url, key)} – {summary}\n  ({' | '.join(parts)}){flag}"

        if status_name in active_statuses:
            if len(in_progress) < MAX_SPRINT_IN_PROGRESS:
                in_progress.append(block)
        else:
            if len(up_next) < MAX_SPRINT_UP_NEXT:
                up_next.append(block)

    return in_progress, up_next, total_sp


def overdue_spd(auth, base_url: str):
    """
    Overdue items in SPD only.
    """
    jql = f"""
        project = {PROJECT_KEY}
        AND assignee = {DEV_ACCOUNT_ID}
        AND duedate < startOfDay()
        AND statusCategory != Done
        AND issuetype != Epic
        AND issuetype != "Sprint Meeting"
        ORDER BY duedate ASC
    """
    issues = jira_search(auth, base_url, jql, fields=["summary", "duedate"], max_results=200)

    today = datetime.now(LONDON).date()
    lines = []

    for i in issues:
        key = i["key"]
        summary = (i.get("fields", {}) or {}).get("summary") or ""
        due = parse_jira_date((i.get("fields", {}) or {}).get("duedate"))
        if not due:
            continue

        days = (today - due).days
        lines.append(f"• {issue_link(base_url, key)} – {summary} ({days} days {zombie_indicator(days)})")

        if len(lines) >= MAX_OVERDUE_LINES:
            break

    return lines


def build_digest(auth, base_url: str):
    time_lines = time_logged_yesterday(auth, base_url)
    qa_lines = deployed_to_qa_previous_2_workdays(auth, base_url)
    upcoming_lines = upcoming_next_days(auth, base_url)

    in_prog, next_up, total_sp = sprint_remaining(auth, base_url)

    capacity = remaining_sprint_capacity_hours()
    overage = total_sp - capacity

    overdue_lines = overdue_spd(auth, base_url)

    upcoming_days = int(os.environ.get("UPCOMING_DAYS", "2").strip() or "2")
    end_label = (datetime.now(LONDON).date() + timedelta(days=upcoming_days)).strftime("%a %d %b")

    msg = f"🧑‍💻 Standup Prep – {DEV_NAME}\n\n"

    msg += "⏱ Yesterday (time logged)\n"
    msg += "\n".join(time_lines) if time_lines else "• No time logged"
    msg += "\n\n"

    msg += "🚀 Deployed to QA (previous 2 working days)\n"
    msg += "\n".join(qa_lines) if qa_lines else "• Nothing deployed to QA"
    msg += "\n\n"

    msg += f"📅 Next {upcoming_days} days (through {end_label})\n"
    msg += "\n".join(upcoming_lines) if upcoming_lines else "• Nothing scheduled (no start/due dates found)"
    msg += "\n\n"

    msg += "🧮 Capacity vs Estimate (Live Sprint)\n"
    msg += f"• Remaining capacity: {capacity:.0f}h\n"
    msg += f"• Committed (SP): {total_sp:.1f}h\n"
    if overage > 0:
        msg += f"⚠️ Over capacity by {overage:.1f}h\n"
    else:
        msg += "✅ Within capacity\n"
    msg += "\n"

    msg += "📌 Live Sprint – Remaining (SPD)\n"
    msg += "──────────────────────────────\n\n"

    msg += "🔥 In Progress\n"
    msg += "\n".join(in_prog) if in_prog else "• None"
    msg += "\n\n"

    msg += "📋 Up Next\n"
    msg += "\n".join(next_up) if next_up else "• None"
    msg += "\n\n"

    msg += "⚠️ Overdue (SPD)\n"
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
    print("[env] SPRINT_ANCHOR_DATE =", present("SPRINT_ANCHOR_DATE"))
    print("[env] JIRA_STORY_POINTS_FIELD =", present("JIRA_STORY_POINTS_FIELD"))
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

    # Auth sanity (no secrets exposed)
    me = api_get(auth, base_url, "/rest/api/3/myself")
    print(f"[sanity] Auth OK. API user={me.get('displayName')} accountId={me.get('accountId')}")

    digest = build_digest(auth, base_url)
    send_chat(webhook, digest)
    print("Digest sent ✅")
