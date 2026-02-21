# sprint_health_digest.py
#
# REQUIRED Secrets / env vars:
#   JIRA_BASE_URL
#   JIRA_EMAIL
#   JIRA_API_TOKEN
#   CHAT_WEBHOOK_URL
#   SPRINT_ANCHOR_DATE (YYYY-MM-DD)
#
# OPTIONAL (but recommended):
#   JIRA_STORY_POINTS_FIELD (default customfield_10016)

import os
import time
import requests
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

PROJECT_KEY = "SPD"

SPRINT_WORKDAYS = 9
SPRINT_GAP_DAYS = 1
HOURS_PER_DAY = 7

MAX_HISTORY_DAYS = 9
MAX_LINES = 6

# Jira rate-limit hardening
MAX_RETRIES = 6
BACKOFF_BASE_SECONDS = 2


def req_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v.strip()


def present(name: str) -> str:
    v = os.environ.get(name)
    return "SET" if (v is not None and v.strip() != "") else "MISSING"


def jira_auth():
    return (req_env("JIRA_EMAIL"), req_env("JIRA_API_TOKEN"))


def _raise_http(r: requests.Response):
    if r.ok:
        return
    snippet = (r.text or "")[:500]
    raise requests.HTTPError(f"{r.status_code} {r.reason} :: {snippet}")


