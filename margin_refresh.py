"""
PS Margin Dashboard — Data Refresh
===================================
Pulls SCW2 Epic data (amount, worklogs) from the Jira REST API
and writes a fresh data.json for the margin dashboard.

Designed to run as a GitHub Action on a 12-hour cron.

Environment variables required:
  JIRA_BASE_URL    — e.g. https://sugatitravel.atlassian.net
  JIRA_EMAIL       — Jira account email
  JIRA_API_TOKEN   — Jira API token
"""

import os
import sys
import json
import base64
import datetime as dt
from zoneinfo import ZoneInfo
from collections import defaultdict
import requests

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
JIRA_BASE_URL = os.environ["JIRA_BASE_URL"].strip().rstrip("/")
JIRA_EMAIL = os.environ["JIRA_EMAIL"].strip()
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"].strip()

TZ = ZoneInfo("Europe/London")

# Amount custom field (discovered 4 Apr 2026)
AMOUNT_FIELD_ID = "customfield_10640"

# Rate card (£ per hour)
RATE_CARD = {
    "default": 62.50,  # Ian, Naval, Shivam, and anyone else
}
LOWER_RATE_NAMES = {"melvin", "constandina"}  # £18.18/hr
LOWER_RATE = 18.18

# Statuses → dashboard categories
STATUS_MAP_DONE = {"done", "closed", "resolved"}
STATUS_MAP_BACKLOG = {"backlog", "to do", "open", "custom work", "scoping"}

# Target margin
TARGET_MARGIN_PCT = 65.0


def jira_headers():
    token = base64.b64encode(
        f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode("utf-8")
    ).decode("utf-8")
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ──────────────────────────────────────────────
# Jira API helpers
# ──────────────────────────────────────────────
def search_epics() -> list[dict]:
    """
    Search for all SCW2 Epics that have an amount set.
    Returns raw Jira issue dicts.
    """
    jql = (
        'project = SCW2 AND issuetype = Epic AND "Amount" > 0 '
        "ORDER BY status ASC, key DESC"
    )
    issues = []
    start_at = 0

    while True:
        # Use the newer /search/jql endpoint (Jira deprecated /search with 410 Gone)
        url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": 100,
            "fields": f"summary,status,{AMOUNT_FIELD_ID}",
        }
        r = requests.get(url, headers=jira_headers(), params=params, timeout=60)
        r.raise_for_status()

        data = r.json()
        batch = data.get("issues", [])
        issues.extend(batch)

        if len(batch) == 0:
            break
        start_at += len(batch)
        total = data.get("total", 0)
        if start_at >= total:
            break

    print(f"[INFO] Found {len(issues)} SCW2 Epics with amount > 0")
    return issues


def fetch_worklogs(issue_key: str) -> list[dict]:
    """Fetch all worklogs for a single issue."""
    url = f"{JIRA_BASE_URL}/rest/api/3/issue/{issue_key}/worklog"
    r = requests.get(url, headers=jira_headers(), timeout=30)
    r.raise_for_status()
    return (r.json() or {}).get("worklogs", [])


def fetch_child_issues(epic_key: str) -> list[str]:
    """
    Find all child issues of an Epic (stories, tasks, sub-tasks, bugs).
    Returns list of issue keys.
    """
    jql = f'"Epic Link" = {epic_key} OR parent = {epic_key}'
    keys = []
    start_at = 0

    while True:
        url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
        params = {
            "jql": jql,
            "startAt": start_at,
            "maxResults": 100,
            "fields": "key",
        }
        r = requests.get(url, headers=jira_headers(), params=params, timeout=30)
        r.raise_for_status()

        data = r.json()
        batch = data.get("issues", [])
        keys.extend([i["key"] for i in batch])

        if len(batch) == 0:
            break
        start_at += len(batch)
        if start_at >= data.get("total", 0):
            break

    return keys


# ──────────────────────────────────────────────
# Rate card logic
# ──────────────────────────────────────────────
def get_hourly_rate(display_name: str) -> float:
    """Return the hourly rate for a person based on their display name."""
    name_lower = (display_name or "").strip().lower()
    for keyword in LOWER_RATE_NAMES:
        if keyword in name_lower:
            return LOWER_RATE
    return RATE_CARD["default"]


