"""Tests for state_monitor.py. No network, no model calls, no writes.

Run: python3 -m unittest -v test_state_monitor
"""

import copy
import importlib
import json
import os
import sys
import unittest
from unittest import mock

for _name in ("anthropic", "requests"):
    try:
        importlib.import_module(_name)
    except ImportError:
        sys.modules[_name] = mock.MagicMock(name=_name)

import state_monitor as sm

PAGE = """<html><header>Site header</header><nav>Menu</nav><script>var x = "<b>";</script>
<style>p {}</style><p>HB 991 2026 Session</p><p>04/01/2026   Signed
by Governor</p>""" + "<p>Bill text.</p>" * 60 + """<footer>Contact</footer></html>"""

BILL = {
    "id": "FL-HB991", "state": "FL", "state_name": "Florida", "bill": "HB 991",
    "type": "threat", "test": 1, "summary": "A summary", "status": "In committee",
    "deadline": None, "legislature_url": "https://bill.example.gov/991",
    "last_changed": "2020-01-01", "last_checked": "2000-01-01", "active": True,
}

ANSWER = {
    "changed": False, "new_status": "In committee", "change_summary": None,
    "confidence": "high", "dead": False, "bill_confirmed": True,
    "session_confirmed": True, "latest_action_date": "2026-04-01",
    "evidence": "04/01/2026 Signed by Governor",
}


def response(status=200, text="", data=None, links=None, headers=None):
    return mock.MagicMock(
        status_code=status, ok=status < 400, text=text, links=links or {},
        headers=headers or {},
        json=mock.MagicMock(return_value=data),
    )


