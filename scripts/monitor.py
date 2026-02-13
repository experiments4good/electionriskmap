#!/usr/bin/env python3
"""
Election Risk Map — Automated Update Monitor
Searches trusted sources for election interference developments,
cross-references against 2+ independent sources, and opens a
GitHub Issue with verified findings for human approval.

Usage:
    python scripts/monitor.py

Requires env vars:
    ANTHROPIC_API_KEY  — Claude API key
    GITHUB_TOKEN       — GitHub token with repo access
    GITHUB_REPOSITORY  — owner/repo (set automatically by Actions)
"""

import os
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 4096

# ---------------------------------------------------------------------------
# Auto-extract current site state from index.html (no manual maintenance)
# ---------------------------------------------------------------------------
def get_current_timeline() -> str:
    """Parse index.html to extract what's already on the site."""
    index_path = os.path.join(os.path.dirname(__file__), "..", "index.html")
    if not os.path.exists(index_path):
        # Try repo root
        index_path = os.path.join(os.path.dirname(__file__), "..", "index.html")
    if not os.path.exists(index_path):
        return "(Could not read index.html — flag everything as potentially new)"

    with open(index_path, "r", encoding="utf-8") as f:
        html = f.read()

    lines = ["Already on the site (do NOT re-report these):
Feb 6: FBI invites all 50 state election officials to mysterious February 25 call on 'midterm preparations' — Nevada Secretary of State calls it 'beyond crazy'\n"]

    # Extract timeline entries
    import re
    tl_dates = re.findall(r'class="tl-date">([^<]+)</div>', html)
    tl_texts = re.findall(r'class="tl-text">(.*?)</div>', html, re.DOTALL)
    for date, text in zip(tl_dates, tl_texts):
        clean = re.sub(r'<[^>]+>', '', text).strip()
        lines.append(f"- {date}: {clean}")

    # Extract court wins
    court_states = re.findall(r'class="court-state">(.*?)</div>', html, re.DOTALL)
    court_details = re.findall(r'class="court-detail">(.*?)</div>', html, re.DOTALL)
    if court_states:
        lines.append("\nCourt rulings already tracked:")
    for state, detail in zip(court_states, court_details):
        clean_state = re.sub(r'<[^>]+>', '', state).strip()
        clean_detail = re.sub(r'<[^>]+>', '', detail).strip()
        lines.append(f"- {clean_state}: {clean_detail}")

    # Extract stat numbers
    stat_nums = re.findall(r'class="stat-num">([^<]+)</div>', html)
    stat_labels = re.findall(r'class="stat-label">([^<]+)</div>', html)
    if stat_nums:
        lines.append("\nCurrent stats on site:")
    for num, label in zip(stat_nums, stat_labels):
        lines.append(f"- {num} {label}")

    # Extract complied states from JS
    complied = re.findall(r'(\w{2}):\{name:"[^"]+",risk:"complied"', html)
    if complied:
        lines.append(f"\nStates marked as complied: {', '.join(sorted(complied))}")

    return "\n".join(lines)


CURRENT_TIMELINE = get_current_timeline()

SEARCH_PROMPT = f"""You are a fact-checker for electionriskmap.org, a nonpartisan site tracking
federal election interference risks ahead of the 2026 midterms.

Your job is to search for NEW developments that are NOT already on the site.
Focus on:
1. New DOJ voter data lawsuits or states complying/resisting
2. Court rulings on existing voter data cases
3. Federal actions targeting state election infrastructure
4. Legislative efforts to federalize elections (SAVE Act, etc.)
5. New threats to election officials or voting access
6. Calls to action / new resources for voters

{CURRENT_TIMELINE}

INSTRUCTIONS:
1. Search for recent election interference news (last 7 days)
2. For each potential update, search for at least 2 INDEPENDENT sources confirming it
3. Only report findings confirmed by 2+ independent sources
4. For each finding, rate confidence: HIGH (3+ sources), MEDIUM (2 sources)
5. Do NOT report anything already listed above
6. Do NOT report opinion pieces, speculation, or predictions — only concrete events

Respond in this exact JSON format (no markdown, no backticks, just raw JSON):
{{
  "search_date": "YYYY-MM-DD",
  "findings": [
    {{
      "headline": "Short headline",
      "date": "YYYY-MM-DD or approximate",
      "description": "2-3 sentence factual description",
      "category": "court_ruling|lawsuit|federal_action|legislation|compliance|other",
      "affected_states": ["XX", "YY"],
      "confidence": "HIGH|MEDIUM",
      "sources": [
        {{"name": "Source Name", "url": "https://..."}},
        {{"name": "Source Name 2", "url": "https://..."}}
      ],
      "suggested_timeline_entry": "Short text for the timeline",
      "suggested_risk_changes": "Any state risk level changes needed, or 'none'"
    }}
  ],
  "no_updates": false,
  "summary": "1-2 sentence summary of what was found (or 'No new verified developments found.')"
}}

If nothing new is found, set "findings" to an empty array and "no_updates" to true.
Be conservative. Only include developments you are confident actually happened.
"""


def call_claude(prompt: str) -> dict:
    """Call Claude API with web search enabled."""
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": prompt}],
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        print(f"Claude API error {e.code}: {body}", file=sys.stderr)
        sys.exit(1)


def extract_text(response: dict) -> str:
    """Extract all text blocks from Claude's response."""
    parts = []
    for block in response.get("content", []):
        if block.get("type") == "text":
            parts.append(block["text"])
    return "\n".join(parts)


def parse_findings(text: str) -> dict:
    """Parse JSON from Claude's response, stripping any markdown fencing."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    # Find the JSON object
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start == -1 or end == 0:
        return {"findings": [], "no_updates": True, "summary": "Failed to parse response."}
    try:
        return json.loads(cleaned[start:end])
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}", file=sys.stderr)
        print(f"Raw text: {cleaned[start:end][:500]}", file=sys.stderr)
        return {"findings": [], "no_updates": True, "summary": "Failed to parse response."}


def format_issue_body(data: dict) -> str:
    """Format findings into a GitHub Issue body."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"## Automated Election Update Scan — {now}",
        "",
    ]

    if data.get("no_updates") or not data.get("findings"):
        lines.append("### No new verified developments found.")
        lines.append("")
        lines.append(f"**Summary:** {data.get('summary', 'No updates.')}")
        lines.append("")
        lines.append("---")
        lines.append("*This scan checked Brennan Center, DOJ press releases, Votebeat, "
                      "Democracy Docket, NPR, and other election news sources.*")
        return "\n".join(lines)

    lines.append(f"**Summary:** {data.get('summary', '')}")
    lines.append("")
    lines.append(f"### {len(data['findings'])} Update(s) Found")
    lines.append("")

    for i, f in enumerate(data["findings"], 1):
        confidence_emoji = "🟢" if f.get("confidence") == "HIGH" else "🟡"
        lines.append(f"---")
        lines.append(f"")
        lines.append(f"#### {i}. {f.get('headline', 'Update')}")
        lines.append(f"")
        lines.append(f"**Date:** {f.get('date', 'Unknown')}  ")
        lines.append(f"**Confidence:** {confidence_emoji} {f.get('confidence', 'UNKNOWN')} "
                      f"({len(f.get('sources', []))} sources)  ")
        lines.append(f"**Category:** {f.get('category', 'other')}  ")
        states = f.get("affected_states", [])
        if states:
            lines.append(f"**Affected states:** {', '.join(states)}  ")
        lines.append(f"")
        lines.append(f"{f.get('description', '')}")
        lines.append(f"")
        lines.append(f"**Sources:**")
        for s in f.get("sources", []):
            lines.append(f"- [{s.get('name', 'Source')}]({s.get('url', '#')})")
        lines.append(f"")
        lines.append(f"**Suggested timeline entry:** {f.get('suggested_timeline_entry', 'N/A')}")
        lines.append(f"")
        if f.get("suggested_risk_changes", "none").lower() != "none":
            lines.append(f"**Risk level changes:** {f['suggested_risk_changes']}")
            lines.append(f"")

    lines.append("---")
    lines.append("")
    lines.append("### What to do next")
    lines.append("")
    lines.append("If these updates are accurate and should be added to the site:")
    lines.append("1. Comment `approved` on this issue")
    lines.append("2. Open a conversation with Claude and say: "
                 '"Update electionriskmap.org with the findings from Issue #[this number]"')
    lines.append("3. Claude will update the site, RSS feed, and draft an email blast")
    lines.append("")
    lines.append("If any finding looks wrong, comment with corrections before approving.")
    lines.append("")
    lines.append("---")
    lines.append("*Automated scan by electionriskmap.org monitoring pipeline. "
                 "All findings require human approval before going live.*")

    return "\n".join(lines)


