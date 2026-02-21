# sprint_health_digest.py
#
# Sprint Health Digest (team) for Jira -> Google Chat
#
# What this script does:
# 1) Figures out the "current sprint window" from an anchor date + your cadence:
#    - Sprint = 9 working days (Mon–Fri only)
#    - Gap = 1 working day
#    - Repeats forever
#
# 2) Pulls ALL issues in the current open sprint (by JQL openSprints()).
# 3) Calculates:
#    - Estimate (Committed) = Story Points * 1 hour (SP = hours)
#    - Spent in sprint = sum of worklog seconds within sprint window
#    - Breakdown by current statusCategory: Done / In Progress / Unstarted
#    - Remaining capacity = remaining working days * 7h * team_size
#    - Flags:
#        - Overspent issues (spent > estimate)
#        - At-risk issues (In Progress and spent >= 80% of estimate)
#
# 4) Builds a daily history (most recent first) for each working day in the sprint so far:
#    - Work logged (by issue, totals for that day)
#    - Status moves: collapses multiple status changes in the day into one "start -> end"
#
# Env vars / Secrets expected:
#   JIRA_BASE_URL            (e.g. https://sugatitravel.atlassian.net)
#   JIRA_EMAIL
#   JIRA_API_TOKEN
#   CHAT_WEBHOOK_URL         (Google Chat incoming webhook)
#   SPRINT_ANCHOR_DATE       (YYYY-MM-DD) e.g. 2026-02-12
#
# Optional:
#   PROJECT_KEY              default "SPD"
#   TEAM_ACCOUNT_IDS         comma-separated Jira accountIds; if set, only counts worklogs by those users
#   WORK_HOURS_PER_DAY       default "7"
#   ENFORCE_9AM_LONDON       true|false (default false) - if true, exits unless weekday 9am London
#   MAX_SPRINT_ISSUES         default "250"
#   MAX_STATUS_MOVE_ISSUES    default "100"
#
# Notes:
# - This script is defensive about Jira 429/rate-limits and transient HTTP issues.

from __future__ import annotations

import os
import sys
import time
import math
import json
import requests
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple, Iterable, Set


LONDON = ZoneInfo("Europe/London")


# ----------------------------
# Helpers: env + formatting
# ----------------------------

def req_env(name: str) -> str:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return v.strip()


def opt_env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if (v is None or v.strip() == "") else v.strip()


def present(name: str) -> str:
    v = os.environ.get(name)
    return "SET" if (v is not None and v.strip() != "") else "MISSING"


def enforce_9am_london() -> bool:
    raw = os.environ.get("ENFORCE_9AM_LONDON", "false").strip().lower()
    return raw in ("1", "true", "yes", "y", "on")


def should_run_now() -> bool:
    if not enforce_9am_london():
        return True
    now = datetime.now(LONDON)
    return now.weekday() < 5 and now.hour == 9


def issue_link(base_url: str, key: str) -> str:
    # Google Chat link format: <url|text>
    return f"<{base_url}/browse/{key}|{key}>"


def seconds_to_pretty(seconds: int) -> str:
    if seconds <= 0:
        return "0m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


def hours_to_1dp(h: float) -> str:
    return f"{h:.1f}h"


def weekday_name(d: date) -> str:
    return d.strftime("%A %d %b")


def parse_jira_dt(dt_str: str) -> datetime:
    """
    Jira timestamps often like:
      2026-02-21T09:15:22.123+0000
    Python wants:
      2026-02-21T09:15:22.123+00:00
    """
    s = dt_str
    if len(s) >= 5 and (s[-5] in ["+", "-"]) and s[-2:].isdigit():
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s)


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def daterange_days(start: date, end_inclusive: date) -> Iterable[date]:
    cur = start
    while cur <= end_inclusive:
        yield cur
        cur = cur + timedelta(days=1)


def working_days_between(start: date, end_inclusive: date) -> List[date]:
    return [d for d in daterange_days(start, end_inclusive) if is_weekday(d)]


# ----------------------------
# Jira HTTP with retry/backoff
# ----------------------------

def _raise(r: requests.Response):
    if r.ok:
        return
    # Truncate large HTML bodies from 429 pages etc.
    body = (r.text or "")[:800]
    raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url} :: {body}")


