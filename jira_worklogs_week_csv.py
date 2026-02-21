#!/usr/bin/env python3
"""
jira_worklogs_week_csv.py

Pull Jira Cloud worklogs for a fixed date range (by worklog.started date)
and export a simple CSV you can inspect.

Default range in this file is 2024-12-09 to 2024-12-13 inclusive.

Requirements:
  pip install requests pandas

Auth:
  Jira Cloud email + API token (basic auth)

Notes:
- Worklog comments in Jira Cloud are Atlassian Document Format (ADF) and can be nested.
  This script extracts a best-effort plain text version.
- Author email is often not available due to privacy; we avoid relying on it.
"""

import os
import time
import logging
from datetime import datetime, date, timezone
from typing import Any, Dict, List, Optional

import requests
import pandas as pd
from requests.auth import HTTPBasicAuth

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# -------------------------
# CONFIG (edit these)
# -------------------------
START_DATE = date(2024, 12, 9)   # inclusive
END_DATE   = date(2024, 12, 13)  # inclusive

# Your Jira projects
DEFAULT_PROJECT_KEYS = ["SPD", "SCW2", "SSH", "ONBOARDING"]

MAX_RESULTS = 50
SLEEP_SECONDS = 0.25  # be polite to rate limits

WORK_TYPE_BY_PROJECT_KEY = {
    "SPD": "Product Dev",
    "SCW2": "Pro Services",
    "SSH": "Support",
    "ONBOARDING": "Onboarding",
}


def to_utc_z(dt: datetime) -> str:
    """Return ISO 8601 UTC with Z suffix (Salesforce-friendly)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt_utc = dt.astimezone(timezone.utc)
    return dt_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_jira_datetime(value: str) -> datetime:
    """
    Jira timestamps are often like: 2024-12-10T14:32:00.000+0000
    Normalise +0000 -> +00:00 so datetime.fromisoformat can parse.
    """
    if value.endswith("Z"):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Convert +0000 -> +00:00
    if len(value) >= 5 and (value[-5] in ["+", "-"]) and value[-2:] != ":00":
        value = value[:-2] + ":" + value[-2:]
    return datetime.fromisoformat(value)


def adf_to_text(node: Any) -> str:
    """Convert Atlassian Document Format (ADF) to plain text (best effort)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(filter(None, (adf_to_text(n) for n in node))).strip()
    if isinstance(node, dict):
        node_type = node.get("type")
        if node_type == "text":
            return node.get("text", "")
        content = node.get("content")
        text = adf_to_text(content)
        if node_type in {"paragraph", "heading", "blockquote"} and text:
            return text + "\n"
        return text
    return ""


def jira_get(session: requests.Session, url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resp = session.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def get_issues_for_project(session: requests.Session, jira_base: str, project_key: str, start_date: date) -> List[Dict[str, Any]]:
    """
    Fetch issues updated since start_date.
    Practical limiter: entering a worklog updates the issue in Jira.
    """
    issues: List[Dict[str, Any]] = []
    start_at = 0

    jql = f'project = "{project_key}" AND updated >= {start_date.strftime("%Y-%m-%d")}'
    fields = "key,id,project,summary,issuetype,status"

    while True:
        url = f"{jira_base}/rest/api/3/search"
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": MAX_RESULTS,
            "fields": fields,
        }
        data = jira_get(session, url, params=params)
        batch = data.get("issues", [])
        issues.extend(batch)

        total = data.get("total", 0)
        if start_at + MAX_RESULTS >= total:
            break
        start_at += MAX_RESULTS
        time.sleep(SLEEP_SECONDS)

    logging.info(f"{project_key}: fetched {len(issues)} issues (updated since {start_date})")
    return issues


def get_worklogs_for_issue(session: requests.Session, jira_base: str, issue_key: str) -> List[Dict[str, Any]]:
    """Fetch all worklogs for an issue (paginated)."""
    worklogs: List[Dict[str, Any]] = []
    start_at = 0

    while True:
        url = f"{jira_base}/rest/api/3/issue/{issue_key}/worklog"
        params = {"startAt": start_at, "maxResults": MAX_RESULTS}
        data = jira_get(session, url, params=params)

        batch = data.get("worklogs", [])
        worklogs.extend(batch)

        total = data.get("total", 0)
        if start_at + MAX_RESULTS >= total:
            break
        start_at += MAX_RESULTS
        time.sleep(SLEEP_SECONDS)

    return worklogs


