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
PAGE_TEXT_LIMIT = 30000
CHECK_NOTE_LIMIT = 200

TODAY = date.today().isoformat()

URGENCY_THRESHOLDS = {
    "URGENT":     14,   # ≤14 days to sine die
    "ACTIVE":     60,   # sine die within 60 days OR status changed
    "MONITORING": 9999, # longer sessions, no recent change
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def gh_repo():
    return os.environ.get("GITHUB_REPOSITORY", "experiments4good/electionriskmap")


def gh_headers():
    return {
        "Authorization": f"token {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
    }


def get_client():
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


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


def normalize_ws(text):
    return re.sub(r"\s+", " ", text).strip()


def html_to_text(html):
    """Drop non-content blocks and tags, collapse whitespace, cap the length."""
    for tag in ("script", "style", "nav", "header", "footer"):
        html = re.sub(
            rf"<{tag}(?=[\s>/])[^>]*>.*?</{tag}\s*>", " ", html,
            flags=re.DOTALL | re.IGNORECASE,
        )
    return normalize_ws(re.sub(r"<[^>]+>", " ", html))[:PAGE_TEXT_LIMIT]


def fetch_bill_page(url):
    """Return (page text, None) on success or (None, short reason) on failure."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "ElectionRiskMap-Monitor/1.0"})
        if resp.status_code >= 400:
            return None, f"fetch error: HTTP {resp.status_code}"
        return html_to_text(resp.text), None
    except Exception as e:
        return None, f"fetch error: {type(e).__name__}"


def check_bill_with_claude(client, bill, page_text):
    """Ask Claude to compare current bill data against fetched page.

    Raises ValueError if the answer is not a JSON object.
    """
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

Also report what the page itself confirms:
- bill_confirmed: true only if the page is clearly this bill number in this state
- session_confirmed: true only if the page shows the 2026 session
- latest_action_date: date of the latest recorded action shown on the page
- evidence: an exact quote, copied character for character from the page text, of the latest recorded action

If you cannot confirm the bill or the session, set changed to false and set bill_confirmed, session_confirmed, latest_action_date and evidence to false or null. Never guess.

Respond ONLY in this exact JSON format (no markdown, no explanation):
{{
  "changed": true or false,
  "new_status": "updated status string, or same as current if unchanged",
  "change_summary": "1-2 sentence plain English description of what changed, or null if unchanged",
  "confidence": "high / medium / low",
  "dead": true or false,
  "bill_confirmed": true or false,
  "session_confirmed": true or false,
  "latest_action_date": "YYYY-MM-DD, or null if not confirmed",
  "evidence": "exact quote from the page text, or null if not confirmed"
}}"""

    resp = client.messages.create(
        model="claude-opus-4-5-20251101",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = resp.content[0].text.strip()
    # Strip any accidental markdown fences
    raw = re.sub(r"```json|```", "", raw).strip()
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("answer is not a JSON object")
    return result


def verification_failure(result, page_text):
    """Return None if the answer is verified, otherwise the short reason it is not."""
    if result.get("bill_confirmed") is not True:
        return "wrong bill: not confirmed on page"
    if result.get("session_confirmed") is not True:
        return "session not confirmed"
    evidence = result.get("evidence")
    evidence = normalize_ws(evidence) if isinstance(evidence, str) else ""
    if not evidence or evidence not in normalize_ws(page_text):
        return "evidence not found"
    return None


def mark_verified(bill, result):
    parts = [result.get("latest_action_date"), normalize_ws(result["evidence"])]
    bill["last_checked"] = TODAY
    bill["check_status"] = "verified"
    bill["check_note"] = " ".join(str(p) for p in parts if p)[:CHECK_NOTE_LIMIT]


def mark_unchecked(bill, reason):
    bill["check_status"] = "unchecked"
    bill["check_note"] = reason[:CHECK_NOTE_LIMIT]
    print(f"  ⚠️  Unchecked: {bill['check_note']}")


def ensure_labels():
    """Create required labels if they don't exist."""
    required = [
        {"name": "state-bill", "color": "0075ca", "description": "State legislature bill tracked by ERM"},
        {"name": "urgent",     "color": "d93f0b", "description": "Sine die ≤14 days"},
        {"name": "active",     "color": "e4e669", "description": "Sine die ≤60 days or recently changed"},
        {"name": "monitoring", "color": "cfd3d7", "description": "Long session, no recent change"},
    ]
    headers = gh_headers()
    existing_resp = requests.get(
        f"https://api.github.com/repos/{gh_repo()}/labels",
        headers=headers,
        params={"per_page": 100}
    )
    existing_names = {l["name"] for l in existing_resp.json()} if existing_resp.ok else set()

    for label in required:
        if label["name"] not in existing_names:
            r = requests.post(
                f"https://api.github.com/repos/{gh_repo()}/labels",
                headers=headers,
                json=label
            )
            if r.ok:
                print(f"  ✅ Created label: {label['name']}")
            else:
                print(f"  ⚠️  Could not create label '{label['name']}': {r.text}")


def open_state_bill_issues():
    """List every open state-bill issue, following pagination. Raises on any failure."""
    url = f"https://api.github.com/repos/{gh_repo()}/issues"
    params = {"state": "open", "labels": "state-bill", "per_page": 100}
    issues = []
    while url:
        resp = requests.get(url, headers=gh_headers(), params=params, timeout=15)
        if not resp.ok:
            raise RuntimeError(f"issue list failed: HTTP {resp.status_code}")
        issues.extend(resp.json())
        # The next link already carries the query string
        url, params = resp.links.get("next", {}).get("url"), None
    return issues


def is_duplicate_issue(issue, bill_id):
    """True only if the body has the exact Bill ID line that create_github_issue writes."""
    wanted = (f"**Bill ID:** `{bill_id}`", f"Bill ID: `{bill_id}`")
    lines = (issue.get("body") or "").splitlines()
    return any(line.strip() in wanted for line in lines)


def create_github_issue(bill, check_result, level):
    """Open a GitHub issue for human review. Returns True only if one was opened."""
    try:
        existing = open_state_bill_issues()
    except Exception as e:
        print(f"  ❌ Could not list open issues ({e}). Not opening one, to avoid a duplicate.")
        return False
    if any(is_duplicate_issue(issue, bill["id"]) for issue in existing):
        print(f"  ⚠️  Open issue already exists for {bill['id']}, not opening another.")
        return False

    dl = days_left(bill.get("deadline"))
    deadline_str = f" · Sine die {bill['deadline']} ({dl}d left)" if dl is not None else ""

    title = f"[{level}] {bill['state']} {bill['bill']} status update"

    body = f"""## Bill Status Change Detected

**Bill:** {bill['bill']} ({bill['state_name']})
**Type:** {bill['type'].upper()} — Test {bill['test']}
**Urgency:** {level}{deadline_str}

### Change
**Previous:** {bill['status']}
**Detected:** {check_result.get('new_status') or bill['status']}

**Summary:** {check_result.get('change_summary', 'No summary provided')}

**Confidence:** {check_result.get('confidence', 'unknown')}

### Action Required
Comment exactly approved (repo writers only) to have the bot update data/bills.json and open a pull request for review.

**Legislature page:** {bill.get('legislature_url', 'N/A')}
**Bill ID:** `{bill['id']}`

---
*Generated by state-monitor.yml on {TODAY}*
"""

    label_map = {
        "URGENT":     "urgent",
        "ACTIVE":     "active",
        "MONITORING": "monitoring",
    }

    issue_resp = requests.post(
        f"https://api.github.com/repos/{gh_repo()}/issues",
        headers=gh_headers(),
        json={
            "title": title,
            "body": body,
            "labels": ["state-bill", label_map.get(level, "monitoring")],
        }
    )

    if issue_resp.ok:
        print(f"  ✅ Issue created: {issue_resp.json()['html_url']}")
        return True
    print(f"  ❌ Issue creation failed: {issue_resp.status_code} {issue_resp.text}")
    return False


def check_bill(client, bill):
    """Check one bill and record the result on it. Returns True if an issue was opened."""
    url = bill.get("legislature_url")
    if not url:
        mark_unchecked(bill, "no source URL")
        return False

    page_text, error = fetch_bill_page(url)
    if error:
        mark_unchecked(bill, error)
        return False

    try:
        result = check_bill_with_claude(client, bill, page_text)
    except ValueError:
        mark_unchecked(bill, "unreadable answer")
        return False
    except Exception as e:
        mark_unchecked(bill, f"model call failed: {type(e).__name__}")
        return False

    reason = verification_failure(result, page_text)
    if reason:
        mark_unchecked(bill, reason)
        if result.get("changed") or result.get("dead"):
            print("  ⚠️  No issue opened: the read was not verified")
        return False

    mark_verified(bill, result)
    print(f"  ✅ Verified: {bill['check_note']}")

    level = urgency_level(bill)
    if result.get("changed"):
        print(f"  📣 CHANGE DETECTED: {result.get('new_status')}")
    elif result.get("dead"):
        print(f"  💀 Bill appears dead, flagging for manual review")
        result["change_summary"] = "Bill appears to have died in committee or been withdrawn."
        level = "ACTIVE"
    else:
        print(f"  ✓  No change (confidence: {result.get('confidence', '?')})")
        return False

    try:
        return create_github_issue(bill, result, level)
    except Exception as e:
        # Do not lose the rest of the run's results over one issue
        print(f"  ❌ Issue creation failed: {type(e).__name__}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    data = json.loads(BILLS_JSON.read_text())
    bills = [b for b in data["bills"] if b.get("active", True)]

    print(f"🗓️  State Bill Monitor — {TODAY}")
    print(f"📋 Checking {len(bills)} active bills\n")
    client = get_client()
    print("🏷️  Ensuring labels exist...")
    ensure_labels()

    issues_opened = 0

    for bill in bills:
        level = urgency_level(bill)
        dl = days_left(bill.get("deadline"))
        deadline_display = f" [{dl}d left]" if dl is not None else ""
        print(f"→ {bill['id']} ({level}{deadline_display})")

        if check_bill(client, bill):
            issues_opened += 1

    unchecked_ids = [b["id"] for b in bills if b["check_status"] != "verified"]
    summary = {
        "checked": len(bills),
        "verified": len(bills) - len(unchecked_ids),
        "unchecked": len(unchecked_ids),
        "issues_opened": issues_opened,
        "unchecked_ids": unchecked_ids,
    }
    data["last_run"] = TODAY
    data["last_run_summary"] = summary
    BILLS_JSON.write_text(json.dumps(data, indent=2))

    print(f"\n📊 Run {TODAY}: {summary['checked']} checked, {summary['verified']} verified, "
          f"{summary['unchecked']} unchecked, {summary['issues_opened']} issue(s) opened")
    print(f"   Unchecked: {', '.join(unchecked_ids) or 'none'}")


if __name__ == "__main__":
    main()
