"""Tests for the approval gate in state_apply_update.py. No network, no git, no writes.

Run: python3 -m unittest -v test_state_apply_update
"""

import importlib
import os
import sys
import unittest
from unittest import mock

ISSUE = {"body": "**Bill ID:** `XX-1`\n**Detected:** Passed House"}
REPO = "example/repo"
BILLS = '{"bills": [{"id": "XX-1", "status": "Old", "last_changed": "2000-01-01", "last_checked": "2000-01-01"}]}'


class ApprovalGateTest(unittest.TestCase):
    def run_main(self, body, permission="write", request_error=None, author="someone"):
        """Run main() and record exit code, requests, subprocess calls and file writes."""
        fake_requests = mock.MagicMock(name="requests")

        def fake_get(url, **kwargs):
            resp = mock.MagicMock()
            if url.endswith("/permission"):
                if request_error:
                    raise request_error
                resp.json.return_value = {"permission": permission}
            else:
                resp.json.return_value = ISSUE
            return resp

        fake_requests.get.side_effect = fake_get
        fake_requests.post.return_value.json.return_value = {"html_url": "http://pr.invalid"}
        env = {
            "GITHUB_TOKEN": "test-token",
            "GITHUB_REPOSITORY": REPO,
            "ISSUE_NUMBER": "1",
            "COMMENT_BODY": body,
            "COMMENT_AUTHOR": author,
        }
        with mock.patch.dict(os.environ, env), \
                mock.patch.dict(sys.modules, {"requests": fake_requests}):
            sys.modules.pop("state_apply_update", None)
            mod = importlib.import_module("state_apply_update")
            bills, index = mock.MagicMock(), mock.MagicMock()
            bills.read_text.return_value = BILLS
            run = mock.MagicMock(return_value=mock.MagicMock(returncode=0))
            with mock.patch.object(mod, "BILLS_JSON", bills), \
                    mock.patch.object(mod, "INDEX_HTML", index), \
                    mock.patch.object(mod.subprocess, "run", run):
                code = None
                try:
                    mod.main()
                except SystemExit as exc:
                    code = exc.code
        return code, fake_requests, run, bills, index, mod

    def assert_skipped(self, result):
        code, req, run, bills, index, _ = result
        self.assertEqual(code, 0)
        req.post.assert_not_called()
        req.patch.assert_not_called()
        run.assert_not_called()
        bills.write_text.assert_not_called()
        index.write_text.assert_not_called()

    def assert_proceeded(self, result):
        code, req, run, bills, index, mod = result
        self.assertIsNone(code)
        self.assertEqual(
            req.get.call_args_list[0][0][0],
            f"https://api.github.com/repos/{REPO}/collaborators/someone/permission",
        )
        self.assertEqual(
            req.get.call_args_list[0][1]["headers"]["Authorization"], "token test-token"
        )
        bills.write_text.assert_called_once()
        written = mod.json.loads(bills.write_text.call_args[0][0])["bills"][0]
        self.assertEqual(written["status"], "Passed House")
        self.assertEqual(written["last_checked"], "2000-01-01")
        index.read_text.assert_not_called()
        index.write_text.assert_not_called()
        run_cmds = [c[0][0] for c in run.call_args_list]
        self.assertIn(["git", "add", "data/bills.json"], run_cmds)
        pr_body = req.post.call_args_list[0][1]["json"]["body"]
        self.assertIn("Review before merging. Merging to main may deploy the site.", pr_body)
        self.assertNotIn("index.html", pr_body)

    def test_approved_from_writer_proceeds(self):
        self.assert_proceeded(self.run_main("approved"))

    def test_trimmed_mixed_case_from_admin_and_maintain_proceeds(self):
        for permission in ("admin", "maintain"):
            with self.subTest(permission):
                self.assert_proceeded(self.run_main("  Approved\r\n", permission))

    def test_not_approved_skips(self):
        result = self.run_main("not approved")
        self.assert_skipped(result)
        result[1].get.assert_not_called()

    def test_approved_question_mark_skips(self):
        result = self.run_main("Approved?")
        self.assert_skipped(result)
        result[1].get.assert_not_called()

    def test_approved_from_read_only_user_skips(self):
        result = self.run_main("approved", permission="read")
        self.assert_skipped(result)
        self.assertEqual(result[1].get.call_count, 1)

    def test_failed_permission_request_skips(self):
        self.assert_skipped(self.run_main("approved", request_error=OSError("network down")))

    def test_missing_author_skips(self):
        self.assert_skipped(self.run_main("approved", author=""))


if __name__ == "__main__":
    unittest.main()