def create_github_issue(title: str, body: str, labels: list = None) -> dict:
    """Create a GitHub Issue via the API."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("Missing GITHUB_TOKEN or GITHUB_REPOSITORY — printing issue locally instead.")
        print(f"\n{'='*60}")
        print(f"ISSUE: {title}")
        print(f"{'='*60}")
        print(body)
        return {}

    payload = {
        "title": title,
        "body": body,
        "labels": labels or ["automated-scan"],
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/issues",
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            print(f"Created issue #{result.get('number')}: {result.get('html_url')}")
            return result
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8") if e.fp else ""
        print(f"GitHub API error {e.code}: {body_text}", file=sys.stderr)
        sys.exit(1)


def main():
    if not ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    print("🔍 Searching for election interference updates...")
    response = call_claude(SEARCH_PROMPT)
    text = extract_text(response)

    print("📋 Parsing findings...")
    findings = parse_findings(text)

    num_findings = len(findings.get("findings", []))
    no_updates = findings.get("no_updates", False)

    if no_updates or num_findings == 0:
        print("✅ No new verified developments found.")
        # Still create an issue on Mondays for visibility (optional)
        today = datetime.now(timezone.utc).strftime("%A")
        if today == "Monday":
            title = f"Weekly scan: No updates found — {datetime.now(timezone.utc).strftime('%b %d, %Y')}"
            body = format_issue_body(findings)
            create_github_issue(title, body, labels=["automated-scan", "no-updates"])
        return

    print(f"🔔 Found {num_findings} update(s)!")

    # Format and create the issue
    today = datetime.now(timezone.utc).strftime("%b %d, %Y")
    title = f"🔔 {num_findings} election update(s) found — {today}"
    body = format_issue_body(findings)

    create_github_issue(title, body, labels=["automated-scan", "needs-review"])


if __name__ == "__main__":
    main()
