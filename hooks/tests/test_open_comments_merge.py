#!/usr/bin/env python3
"""Tests for the pure merge-mode helpers in open_comments.py (stack
annotation and merge-readiness). Imported directly; no gh/network needed."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "skills", "babysit-prs"))
import open_comments as oc  # noqa: E402


def pr(number, head, base, **kw):
    return {"number": number, "headRefName": head, "baseRefName": base, **kw}


class TestAnnotateStacks(unittest.TestCase):
    def test_plain_prs_are_depth_zero(self):
        prs = oc.annotate_stacks([pr(1, "a", "main"), pr(2, "b", "main")], "main")
        for p in prs:
            self.assertEqual((p["stackParent"], p["stackDepth"], p["stackRoot"]), (None, 0, p["number"]))

    def test_three_deep_stack(self):
        prs = oc.annotate_stacks(
            [pr(3, "c", "b"), pr(1, "a", "main"), pr(2, "b", "a")], "main")
        by = {p["number"]: p for p in prs}
        self.assertEqual((by[1]["stackDepth"], by[1]["stackRoot"]), (0, 1))
        self.assertEqual((by[2]["stackParent"], by[2]["stackDepth"], by[2]["stackRoot"]), (1, 1, 1))
        self.assertEqual((by[3]["stackParent"], by[3]["stackDepth"], by[3]["stackRoot"]), (2, 2, 1))

    def test_base_branch_with_no_open_pr_is_not_stacked(self):
        p = oc.annotate_stacks([pr(5, "x", "some-feature")], "main")[0]
        self.assertIsNone(p["stackParent"])

    def test_cycle_does_not_hang(self):
        oc.annotate_stacks([pr(1, "a", "b"), pr(2, "b", "a")], "main")


def state(**kw):
    s = {"state": "OPEN", "isDraft": False, "approved": True, "reviewDecision": "APPROVED",
         "baseRefName": "main", "ciFailing": False, "failingChecks": [],
         "pendingChecks": [], "mergeStateStatus": "CLEAN"}
    s.update(kw)
    return s


class TestMergeBlockers(unittest.TestCase):
    def test_ready(self):
        self.assertEqual(oc.merge_blockers(state(), 0, "main"), [])

    def test_each_blocker(self):
        cases = [
            (state(state="MERGED"), 0, "state=MERGED"),
            (state(isDraft=True), 0, "draft"),
            (state(approved=False, reviewDecision="CHANGES_REQUESTED"), 0, "not-approved"),
            (state(baseRefName="parent"), 0, "base=parent"),
            (state(ciFailing=True, failingChecks=["lint"]), 0, "ci-failing(lint)"),
            (state(pendingChecks=["unit"]), 0, "ci-pending(unit)"),
            (state(), 2, "open-threads(2)"),
            (state(mergeStateStatus="BEHIND"), 0, "mergeState=BEHIND"),
        ]
        for s, threads, expected in cases:
            with self.subTest(expected=expected):
                joined = "; ".join(oc.merge_blockers(s, threads, "main"))
                self.assertIn(expected, joined)


if __name__ == "__main__":
    unittest.main()