def calculate_cost_from_worklogs(worklogs: list[dict]) -> tuple[float, float]:
    """
    Calculate total cost and total hours from a list of worklogs,
    applying the rate card per person.

    Returns (total_cost_gbp, total_hours).
    """
    total_cost = 0.0
    total_seconds = 0

    for wl in worklogs:
        seconds = int(wl.get("timeSpentSeconds") or 0)
        if seconds <= 0:
            continue

        author = wl.get("author") or {}
        display_name = author.get("displayName") or ""
        rate = get_hourly_rate(display_name)

        hours = seconds / 3600.0
        total_cost += hours * rate
        total_seconds += seconds

    total_hours = total_seconds / 3600.0
    return total_cost, total_hours


# ──────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────
def classify_status(status_name: str) -> str:
    """Map Jira status to dashboard category."""
    s = status_name.strip().lower()
    if s in STATUS_MAP_DONE:
        return "Done"
    if s in STATUS_MAP_BACKLOG:
        return "Backlog"
    return "In Progress"


def build_dashboard_data() -> list[dict]:
    """Pull all data from Jira and compute margin for each Epic."""

    print(f"[INFO] Using Amount field: {AMOUNT_FIELD_ID}")
    epics = search_epics()

    results = []

    for i, epic in enumerate(epics):
        key = epic["key"]
        fields = epic.get("fields", {})
        summary = (fields.get("summary") or "").strip()

        # Status
        status_obj = fields.get("status") or {}
        status_name = (status_obj.get("name") or "Open").strip()
        dashboard_status = classify_status(status_name)

        # Amount (sold value in £)
        amount = 0
        amount_val = fields.get(AMOUNT_FIELD_ID)
        if amount_val is not None:
            try:
                amount = float(amount_val)
            except (TypeError, ValueError):
                amount = 0

        # Sold days (£1000 = 1 day)
        sold_days = amount / 1000.0 if amount > 0 else 0

        # Fetch worklogs from the Epic itself + all child issues
        print(f"  [{i+1}/{len(epics)}] {key} — {summary}")
        all_worklogs = []

        # Epic-level worklogs
        try:
            all_worklogs.extend(fetch_worklogs(key))
        except Exception as e:
            print(f"    [WARN] Could not fetch worklogs for {key}: {e}")

        # Child issue worklogs
        try:
            children = fetch_child_issues(key)
            for child_key in children:
                try:
                    all_worklogs.extend(fetch_worklogs(child_key))
                except Exception as e:
                    print(f"    [WARN] Could not fetch worklogs for {child_key}: {e}")
        except Exception as e:
            print(f"    [WARN] Could not fetch children for {key}: {e}")

        # Calculate cost and hours
        total_cost, total_hours = calculate_cost_from_worklogs(all_worklogs)
        logged_days = total_hours / 8.0  # 8-hour day

        # Margin
        has_data = total_hours > 0
        monetary_margin = amount - total_cost
        monetary_margin_pct = (
            (monetary_margin / amount * 100.0) if amount > 0 and has_data else
            100.0 if amount > 0 else 0.0
        )

        results.append({
            "key": key,
            "summary": summary,
            "status": dashboard_status,
            "amount": round(amount, 2),
            "sold_days": round(sold_days, 2),
            "logged_days": round(logged_days, 5),
            "total_cost": round(total_cost, 2),
            "monetary_margin": round(monetary_margin, 2),
            "monetary_margin_pct": round(monetary_margin_pct, 2),
            "has_data": has_data,
        })

    return results


def main():
    print(f"[START] Margin dashboard refresh — {dt.datetime.now(TZ).isoformat()}")

    data = build_dashboard_data()

    # Summary stats
    with_data = [d for d in data if d["has_data"]]
    total_sold = sum(d["amount"] for d in data)
    total_margin = sum(d["monetary_margin"] for d in with_data)
    avg_margin = (
        sum(d["monetary_margin_pct"] for d in with_data) / len(with_data)
        if with_data else 0
    )

    output = {
        "generated_at": dt.datetime.now(TZ).isoformat(),
        "target_margin_pct": TARGET_MARGIN_PCT,
        "summary": {
            "total_epics": len(data),
            "total_sold_gbp": round(total_sold, 2),
            "total_margin_gbp": round(total_margin, 2),
            "avg_margin_pct": round(avg_margin, 2),
        },
        "epics": data,
    }

    # Write to data.json in the same directory as this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(script_dir, "data.json")

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[DONE] Wrote {len(data)} epics to {output_path}")
    print(f"  Total sold: £{total_sold:,.0f}")
    print(f"  Total margin: £{total_margin:,.0f}")
    print(f"  Avg margin: {avg_margin:.1f}%")


if __name__ == "__main__":
    main()