def request_with_retry(
    method: str,
    url: str,
    *,
    auth: Tuple[str, str],
    headers: Dict[str, str],
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
    max_attempts: int = 6,
) -> requests.Response:
    """
    Handles:
      - 429 Too Many Requests (uses Retry-After if present)
      - chunked/connection hiccups
      - 5xx
    """
    sleep_s = 1.0
    last_exc: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.request(
                method,
                url,
                auth=auth,
                headers=headers,
                params=params,
                json=json_body,
                timeout=timeout,
            )

            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                if ra:
                    try:
                        wait = float(ra)
                    except Exception:
                        wait = sleep_s
                else:
                    wait = sleep_s
                time.sleep(min(wait, 20.0))
                sleep_s = min(sleep_s * 2.0, 20.0)
                continue

            if 500 <= r.status_code <= 599:
                time.sleep(min(sleep_s, 20.0))
                sleep_s = min(sleep_s * 2.0, 20.0)
                continue

            _raise(r)
            return r

        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_exc = e
            time.sleep(min(sleep_s, 20.0))
            sleep_s = min(sleep_s * 2.0, 20.0)
            continue
        except Exception as e:
            last_exc = e
            break

    if last_exc:
        raise last_exc
    raise RuntimeError("request_with_retry failed without exception (unexpected)")


# ----------------------------
# Jira API wrappers
# ----------------------------

def jira_auth() -> Tuple[str, str]:
    return (req_env("JIRA_EMAIL"), req_env("JIRA_API_TOKEN"))


def jira_base_url() -> str:
    return req_env("JIRA_BASE_URL").rstrip("/")


