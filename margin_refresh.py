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
    "default": 62.50,  # Ian, Naval, and anyone else
}
OFFSHORE_RATE_NAMES = {"shivam", "kishika"}  # £20.00/hr
OFFSHORE_RATE = 20.00
OTHER_RATE_NAMES = {"becky", "laura"}  # £40.00/hr
OTHER_RATE = 40.00
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
    logs = (r.json() or {}).get("worklogs", [])
    for wl in logs:
        wl["_issue_key"] = issue_key
    return logs


def fetch_child_issues(epic_key: str) -> tuple[list[str], list[str]]:
    """
    Walk the whole tree beneath an Epic — direct children AND anything nested
    below them — and harvest linked SPD tickets along the way.

    Returns (scw2_keys_with_time, linked_spd_keys_with_time).

    Links come free: the search response already carries issuelinks, so no
    extra API calls are needed to find them. Only the linked SPD ticket itself
    is returned, not its own children — package tickets often cover work for
    more than one client.
    """

    def run_jql(jql: str, want_links: bool = False):
        keys, links, start_at = [], set(), 0
        while True:
            url = f"{JIRA_BASE_URL}/rest/api/3/search/jql"
            params = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": 100,
                "fields": "key,issuelinks" if want_links else "key",
            }
            r = requests.get(url, headers=jira_headers(), params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            batch = data.get("issues", [])
            for issue in batch:
                keys.append(issue["key"])
                if not want_links:
                    continue
                for link in (issue.get("fields") or {}).get("issuelinks") or []:
                    for side in ("outwardIssue", "inwardIssue"):
                        other = link.get(side)
                        if other and other.get("key", "").startswith("SPD-"):
                            links.add(other["key"])
            if not batch:
                break
            start_at += len(batch)
            if start_at >= data.get("total", 0):
                break
        return keys, links

    MAX_DEPTH = 6
    seen = {epic_key}
    descendants: list[str] = []
    spd_links: set[str] = set()
    frontier = [epic_key]
    depth = 0

    # The epic's own links count too.
    try:
        _, epic_links = run_jql(f"key = {epic_key}", want_links=True)
        spd_links |= epic_links
    except Exception as e:
        print(f"    [WARN] Could not read links on {epic_key}: {e}")

    while frontier and depth < MAX_DEPTH:
        found: list[str] = []
        if depth == 0:
            found, links = run_jql(
                f'"Epic Link" = {epic_key} OR parent = {epic_key}', want_links=True)
            spd_links |= links
        else:
            for i in range(0, len(frontier), 50):
                chunk = ",".join(frontier[i:i + 50])
                try:
                    f2, links = run_jql(f"parent in ({chunk})", want_links=True)
                    found.extend(f2)
                    spd_links |= links
                except Exception as e:
                    print(f"    [WARN] Could not fetch children at depth {depth}: {e}")
        new = [k for k in found if k not in seen]
        for k in new:
            seen.add(k)
            descendants.append(k)
        frontier = new
        depth += 1

    def only_with_time(keys: list[str]) -> list[str]:
        if not keys:
            return []
        out: list[str] = []
        for i in range(0, len(keys), 100):
            chunk = ",".join(keys[i:i + 100])
            try:
                got, _ = run_jql(f"key in ({chunk}) AND timespent > 0")
                out.extend(got)
            except Exception as e:
                print(f"    [WARN] timespent filter failed, checking all: {e}")
                return keys
        return out

    scw2 = only_with_time(descendants)
    spds = only_with_time(sorted(spd_links))
    print(f"    {len(descendants)} descendants ({len(scw2)} with time)"
          f" · {len(spd_links)} linked SPD ({len(spds)} with time)")
    return scw2, spds


# ──────────────────────────────────────────────
# Rate card logic
# ──────────────────────────────────────────────
def get_hourly_rate(display_name: str) -> float:
    """Return the hourly rate for a person based on their display name."""
    name_lower = (display_name or "").strip().lower()
    for keyword in LOWER_RATE_NAMES:
        if keyword in name_lower:
            return LOWER_RATE
    for keyword in OFFSHORE_RATE_NAMES:
        if keyword in name_lower:
            return OFFSHORE_RATE
    for keyword in OTHER_RATE_NAMES:
        if keyword in name_lower:
            return OTHER_RATE
    return RATE_CARD["default"]


def calculate_cost_from_worklogs(worklogs: list[dict]) -> tuple[float, float, dict, dict, dict]:
    """
    Calculate cost and hours from worklogs, applying the rate card per person.

    Returns (total_cost_gbp, total_hours, by_person, by_ticket, by_month) where
    the three dicts break the same figures down for the dashboard pop-up.
    """
    total_cost = 0.0
    total_seconds = 0
    by_person: dict = {}
    by_ticket: dict = {}
    by_month: dict = {}

    for wl in worklogs:
        seconds = int(wl.get("timeSpentSeconds") or 0)
        if seconds <= 0:
            continue

        author = wl.get("author") or {}
        display_name = (author.get("displayName") or "Unknown").strip()
        rate = get_hourly_rate(display_name)

        hours = seconds / 3600.0
        cost = hours * rate
        total_cost += cost
        total_seconds += seconds

        person = by_person.setdefault(
            display_name, {"hours": 0.0, "cost": 0.0, "rate": rate})
        person["hours"] += hours
        person["cost"] += cost

        ticket_key = wl.get("_issue_key") or wl.get("issueKey") or "unknown"
        ticket = by_ticket.setdefault(ticket_key, {"hours": 0.0, "cost": 0.0})
        ticket["hours"] += hours
        ticket["cost"] += cost

        started = (wl.get("started") or "")[:7]  # YYYY-MM
        if started:
            month = by_month.setdefault(started, {"hours": 0.0, "cost": 0.0})
            month["hours"] += hours
            month["cost"] += cost

    for bucket in (by_person, by_ticket, by_month):
        for v in bucket.values():
            v["hours"] = round(v["hours"], 3)
            v["cost"] = round(v["cost"], 2)

    return total_cost, total_seconds / 3600.0, by_person, by_ticket, by_month


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
    spd_claims: dict = {}  # SPD key -> [epic keys], to spot double counting

    for i, epic in enumerate(epics):
        key = epic["key"]
        fields = epic.get("fields", {})
        summary = (fields.get("summary") or "").strip()

        status_obj = fields.get("status") or {}
        status_name = (status_obj.get("name") or "Open").strip()
        dashboard_status = classify_status(status_name)

        amount = 0
        amount_val = fields.get(AMOUNT_FIELD_ID)
        if amount_val is not None:
            try:
                amount = float(amount_val)
            except (TypeError, ValueError):
                amount = 0

        sold_days = amount / 1000.0 if amount > 0 else 0

        print(f"  [{i+1}/{len(epics)}] {key} — {summary}")
        all_worklogs = []
        spd_worklogs = []
        spd_keys: list[str] = []

        try:
            all_worklogs.extend(fetch_worklogs(key))
        except Exception as e:
            print(f"    [WARN] Could not fetch worklogs for {key}: {e}")

        try:
            children, spd_keys = fetch_child_issues(key)
            for child_key in children:
                try:
                    all_worklogs.extend(fetch_worklogs(child_key))
                except Exception as e:
                    print(f"    [WARN] Could not fetch worklogs for {child_key}: {e}")
            for spd_key in spd_keys:
                spd_claims.setdefault(spd_key, []).append(key)
                try:
                    spd_worklogs.extend(fetch_worklogs(spd_key))
                except Exception as e:
                    print(f"    [WARN] Could not fetch worklogs for {spd_key}: {e}")
        except Exception as e:
            print(f"    [WARN] Could not fetch children for {key}: {e}")

        combined = all_worklogs + spd_worklogs
        total_cost, total_hours, by_person, by_ticket, by_month = (
            calculate_cost_from_worklogs(combined))
        spd_cost, spd_hours, _, _, _ = calculate_cost_from_worklogs(spd_worklogs)

        logged_days = total_hours / 8.0  # 8-hour day

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
            "by_person": by_person,
            "by_ticket": by_ticket,
            "by_month": by_month,
            "linked_spd": spd_keys,
            "spd_days": round(spd_hours / 8.0, 5),
            "spd_cost": round(spd_cost, 2),
        })

    shared = {k: v for k, v in spd_claims.items() if len(v) > 1}
    if shared:
        print("[WARN] SPD tickets linked from more than one epic — "
              "their time is counted against each:")
        for spd_key, epic_keys in sorted(shared.items()):
            print(f"  {spd_key} → {', '.join(epic_keys)}")

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