def request_with_retry(method: str, url: str, **kwargs):
    """
    Retries on 429 and transient 5xx. Respects Retry-After when present.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        r = requests.request(method, url, auth=jira_auth(), timeout=30, **kwargs)

        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                sleep_s = int(retry_after)
            else:
                sleep_s = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            time.sleep(min(sleep_s, 60))
            continue

        if 500 <= r.status_code < 600:
            sleep_s = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            time.sleep(min(sleep_s, 60))
            continue

        _raise_http(r)
        return r

    # last response was not OK
    _raise_http(r)
    return r  # unreachable


def jira_get(url, **kwargs):
    r = request_with_retry("GET", url, headers={"Accept": "application/json"}, **kwargs)
    return r.json()


def jira_post(url, payload):
    r = request_with_retry(
        "POST",
        url,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=payload,
    )
    return r.json()


def jira_search(jql: str, fields=None, max_results=200):
    base = req_env("JIRA_BASE_URL").rstrip("/")
    url = f"{base}/rest/api/3/search/jql"

    payload = {
        "jql": " ".join(jql.split()),
        "maxResults": max_results,
        "fields": fields or ["summary", "status"],
    }

    return jira_post(url, payload).get("issues", [])


def issue_link(key: str) -> str:
    base = req_env("JIRA_BASE_URL").rstrip("/")
    return f"<{base}/browse/{key}|{key}>"


def parse_jira_dt(dt_str: str):
    if len(dt_str) >= 5 and dt_str[-5] in ["+", "-"]:
        dt_str = dt_str[:-2] + ":" + dt_str[-2:]
    return datetime.fromisoformat(dt_str)


def seconds_to_pretty(seconds: int) -> str:
    seconds = int(seconds or 0)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


# ----------------
# Sprint capacity
# ----------------
def count_workdays(start: date, end: date) -> int:
    d = start
    days = 0
    while d < end:
        if d.weekday() < 5:
            days += 1
        d += timedelta(days=1)
    return days


def remaining_capacity_hours() -> int:
    anchor = date.fromisoformat(req_env("SPRINT_ANCHOR_DATE"))
    today = datetime.now(LONDON).date()

    cycle = SPRINT_WORKDAYS + SPRINT_GAP_DAYS
    pos = count_workdays(anchor, today) % cycle

    if pos >= SPRINT_WORKDAYS:
        return 0

    remaining_days = SPRINT_WORKDAYS - pos
    return remaining_days * HOURS_PER_DAY


# ----------------
# Jira helpers
# ----------------
def get_worklogs(issue_key: str):
    base = req_env("JIRA_BASE_URL").rstrip("/")
    url = f"{base}/rest/api/3/issue/{issue_key}/worklog"
    return jira_get(url, params={"maxResults": 5000}).get("worklogs", [])


def get_changelog_histories(issue_key: str):
    base = req_env("JIRA_BASE_URL").rstrip("/")
    url = f"{base}/rest/api/3/issue/{issue_key}"
    return jira_get(url, params={"expand": "changelog"}).get("changelog", {}).get("histories", [])


def status_category_key(issue) -> str:
    """
    Jira statusCategory keys:
      - 'new'           => To Do
      - 'indeterminate' => In Progress
      - 'done'          => Done
    """
    f = issue.get("fields", {}) or {}
    st = f.get("status") or {}
    cat = st.get("statusCategory") or {}
    return (cat.get("key") or "").strip().lower()


def get_sp_hours(issue, sp_field: str) -> float:
    v = (issue.get("fields", {}) or {}).get(sp_field)
    return float(v) if isinstance(v, (int, float)) else 0.0


def sprint_issues_all(sp_field: str):
    # Grab ALL issues in the open sprint for summary totals + history selection
    fields = ["summary", "status", sp_field]
    jql = f"""
        project = {PROJECT_KEY}
        AND sprint in openSprints()
        ORDER BY Rank ASC
    """
    return jira_search(jql, fields=fields, max_results=500)


def keys_with_worklogs_on_day(day_offset: int):
    """
    day_offset=1 => yesterday
    Uses Jira relative functions so we don't need timezone math in JQL.
    """
    start = f"startOfDay(-{day_offset})"
    end = "startOfDay()" if day_offset == 0 else f"startOfDay(-{day_offset - 1})"
    jql = f"""
        project = {PROJECT_KEY}
        AND sprint in openSprints()
        AND worklogDate >= {start}
        AND worklogDate < {end}
    """
    issues = jira_search(jql, fields=["summary"], max_results=500)
    return {i["key"] for i in issues}


def keys_with_status_changes_on_day(day_offset: int):
    start = f"startOfDay(-{day_offset})"
    end = "startOfDay()" if day_offset == 0 else f"startOfDay(-{day_offset - 1})"
    jql = f"""
        project = {PROJECT_KEY}
        AND sprint in openSprints()
        AND status CHANGED DURING ({start}, {end})
    """
    issues = jira_search(jql, fields=["summary"], max_results=500)
    return {i["key"] for i in issues}


# ----------------
# History builder (rate-limit safe)
# ----------------
def build_daily_history(summary_by_key: dict[str, str]):
    today = datetime.now(LONDON).date()

    # Build list of last N working days and their offsets
    day_offsets = []
    d = today
    offset = 0
    while len(day_offsets) < MAX_HISTORY_DAYS:
        if d.weekday() < 5:
            # offset from today in whole days
            delta = (today - d).days
            # delta=0 => today, 1 => yesterday ...
            day_offsets.append((d, delta))
        d -= timedelta(days=1)

    blocks = []

    for day, delta in day_offsets:
        start = datetime.combine(day, datetime.min.time(), tzinfo=LONDON)
        end = start + timedelta(days=1)

        # Only fetch details for issues that actually had activity that day
        wl_keys = keys_with_worklogs_on_day(delta) if delta > 0 else set()
        st_keys = keys_with_status_changes_on_day(delta) if delta > 0 else set()
        active_keys = wl_keys.union(st_keys)

        worklog_totals: dict[str, int] = {}
        status_moves_by_key: dict[str, list[tuple[datetime, str, str]]] = {}

        # If no activity at all, keep the day but show "None"
        if active_keys:
            for key in sorted(active_keys):
                summary = summary_by_key.get(key, "").strip()

                # Worklogs (all authors)
                total_seconds = 0
                if key in wl_keys:
                    for wl in get_worklogs(key):
                        started = wl.get("started")
                        if not started:
                            continue
                        dt_local = parse_jira_dt(started).astimezone(LONDON)
                        if start <= dt_local < end:
                            total_seconds += int(wl.get("timeSpentSeconds", 0))
                if total_seconds:
                    worklog_totals[key] = total_seconds

                # Status changes
                if key in st_keys:
                    for h in get_changelog_histories(key):
                        created = parse_jira_dt(h["created"]).astimezone(LONDON)
                        if not (start <= created < end):
                            continue
                        for item in h.get("items", []):
                            if item.get("field") == "status":
                                frm = (item.get("fromString") or "").strip()
                                to = (item.get("toString") or "").strip()
                                status_moves_by_key.setdefault(key, []).append((created, frm, to))

        # Collapse status changes per issue/day
        collapsed_lines = []
        for key, moves in status_moves_by_key.items():
            moves_sorted = sorted(moves, key=lambda x: x[0])
            start_status = moves_sorted[0][1] or "?"
            end_status = moves_sorted[-1][2] or "?"
            hops = len(moves_sorted)

            summary = summary_by_key.get(key, "")
            extra = f" (+{hops} moves)" if hops > 1 else ""
            collapsed_lines.append(f"• {issue_link(key)} — {summary}: {start_status} → {end_status}{extra}")

        def hops_count(line: str) -> int:
            if "(+" in line and " moves)" in line:
                try:
                    inside = line.split("(+")[1].split(" moves)")[0]
                    return int(inside)
                except Exception:
                    return 0
            return 1

        collapsed_lines.sort(key=lambda s: (-hops_count(s), s))

        block = f"📅 {day.strftime('%A %d %b')}\n\n"

        block += "⏱ Work logged\n"
        if worklog_totals:
            sorted_items = sorted(worklog_totals.items(), key=lambda x: x[1], reverse=True)
            for k, sec in sorted_items[:MAX_LINES]:
                s = summary_by_key.get(k, "")
                block += f"• {issue_link(k)} — {s} ({seconds_to_pretty(sec)})\n"
            if len(sorted_items) > MAX_LINES:
                block += f"• +{len(sorted_items) - MAX_LINES} more…\n"
        else:
            block += "• None\n"

        block += "\n🔁 Status moves\n"
        if collapsed_lines:
            for line in collapsed_lines[:MAX_LINES]:
                block += f"{line}\n"
            if len(collapsed_lines) > MAX_LINES:
                block += f"• +{len(collapsed_lines) - MAX_LINES} more…\n"
        else:
            block += "• None\n"

        blocks.append(block.rstrip())

    return blocks


# ----------------
# Digest builder
# ----------------
def build_digest():
    sp_field = os.environ.get("JIRA_STORY_POINTS_FIELD", "customfield_10016").strip()
    issues = sprint_issues_all(sp_field)

    summary_by_key: dict[str, str] = {}
    for it in issues:
        k = it.get("key")
        f = it.get("fields", {}) or {}
        summary_by_key[k] = (f.get("summary") or "").strip()

    total_hours = 0.0
    done_hours = 0.0
    inprog_hours = 0.0
    todo_hours = 0.0

    for it in issues:
        h = get_sp_hours(it, sp_field)
        total_hours += h

        cat = status_category_key(it)
        if cat == "done":
            done_hours += h
        elif cat == "indeterminate":
            inprog_hours += h
        else:
            todo_hours += h

    remaining_hours = total_hours - done_hours

    capacity = float(remaining_capacity_hours())
    unstarted_over = todo_hours - capacity

    msg = "📊 *Sprint Health Digest*\n\n"

    msg += "🧮 Capacity vs Commitment (SP = hours)\n"
    msg += f"• Remaining capacity: {capacity:.0f}h\n"
    msg += f"• Total in sprint: {total_hours:.1f}h\n"
    msg += f"• Done: {done_hours:.1f}h\n"
    msg += f"• In progress: {inprog_hours:.1f}h\n"
    msg += f"• Unstarted (To Do): {todo_hours:.1f}h\n"
    msg += f"• Remaining (not Done): {remaining_hours:.1f}h\n"

    if unstarted_over > 0:
        msg += f"⚠ Unstarted over capacity by {unstarted_over:.1f}h\n"
    else:
        msg += f"✅ Unstarted within capacity (buffer {abs(unstarted_over):.1f}h)\n"

    msg += "\n──────────────────────────────\n\n"
    msg += "\n\n".join(build_daily_history(summary_by_key))

    return msg


def send_chat(text: str):
    r = requests.post(req_env("CHAT_WEBHOOK_URL"), json={"text": text}, timeout=30)
    if not r.ok:
        raise requests.HTTPError(f"Chat error {r.status_code}: {r.text[:500]}")


if __name__ == "__main__":
    print("[env] SPRINT_ANCHOR_DATE =", present("SPRINT_ANCHOR_DATE"))
    print("[env] JIRA_STORY_POINTS_FIELD =", present("JIRA_STORY_POINTS_FIELD"))

    digest = build_digest()
    send_chat(digest)
    print("Sprint digest sent ✅")