def jira_get(path: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base = jira_base_url()
    url = f"{base}{path}"
    r = request_with_retry(
        "GET",
        url,
        auth=jira_auth(),
        headers={"Accept": "application/json"},
        params=params,
    )
    return r.json()


def jira_post(path: str, *, payload: Dict[str, Any]) -> Dict[str, Any]:
    base = jira_base_url()
    url = f"{base}{path}"
    r = request_with_retry(
        "POST",
        url,
        auth=jira_auth(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json_body=payload,
    )
    return r.json()


def jira_search(jql: str, *, fields: List[str], max_results: int = 200) -> List[Dict[str, Any]]:
    """
    Uses Jira Cloud /rest/api/3/search/jql (new endpoint) via POST.
    """
    jql_clean = " ".join(line.strip() for line in jql.splitlines() if line.strip())
    payload = {
        "jql": jql_clean,
        "maxResults": max_results,
        "fields": fields,
    }
    data = jira_post("/rest/api/3/search/jql", payload=payload)
    return data.get("issues", []) or []


def get_issue_worklogs(issue_key: str) -> List[Dict[str, Any]]:
    data = jira_get(f"/rest/api/3/issue/{issue_key}/worklog", params={"maxResults": 5000})
    return data.get("worklogs", []) or []


def get_issue_changelog(issue_key: str) -> List[Dict[str, Any]]:
    """
    Pull a chunk of changelog histories. Usually enough for a sprint window.
    If you find it truncates, we can paginate later.
    """
    data = jira_get(f"/rest/api/3/issue/{issue_key}/changelog", params={"maxResults": 200})
    return data.get("values", []) or []


# ----------------------------
# Sprint window logic
# ----------------------------

@dataclass(frozen=True)
class SprintWindow:
    start: date
    end: date  # inclusive
    gap_day: Optional[date]  # 1 working-day gap (if any) between sprints


def compute_sprint_window(anchor_start: date, today: date) -> SprintWindow:
    """
    Your cadence:
      - Sprint: 9 working days
      - Gap:   1 working day
      - Repeats

    We compute the "current sprint" as:
      - If today falls in a sprint, return that sprint.
      - If today falls in the gap day, return the NEXT sprint (so planning isn't stale).
      - If today is before anchor, return anchor sprint.
    """
    if today < anchor_start:
        # Build first sprint from anchor
        sprint_days = build_working_day_block(anchor_start, 9)
        return SprintWindow(start=sprint_days[0], end=sprint_days[-1], gap_day=None)

    cursor = anchor_start

    while True:
        sprint_days = build_working_day_block(cursor, 9)
        sprint_start = sprint_days[0]
        sprint_end = sprint_days[-1]

        gap_start = next_working_day(sprint_end + timedelta(days=1))
        gap_days = build_working_day_block(gap_start, 1)
        gap_day = gap_days[0]

        next_sprint_start = next_working_day(gap_day + timedelta(days=1))

        # If within sprint:
        if sprint_start <= today <= sprint_end:
            return SprintWindow(start=sprint_start, end=sprint_end, gap_day=gap_day)

        # If in gap day, treat next sprint as "current"
        if today == gap_day:
            next_days = build_working_day_block(next_sprint_start, 9)
            return SprintWindow(start=next_days[0], end=next_days[-1], gap_day=None)

        # If beyond, advance
        if today > gap_day:
            cursor = next_sprint_start
            continue

        # Defensive fallback
        return SprintWindow(start=sprint_start, end=sprint_end, gap_day=gap_day)


def next_working_day(d: date) -> date:
    cur = d
    while not is_weekday(cur):
        cur = cur + timedelta(days=1)
    return cur


def build_working_day_block(start: date, n_working_days: int) -> List[date]:
    days: List[date] = []
    cur = start
    while len(days) < n_working_days:
        if is_weekday(cur):
            days.append(cur)
        cur = cur + timedelta(days=1)
    return days


def sprint_datetime_bounds(window: SprintWindow) -> Tuple[datetime, datetime]:
    """
    Returns [start_dt, end_dt) in London timezone (end exclusive).
    """
    start_dt = datetime(window.start.year, window.start.month, window.start.day, 0, 0, 0, tzinfo=LONDON)
    end_next = window.end + timedelta(days=1)
    end_dt = datetime(end_next.year, end_next.month, end_next.day, 0, 0, 0, tzinfo=LONDON)
    return start_dt, end_dt


def remaining_working_days_in_sprint(window: SprintWindow, today: date) -> int:
    """
    Remaining working days from today (or next working day if weekend) through sprint end inclusive.
    """
    start = today
    if not is_weekday(start):
        start = next_working_day(start + timedelta(days=1))
    if start > window.end:
        return 0
    return len(working_days_between(start, window.end))


# ----------------------------
# Core computations
# ----------------------------

def status_category_name(issue: Dict[str, Any]) -> str:
    """
    Returns one of: Done / In Progress / To Do  (Jira's statusCategory names)
    We'll map "To Do" -> "Unstarted" for display.
    """
    f = issue.get("fields", {}) or {}
    st = f.get("status") or {}
    cat = st.get("statusCategory") or {}
    name = (cat.get("name") or "").strip()
    return name if name else "Unknown"


def normalize_bucket(cat_name: str) -> str:
    n = cat_name.strip().lower()
    if n == "done":
        return "Done"
    if n == "in progress":
        return "In Progress"
    if n == "to do":
        return "Unstarted"
    return cat_name or "Unknown"


def get_story_points(issue: Dict[str, Any], sp_field: str) -> float:
    f = issue.get("fields", {}) or {}
    v = f.get(sp_field)
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    # Sometimes comes as string-ish
    try:
        return float(str(v))
    except Exception:
        return 0.0


def extract_summary(issue: Dict[str, Any]) -> str:
    f = issue.get("fields", {}) or {}
    return (f.get("summary") or "").strip()


def extract_timespent_total_seconds(issue: Dict[str, Any]) -> int:
    """
    Jira field 'timespent' is total time spent (all time).
    We only use it to decide whether it's worth fetching worklogs.
    """
    f = issue.get("fields", {}) or {}
    v = f.get("timespent")
    if isinstance(v, int):
        return v
    return 0


def filter_worklogs_in_window(
    worklogs: List[Dict[str, Any]],
    start_dt: datetime,
    end_dt: datetime,
    allowed_account_ids: Optional[Set[str]],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for wl in worklogs:
        started = wl.get("started")
        if not started:
            continue
        dt0 = parse_jira_dt(started).astimezone(LONDON)
        if not (start_dt <= dt0 < end_dt):
            continue

        if allowed_account_ids is not None:
            aid = wl.get("author", {}).get("accountId")
            if not aid or aid not in allowed_account_ids:
                continue

        out.append(wl)
    return out


def sum_worklogs_seconds(worklogs: List[Dict[str, Any]]) -> int:
    total = 0
    for wl in worklogs:
        total += int(wl.get("timeSpentSeconds") or 0)
    return total


def worklogs_by_day_seconds(worklogs: List[Dict[str, Any]]) -> Dict[date, int]:
    """
    Worklogs grouped by day (London) => seconds
    """
    by: Dict[date, int] = {}
    for wl in worklogs:
        started = wl.get("started")
        if not started:
            continue
        dt0 = parse_jira_dt(started).astimezone(LONDON)
        d = dt0.date()
        by[d] = by.get(d, 0) + int(wl.get("timeSpentSeconds") or 0)
    return by


def build_capacity_commitment_section(
    issues_in_sprint: List[Dict[str, Any]],
    worklogs_in_sprint_by_key: Dict[str, List[Dict[str, Any]]],
    sp_field: str,
    window: SprintWindow,
    today: date,
) -> str:
    """
    Produces the "Capacity vs Commitment (SP=hours)" block with breakdown and flags.
    """
    work_hours_per_day = float(opt_env("WORK_HOURS_PER_DAY", "7"))

    # Team size fixed to 2 (two team members in play)
    team_size = 2

    rem_days = remaining_working_days_in_sprint(window, today)
    remaining_capacity_h = rem_days * work_hours_per_day * team_size

    # Estimate buckets (SP=hours)
    est_by_bucket: Dict[str, float] = {"Done": 0.0, "In Progress": 0.0, "Unstarted": 0.0, "Unknown": 0.0}
    total_est = 0.0

    # Spent buckets (worklog hours in sprint window)
    spent_by_bucket: Dict[str, float] = {"Done": 0.0, "In Progress": 0.0, "Unstarted": 0.0, "Unknown": 0.0}
    total_spent = 0.0

    # Issue-level flags
    overspent: List[Tuple[str, str, float, float]] = []  # key, summary, est_h, spent_h
    at_risk: List[Tuple[str, str, float, float]] = []    # key, summary, est_h, spent_h

    for it in issues_in_sprint:
        key = it.get("key")
        if not key:
            continue

        bucket = normalize_bucket(status_category_name(it))
        sp = get_story_points(it, sp_field)
        est_h = sp * 1.0  # SP=hours
        total_est += est_h
        if bucket not in est_by_bucket:
            est_by_bucket["Unknown"] += est_h
        else:
            est_by_bucket[bucket] += est_h

        wls = worklogs_in_sprint_by_key.get(key, [])
        spent_sec = sum_worklogs_seconds(wls)
        spent_h = spent_sec / 3600.0
        total_spent += spent_h
        if bucket not in spent_by_bucket:
            spent_by_bucket["Unknown"] += spent_h
        else:
            spent_by_bucket[bucket] += spent_h

        # Overspent / at-risk flags
        if est_h > 0:
            if spent_h > est_h + 1e-9:
                overspent.append((key, extract_summary(it), est_h, spent_h))
            # At risk: only if currently In Progress, and >=80% spent
            if bucket == "In Progress" and spent_h >= 0.8 * est_h and spent_h <= est_h + 1e-9:
                at_risk.append((key, extract_summary(it), est_h, spent_h))

    # Remaining estimate (not Done)
    remaining_est = (
        est_by_bucket.get("In Progress", 0.0)
        + est_by_bucket.get("Unstarted", 0.0)
        + est_by_bucket.get("Unknown", 0.0)
    )

    # Spent on not-Done (i.e. progress already made on remaining work)
    spent_not_done = (
        spent_by_bucket.get("In Progress", 0.0)
        + spent_by_bucket.get("Unstarted", 0.0)
        + spent_by_bucket.get("Unknown", 0.0)
    )

    # True remaining after work done (clamp at 0)
    true_remaining = max(0.0, remaining_est - spent_not_done)

    over_by = remaining_est - remaining_capacity_h
    within = over_by <= 0.0

    lines: List[str] = []
    lines.append("🧮 *Capacity vs Commitment (SP = hours)*")
    lines.append(f"• Sprint: {window.start.strftime('%d %b')} → {window.end.strftime('%d %b')}")
    lines.append(f"• Remaining capacity: {hours_to_1dp(remaining_capacity_h)}")
    lines.append(f"• Estimate total (Committed): {hours_to_1dp(total_est)}")
    lines.append(f"• Spent so far (in sprint): {hours_to_1dp(total_spent)}")
    lines.append(f"• Remaining estimate (not Done): {hours_to_1dp(remaining_est)}")
    lines.append(f"• Spent on not-Done: {hours_to_1dp(spent_not_done)}")
    lines.append(f"• True remaining (Est - Spent on not-Done): {hours_to_1dp(true_remaining)}")

    # Breakdown
    lines.append("")
    lines.append("*Breakdown (Estimate | Spent)*")
    lines.append(f"• Done: {hours_to_1dp(est_by_bucket['Done'])} | {hours_to_1dp(spent_by_bucket['Done'])}")
    lines.append(f"• In progress: {hours_to_1dp(est_by_bucket['In Progress'])} | {hours_to_1dp(spent_by_bucket['In Progress'])}")
    lines.append(f"• Unstarted (To Do): {hours_to_1dp(est_by_bucket['Unstarted'])} | {hours_to_1dp(spent_by_bucket['Unstarted'])}")
    if est_by_bucket.get("Unknown", 0.0) > 0.0 or spent_by_bucket.get("Unknown", 0.0) > 0.0:
        lines.append(f"• Unknown: {hours_to_1dp(est_by_bucket['Unknown'])} | {hours_to_1dp(spent_by_bucket['Unknown'])}")

    lines.append("")
    if within:
        lines.append(f"✅ Remaining estimate within capacity (buffer {hours_to_1dp(-over_by)})")
    else:
        lines.append(f"⚠️ Over capacity by {hours_to_1dp(over_by)} *(based on remaining estimate vs remaining capacity)*")

    # Top flags
    base = jira_base_url()
    if overspent:
        overspent.sort(key=lambda t: (t[3] - t[2]), reverse=True)
        lines.append("")
        lines.append("🚨 *Overspent (Spent > Est)*")
        for key, summ, est_h, sp_h in overspent[:8]:
            lines.append(f"• {issue_link(base, key)} – {summ} _(Est {hours_to_1dp(est_h)} | Spent {hours_to_1dp(sp_h)} | Over {hours_to_1dp(sp_h - est_h)})_")
        if len(overspent) > 8:
            lines.append(f"• +{len(overspent) - 8} more…")

    if at_risk:
        at_risk.sort(key=lambda t: (t[3] / max(t[2], 1e-9)), reverse=True)
        lines.append("")
        lines.append("⚠️ *At risk (≥80% of Est, still In Progress)*")
        for key, summ, est_h, sp_h in at_risk[:8]:
            pct = int(round((sp_h / est_h) * 100)) if est_h > 0 else 0
            lines.append(f"• {issue_link(base, key)} – {summ} _(Est {hours_to_1dp(est_h)} | Spent {hours_to_1dp(sp_h)} | {pct}%)_")
        if len(at_risk) > 8:
            lines.append(f"• +{len(at_risk) - 8} more…")

    return "\n".join(lines)


def sprint_issues_jql(project_key: str) -> str:
    # Keep it consistent: open sprint issues only; exclude Epics and Sprint Meeting.
    return f"""
        project = {project_key}
        AND sprint in openSprints()
        AND issuetype != Epic
        AND issuetype != "Sprint Meeting"
        ORDER BY Rank ASC
    """


def fetch_sprint_issues(project_key: str, sp_field: str) -> List[Dict[str, Any]]:
    max_issues = int(opt_env("MAX_SPRINT_ISSUES", "250"))
    fields = [
        "summary",
        "status",
        "issuetype",
        "timespent",
        sp_field,
    ]
    return jira_search(sprint_issues_jql(project_key), fields=fields, max_results=max_issues)


def build_daily_history(
    issues_in_sprint: List[Dict[str, Any]],
    worklogs_in_sprint_by_key: Dict[str, List[Dict[str, Any]]],
    window: SprintWindow,
    today: date,
) -> str:
    """
    For each working day in sprint so far (most recent first):
      - Work logged (sum per issue for that day)
      - Status moves collapsed to "start -> end" for the day
    """
    base = jira_base_url()
    start_dt, end_dt = sprint_datetime_bounds(window)

    # Determine working days from sprint start to min(today, sprint end)
    upto = min(today, window.end)
    sprint_days = [d for d in working_days_between(window.start, upto)]
    sprint_days.reverse()  # most recent first

    # Build quick maps for summaries
    summary_by_key: Dict[str, str] = {it["key"]: extract_summary(it) for it in issues_in_sprint if it.get("key")}

    # Precompute per-issue per-day logged time (seconds)
    per_day_issue_seconds: Dict[date, Dict[str, int]] = {}
    for key, wls in worklogs_in_sprint_by_key.items():
        by_day = worklogs_by_day_seconds(wls)
        for d, sec in by_day.items():
            if d < window.start or d > window.end:
                continue
            if not is_weekday(d):
                continue
            per_day_issue_seconds.setdefault(d, {})
            per_day_issue_seconds[d][key] = per_day_issue_seconds[d].get(key, 0) + sec

    # For status changes: for each day, find keys with status change using JQL, then fetch changelog only for those keys.
    max_status_issues = int(opt_env("MAX_STATUS_MOVE_ISSUES", "100"))
    project_key = opt_env("PROJECT_KEY", "SPD").strip()

    out_lines: List[str] = []
    out_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    out_lines.append("📊 *Daily History (most recent first)*")

    for d in sprint_days:
        out_lines.append("")
        out_lines.append(f"📅 *{weekday_name(d)}*")

        # Work logged
        out_lines.append("")
        out_lines.append("⏱ *Work logged*")
        items = per_day_issue_seconds.get(d, {})
        if not items:
            out_lines.append("• None")
        else:
            # Sort by most time
            rows = sorted(items.items(), key=lambda kv: kv[1], reverse=True)
            for key, sec in rows[:12]:
                summ = summary_by_key.get(key, "")
                out_lines.append(f"• {issue_link(base, key)} – {summ} ({seconds_to_pretty(sec)})")
            if len(rows) > 12:
                out_lines.append(f"• +{len(rows) - 12} more…")

        # Status moves (collapsed start->end)
        out_lines.append("")
        out_lines.append("🔁 *Status moves*")

        day_start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=LONDON)
        day_end = day_start + timedelta(days=1)

        # JQL: status changed during that day, limited to sprint issues via openSprints()
        # Use absolute date strings to avoid '+' reserved-char problems.
        # Jira accepts "YYYY-MM-DD" dates in DURING.
        jql = f"""
            project = {project_key}
            AND sprint in openSprints()
            AND status CHANGED DURING ("{d.isoformat()}", "{(d + timedelta(days=1)).isoformat()}")
            AND issuetype != Epic
            AND issuetype != "Sprint Meeting"
            ORDER BY updated DESC
        """

        changed_issues = jira_search(jql, fields=["summary"], max_results=max_status_issues)
        if not changed_issues:
            out_lines.append("• None")
            continue

        # For each issue, fetch changelog and collapse first->last status change in that day
        collapsed: List[Tuple[str, str, str, str]] = []  # key, summary, from, to
        for it in changed_issues:
            key = it.get("key")
            if not key:
                continue
            summ = (it.get("fields", {}) or {}).get("summary") or summary_by_key.get(key, "")
            try:
                histories = get_issue_changelog(key)
            except Exception:
                # If we hit a transient issue, skip this key (we'll still show others)
                continue

            day_moves: List[Tuple[str, str]] = []
            for h in histories:
                created = h.get("created")
                if not created:
                    continue
                created_dt = parse_jira_dt(created).astimezone(LONDON)
                if not (day_start <= created_dt < day_end):
                    continue
                for item in h.get("items", []) or []:
                    if (item.get("field") or "").strip().lower() == "status":
                        frm = (item.get("fromString") or "").strip()
                        to = (item.get("toString") or "").strip()
                        if frm or to:
                            day_moves.append((frm, to))

            if not day_moves:
                continue

            frm0 = day_moves[0][0]
            to_last = day_moves[-1][1]
            collapsed.append((key, str(summ).strip(), frm0, to_last))

        if not collapsed:
            out_lines.append("• None")
        else:
            for key, summ, frm, to in collapsed[:12]:
                out_lines.append(f"• {issue_link(base, key)} – {summ}: *{frm}* → *{to}*")
            if len(collapsed) > 12:
                out_lines.append(f"• +{len(collapsed) - 12} more…")

    return "\n".join(out_lines)


def post_to_chat(text: str):
    webhook = req_env("CHAT_WEBHOOK_URL")
    r = request_with_retry(
        "POST",
        webhook,
        auth=("",""),  # not used for webhooks
        headers={"Content-Type": "application/json"},
        json_body={"text": text},
        timeout=30,
        max_attempts=4,
    )
    # webhook returns 200/204; ignore body


def build_digest() -> str:
    # Safe env presence check (no values printed)
    print("[env] JIRA_BASE_URL =", present("JIRA_BASE_URL"))
    print("[env] JIRA_EMAIL =", present("JIRA_EMAIL"))
    print("[env] JIRA_API_TOKEN =", present("JIRA_API_TOKEN"))
    print("[env] CHAT_WEBHOOK_URL =", present("CHAT_WEBHOOK_URL"))
    print("[env] SPRINT_ANCHOR_DATE =", present("SPRINT_ANCHOR_DATE"))
    print("[env] TEAM_ACCOUNT_IDS =", "SET" if os.environ.get("TEAM_ACCOUNT_IDS", "").strip() else "MISSING (will count all worklogs)")

    base = jira_base_url()
    # Auth sanity
    me = jira_get("/rest/api/3/myself")
    print(f"[sanity] Auth OK. API user={me.get('displayName')} accountId={me.get('accountId')}")

    project_key = opt_env("PROJECT_KEY", "SPD").strip()

    # Hard-coded Story Point field (Story point estimate)
    sp_field = "customfield_10016"

    anchor = date.fromisoformat(req_env("SPRINT_ANCHOR_DATE"))
    today = datetime.now(LONDON).date()

    window = compute_sprint_window(anchor, today)
    start_dt, end_dt = sprint_datetime_bounds(window)

    # 1) Fetch sprint issues
    issues = fetch_sprint_issues(project_key, sp_field)
    print(f"[debug] sprint_window={window.start.isoformat()}..{window.end.isoformat()} issues_in_sprint={len(issues)} sp_field={sp_field}")

    # 2) Worklogs in sprint window (filtered by TEAM_ACCOUNT_IDS if present)
    team_ids_raw = os.environ.get("TEAM_ACCOUNT_IDS", "").strip()
    allowed_ids: Optional[Set[str]] = None
    if team_ids_raw:
        allowed_ids = {x.strip() for x in team_ids_raw.split(",") if x.strip()}

    # Only fetch worklogs for issues where Jira says some time was spent at all
    # (still imperfect, but reduces API calls a lot)
    worklogs_in_sprint_by_key: Dict[str, List[Dict[str, Any]]] = {}
    for it in issues:
        key = it.get("key")
        if not key:
            continue
        if extract_timespent_total_seconds(it) <= 0:
            continue

        wls = get_issue_worklogs(key)
        in_window = filter_worklogs_in_window(wls, start_dt, end_dt, allowed_ids)
        if in_window:
            worklogs_in_sprint_by_key[key] = in_window

    # 3) Build message
    msg_lines: List[str] = []
    msg_lines.append("📈 *Sprint Health Digest*")
    msg_lines.append(f"Project: *{project_key}*")
    msg_lines.append("")

    # Capacity vs Commitment + breakdown
    msg_lines.append(build_capacity_commitment_section(
        issues_in_sprint=issues,
        worklogs_in_sprint_by_key=worklogs_in_sprint_by_key,
        sp_field=sp_field,
        window=window,
        today=today
    ))

    # Daily history
    msg_lines.append("")
    msg_lines.append(build_daily_history(
        issues_in_sprint=issues,
        worklogs_in_sprint_by_key=worklogs_in_sprint_by_key,
        window=window,
        today=today
    ))

    return "\n".join(msg_lines)


if __name__ == "__main__":
    if not should_run_now():
        print("Not 9am London (or weekend) — exiting.")
        sys.exit(0)

    digest = build_digest()
    post_to_chat(digest)
    print("Digest sent ✅")
