# status_changes_digest.py
#
# Jira -> Google Chat: Status Changes Digest
#
# Goal:
#   Every couple of hours, post ONLY status changes in the last N hours.
#   Format:
#     User Name changed Task Name (clickable) from X to Y
#
# Behaviour:
#   - Status changes ONLY (no comments / edits / SP changes)
#   - Collapses multiple status changes on the same issue into one line: first -> last
#   - If there are NO status changes in the window: POST NOTHING
#
# Required env vars (GitHub Secrets):
#   JIRA_BASE_URL        e.g. https://sugatitravel.atlassian.net
#   JIRA_EMAIL
#   JIRA_API_TOKEN
#   CHAT_WEBHOOK_URL
#
# Optional env vars:
#   PROJECT_KEY          default "SPD"
#   LOOKBACK_HOURS       default "2"
#   MAX_ISSUES           default "100"   (caps how many updated issues we inspect each run)
#   ENFORCE_WEEKDAYS     true|false (default false) - if true, exits on Sat/Sun (London time)
#
# Notes:
# - Uses JQL updated >= -Nh AND sprint in openSprints() to keep it relevant.
# - Fetches changelog per issue to reliably detect status transitions + author.

from __future__ import annotations

import os
import sys
import time
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple


LONDON = ZoneInfo("Europe/London")


# ----------------------------
# Env helpers
# ----------------------------

def req_env(name: str) -> str:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return v.strip()


def opt_env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if (v is None or v.strip() == "") else v.strip()


def as_bool(name: str, default: str = "false") -> bool:
    raw = os.environ.get(name, default).strip().lower()
    return raw in ("1", "true", "yes", "y", "on")


def parse_jira_dt(dt_str: str) -> datetime:
    # Jira: 2026-02-21T09:15:22.123+0000
    # Py:   2026-02-21T09:15:22.123+00:00
    s = dt_str
    if len(s) >= 5 and (s[-5] in ["+", "-"]) and s[-2:].isdigit():
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s)


# ----------------------------
# HTTP retry/backoff
# ----------------------------

def _raise(r: requests.Response):
    if r.ok:
        return
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
    sleep_s = 1.0
    last_exc: Optional[Exception] = None

    for _attempt in range(1, max_attempts + 1):
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
                wait = float(ra) if ra and ra.isdigit() else sleep_s
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
# Jira API
# ----------------------------

def jira_base_url() -> str:
    return req_env("JIRA_BASE_URL").rstrip("/")


def jira_auth() -> Tuple[str, str]:
    return (req_env("JIRA_EMAIL"), req_env("JIRA_API_TOKEN"))


def jira_get(path: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{jira_base_url()}{path}"
    r = request_with_retry(
        "GET",
        url,
        auth=jira_auth(),
        headers={"Accept": "application/json"},
        params=params,
    )
    return r.json()


def jira_post(path: str, *, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{jira_base_url()}{path}"
    r = request_with_retry(
        "POST",
        url,
        auth=jira_auth(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json_body=payload,
    )
    return r.json()


def jira_search(jql: str, *, fields: List[str], max_results: int) -> List[Dict[str, Any]]:
    jql_clean = " ".join(line.strip() for line in jql.splitlines() if line.strip())
    payload = {
        "jql": jql_clean,
        "maxResults": max_results,
        "fields": fields,
    }
    data = jira_post("/rest/api/3/search/jql", payload=payload)
    return data.get("issues", []) or []


def jira_issue_changelog(issue_key: str, *, max_results: int = 200) -> List[Dict[str, Any]]:
    # paginate in case there are more than max_results entries
    start_at = 0
    out: List[Dict[str, Any]] = []
    while True:
        data = jira_get(
            f"/rest/api/3/issue/{issue_key}/changelog",
            params={"maxResults": max_results, "startAt": start_at},
        )
        values = data.get("values", []) or []
        out.extend(values)
        is_last = bool(data.get("isLast", False))
        if is_last or len(values) == 0:
            break
        start_at += len(values)
        if start_at > 2000:  # hard safety cap
            break
    return out


# ----------------------------
# Google Chat
# ----------------------------

def post_to_chat(text: str) -> None:
    webhook = req_env("CHAT_WEBHOOK_URL")
    request_with_retry(
        "POST",
        webhook,
        auth=("", ""),  # not used for webhooks
        headers={"Content-Type": "application/json"},
        json_body={"text": text},
        timeout=30,
        max_attempts=4,
    )


def issue_link_text(summary: str, key: str) -> str:
    # clickable task name
    # <url|text>
    return f"<{jira_base_url()}/browse/{key}|{summary} ({key})>"


# ----------------------------
# Core logic
# ----------------------------

def build_jql(project_key: str, lookback_hours: int) -> str:
    # Only open sprint issues that were updated recently.
    # (We still inspect changelog to ensure it was a status change.)
    return f"""
        project = {project_key}
        AND sprint in openSprints()
        AND updated >= -{lookback_hours}h
        ORDER BY updated DESC
    """


def extract_status_changes_in_window(
    histories: List[Dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
) -> List[Tuple[datetime, str, str, str]]:
    """
    Returns list of (created_dt, author_name, from_status, to_status) sorted by created_dt.
    """
    moves: List[Tuple[datetime, str, str, str]] = []

    for h in histories:
        created = h.get("created")
        if not created:
            continue
        created_dt = parse_jira_dt(created).astimezone(LONDON)
        if not (window_start <= created_dt < window_end):
            continue

        author_name = (h.get("author", {}) or {}).get("displayName") or "Someone"
        for item in h.get("items", []) or []:
            if (item.get("field") or "").strip().lower() != "status":
                continue
            frm = (item.get("fromString") or "").strip()
            to = (item.get("toString") or "").strip()
            if frm or to:
                moves.append((created_dt, author_name, frm, to))

    moves.sort(key=lambda t: t[0])
    return moves


def main() -> int:
    if as_bool("ENFORCE_WEEKDAYS", "false"):
        now = datetime.now(LONDON)
        if now.weekday() >= 5:
            return 0

    project_key = opt_env("PROJECT_KEY", "SPD").strip()
    lookback_hours = int(opt_env("LOOKBACK_HOURS", "2"))
    max_issues = int(opt_env("MAX_ISSUES", "100"))

    now = datetime.now(LONDON)
    window_end = now
    window_start = now - timedelta(hours=lookback_hours)

    # Search issues updated recently in open sprint
    issues = jira_search(
        build_jql(project_key, lookback_hours),
        fields=["summary", "updated"],
        max_results=max_issues,
    )

    lines: List[str] = []

    for it in issues:
        key = it.get("key")
        if not key:
            continue
        summary = ((it.get("fields", {}) or {}).get("summary") or "").strip()
        if not summary:
            summary = key

        try:
            histories = jira_issue_changelog(key)
        except Exception:
            # If one issue fails, don't kill the digest
            continue

        moves = extract_status_changes_in_window(histories, window_start, window_end)
        if not moves:
            continue

        # Collapse: first -> last within the window
        first = moves[0]
        last = moves[-1]
        author_last = last[1]
        from_status = first[2] or "?"
        to_status = last[3] or "?"

        lines.append(f"• {author_last} changed {issue_link_text(summary, key)} from *{from_status}* to *{to_status}*")

    # If no status changes: post nothing
    if not lines:
        return 0

    header = f"🔁 *Sprint Status Changes (last {lookback_hours}h)*"
    msg = "\n".join([header, ""] + lines)
    post_to_chat(msg)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:
        print(f"ERROR: {e}")
        raise
