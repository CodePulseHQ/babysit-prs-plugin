#!/usr/bin/env python3
"""Tests for babysit-prs-title.py, run as a subprocess (it's a hook script,
invoked as a standalone process reading JSON from stdin - not imported)."""
import json
import os
import subprocess
import sys
import unittest

HOOK_PATH = os.path.join(os.path.dirname(__file__), "..", "babysit-prs-title.py")


def run_hook(prompt, cwd=".", gh_repo_name=None, gh_pr_number=None):
    """Runs the real hook script in a subprocess, with `gh`/`git` stubbed out
    via a fake PATH entry so tests don't depend on an actual repo or network."""
    fake_bin = os.path.join(os.path.dirname(__file__), "_fake_bin")
    env = dict(os.environ)
    env["PATH"] = fake_bin + os.pathsep + env.get("PATH", "")
    env["FAKE_GH_REPO_NAME"] = gh_repo_name or ""
    env["FAKE_GH_PR_NUMBER"] = gh_pr_number or ""
    result = subprocess.run(
        [sys.executable, HOOK_PATH],
        input=json.dumps({"prompt": prompt, "cwd": cwd}),
        capture_output=True, text=True, timeout=10, env=env,
    )
    return result.stdout.strip()


class TestSessionTitleHook(unittest.TestCase):
    def test_full_pr_url(self):
        out = run_hook("/babysit-prs:babysit-prs https://github.com/ashalliants/ninfer-gateway/pull/2")
        self.assertEqual(
            json.loads(out)["hookSpecificOutput"]["sessionTitle"],
            "ninfer-gateway#2",
        )

    def test_full_pr_url_trailing_slash(self):
        out = run_hook("/babysit-prs https://github.com/owner/some-repo/pull/42/")
        self.assertEqual(
            json.loads(out)["hookSpecificOutput"]["sessionTitle"],
            "some-repo#42",
        )

    def test_plain_pr_number(self):
        out = run_hook("/babysit-prs 287", gh_repo_name="myrepo")
        self.assertEqual(
            json.loads(out)["hookSpecificOutput"]["sessionTitle"],
            "myrepo#287",
        )

    def test_all_argument(self):
        out = run_hook("/babysit-prs all", gh_repo_name="myrepo")
        self.assertEqual(
            json.loads(out)["hookSpecificOutput"]["sessionTitle"],
            "myrepo#ALL",
        )

    def test_no_argument_uses_current_pr(self):
        out = run_hook("/babysit-prs", gh_repo_name="myrepo", gh_pr_number="99")
        self.assertEqual(
            json.loads(out)["hookSpecificOutput"]["sessionTitle"],
            "myrepo#99",
        )

    def test_plugin_qualified_invocation(self):
        out = run_hook("/babysit-prs:babysit-prs 5", gh_repo_name="myrepo")
        self.assertEqual(
            json.loads(out)["hookSpecificOutput"]["sessionTitle"],
            "myrepo#5",
        )

    def test_unrecognized_argument_produces_no_output(self):
        out = run_hook("/babysit-prs some-branch-name", gh_repo_name="myrepo")
        self.assertEqual(out, "")

    def test_non_matching_prompt_produces_no_output(self):
        out = run_hook("do something unrelated")
        self.assertEqual(out, "")

    def test_no_current_pr_produces_no_output(self):
        out = run_hook("/babysit-prs", gh_repo_name="myrepo", gh_pr_number="")
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