def within_range(worklog_started: datetime, start: date, end: date) -> bool:
    d = worklog_started.date()
    return start <= d <= end


def main() -> None:
    jira_domain = input("Enter Jira domain (e.g. your-domain.atlassian.net): ").strip()
    email = input("Enter Jira email: ").strip()
    api_token = os.getenv("JIRA_API_TOKEN") or input("Enter Jira API token (or set JIRA_API_TOKEN env var): ").strip()

    raw = input(f"Enter project keys comma-separated (default {','.join(DEFAULT_PROJECT_KEYS)}): ").strip()
    project_keys = [k.strip() for k in raw.split(",") if k.strip()] if raw else DEFAULT_PROJECT_KEYS

    jira_base = f"https://{jira_domain}"
    session = requests.Session()
    session.auth = HTTPBasicAuth(email, api_token)
    session.headers.update({"Accept": "application/json"})

    rows: List[Dict[str, Any]] = []

    for project_key in project_keys:
        issues = get_issues_for_project(session, jira_base, project_key, START_DATE)

        for issue in issues:
            issue_key = issue.get("key")
            issue_id = issue.get("id")
            issue_fields = issue.get("fields", {}) or {}
            project = issue_fields.get("project", {}) or {}
            project_key_from_issue = project.get("key") or project_key

            work_type = WORK_TYPE_BY_PROJECT_KEY.get(project_key_from_issue, "Unknown")

            try:
                worklogs = get_worklogs_for_issue(session, jira_base, issue_key)
            except requests.RequestException as e:
                logging.error(f"Failed worklogs for {issue_key}: {e}")
                continue

            for wl in worklogs:
                started_raw = wl.get("started")
                if not started_raw:
                    continue

                try:
                    started_dt = parse_jira_datetime(started_raw)
                except Exception:
                    logging.warning(f"Could not parse worklog.started '{started_raw}' for {issue_key}")
                    continue

                if not within_range(started_dt, START_DATE, END_DATE):
                    continue

                author = wl.get("author", {}) or {}
                comment_text = adf_to_text(wl.get("comment")).strip()

                created_raw = wl.get("created")
                updated_raw = wl.get("updated")

                created_dt = parse_jira_datetime(created_raw) if created_raw else None
                updated_dt = parse_jira_datetime(updated_raw) if updated_raw else None

                rows.append({
                    # Simple “look at it” columns
                    "WORK_TYPE": work_type,
                    "PROJECT_KEY": project_key_from_issue,
                    "JIRA_ISSUE_KEY": issue_key,
                    "JIRA_ISSUE_ID": str(issue_id or ""),

                    "JIRA_WORKLOG_ID": str(wl.get("id", "")),
                    "AUTHOR_ACCOUNT_ID": str(author.get("accountId", "")),
                    "AUTHOR_DISPLAY_NAME": author.get("displayName", ""),

                    # SF-friendly timestamps + canonical duration unit
                    "WORKLOG_STARTED_AT_UTC": to_utc_z(started_dt),
                    "TIME_SPENT_SECONDS": int(wl.get("timeSpentSeconds") or 0),

                    "WORKLOG_COMMENT": comment_text,

                    "WORKLOG_CREATED_AT_UTC": to_utc_z(created_dt) if created_dt else "",
                    "WORKLOG_UPDATED_AT_UTC": to_utc_z(updated_dt) if updated_dt else "",
                })

            time.sleep(SLEEP_SECONDS)

    if not rows:
        logging.info("No worklogs found for the date range.")
        return

    df = pd.DataFrame(rows)
    df = df.sort_values(by=["WORKLOG_STARTED_AT_UTC", "PROJECT_KEY", "JIRA_ISSUE_KEY", "AUTHOR_DISPLAY_NAME"])

    filename = f"jira_worklogs_{START_DATE.isoformat()}_{END_DATE.isoformat()}.csv"
    df.to_csv(filename, index=False, encoding="utf-8-sig")
    logging.info(f"Saved {len(df)} rows to {filename}")


if __name__ == "__main__":
    main()
