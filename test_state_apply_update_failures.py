"""Tests for git and PR failures in state_apply_update.py. No network, no git, no writes.

Run: python3 -m unittest -v test_state_apply_update_failures
"""

import importlib
import os
import sys
import unittest
from unittest import mock

ISSUE = {"body": "**Bill ID:** `XX-1`\n**Detected:** Passed House"}
BILLS = '{"bills": [{"id": "XX-1", "status": "Old", "last_changed": "2000-01-01"}]}'
API = "https://api.github.com/repos/example/repo"


class ApplyFailureTest(unittest.TestCase):
    def run_main(self, fail_git=None, pr_ok=True, pr_error=None, link_error=None):
        req = mock.MagicMock(name="requests")

        def fake_get(url, **kwargs):
            resp = mock.MagicMock()
            resp.json.return_value = {"permission": "write"} if url.endswith("/permission") else ISSUE
            return resp

        def fake_post(url, **kwargs):
            if url.endswith("/pulls"):
                if pr_error:
                    raise pr_error
                resp = mock.MagicMock(ok=pr_ok, status_code=201 if pr_ok else 403, text="denied")
                resp.json.return_value = {"html_url": "http://pr.invalid"}
                return resp
            if link_error:
                raise link_error
            return mock.MagicMock(ok=True)

        req.get.side_effect = fake_get
        req.post.side_effect = fake_post

        def fake_run(cmd, **kwargs):
            return mock.MagicMock(returncode=1 if cmd[1] == fail_git else 0, stderr="failed")

        env = {
            "GITHUB_TOKEN": "test-token", "GITHUB_REPOSITORY": "example/repo",
            "ISSUE_NUMBER": "1", "COMMENT_BODY": "approved", "COMMENT_AUTHOR": "someone",
        }
        with mock.patch.dict(os.environ, env), mock.patch.dict(sys.modules, {"requests": req}):
            sys.modules.pop("state_apply_update", None)
            mod = importlib.import_module("state_apply_update")
            bills = mock.MagicMock()
            bills.read_text.return_value = BILLS
            self.code, self.error = None, None
            with mock.patch.object(mod, "BILLS_JSON", bills), \
                    mock.patch.object(mod.subprocess, "run", side_effect=fake_run) as run, \
                    mock.patch("builtins.print"):
                try:
                    mod.main()
                except SystemExit as exc:
                    self.code = exc.code
                except Exception as exc:
                    self.error = exc
        self.git = [c[0][0][1] for c in run.call_args_list]
        self.posts = [c[0][0] for c in req.post.call_args_list]
        self.comments = [c[1]["json"]["body"] for c in req.post.call_args_list
                         if c[0][0] == f"{API}/issues/1/comments"]
        self.patch = req.patch

    def assert_left_open_with_comment(self):
        self.patch.assert_not_called()
        self.assertEqual(len(self.comments), 1)
        self.assertIn("update failed", self.comments[0])
        self.assertIn("Actions log", self.comments[0])

    def test_failed_push_stops_before_pr(self):
        self.run_main(fail_git="push")
        self.assertEqual(self.code, 1)
        self.assertNotIn(f"{API}/pulls", self.posts)
        self.assert_left_open_with_comment()

    def test_stops_on_first_failed_git_command(self):
        self.run_main(fail_git="checkout")
        self.assertEqual(self.code, 1)
        self.assertEqual(self.git[-1], "checkout")
        self.assertNotIn("push", self.git)
        self.assert_left_open_with_comment()

    def test_failed_pr_leaves_issue_open(self):
        self.run_main(pr_ok=False)
        self.assertEqual(self.code, 1)
        self.assertIn(f"{API}/pulls", self.posts)
        self.assert_left_open_with_comment()

    def test_pr_request_error_leaves_issue_open(self):
        self.run_main(pr_error=OSError("down"))
        self.assertIsInstance(self.error, OSError)
        self.assert_left_open_with_comment()

    def test_opened_pr_closes_issue(self):
        self.run_main()
        self.assertIsNone(self.code)
        self.patch.assert_called_once()
        self.assertEqual(len(self.comments), 1)
        self.assertIn("http://pr.invalid", self.comments[0])

    def test_link_comment_error_still_closes_issue(self):
        self.run_main(link_error=OSError("down"))
        self.assertIsNone(self.error)
        self.patch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
