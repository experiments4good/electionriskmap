#!/usr/bin/env python3
"""
State Bill Monitor for ElectionRiskMap.org
Runs daily via GitHub Actions. Checks tracked bills for status changes,
creates GitHub issues for review, tiered by urgency.

Approval workflow: comment "approved" on issue → apply-update bot fires.
"""

import json
import os
import sys
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import anthropic
import requests

# ── Config ────────────────────────────────────────────────────────────────────

BILLS_JSON   = Path("data/bills.json")
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
GH_TOKEN      = os.environ["GITHUB_TOKEN"]
GH_REPO       = os.environ.get("GITHUB_REPOSITORY", "experiments4good/electionriskmap")

TODAY = date.today().isoformat()

URGENCY_THRESHOLDS = {
    "URGENT":     14,   # ≤14 days to sine die
    "ACTIVE":     60,   # sine die within 60 days OR status changed
    "MONITORING": 9999, # longer sessions, no recent change
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def days_left(deadline_str):
    if not deadline_str:
        return None
    deadline = date.fromisoformat(deadline_str)
    return (deadline - date.today()).days


def urgency_level(bill):
    dl = days_left(bill.get("deadline"))
    if dl is not None:
        if dl <= URGENCY_THRESHOLDS["URGENT"]:
            return "URGENT"
        if dl <= URGENCY_THRESHOLDS["ACTIVE"]:
            return "ACTIVE"
    last_changed = bill.get("last_changed", "2020-01-01")
    days_since_change = (date.today() - date.fromisoformat(last_changed)).days
    if days_since_change <= 14:
        return "ACTIVE"
    return "MONITORING"


def fetch_bill_page(url):
    """Fetch raw text from a legislature URL for Claude to analyze."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "ElectionRiskMap-Monitor/1.0"})
        resp.raise_for_status()
        # Strip tags crudely — Claude can handle messy HTML
        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"\s{3,}", "\n", text)
        return text[:8000]  # Keep within context budget
    except Exception as e:
        return f"FETCH_ERROR: {e}"


def check_bill_with_claude(bill, page_text):
    """Ask Claude to compare current bill data against fetched page."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = f"""You are monitoring a state legislature bill for ElectionRiskMap.org.

Current tracked data for {bill['bill']} ({bill['state']}):
- Status: {bill['status']}
- Last changed: {bill['last_changed']}
- Summary: {bill['summary']}
- Session year: 2026
- Legislature URL: {bill['legislature_url']}

Today's date: {TODAY}

CRITICAL: This bill is from the **2026 legislative session only**. The page may contain data from multiple sessions (2024, 2025, 2026). You must IGNORE any data from sessions other than 2026. If you cannot confirm the data is from the 2026 session, respond with changed: false and confidence: low.

Legislature page text (may be partial or messy):
---
{page_text}
---

Has the bill status changed from what we have tracked? Look for:
- Committee votes or referrals
- Floor votes (passage or failure)
- Governor action (signed, vetoed, desk)
- New deadlines or amendments
- Bill being tabled, withdrawn, or killed

Respond ONLY in this exact JSON format (no markdown, no explanation):
{{
  "changed": true or false,
  "new_status": "updated status string, or same as current if unchanged",
  "change_summary": "1-2 sentence plain English description of what changed, or null if unchanged",
  "confidence": "high / medium / low",
  "dead": true or false
}}"""

    resp = client.messages.create(
        model="claude-opus-4-5-20251101",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = resp.content[0].text.strip()
    # Strip any accidental markdown fences
    raw = re.sub(r"```json|```", "", raw).strip()
    return json.loads(raw)


def ensure_labels():
    """Create required labels if they don't exist."""
    required = [
        {"name": "state-bill", "color": "0075ca", "description": "State legislature bill tracked by ERM"},
        {"name": "urgent",     "color": "d93f0b", "description": "Sine die ≤14 days"},
        {"name": "active",     "color": "e4e669", "description": "Sine die ≤60 days or recently changed"},
        {"name": "monitoring", "color": "cfd3d7", "description": "Long session, no recent change"},
    ]
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    existing_resp = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/labels",
        headers=headers,
        params={"per_page": 100}
    )
    existing_names = {l["name"] for l in existing_resp.json()} if existing_resp.ok else set()

    for label in required:
        if label["name"] not in existing_names:
            r = requests.post(
                f"https://api.github.com/repos/{GH_REPO}/labels",
                headers=headers,
                json=label
            )
            if r.ok:
                print(f"  ✅ Created label: {label['name']}")
            else:
                print(f"  ⚠️  Could not create label '{label['name']}': {r.text}")


