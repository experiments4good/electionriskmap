#!/usr/bin/env python3
"""
State Bill Apply-Update for ElectionRiskMap.org
Triggered by state-apply-update.yml when a GitHub issue comment contains "approved".

Reads the issue, finds the bill ID, updates bills.json, and opens a PR.
"""

import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

BILLS_JSON     = Path("data/bills.json")
INDEX_HTML     = Path("index.html")
GH_TOKEN       = os.environ["GITHUB_TOKEN"]
GH_REPO        = os.environ.get("GITHUB_REPOSITORY", "experiments4good/electionriskmap")
ISSUE_NUMBER   = os.environ["ISSUE_NUMBER"]
TODAY          = date.today().isoformat()

HEADERS = {
    "Authorization": f"token {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_issue(number):
    resp = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/issues/{number}",
        headers=HEADERS
    )
    resp.raise_for_status()
    return resp.json()


def parse_issue_body(body):
    """Extract bill ID and new status from the issue body."""
    bill_id_match = re.search(r"\*\*Bill ID:\*\*\s*`([^`]+)`", body)
    new_status_match = re.search(r"\*\*Detected:\*\*\s*(.+)", body)

    bill_id = bill_id_match.group(1).strip() if bill_id_match else None
    new_status = new_status_match.group(1).strip() if new_status_match else None

    return bill_id, new_status


def update_bills_json(bill_id, new_status):
    """Update the bill record in bills.json."""
    data = json.loads(BILLS_JSON.read_text())

    updated = False
    for bill in data["bills"]:
        if bill["id"] == bill_id:
            old_status = bill["status"]
            bill["status"] = new_status
            bill["last_changed"] = TODAY
            bill["last_checked"] = TODAY
            updated = True
            print(f"  ✅ Updated {bill_id}")
            print(f"     Old: {old_status}")
            print(f"     New: {new_status}")
            break

    if not updated:
        print(f"  ❌ Bill ID '{bill_id}' not found in bills.json")
        sys.exit(1)

    BILLS_JSON.write_text(json.dumps(data, indent=2))
    return data


def rebuild_interventions_block(data):
    """
    Rebuild the INTERVENTIONS constant in index.html from bills.json.
    Finds the block between the INTERVENTIONS markers and replaces it.
    """
    html = INDEX_HTML.read_text()

    # Build new INTERVENTIONS JS constant
    bills_by_state = {}
    for bill in data["bills"]:
        if not bill.get("active", True):
            continue
        state = bill["state"]
        if state not in bills_by_state:
            bills_by_state[state] = []
        bills_by_state[state].append(bill)

    lines = ["const INTERVENTIONS = {"]
    state_entries = []

    for state, state_bills in sorted(bills_by_state.items()):
        bill_lines = []
        for b in state_bills:
            deadline_val = f'"{b["deadline"]}"' if b.get("deadline") else "null"
            bill_entry = (
                f'        {{\n'
                f'          bill: "{b["bill"]}",\n'
                f'          type: "{b["type"]}",\n'
                f'          summary: "{b["summary"].replace(chr(34), chr(39))}",\n'
                f'          status: "{b["status"].replace(chr(34), chr(39))}",\n'
                f'          deadline: {deadline_val},\n'
                f'          action: "{b["action"]}"\n'
                f'        }}'
            )
            bill_lines.append(bill_entry)
        state_entry = f'  "{state}": [\n' + ",\n".join(bill_lines) + "\n  ]"
        state_entries.append(state_entry)

    lines.append(",\n".join(state_entries))
    lines.append("};")
    new_block = "\n".join(lines)

    # Replace existing block between markers
    pattern = r"(// INTERVENTIONS-START\n).*?(// INTERVENTIONS-END)"
    replacement = f"// INTERVENTIONS-START\n{new_block}\n// INTERVENTIONS-END"

    if re.search(pattern, html, flags=re.DOTALL):
        new_html = re.sub(pattern, replacement, html, flags=re.DOTALL)
        INDEX_HTML.write_text(new_html)
        print(f"  ✅ index.html INTERVENTIONS block rebuilt")
    else:
        print(f"  ⚠️  INTERVENTIONS markers not found in index.html — skipping HTML rebuild")
        print(f"      Add // INTERVENTIONS-START and // INTERVENTIONS-END markers around the block")


def git_commit_and_pr(bill_id, new_status):
    """Commit changes and open a PR."""
    branch = f"state-update/{bill_id.lower()}-{TODAY}"

    cmds = [
        ["git", "config", "user.email", "bot@electionriskmap.org"],
        ["git", "config", "user.name", "ERM State Monitor Bot"],
        ["git", "checkout", "-b", branch],
        ["git", "add", "data/bills.json", "index.html"],
        ["git", "commit", "-m", f"state: update {bill_id} status ({TODAY})"],
        ["git", "push", "origin", branch],
    ]

    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ⚠️  Git command failed: {' '.join(cmd)}")
            print(f"     {result.stderr}")

    # Open PR
    pr_resp = requests.post(
        f"https://api.github.com/repos/{GH_REPO}/pulls",
        headers=HEADERS,
        json={
            "title": f"[State Update] {bill_id} — {TODAY}",
            "body": f"Auto-generated from approved issue #{ISSUE_NUMBER}.\n\nUpdates `{bill_id}` status in `data/bills.json` and rebuilds `index.html` INTERVENTIONS block.\n\n**Deploy:** Drag `index.html` to Netlify after merging.",
            "head": branch,
            "base": "main",
        }
    )

    if pr_resp.ok:
        pr_url = pr_resp.json()["html_url"]
        print(f"  ✅ PR opened: {pr_url}")

        # Comment PR link on the original issue
        requests.post(
            f"https://api.github.com/repos/{GH_REPO}/issues/{ISSUE_NUMBER}/comments",
            headers=HEADERS,
            json={"body": f"✅ Update applied. PR ready for review and deploy: {pr_url}"}
        )
    else:
        print(f"  ❌ PR creation failed: {pr_resp.status_code} {pr_resp.text}")


def close_issue():
    requests.patch(
        f"https://api.github.com/repos/{GH_REPO}/issues/{ISSUE_NUMBER}",
        headers=HEADERS,
        json={"state": "closed"}
    )
    print(f"  ✅ Issue #{ISSUE_NUMBER} closed")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"🔧 State Apply-Update — Issue #{ISSUE_NUMBER} — {TODAY}\n")

    issue = get_issue(ISSUE_NUMBER)
    bill_id, new_status = parse_issue_body(issue["body"])

    if not bill_id or not new_status:
        print(f"❌ Could not parse bill_id or new_status from issue body.")
        print(f"   body preview: {issue['body'][:300]}")
        sys.exit(1)

    print(f"📋 Bill ID: {bill_id}")
    print(f"📋 New status: {new_status}\n")

    data = update_bills_json(bill_id, new_status)
    rebuild_interventions_block(data)
    git_commit_and_pr(bill_id, new_status)
    close_issue()

    print(f"\n✅ Done.")


if __name__ == "__main__":
    main()
