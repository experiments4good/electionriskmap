#!/usr/bin/env python3
"""
Election Risk Map — Apply Approved Update

Two modes:
  "approved"           — Findings are good. Bot generates site updates + email from the issue.
  "approved with edits" — Comment contains corrections + our pre-drafted email. Bot uses those.

Triggered by apply-update.yml when someone comments on a "needs-review" issue.
"""

import os
import sys
import json
import re
import http.client
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BUTTONDOWN_API_KEY = os.environ.get("BUTTONDOWN_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 8192
SITE_URL = "https://electionriskmap.org"


def load_github_event():
    """Load issue/comment data from GitHub event JSON (preserves markdown perfectly)."""
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    if event_path and os.path.exists(event_path):
        with open(event_path, "r") as f:
            event = json.load(f)
        return {
            "issue_number": str(event.get("issue", {}).get("number", "")),
            "issue_title": event.get("issue", {}).get("title", ""),
            "issue_body": event.get("issue", {}).get("body", ""),
            "comment_body": event.get("comment", {}).get("body", ""),
        }
    # Fallback to env vars (for local testing)
    return {
        "issue_number": os.environ.get("ISSUE_NUMBER", ""),
        "issue_title": os.environ.get("ISSUE_TITLE", ""),
        "issue_body": os.environ.get("ISSUE_BODY", ""),
        "comment_body": os.environ.get("COMMENT_BODY", ""),
    }


EVENT = load_github_event()
ISSUE_NUMBER = EVENT["issue_number"]
ISSUE_TITLE = EVENT["issue_title"]
ISSUE_BODY = EVENT["issue_body"]
COMMENT_BODY = EVENT["comment_body"]


# ---------------------------------------------------------------------------
# Detect mode
# ---------------------------------------------------------------------------
def detect_mode(comment: str) -> str:
    """Detect whether this is 'approved' or 'approved with edits'."""
    lower = comment.lower().strip()
    if "approved with edits" in lower:
        return "with_edits"
    return "clean"


# ---------------------------------------------------------------------------
# Parse the "approved with edits" comment
# ---------------------------------------------------------------------------
def parse_edits_comment(comment: str) -> dict:
    """
    Parse a comment in this format:

    approved with edits

    ## Corrections
    - correction 1
    - correction 2

    ## Send this email via Buttondown
    **Subject:** ...
    **Body:**
    ...email text...
    """
    result = {
        "corrections": "",
        "email_subject": "",
        "email_body": "",
    }

    # Extract corrections section
    corrections_match = re.search(
        r'##?\s*Corrections.*?\n(.*?)(?=##?\s|$)',
        comment,
        re.DOTALL | re.IGNORECASE,
    )
    if corrections_match:
        result["corrections"] = corrections_match.group(1).strip()

    # Extract email subject — handle **Subject:** or Subject: or *Subject:*
    subject_match = re.search(
        r'(?:\*\*)?Subject:?\*?\*?\s*(.+?)(?:\n|$)',
        comment,
        re.IGNORECASE,
    )
    if subject_match:
        result["email_subject"] = subject_match.group(1).strip()

    # Extract email body — everything after **Body:** or Body: until the end or next section
    body_match = re.search(
        r'(?:\*\*)?Body:?\*?\*?\s*\n(.*?)(?=\n---|\Z)',
        comment,
        re.DOTALL | re.IGNORECASE,
    )
    if body_match:
        result["email_body"] = body_match.group(1).strip()
    else:
        # Fallback: everything after the Subject line that isn't the subject
        email_section = re.search(
            r'##?\s*(?:Send this )?[Ee]mail.*?\n(.*)',
            comment,
            re.DOTALL,
        )
        if email_section:
            section = email_section.group(1)
            # Remove the subject line, keep the rest as body
            body_part = re.sub(r'\*\*Subject:\*\*.*?\n', '', section).strip()
            # Remove **Body:** marker if present
            body_part = re.sub(r'^\*\*Body:\*\*\s*', '', body_part).strip()
            result["email_body"] = body_part

    return result


# ---------------------------------------------------------------------------
# Claude API helpers
# ---------------------------------------------------------------------------
def call_claude(system_prompt: str, user_prompt: str) -> dict:
    """Call Claude API."""
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
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
    return "\n".join(
        b["text"] for b in response.get("content", []) if b.get("type") == "text"
    )


def parse_json_response(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start == -1 or end == 0:
        print(f"No JSON found in response: {cleaned[:300]}", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(cleaned[start:end])
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}", file=sys.stderr)
        print(f"Raw: {cleaned[start:end][:500]}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Claude prompts
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are the update engine for electionriskmap.org, a nonpartisan site
tracking federal election interference risks ahead of the 2026 midterms.

You will receive findings from an automated scan (already fact-checked and approved by a human),
plus the current timeline HTML and feed.xml.

Your job is to generate structured JSON output with the exact updates to apply.
Be precise. Match the existing HTML/XML style exactly. Do not invent facts.

CRITICAL RULES:
- Timeline entries are ordered newest-first by EVENT DATE (not by when they were added)
- New entries must be inserted at the correct chronological position, NOT always at the top
- Each new entry MUST use this exact HTML structure:
  <div class="tl-item">
    <div class="tl-date">Feb 6</div>
    <div class="tl-dot" style="background:var(--elevated)"></div>
    <div class="tl-text"><strong>Key phrase</strong> rest of text <span class="tl-new">New</span></div>
  </div>
- The "New" tag MUST be: <span class="tl-new">New</span>
- Dot colors: var(--critical) for alarming, var(--elevated) for concerning, var(--moderate) for moderate, #22C55E for good news
- feed.xml items go newest-first (after the channel metadata)
- Email should be concise, factual, and include a call to action
- Email body is markdown (Buttondown renders it)
- Use only ASCII in email subject lines (no em-dashes, arrows, or special characters)
"""


def build_clean_prompt(issue_body, timeline_html, feed_xml):
    """Prompt for 'approved' mode — generate everything from the issue."""
    return f"""Here are the approved findings from the automated scan:

--- ISSUE BODY ---
{issue_body}
--- END ISSUE BODY ---

--- CURRENT TIMELINE HTML ---
{timeline_html}
--- END TIMELINE ---

--- CURRENT feed.xml ---
{feed_xml}
--- END feed.xml ---

Generate a JSON response with this exact structure:
{{
  "new_timeline_entries_html": "HTML string of new <div class='tl-item'> entries. MUST use tl-item/tl-date/tl-dot/tl-text classes. Include <span class='tl-new'>New</span> in each tl-text.",
  "stat_updates": {{
    "states_sued": null,
    "states_complied": null,
    "states_contacted": null,
    "court_wins_merits": null
  }},
  "new_feed_items_xml": "XML string of new <item> elements for feed.xml.",
  "feed_last_build_date": "RFC 822 date string",
  "monitor_timeline_additions": "Plain text lines to add to CURRENT_TIMELINE in monitor.py. Format: '- Mon DD, YYYY: Brief description'",
  "email_subject": "Email subject line - ASCII only, no special characters",
  "email_body": "Email body in markdown. Under 300 words. Include what happened, why it matters, court score, what readers can do, link to map.",
  "last_updated_date": "Month DD, YYYY"
}}

Set stat fields to null if unchanged. Respond ONLY with JSON."""


def build_edits_prompt(issue_body, corrections, timeline_html, feed_xml):
    """Prompt for 'approved with edits' mode — apply corrections to findings."""
    return f"""Here are the findings from the automated scan, BUT they need corrections.
Apply the corrections below before generating updates.

--- ISSUE BODY (original findings — may contain errors) ---
{issue_body}
--- END ISSUE BODY ---

--- CORRECTIONS TO APPLY ---
{corrections}
--- END CORRECTIONS ---

--- CURRENT TIMELINE HTML ---
{timeline_html}
--- END TIMELINE ---

--- CURRENT feed.xml ---
{feed_xml}
--- END feed.xml ---

Generate the CORRECTED updates as JSON (same structure as always):
{{
  "new_timeline_entries_html": "HTML with corrections applied. MUST use tl-item/tl-date/tl-dot/tl-text classes. Include <span class='tl-new'>New</span>.",
  "stat_updates": {{
    "states_sued": null,
    "states_complied": null,
    "states_contacted": null,
    "court_wins_merits": null
  }},
  "new_feed_items_xml": "Corrected XML items for feed.xml.",
  "feed_last_build_date": "RFC 822 date string",
  "monitor_timeline_additions": "Corrected plain text lines for monitor.py.",
  "last_updated_date": "Month DD, YYYY"
}}

NOTE: Do NOT generate email fields — the email was already drafted by the human.
Set stat fields to null if unchanged. Respond ONLY with JSON."""


# ---------------------------------------------------------------------------
# File manipulation helpers
# ---------------------------------------------------------------------------
def extract_timeline_section(html: str) -> str:
    """Extract timeline entries from index.html (first 10 for context)."""
    entries = re.findall(r'<div class="tl-item">.*?</div>\s*</div>\s*</div>', html, re.DOTALL)
    if entries:
        return "\n".join(entries[:10])
    # Fallback: broader match
    match = re.search(r'(<div class="timeline[^"]*"[^>]*>)(.*?)(</div>\s*</section>)', html, re.DOTALL)
    if match:
        return match.group(0)[:3000]
    return "(Could not extract timeline section)"


def insert_timeline_entries(html: str, new_entries: str) -> str:
    """Insert new entries in chronological position (newest first by event date)."""
    import re
    from datetime import datetime

    def parse_tl_date(date_str):
        """Parse a timeline date string into a sortable value."""
        date_str = date_str.strip()
        # Try "Feb 6" style (current year)
        for fmt in ["%b %d", "%B %d"]:
            try:
                d = datetime.strptime(date_str, fmt).replace(year=2026)
                return d
            except ValueError:
                pass
        # Try "Jan 2026" style
        for fmt in ["%b %Y", "%B %Y"]:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                pass
        # Try "Dec 2025" etc
        for fmt in ["%b %Y", "%B %Y"]:
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                pass
        # Fallback: just a year
        try:
            return datetime(int(date_str), 1, 1)
        except (ValueError, TypeError):
            return datetime(2020, 1, 1)  # unknown dates go to bottom

    # Extract date from the new entry
    new_date_match = re.search(r'<div class="tl-date">(.*?)</div>', new_entries)
    if not new_date_match:
        # Can't determine date, insert at top as fallback
        marker = '<div class="timeline-title mono">'
        idx = html.find(marker)
        if idx != -1:
            close_idx = html.find("</div>", idx)
            if close_idx != -1:
                insert_pos = close_idx + len("</div>")
                return html[:insert_pos] + "\n" + new_entries + "\n" + html[insert_pos:]
        return html

    new_date = parse_tl_date(new_date_match.group(1))

    # Find all existing tl-item positions and their dates
    existing_items = list(re.finditer(r'<div class="tl-item">', html))
    if not existing_items:
        # No existing items, insert after timeline-title
        marker = '<div class="timeline-title mono">'
        idx = html.find(marker)
        if idx != -1:
            close_idx = html.find("</div>", idx)
            if close_idx != -1:
                insert_pos = close_idx + len("</div>")
                return html[:insert_pos] + "\n" + new_entries + "\n" + html[insert_pos:]
        return html

    # Find the right position: insert before the first entry with an older date
    for item_match in existing_items:
        item_start = item_match.start()
        date_match = re.search(r'<div class="tl-date">(.*?)</div>', html[item_start:item_start+200])
        if date_match:
            existing_date = parse_tl_date(date_match.group(1))
            if new_date >= existing_date:
                # Insert before this item
                return html[:item_start] + new_entries + "\n    " + html[item_start:]

    # New entry is oldest — insert at the end (before the closing div of the timeline)
    last_item = existing_items[-1]
    # Find the end of the last tl-item block
    search_from = last_item.start()
    # Find closing </div></div></div> pattern for the last tl-item
    end_of_last = html.find("</div>", html.find("</div>", html.find("</div>", search_from) + 1) + 1) + len("</div>")
    return html[:end_of_last] + "\n    " + new_entries + "\n" + html[end_of_last:]


def remove_old_new_tags(html: str) -> str:
    """Remove existing 'New' tags so only the fresh entries have them."""
    # Match the actual class used in the site
    html = re.sub(r'\s*<span class="tl-new">New</span>', '', html)
    # Also catch any old-style tags just in case
    html = re.sub(r'\s*<span class="timeline-tag new">New</span>', '', html)
    return html


def update_stats(html: str, stats: dict) -> str:
    """Update stat numbers. Pattern depends on site HTML structure."""
    if not stats:
        return html
    for key, value in stats.items():
        if value is None:
            continue
        if key == "states_sued":
            html = re.sub(r'(data-stat="sued"[^>]*>)\s*(\d+)', f'\\g<1>{value}', html)
        elif key == "states_complied":
            html = re.sub(r'(data-stat="complied"[^>]*>)\s*(\d+)', f'\\g<1>{value}', html)
        elif key == "court_wins_merits":
            html = re.sub(r'(data-stat="court"[^>]*>)\s*(\d+)', f'\\g<1>{value}', html)
        elif key == "states_contacted":
            html = re.sub(r'(data-stat="contacted"[^>]*>)\s*(\d+)', f'\\g<1>{value}', html)
    return html


def update_last_updated(html: str, date_str: str) -> str:
    """Update last-updated dates in the HTML."""
    html = re.sub(
        r'(Last updated[:\s]*)\w+ \d{1,2}, \d{4}',
        f'\\g<1>{date_str}',
        html,
        flags=re.IGNORECASE,
    )
    html = re.sub(
        r'(Data as of )\w+ \d{4}',
        f'\\g<1>{datetime.now().strftime("%B %Y")}',
        html,
    )
    return html


def insert_feed_items(feed_xml: str, new_items: str, last_build_date: str) -> str:
    """Insert new items into feed.xml."""
    if last_build_date:
        feed_xml = re.sub(
            r'<lastBuildDate>.*?</lastBuildDate>',
            f'<lastBuildDate>{last_build_date}</lastBuildDate>',
            feed_xml,
        )
    # Insert after the channel's </description>
    match = re.search(r'(</description>\s*\n)', feed_xml)
    if match:
        pos = match.end()
        feed_xml = feed_xml[:pos] + new_items + "\n" + feed_xml[pos:]
    else:
        print("WARNING: Could not find insertion point in feed.xml", file=sys.stderr)
    return feed_xml


def update_monitor_timeline(monitor_py: str, new_lines: str) -> str:
    """Add entries to CURRENT_TIMELINE in monitor.py."""
    marker = "Already on the site (do NOT re-report these):"
    idx = monitor_py.find(marker)
    if idx == -1:
        print("WARNING: Could not find CURRENT_TIMELINE in monitor.py", file=sys.stderr)
        return monitor_py
    insert_pos = idx + len(marker)
    return monitor_py[:insert_pos] + "\n" + new_lines + monitor_py[insert_pos:]


# ---------------------------------------------------------------------------
# Buttondown — uses http.client to avoid urllib's latin-1 header encoding bug
# ---------------------------------------------------------------------------
def send_buttondown_email(subject: str, body: str) -> bool:
    """Send email via Buttondown API. Uses http.client for proper UTF-8 support."""
    if not BUTTONDOWN_API_KEY:
        print("WARNING: No BUTTONDOWN_API_KEY -- skipping email", file=sys.stderr)
        return False
    if not subject or not body:
        print("WARNING: Empty subject or body -- skipping email", file=sys.stderr)
        return False

    try:
        payload = {
            "subject": subject,
            "body": body,
            "status": "about_to_send",
        }
        json_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        conn = http.client.HTTPSConnection("api.buttondown.com", timeout=30)
        conn.request(
            "POST",
            "/v1/emails",
            body=json_data,
            headers={
                "Authorization": f"Token {BUTTONDOWN_API_KEY}",
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(json_data)),
            },
        )
        resp = conn.getresponse()
        resp_body = resp.read().decode("utf-8")
        conn.close()

        if resp.status in (200, 201):
            result = json.loads(resp_body)
            print(f"Buttondown email sent: {result.get('id', '?')}")
            return True
        else:
            print(f"Buttondown API error {resp.status}: {resp_body}", file=sys.stderr)
            return False
    except Exception as e:
        print(f"Buttondown error: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# GitHub issue management
# ---------------------------------------------------------------------------
def comment_and_close_issue(comment: str):
    """Comment on the issue and close it."""
    if not GITHUB_TOKEN or not GITHUB_REPO or not ISSUE_NUMBER:
        print(f"Would comment: {comment[:200]}...")
        return

    # Comment
    payload = {"body": comment}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/issues/{ISSUE_NUMBER}/comments",
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            print(f"Comment added to issue #{ISSUE_NUMBER}")
    except urllib.error.HTTPError as e:
        print(f"GitHub comment error: {e.code}", file=sys.stderr)

    # Close
    payload = {"state": "closed"}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/issues/{ISSUE_NUMBER}",
        data=data,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Content-Type": "application/json",
        },
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(req, timeout=30):
            print(f"Issue #{ISSUE_NUMBER} closed")
    except urllib.error.HTTPError as e:
        print(f"GitHub close error: {e.code}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not ANTHROPIC_API_KEY:
        print("Error: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)
    if not ISSUE_BODY:
        print("Error: No issue body provided.", file=sys.stderr)
        sys.exit(1)

    mode = detect_mode(COMMENT_BODY)
    print(f"Mode: {'approved with edits' if mode == 'with_edits' else 'approved (clean)'}")
    print(f"Comment length: {len(COMMENT_BODY)} chars")
    print(f"Comment preview: {COMMENT_BODY[:150]}...")

    # Read current files
    print("Reading current site files...")
    try:
        with open("index.html", "r") as f:
            html = f.read()
        with open("feed.xml", "r") as f:
            feed_xml = f.read()
        with open("scripts/monitor.py", "r") as f:
            monitor_py = f.read()
    except FileNotFoundError as e:
        print(f"Error reading file: {e}", file=sys.stderr)
        sys.exit(1)

    timeline_html = extract_timeline_section(html)

    # --- MODE: APPROVED WITH EDITS ---
    if mode == "with_edits":
        print("Parsing corrections and email from comment...")
        edits = parse_edits_comment(COMMENT_BODY)

        # Debug: show what was parsed
        print(f"  Corrections found: {'yes' if edits['corrections'] else 'no'} ({len(edits['corrections'])} chars)")
        print(f"  Email subject found: {'yes' if edits['email_subject'] else 'no'}")
        print(f"  Email body found: {'yes' if edits['email_body'] else 'no'} ({len(edits['email_body'])} chars)")

        if not edits["email_subject"] or not edits["email_body"]:
            print("WARNING: Could not find email subject/body in comment.", file=sys.stderr)
            print("Expected format:", file=sys.stderr)
            print("  **Subject:** Your subject here", file=sys.stderr)
            print("  **Body:**", file=sys.stderr)
            print("  Your email text here", file=sys.stderr)

        # Call Claude with corrections to generate site updates (no email)
        print("Calling Claude to generate corrected site updates...")
        user_prompt = build_edits_prompt(
            ISSUE_BODY, edits["corrections"], timeline_html, feed_xml
        )
        response = call_claude(SYSTEM_PROMPT, user_prompt)
        updates = parse_json_response(extract_text(response))

        # Email comes from the human's comment, not Claude
        email_subject = edits["email_subject"]
        email_body = edits["email_body"]

    # --- MODE: APPROVED (CLEAN) ---
    else:
        print("Calling Claude to generate updates and email...")
        user_prompt = build_clean_prompt(ISSUE_BODY, timeline_html, feed_xml)
        response = call_claude(SYSTEM_PROMPT, user_prompt)
        updates = parse_json_response(extract_text(response))

        email_subject = updates.get("email_subject", "")
        email_body = updates.get("email_body", "")

    # --- APPLY SITE UPDATES ---
    print("Applying updates to index.html...")
    html = remove_old_new_tags(html)
    if updates.get("new_timeline_entries_html"):
        html = insert_timeline_entries(html, updates["new_timeline_entries_html"])
    if updates.get("stat_updates"):
        html = update_stats(html, updates["stat_updates"])
    if updates.get("last_updated_date"):
        html = update_last_updated(html, updates["last_updated_date"])

    print("Applying updates to feed.xml...")
    if updates.get("new_feed_items_xml"):
        feed_xml = insert_feed_items(
            feed_xml,
            updates["new_feed_items_xml"],
            updates.get("feed_last_build_date", ""),
        )

    print("Updating monitor.py...")
    if updates.get("monitor_timeline_additions"):
        monitor_py = update_monitor_timeline(monitor_py, updates["monitor_timeline_additions"])

    # Write files
    print("Writing updated files...")
    with open("index.html", "w") as f:
        f.write(html)
    with open("feed.xml", "w") as f:
        f.write(feed_xml)
    with open("scripts/monitor.py", "w") as f:
        f.write(monitor_py)

    # --- SEND EMAIL (non-fatal — site updates already written) ---
    print("Sending email via Buttondown...")
    try:
        email_sent = send_buttondown_email(email_subject, email_body)
    except Exception as e:
        print(f"Email failed (non-fatal): {e}", file=sys.stderr)
        email_sent = False

    # --- CLOSE ISSUE ---
    changes = []
    if updates.get("new_timeline_entries_html"):
        changes.append("Added timeline entries")
    if updates.get("stat_updates") and any(v is not None for v in updates["stat_updates"].values()):
        changes.append("Updated stats")
    if updates.get("new_feed_items_xml"):
        changes.append("Updated RSS feed")
    if email_sent:
        changes.append("Email sent via Buttondown")
    else:
        changes.append("Email skipped (missing API key, empty content, or error)")
    if mode == "with_edits":
        changes.append("Applied human corrections (approved with edits)")

    summary = f"""Update applied{'  with corrections' if mode == 'with_edits' else ''}.

Mode: {'approved with edits' if mode == 'with_edits' else 'approved'}

Changes:
{chr(10).join(f'- {c}' for c in changes)}

Email subject: {email_subject or '(none)'}

Last updated: {updates.get('last_updated_date', 'N/A')}

To deploy: git pull in your site folder, then drag to Netlify.

---
Applied by election-map-bot."""

    print("Commenting and closing issue...")
    comment_and_close_issue(summary)
    print("Done!")


if __name__ == "__main__":
    main()
