"""
Premium Support Overview — Data Refresh
========================================
Pulls SSH (Sugati Support Helpdesk) tickets created this financial year,
grouped by JSM Organization, with time from the SSH tickets themselves
PLUS any linked SCW2/SPD tickets and their descendants.

Writes support.json alongside data.json. Salesforce contract amounts are
NOT in this file — the artifact bakes those in from the Support Monthly
Opps Won report at build time, which is also where funded vs unfunded
is decided.

Rules (v2, agreed 21 Sep 2026):
  - Cohort: every SSH ticket created on/after the window start (1 Apr 2025,
    two financial years), ALL orgs — no Support Contract filter; contract
    matching happens at build time from the Salesforce reports.
  - Time: WORKLOG-DATE based. Only worklogs started on/after the window
    start count, on SSH tickets and linked trees alike, so time can be
    matched to contract coverage periods at build time.
  - Linked SCW2/SPD: the linked ticket AND its descendants.
  - Linked SCW2 time is tagged with its root epic and whether that epic
    has an Amount (i.e. is already counted in the PS margin view).

Reuses the rate card, worklog fetch and cost logic from margin_refresh.py.

Environment variables required (same as margin_refresh.py):
  JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN
Optional:
  WINDOW_START — override the window start, e.g. 2025-04-01
"""

import os
import json
import datetime as dt
from zoneinfo import ZoneInfo
import requests

from margin_refresh import (
    JIRA_BASE_URL,
    jira_headers,
    fetch_worklogs,
    calculate_cost_from_worklogs,
)

TZ = ZoneInfo("Europe/London")

SSH_PROJECT = "SSH"
LINKED_PROJECTS = ("SCW2", "SPD")
AMOUNT_FIELD_ID = "customfield_10640"     # Amount on SCW2 epics
EPIC_LINK_FIELD_ID = "customfield_10014"  # Epic Link
MAX_DEPTH = 5


def window_start() -> str:
    """1 April of the PREVIOUS FY (two-year window), unless overridden."""
    override = os.environ.get("WINDOW_START", "").strip()
    if override:
        return override
    today = dt.date.today()
    year = today.year if today.month >= 4 else today.year - 1
    return f"{year - 1}-04-01"