class MonitorTest(unittest.TestCase):
    def run_main(self, bills=None, answer=None, raw=None, page=None, issue_pages=None,
                 model_error=None):
        """Run main() with everything external mocked. Returns the written data."""
        page = page if page is not None else response(text=PAGE)
        issue_pages = issue_pages or [[]]
        base = "https://api.github.com/repos/example/repo"
        self.issue_gets = []
        self.page_gets = []
        # Fake clock: sleeping advances it, nothing really waits
        clock = [1000.0]
        self.sleeps = []

        def fake_sleep(seconds):
            self.sleeps.append(seconds)
            clock[0] += seconds

        fake_time = mock.MagicMock()
        fake_time.monotonic.side_effect = lambda: clock[0]
        fake_time.time.side_effect = lambda: clock[0]
        fake_time.sleep.side_effect = fake_sleep

        def fake_get(url, **kwargs):
            if url == f"{base}/labels":
                return response(data=[])
            if url.startswith(f"{base}/issues"):
                self.issue_gets.append(kwargs)
                number = int(url.rsplit("page=", 1)[1]) if "page=" in url else 1
                links = {}
                if number < len(issue_pages):
                    links = {"next": {"url": f"{base}/issues?page={number + 1}"}}
                return response(data=issue_pages[number - 1], links=links)
            if not url.startswith("https://bill.example.gov/"):
                raise AssertionError(f"unexpected URL {url}")
            self.page_gets.append(kwargs)
            current = page[len(self.page_gets) - 1] if isinstance(page, list) else page
            if isinstance(current, Exception):
                raise current
            return current

        fake_requests = mock.MagicMock()
        fake_requests.get.side_effect = fake_get
        fake_requests.post.return_value = response(201, data={"html_url": "http://issue.invalid"})
        text = raw if raw is not None else json.dumps({**ANSWER, **(answer or {})})
        fake_anthropic = mock.MagicMock()
        self.create = fake_anthropic.Anthropic.return_value.messages.create
        self.create.return_value.content = [mock.MagicMock(text=text)]
        self.create.side_effect = model_error
        path = mock.MagicMock()
        self.original = {"last_checked": "2000-01-01", "bills": bills or [copy.deepcopy(BILL)]}
        path.read_text.return_value = json.dumps(self.original)
        env = {"ANTHROPIC_API_KEY": "test", "GITHUB_TOKEN": "test", "GITHUB_REPOSITORY": "example/repo"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(sm, "requests", fake_requests), \
                mock.patch.object(sm, "anthropic", fake_anthropic), \
                mock.patch.object(sm, "BILLS_JSON", path), \
                mock.patch.object(sm, "time", fake_time), \
                mock.patch.object(sm, "_last_request", {}), \
                mock.patch("builtins.print") as fake_print:
            sm.main()
        self.log = "\n".join(str(c[0][0]) for c in fake_print.call_args_list if c[0])
        self.issue_posts = [c for c in fake_requests.post.call_args_list
                            if c[0][0] == f"{base}/issues"]
        path.write_text.assert_called_once()
        return json.loads(path.write_text.call_args[0][0])

    def assert_unchecked(self, data, note):
        bill = data["bills"][0]
        self.assertEqual(bill["last_checked"], "2000-01-01")
        self.assertEqual(bill["check_status"], "unchecked")
        self.assertEqual(bill["check_note"], note)
        self.assertEqual(data["last_run_summary"]["unchecked_ids"], [bill["id"]])

    def assert_verified(self, bill):
        self.assertEqual(bill["last_checked"], sm.TODAY)
        self.assertEqual(bill["check_status"], "verified")
        self.assertEqual(bill["check_note"], "2026-04-01 04/01/2026 Signed by Governor")

    def test_monitoring_bill_is_checked(self):
        self.assertEqual(sm.urgency_level(BILL), "MONITORING")
        self.run_main()
        self.create.assert_called_once()

    def test_inactive_bill_is_not_checked(self):
        data = self.run_main([copy.deepcopy(BILL), {**BILL, "id": "XX-2", "active": False}])
        self.create.assert_called_once()
        self.assertNotIn("check_status", data["bills"][1])

    def test_verified_sets_only_check_fields(self):
        data = self.run_main()
        self.assert_verified(data["bills"][0])
        for key, value in BILL.items():
            if key != "last_checked":
                self.assertEqual(data["bills"][0][key], value)

    def test_check_note_is_cut_to_200(self):
        long = "x" * 500
        data = self.run_main(answer={"evidence": long}, page=response(text="HB 991 " + long))
        self.assertEqual(len(data["bills"][0]["check_note"]), 200)

    def test_fetch_errors_are_unchecked(self):
        self.assert_unchecked(self.run_main(page=response(404)), "fetch error: HTTP 404")
        self.create.assert_not_called()
        self.assert_unchecked(self.run_main(page=OSError("down")), "fetch error: OSError")

    def test_wrong_session_is_unchecked(self):
        self.assert_unchecked(self.run_main(answer={"session_confirmed": False}),
                              "session not confirmed")

    def test_wrong_bill_is_unchecked(self):
        for value in (False, None, "true"):
            with self.subTest(value):
                self.assert_unchecked(self.run_main(answer={"bill_confirmed": value}),
                                      "wrong bill: not confirmed on page")

    def test_evidence_not_in_page_is_unchecked(self):
        for value in ("Vetoed by Governor", "", "  ", None, 5):
            with self.subTest(value):
                self.assert_unchecked(self.run_main(answer={"evidence": value}), "evidence not found")

    def test_unreadable_answer_is_unchecked(self):
        for raw in ("not json", "[]"):
            with self.subTest(raw):
                self.assert_unchecked(self.run_main(raw=raw), "unreadable answer")

    def test_model_failure_is_unchecked(self):
        self.assert_unchecked(self.run_main(model_error=OSError("x")), "model call failed: OSError")

    def test_no_url_is_unchecked_and_not_fetched(self):
        data = self.run_main([{**BILL, "legislature_url": None}])
        self.assert_unchecked(data, "no source URL")
        self.create.assert_not_called()

    def test_change_opens_issue(self):
        data = self.run_main(answer={"changed": True, "new_status": "Signed"})
        self.assertEqual(len(self.issue_posts), 1)
        self.assertEqual(data["last_run_summary"]["issues_opened"], 1)
        self.assertEqual(self.issue_gets[0]["params"]["per_page"], 100)
        # The body this script writes must be recognized as its own duplicate
        self.assertTrue(sm.is_duplicate_issue(self.issue_posts[0][1]["json"], "FL-HB991"))

    def test_unverified_change_opens_no_issue(self):
        for flag in ("changed", "dead"):
            with self.subTest(flag):
                data = self.run_main(answer={flag: True, "new_status": "Signed",
                                             "session_confirmed": False})
                self.assertEqual(self.issue_posts, [])
                self.assertEqual(data["last_run_summary"]["issues_opened"], 0)
                self.assert_unchecked(data, "session not confirmed")
                self.assertIn("No issue opened: the read was not verified", self.log)

    def test_matching_bill_id_skips_only_the_issue(self):
        dup = {"title": "x", "body": "text\r\n**Bill ID:** `FL-HB991`\r\n"}
        data = self.run_main(answer={"changed": True, "new_status": "Signed"}, issue_pages=[[dup]])
        self.create.assert_called_once()
        self.assertEqual(self.issue_posts, [])
        self.assert_verified(data["bills"][0])
        self.assertEqual(data["last_run_summary"]["issues_opened"], 0)

    def test_duplicate_on_second_page_is_found(self):
        dup = {"title": "x", "body": "Bill ID: `FL-HB991`"}
        self.run_main(answer={"changed": True, "new_status": "Signed"}, issue_pages=[[], [dup]])
        self.assertEqual(len(self.issue_gets), 2)
        self.assertEqual(self.issue_posts, [])

    def test_title_match_for_other_bill_is_not_duplicate(self):
        other = {"title": "[ACTIVE] GA HB 991 status update", "body": "**Bill ID:** `GA-HB991`"}
        self.run_main(answer={"changed": True, "new_status": "Signed"}, issue_pages=[[other]])
        self.assertEqual(len(self.issue_posts), 1)

    def test_run_summary(self):
        data = self.run_main([copy.deepcopy(BILL), {**BILL, "id": "XX-2", "legislature_url": None}])
        self.assertEqual(data["last_checked"], "2000-01-01")
        self.assertEqual(data["last_run"], sm.TODAY)
        self.assertEqual(data["last_run_summary"], {
            "checked": 2, "verified": 1, "unchecked": 1, "issues_opened": 0,
            "unchecked_ids": ["XX-2"],
        })
        self.assertIn("2 checked, 1 verified, 1 unchecked, 0 issue(s) opened", self.log)
        self.assertIn("Unchecked: XX-2", self.log)

    def test_news_link_is_not_official(self):
        data = self.run_main([{**BILL, "legislature_url": "https://www.votebeat.org/a/991"}],
                             answer={"changed": True, "new_status": "Signed"})
        self.assert_unchecked(data, "not an official source")
        self.create.assert_not_called()
        self.assertEqual(self.page_gets, [])
        self.assertEqual(self.issue_posts, [])

    def test_official_hosts(self):
        for url in ("https://www.flsenate.gov/x", "https://www.njleg.state.nj.us/x", "https://A.GOV/x"):
            self.assertTrue(sm.is_official_source(url), url)
        for url in ("https://votebeat.org/x.gov", "https://a.gov.example.com/", "https://a.gov@b.org/",
                    "https://fakegov/", "https://[bad", "nonsense"):
            self.assertFalse(sm.is_official_source(url), url)

    def test_two_year_session_can_verify(self):
        page = response(text=PAGE.replace("2026 Session", "2025-2026 Legislative Session"))
        data = self.run_main(page=page)
        self.assert_verified(data["bills"][0])
        self.assertEqual(self.create.call_args[1]["max_tokens"], 1500)
        prompt = self.create.call_args[1]["messages"][0]["content"]
        self.assertIn("2025-2026 Legislative Session", prompt)
        self.assertIn("two-year session or term that includes 2026", prompt)
        self.assertIn("Never guess.", prompt)
        self.assertNotIn("2026 legislative session only", prompt)

    def test_short_page_is_unchecked(self):
        for length in (300, 499):
            with self.subTest(length):
                page = response(text="HB 991 ".ljust(length, "x"))
                self.assert_unchecked(self.run_main(page=page), sm.NO_BILL_TEXT)
                self.create.assert_not_called()
        self.assertEqual(sm.NO_BILL_TEXT, "page has no readable bill text (may be built by script)")
        self.run_main(page=response(text="HB 991 ".ljust(500, "x")))
        self.create.assert_called_once()

    def test_page_without_bill_number_is_unchecked(self):
        self.assert_unchecked(self.run_main(page=response(text=PAGE.replace("991", "992"))),
                              sm.NO_BILL_TEXT)
        self.create.assert_not_called()

    def test_first_bill_number_is_used(self):
        text = "x" * 500
        self.assertTrue(sm.has_bill_text({"bill": "HB 965 / SB 322"}, text + " HB965"))
        self.assertFalse(sm.has_bill_text({"bill": "HB 965 / SB 322"}, text + " SB322"))
        self.assertFalse(sm.has_bill_text({"bill": "Executive Order"}, text))

    def test_retry_succeeds(self):
        for status in (429, 503):
            with self.subTest(status):
                data = self.run_main(page=[response(status, headers={"Retry-After": "7"}),
                                           response(text=PAGE)])
                self.assert_verified(data["bills"][0])
                self.assertEqual(len(self.page_gets), 2)
                self.assertEqual(self.sleeps, [7])

    def test_retry_only_once(self):
        data = self.run_main(page=[response(429), response(429), response(text=PAGE)])
        self.assert_unchecked(data, "fetch error: HTTP 429")
        self.assertEqual(len(self.page_gets), 2)
        self.assertEqual(self.sleeps, [20])
        self.create.assert_not_called()

    def test_final_code_is_recorded(self):
        self.assert_unchecked(self.run_main(page=[response(429), response(403)]),
                              "fetch error: HTTP 403")

    def test_other_errors_are_not_retried(self):
        self.run_main(page=response(403))
        self.assertEqual(len(self.page_gets), 1)
        self.assertEqual(self.sleeps, [])

    def test_retry_wait(self):
        for value, seconds in ((None, 20), ("soon", 20), ("30", 30), ("600", 60), ("-5", 0)):
            self.assertEqual(sm.retry_wait(value), seconds, value)
        self.assertEqual(sm.retry_wait("Fri, 31 Dec 2100 23:59:59 GMT"), 60)
        self.assertEqual(sm.retry_wait("Sat, 01 Jan 2000 00:00:00 GMT"), 0)

    def test_same_host_is_spaced_out(self):
        self.run_main([copy.deepcopy(BILL), {**BILL, "id": "XX-2"}])
        self.assertEqual(self.sleeps, [5])
        # A short Retry-After still keeps the 5 second gap
        self.run_main(page=[response(429, headers={"Retry-After": "1"}), response(text=PAGE)])
        self.assertEqual(self.sleeps, [1, 4])

    def test_different_hosts_do_not_wait(self):
        with mock.patch.object(sm, "requests"), mock.patch.object(sm, "_last_request", {}), \
                mock.patch.object(sm, "time") as fake_time:
            sm.polite_get("https://a.gov/1")
            sm.polite_get("https://b.gov/1")
            fake_time.sleep.assert_not_called()

    def test_headers(self):
        self.run_main()
        headers = self.page_gets[0]["headers"]
        self.assertEqual(headers["User-Agent"],
                         "ElectionRiskMap-Monitor/1.1 (+https://electionriskmap.org/about.html)")
        self.assertIn("text/html", headers["Accept"])
        self.assertIn("en-US", headers["Accept-Language"])

    def test_fetch_log_line(self):
        text = sm.html_to_text(PAGE)
        self.run_main()
        self.assertIn(f"HTTP 200, {len(text)} characters: {text[:200]}\n", self.log)
        self.assertNotIn(text[:201], self.log)
        self.run_main(page=response(403, text="Denied"))
        self.assertIn("HTTP 403, 6 characters: Denied", self.log)

    def test_reply_with_words_around_json_parses(self):
        for raw in (f"Here it is:\n{json.dumps(ANSWER)}\nDone.",
                    f"```json\n{json.dumps(ANSWER)}\n```"):
            with self.subTest(raw[:10]):
                self.assert_verified(self.run_main(raw=raw)["bills"][0])

    def test_reply_without_json_is_unreadable(self):
        for raw in ("I cannot tell. " * 40, "} backwards {", "{cut off"):
            with self.subTest(raw[:10]):
                self.assert_unchecked(self.run_main(raw=raw), "unreadable answer")
                self.assertIn(repr(raw.strip()[:200]), self.log)

    def test_page_text(self):
        self.assertEqual(sm.html_to_text(PAGE), "HB 991 2026 Session 04/01/2026 Signed by Governor"
                         + " Bill text." * 60)
        self.assertEqual(len(sm.html_to_text("y" * 40000)), 30000)


if __name__ == "__main__":
    unittest.main()