def create_github_issue(bill, check_result, level):
    """Open a GitHub issue for human review before applying update."""
    dl = days_left(bill.get("deadline"))
    deadline_str = f" · Sine die {bill['deadline']} ({dl}d left)" if dl is not None else ""

    title = f"[{level}] {bill['state']} {bill['bill']} status update"

    body = f"""## Bill Status Change Detected

**Bill:** {bill['bill']} ({bill['state_name']})
**Type:** {bill['type'].upper()} — Test {bill['test']}
**Urgency:** {level}{deadline_str}

### Change
**Previous:** {bill['status']}
**Detected:** {check_result['new_status']}

**Summary:** {check_result.get('change_summary', 'No summary provided')}

**Confidence:** {check_result.get('confidence', 'unknown')}

### Action Required
Review the change, then comment **`approved`** on this issue to apply the update.

The apply-update bot will:
1. Update `data/bills.json` with the new status and today's date
2. Rebuild the relevant section of `index.html`
3. Open a draft PR for final deploy review

**Legislature page:** {bill.get('legislature_url', 'N/A')}
**Bill ID:** `{bill['id']}`

---
*Generated by state-monitor.yml on {TODAY}*
"""

    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
    }

    label_map = {
        "URGENT":     "urgent",
        "ACTIVE":     "active",
        "MONITORING": "monitoring",
    }

    # Check if open issue already exists for this bill
    search_resp = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/issues",
        headers=headers,
        params={"state": "open", "labels": "state-bill"},
    )
    existing = search_resp.json() if search_resp.ok else []
    for issue in existing:
        if bill["id"] in issue.get("title", "") or bill["bill"] in issue.get("title", ""):
            print(f"  ⚠️  Open issue already exists for {bill['id']}, skipping.")
            return

    issue_resp = requests.post(
        f"https://api.github.com/repos/{GH_REPO}/issues",
        headers=headers,
        json={
            "title": title,
            "body": body,
            "labels": ["state-bill", label_map.get(level, "monitoring")],
        }
    )

    if issue_resp.ok:
        print(f"  ✅ Issue created: {issue_resp.json()['html_url']}")
    else:
        print(f"  ❌ Issue creation failed: {issue_resp.status_code} {issue_resp.text}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    data = json.loads(BILLS_JSON.read_text())
    bills = [b for b in data["bills"] if b.get("active", True)]

    print(f"🗓️  State Bill Monitor — {TODAY}")
    print(f"📋 Checking {len(bills)} tracked bills\n")
    print("🏷️  Ensuring labels exist...")
    ensure_labels()

    changed_count = 0

    for bill in bills:
        level = urgency_level(bill)
        dl = days_left(bill.get("deadline"))
        deadline_display = f" [{dl}d left]" if dl is not None else ""
        print(f"→ {bill['id']} ({level}{deadline_display})")

        # Skip low-priority bills that haven't changed recently unless URGENT
        if level == "MONITORING":
            print(f"  ⏭️  Low priority, skipping detailed check")
            continue

        page_text = fetch_bill_page(bill["legislature_url"])
        if page_text.startswith("FETCH_ERROR"):
            print(f"  ⚠️  Fetch failed: {page_text}")
            continue

        try:
            result = check_bill_with_claude(bill, page_text)
        except Exception as e:
            print(f"  ❌ Claude check failed: {e}")
            continue

        if result.get("changed"):
            changed_count += 1
            print(f"  📣 CHANGE DETECTED: {result['new_status']}")
            create_github_issue(bill, result, level)
        elif result.get("dead"):
            print(f"  💀 Bill appears dead — flagging for manual review")
            result["change_summary"] = "Bill appears to have died in committee or been withdrawn."
            create_github_issue(bill, result, "ACTIVE")
        else:
            print(f"  ✓  No change (confidence: {result.get('confidence', '?')})")

    print(f"\n📊 Done. {changed_count} change(s) detected out of {len(bills)} active bills.")

    # Update last_checked date in bills.json
    data["last_checked"] = TODAY
    BILLS_JSON.write_text(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