# ──────────────────────────────────────────────
# Jira API helpers
# ──────────────────────────────────────────────
def search_jql(jql: str, fields: str) -> list[dict]:
    """
    Paginated search against /rest/api/3/search/jql.
    NOTE: this endpoint paginates with nextPageToken, not startAt/total.
    """
    issues: list[dict] = []
    token = None
    while True:
        params = {"jql": jql, "maxResults": 100, "fields": fields}
        if token:
            params["nextPageToken"] = token
        r = requests.get(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            headers=jira_headers(), params=params, timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        issues.extend(data.get("issues", []))
        token = data.get("nextPageToken")
        if not token or data.get("isLast"):
            break
    return issues


def resolve_field_ids() -> tuple[str, str | None]:
    """Find the Organizations (and, if present, Support Contract) field ids."""
    r = requests.get(f"{JIRA_BASE_URL}/rest/api/3/field",
                     headers=jira_headers(), timeout=30)
    r.raise_for_status()
    orgs_id, support_id = None, None
    for f in r.json():
        name = (f.get("name") or "").strip().lower()
        if name == "organizations":
            orgs_id = f["id"]
        elif name == "support contract":
            support_id = f["id"]
    if not orgs_id:
        raise RuntimeError("Could not find the Organizations field id")
    print(f"[INFO] Organizations field: {orgs_id}"
          f" · Support Contract field: {support_id or 'not found'}")
    return orgs_id, support_id


def fetch_org_directory() -> dict[str, str]:
    """id -> name map from the JSM organization directory (fallback for
    orgs that come back as bare ids on the issue)."""
    directory: dict[str, str] = {}
    start = 0
    while True:
        r = requests.get(
            f"{JIRA_BASE_URL}/rest/servicedeskapi/organization",
            headers=jira_headers(),
            params={"start": start, "limit": 50}, timeout=30,
        )
        if r.status_code != 200:
            print(f"[WARN] Org directory unavailable ({r.status_code})")
            return directory
        data = r.json()
        for org in data.get("values", []):
            directory[str(org.get("id"))] = org.get("name") or str(org.get("id"))
        if data.get("isLastPage", True):
            break
        start += len(data.get("values", []))
    return directory


def org_names(raw, directory: dict[str, str]) -> list[str]:
    """Normalise the Organizations field value to a list of names."""
    names = []
    for entry in raw or []:
        if isinstance(entry, dict):
            names.append(entry.get("name") or directory.get(
                str(entry.get("id")), str(entry.get("id"))))
        else:
            names.append(directory.get(str(entry), str(entry)))
    return [n for n in names if n]


# ──────────────────────────────────────────────
# Linked-ticket tree walk
# ──────────────────────────────────────────────
def walk_descendants(roots: list[str]) -> tuple[dict[str, str], dict[str, int]]:
    """
    Walk the trees beneath the linked roots (parent OR Epic Link), all
    roots batched together. Returns:
      root_of  — descendant key -> its root linked ticket
      seconds  — key -> timespent seconds (roots and descendants with time)
    """
    root_of: dict[str, str] = {k: k for k in roots}
    seconds: dict[str, int] = {}
    parent_of: dict[str, str] = {}

    # timespent on the roots themselves
    for i in range(0, len(roots), 50):
        chunk = ",".join(roots[i:i + 50])
        for issue in search_jql(f"key in ({chunk})", "timespent"):
            ts = int((issue.get("fields") or {}).get("timespent") or 0)
            if ts > 0:
                seconds[issue["key"]] = ts

    frontier = list(roots)
    for depth in range(MAX_DEPTH):
        found: list[str] = []
        for i in range(0, len(frontier), 50):
            chunk = ",".join(frontier[i:i + 50])
            jql = f'parent in ({chunk}) OR "Epic Link" in ({chunk})'
            try:
                batch = search_jql(
                    jql, f"parent,{EPIC_LINK_FIELD_ID},timespent")
            except Exception as e:
                print(f"    [WARN] descendant walk failed at depth {depth}: {e}")
                continue
            for issue in batch:
                key = issue["key"]
                if key in root_of or key in parent_of:
                    continue
                fields = issue.get("fields") or {}
                parent = (fields.get("parent") or {}).get("key") \
                    or fields.get(EPIC_LINK_FIELD_ID)
                if not parent:
                    continue
                parent_of[key] = parent
                found.append(key)
                ts = int(fields.get("timespent") or 0)
                if ts > 0:
                    seconds[key] = ts
        if not found:
            break
        frontier = found

    # resolve every collected key back to its root
    def to_root(key: str) -> str | None:
        hops = 0
        while key not in root_of and hops < MAX_DEPTH + 2:
            key = parent_of.get(key, "")
            hops += 1
            if not key:
                return None
        return root_of.get(key)

    for key in list(parent_of):
        root = to_root(key)
        if root:
            root_of[key] = root

    return root_of, seconds


def resolve_root_epics(scw2_keys: list[str]) -> dict[str, str | None]:
    """For each linked SCW2 ticket, walk up to its epic (or None)."""
    epic_of: dict[str, str | None] = {}
    # orig ticket -> the key currently being examined on its parent chain
    current = {k: k for k in dict.fromkeys(scw2_keys)}
    for _ in range(4):
        if not current:
            break
        examine: dict[str, list[str]] = {}
        for orig, key in current.items():
            examine.setdefault(key, []).append(orig)
        info: dict[str, dict] = {}
        keys = list(examine)
        for i in range(0, len(keys), 50):
            chunk = ",".join(keys[i:i + 50])
            for issue in search_jql(
                    f"key in ({chunk})",
                    f"issuetype,parent,{EPIC_LINK_FIELD_ID}"):
                info[issue["key"]] = issue.get("fields") or {}
        nxt: dict[str, str] = {}
        for key, origs in examine.items():
            f = info.get(key, {})
            itype = ((f.get("issuetype") or {}).get("name") or "").lower()
            if itype == "epic":
                for o in origs:
                    epic_of[o] = key
                continue
            up = (f.get("parent") or {}).get("key") or f.get(EPIC_LINK_FIELD_ID)
            if up:
                for o in origs:
                    nxt[o] = up
            else:
                for o in origs:
                    epic_of[o] = None
        current = nxt
    for o, key in current.items():
        epic_of[o] = key  # best effort after max hops
    return epic_of


def paid_epic_set() -> set[str]:
    """SCW2 epics with an Amount — already counted in the PS margin view."""
    issues = search_jql(
        f'project = SCW2 AND issuetype = Epic AND "Amount" > 0', "key")
    return {i["key"] for i in issues}


# ──────────────────────────────────────────────
# Main pipeline
# ──────────────────────────────────────────────
def main():
    start_date = window_start()
    print(f"[START] Support refresh — {dt.datetime.now(TZ).isoformat()}"
          f" · window start {start_date}")

    orgs_field, support_field = resolve_field_ids()
    directory = fetch_org_directory()

    fields = f"summary,status,created,resolutiondate,timespent,issuelinks,{orgs_field}"
    if support_field:
        fields += f",{support_field}"

    tickets = search_jql(
        f"project = {SSH_PROJECT} AND created >= {start_date} ORDER BY created ASC",
        fields,
    )
    print(f"[INFO] {len(tickets)} SSH tickets created since {start_date}")

    warnings: list[str] = []
    orgs: dict[str, dict] = {}
    link_claims: dict[str, set[str]] = {}   # linked root -> orgs claiming it
    all_linked_roots: set[str] = set()

    for issue in tickets:
        f = issue.get("fields") or {}
        names = org_names(f.get(orgs_field), directory) or ["No organisation"]
        if len(names) > 1:
            warnings.append(
                f"{issue['key']} has multiple organisations: {', '.join(names)}"
                " — counted against each")

        linked = set()
        for link in f.get("issuelinks") or []:
            for side in ("outwardIssue", "inwardIssue"):
                other = link.get(side)
                if other and other.get("key", "").split("-")[0] in LINKED_PROJECTS:
                    linked.add(other["key"])
        all_linked_roots |= linked

        status = f.get("status") or {}
        contract = None
        if support_field:
            raw = f.get(support_field)
            contract = raw.get("value") if isinstance(raw, dict) else raw

        row = {
            "key": issue["key"],
            "summary": (f.get("summary") or "").strip(),
            "status": (status.get("name") or "").strip(),
            "status_category": ((status.get("statusCategory") or {})
                                .get("name") or "").strip(),
            "created": (f.get("created") or "")[:10],
            "resolved": (f.get("resolutiondate") or "")[:10] or None,
            "support_contract": contract,
            "timespent_seconds": int(f.get("timespent") or 0),
            "linked": sorted(linked),
        }
        for name in names:
            org = orgs.setdefault(name, {"tickets": [], "linked_roots": set()})
            org["tickets"].append(row)
            org["linked_roots"] |= linked
            for lk in linked:
                link_claims.setdefault(lk, set()).add(name)

    for lk, claimants in sorted(link_claims.items()):
        if len(claimants) > 1:
            warnings.append(
                f"{lk} is linked from tickets of more than one organisation"
                f" ({', '.join(sorted(claimants))}) — counted against each")

    # Linked trees, shared across all orgs then attributed per org
    print(f"[INFO] {len(all_linked_roots)} linked SCW2/SPD roots — walking trees")
    root_of, linked_seconds = walk_descendants(sorted(all_linked_roots))

    # Which linked SCW2 roots sit under a paid bespoke epic?
    paid = paid_epic_set()
    scw2_roots = [k for k in all_linked_roots if k.startswith("SCW2-")]
    epic_of = resolve_root_epics(scw2_roots)

    # Worklogs: SSH tickets with time + every linked-tree key with time
    worklog_cache: dict[str, list[dict]] = {}

    def logs(key: str) -> list[dict]:
        if key not in worklog_cache:
            try:
                got = fetch_worklogs(key)
                worklog_cache[key] = [
                    wl for wl in got
                    if (wl.get("started") or "")[:10] >= start_date]
            except Exception as e:
                print(f"    [WARN] worklogs failed for {key}: {e}")
                worklog_cache[key] = []
        return worklog_cache[key]

    keys_by_root: dict[str, list[str]] = {}
    for key, root in root_of.items():
        if key in linked_seconds:
            keys_by_root.setdefault(root, []).append(key)

    out_orgs = []
    for name in sorted(orgs):
        org = orgs[name]
        ssh_logs, linked_logs, linked_items = [], [], []

        for row in org["tickets"]:
            if row["timespent_seconds"] > 0:
                ssh_logs.extend(logs(row["key"]))

        for root in sorted(org["linked_roots"]):
            tree_keys = keys_by_root.get(root, [])
            tree_logs = []
            for key in tree_keys:
                tree_logs.extend(logs(key))
            cost, hours, _, _, _ = calculate_cost_from_worklogs(tree_logs)
            linked_logs.extend(tree_logs)
            epic = epic_of.get(root) if root.startswith("SCW2-") else None
            linked_items.append({
                "key": root,
                "epic": epic,
                "paid_epic": bool(epic and epic in paid),
                "tree_tickets": len(tree_keys),
                "hours": round(hours, 3),
                "cost": round(cost, 2),
            })

        ssh_cost, ssh_hours, ssh_pp, ssh_pt, ssh_pm = (
            calculate_cost_from_worklogs(ssh_logs))
        lk_cost, lk_hours, lk_pp, lk_pt, lk_pm = (
            calculate_cost_from_worklogs(linked_logs))
        combined_cost, combined_hours, c_pp, _, c_pm = (
            calculate_cost_from_worklogs(ssh_logs + linked_logs))
        paid_epic_cost = round(
            sum(li["cost"] for li in linked_items if li["paid_epic"]), 2)

        out_orgs.append({
            "name": name,
            "ticket_count": len(org["tickets"]),
            "tickets_with_time": sum(
                1 for r in org["tickets"] if r["timespent_seconds"] > 0),
            "tickets": org["tickets"],
            "ssh": {"hours": round(ssh_hours, 3), "cost": round(ssh_cost, 2),
                    "by_person": ssh_pp, "by_ticket": ssh_pt, "by_month": ssh_pm},
            "linked": {"hours": round(lk_hours, 3), "cost": round(lk_cost, 2),
                       "by_person": lk_pp, "by_ticket": lk_pt, "by_month": lk_pm,
                       "items": linked_items,
                       "paid_epic_cost": paid_epic_cost},
            "total_hours": round(combined_hours, 3),
            "total_cost": round(combined_cost, 2),
            "by_person": c_pp,
            "by_month": c_pm,
        })
        print(f"  {name}: {len(org['tickets'])} tickets · "
              f"{combined_hours:.1f}h · £{combined_cost:,.0f}"
              f"{' (incl. paid-epic £%s)' % paid_epic_cost if paid_epic_cost else ''}")

    output = {
        "generated_at": dt.datetime.now(TZ).isoformat(),
        "window_start": start_date,
        "summary": {
            "organisations": len(out_orgs),
            "tickets": sum(o["ticket_count"] for o in out_orgs),
            "total_hours": round(sum(o["total_hours"] for o in out_orgs), 1),
            "total_cost_gbp": round(sum(o["total_cost"] for o in out_orgs), 2),
        },
        "organisations": out_orgs,
        "warnings": warnings,
    }

    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(script_dir, "support.json")
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)

    print(f"[DONE] Wrote {len(out_orgs)} organisations to {output_path}")
    print(f"  Total: {output['summary']['total_hours']}h ·"
          f" £{output['summary']['total_cost_gbp']:,.0f}")
    for w in warnings:
        print(f"[WARN] {w}")


if __name__ == "__main__":
    main()
