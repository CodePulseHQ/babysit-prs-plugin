#!/usr/bin/env python3
"""
Surface the most recent OPEN review activity on a PR, across ALL pages,
newest-first, capped.

WHY THIS EXISTS
---------------
GitHub's GraphQL `reviewThreads(first: N)` silently TRUNCATES. On a long-lived
PR the threads come back in creation order, so the newest *unresolved* threads
sort to the very END of the list. A single unpaginated page (`first: 100`) on a
PR with 300+ threads therefore hides exactly the comments you need to act on -
you see 100 ancient, already-resolved threads and miss the 4 fresh blockers.

This script paginates EVERYTHING, filters to threads that still need attention,
re-orders by most-recent activity, and caps the output so you only read what
matters.

USAGE
-----
  open_comments.py --pr 622 [--repo owner/name] [--me <login>] [--limit 30]
                   [--all]        # include resolved threads too (default: only open)
                   [--json]       # raw JSON (default: human-readable summary)

  open_comments.py --list [--repo owner/name] [--me <login>] [--json]
                   # "all" mode PR discovery: your open, non-draft, non-approved PRs
                   # targeting the repo's default branch (main/master/develop),
                   # oldest PR number first. One gh+jq round trip instead of two.

Defaults: --repo from `gh repo view`, --me from `gh api user`, --limit 30.

OUTPUT (per review thread)
  threadId        - GraphQL node id (pass to resolveReviewThread)
  replyToId       - databaseId of the FIRST comment (pass to REST --in_reply_to)
  isResolved      - bool
  path:line       - location
  lastAuthor      - login of the most recent comment's author
  needsReply      - true when lastAuthor != you (a new comment you haven't answered)
  lastAt          - ISO timestamp of the most recent comment (sort key)
  snippet         - first ~200 chars of the first comment

Also lists the most recent issue-level (non-thread) comments the same way, and
the BODY of every review by someone other than you, newest-first (`reviews` in
--json). A review body is where a bot (codepulse) or human posts a should-fix
SUMMARY with no inline thread - invisible to reviewThreads/issue-comments - so a
CHANGES_REQUESTED review with 0 open threads is NOT automatically stale. Read the
newest review body every cycle.

It ALSO reports PR state in the same run (saves a separate `gh pr view` call):
  reviewDecision, state, mergeable, mergeStateStatus, baseRefName, and a derived
  `needsRebase` flag (true when mergeStateStatus is BEHIND/DIRTY or mergeable is
  CONFLICTING) so the per-cycle exit-condition checks come from one invocation.

MERGE MODE (opt-in, `--merge`)
  --list --merge   keeps approved PRs and PRs stacked on another of your open PRs,
                   adds stackParent/stackDepth/stackRoot, sorts bottom-of-stack
                   first, and reports the repo's allowedMergeMethods.
  --pr N --merge   adds mergeReady / mergeBlockers (approved, CI green, no open
                   threads, mergeState CLEAN, base == default branch).
"""
import argparse
import json
import re
import subprocess
import sys


# Windows defaults to a legacy codepage (e.g. cp1252); force UTF-8 so emoji in
# comments don't crash stdin/stdout handling.
for _s in (sys.stdin, sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace")


def gh(args):
    """Run a gh command, return stdout, exit on failure."""
    p = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        sys.exit(p.returncode)
    return p.stdout


def detect_repo():
    """Resolve owner/repo for the CWD's git checkout.

    Prefer the `origin` remote's URL directly. `gh repo view` resolves the
    "current" repo from ALL configured remotes, and when a fork also has an
    `upstream` remote pointing at the parent repo, gh can pick `upstream`
    instead of `origin` - silently operating on the wrong repo (wrong PR
    numbers, wrong issue/comment targets). Reading `origin` explicitly avoids
    that ambiguity; only fall back to `gh repo view` if there's no `origin`
    remote or it isn't a GitHub URL.
    """
    p = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if p.returncode == 0:
        m = re.search(
            r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?/?$", p.stdout.strip()
        )
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return json.loads(gh(["repo", "view", "--json", "nameWithOwner"]))["nameWithOwner"]


def detect_default_branch():
    return json.loads(
        gh(["repo", "view", "--json", "defaultBranchRef"])
    )["defaultBranchRef"]["name"]


def detect_me():
    return gh(["api", "user", "--jq", ".login"]).strip()


def list_babysittable_prs(repo, me):
    """'all' mode PR discovery: open, non-draft, non-approved, targeting the
    default branch, oldest number first. Mirrors the filter/sort babysit-prs
    applies for `all` mode, in one gh call instead of two plus a jq pipeline.

    Drafts are excluded: a draft isn't ready for review yet, so there's
    nothing to babysit - reviewers won't comment and CI is often not even
    required to pass. Explicit `/babysit-prs <number>` still works on a draft
    if you ask for it by number; only `all` mode's auto-discovery skips them.
    """
    default_branch = detect_default_branch()
    prs = json.loads(gh([
        "pr", "list", "--repo", repo, "--author", me, "--state", "open",
        "--json", "number,title,reviewDecision,baseRefName,headRefName,url,isDraft",
    ]))
    out = [
        p for p in prs
        if not p.get("isDraft")
        and p.get("reviewDecision") != "APPROVED"
        and p.get("baseRefName") == default_branch
    ]
    out.sort(key=lambda p: p["number"])
    return default_branch, out


def annotate_stacks(prs, default_branch):
    """Add stack info to each PR (mutates and returns `prs`).

    A PR is "stacked" when its base branch is another open PR's head branch
    instead of the default branch. Adds:
      stackParent - number of the open PR it is stacked on (None if none)
      stackDepth  - 0 for a PR targeting the default branch, 1 for a PR on
                    top of that, and so on
      stackRoot   - number of the bottom PR of its stack (itself when depth 0)
    """
    by_head = {p["headRefName"]: p for p in prs if p.get("headRefName")}

    def parent_of(p):
        if p.get("baseRefName") == default_branch:
            return None
        return by_head.get(p.get("baseRefName"))

    for p in prs:
        depth, root, seen = 0, p, {p["number"]}
        while True:
            parent = parent_of(root)
            if parent is None or parent["number"] in seen:  # seen: cycle guard
                break
            seen.add(parent["number"])
            depth, root = depth + 1, parent
        direct = parent_of(p)
        p["stackParent"] = direct["number"] if direct else None
        p["stackDepth"] = depth
        p["stackRoot"] = root["number"]
    return prs


def list_mergeable_candidates(repo, me):
    """'all merge' mode PR discovery. Unlike list_babysittable_prs this KEEPS
    approved PRs (they stay monitored until they merge) and keeps PRs stacked
    on another open PR of yours. Sorted bottom-of-stack first (depth, then PR
    number) so merging naturally goes in the only safe order."""
    default_branch = detect_default_branch()
    prs = json.loads(gh([
        "pr", "list", "--repo", repo, "--author", me, "--state", "open",
        "--json", "number,title,reviewDecision,baseRefName,headRefName,url,isDraft",
    ]))
    prs = [p for p in prs if not p.get("isDraft")]
    annotate_stacks(prs, default_branch)
    out = [
        p for p in prs
        if p["baseRefName"] == default_branch or p["stackParent"] is not None
    ]
    out.sort(key=lambda p: (p["stackDepth"], p["number"]))
    return default_branch, out


def allowed_merge_methods(repo):
    """Merge methods the repo permits, as `gh pr merge` flag names."""
    r = json.loads(gh([
        "repo", "view", repo, "--json",
        "mergeCommitAllowed,squashMergeAllowed,rebaseMergeAllowed",
    ]))
    return [m for m, key in (
        ("merge", "mergeCommitAllowed"),
        ("squash", "squashMergeAllowed"),
        ("rebase", "rebaseMergeAllowed"),
    ) if r.get(key)]


def merge_blockers(state, open_threads, default_branch):
    """Reasons this PR must NOT be merged right now (empty list = safe to merge).

    Merging is opt-in, so this is deliberately strict: approved, CI green,
    nothing unresolved, GitHub reports it CLEAN, and it targets the default
    branch (which means any parent in a stack has already merged and GitHub
    retargeted it - children never merge into a parent's branch)."""
    blockers = []
    if state.get("state") != "OPEN":
        blockers.append(f"state={state.get('state')}")
    if state.get("isDraft"):
        blockers.append("draft")
    if not state.get("approved"):
        blockers.append(f"not-approved(review={state.get('reviewDecision')})")
    if state.get("baseRefName") != default_branch:
        blockers.append(f"base={state.get('baseRefName')}(stacked, parent not merged yet)")
    if state.get("ciFailing"):
        blockers.append(f"ci-failing({','.join(state['failingChecks'])})")
    if state.get("pendingChecks"):
        blockers.append(f"ci-pending({','.join(state['pendingChecks'])})")
    if open_threads:
        blockers.append(f"open-threads({open_threads})")
    if state.get("mergeStateStatus") != "CLEAN":
        blockers.append(f"mergeState={state.get('mergeStateStatus')}")
    return blockers


def fetch_pr_checks(repo, pr):
    """CI check status. `gh pr checks` exits non-zero when checks are failing
    (1) or still pending (8) - that's normal signal, not a call failure, so
    this does NOT use the gh() helper (which sys.exit()s on non-zero). Only
    an empty/unparseable stdout (no checks configured, transient gh error)
    is treated as "no checks".

    Takes repo explicitly and passes --repo: without it, `gh pr checks`
    resolves the repo itself from the CWD's git remotes the same way `gh
    repo view` does, which silently picks the wrong PR number (and thus the
    wrong checks) in exactly the fork-with-upstream setups detect_repo() is
    designed to avoid for every other call in this script."""
    p = subprocess.run(
        ["gh", "pr", "checks", str(pr), "--repo", repo, "--json", "name,state,bucket,link"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = p.stdout.strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def fetch_pr_state(repo, pr):
    """One `gh pr view` (+ `gh pr checks`) covering every per-cycle
    exit-condition signal, INCLUDING CI - folded in here so a cycle can never
    skip checking CI just because there are no new comments.

    Takes repo explicitly and passes --repo, for the same reason
    fetch_pr_checks does: left to its own repo detection, `gh pr view <pr>`
    can resolve against `upstream` instead of the fork, reporting a
    different PR's state entirely (wrong review decision, wrong mergeable/
    mergeStateStatus, even CLOSED when the real PR is open)."""
    s = json.loads(gh([
        "pr", "view", str(pr), "--repo", repo, "--json",
        "reviewDecision,state,mergeable,mergeStateStatus,baseRefName,isDraft",
    ]))
    # BEHIND  = branch is behind base (needs rebase/update to merge)
    # DIRTY   = merge conflicts present
    # CONFLICTING (mergeable) = same, surfaced on the other field
    s["needsRebase"] = (
        s.get("mergeStateStatus") in ("BEHIND", "DIRTY")
        or s.get("mergeable") == "CONFLICTING"
    )
    s["approved"] = s.get("reviewDecision") == "APPROVED"

    checks = fetch_pr_checks(repo, pr)
    s["checks"] = checks
    s["failingChecks"] = [c["name"] for c in checks if c.get("bucket") == "fail"]
    s["pendingChecks"] = [c["name"] for c in checks if c.get("bucket") == "pending"]
    s["ciFailing"] = bool(s["failingChecks"])
    return s


REVIEW_THREADS_QUERY = """
query($owner:String!, $repo:String!, $pr:Int!, $cursor:String) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$pr) {
      reviewThreads(first:100, after:$cursor) {
        nodes {
          id
          isResolved
          first: comments(first:1) { nodes { databaseId path line body } }
          last: comments(last:1) { nodes { author { login } createdAt } }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def fetch_review_threads(owner, repo, pr):
    """Cursor-paginate ALL review threads (never trust a single page)."""
    nodes, cursor = [], None
    while True:
        args = [
            "api", "graphql",
            "-f", f"query={REVIEW_THREADS_QUERY}",
            "-F", f"owner={owner}", "-F", f"repo={repo}", "-F", f"pr={pr}",
        ]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        page = json.loads(gh(args))["data"]["repository"]["pullRequest"]["reviewThreads"]
        nodes.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    return nodes


REVIEWS_QUERY = """
query($owner:String!, $repo:String!, $pr:Int!, $cursor:String) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$pr) {
      reviews(first:100, after:$cursor) {
        nodes { author { login } state submittedAt body url }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def fetch_reviews(owner, repo, pr, me):
    """Cursor-paginate ALL reviews, keep those from others carrying a body.

    A review BODY is where a bot (codepulse) or a human posts a should-fix
    SUMMARY that has no inline thread attached - the single most-missed signal,
    because it never appears in reviewThreads/issue-comments. A re-submitted
    CHANGES_REQUESTED review keeps reviewDecision=CHANGES_REQUESTED even when
    every inline thread is resolved, so that decision is NOT necessarily stale:
    read the newest non-empty review body to see if a fresh finding is open.
    """
    nodes, cursor = [], None
    while True:
        args = [
            "api", "graphql",
            "-f", f"query={REVIEWS_QUERY}",
            "-F", f"owner={owner}", "-F", f"repo={repo}", "-F", f"pr={pr}",
        ]
        if cursor:
            args += ["-F", f"cursor={cursor}"]
        page = json.loads(gh(args))["data"]["repository"]["pullRequest"]["reviews"]
        nodes.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    out = []
    for r in nodes:
        author = (r.get("author") or {}).get("login")
        body = (r.get("body") or "").strip()
        if not body or not author or author == me:
            continue  # empty-body reviews carry no summary; skip your own
        out.append({
            "author": author,
            "state": r.get("state"),
            "submittedAt": r.get("submittedAt") or "",
            "url": r.get("url"),
            "snippet": body.replace("\n", " ")[:300],
        })
    out.sort(key=lambda r: r["submittedAt"], reverse=True)
    return out


def normalize_thread(node, me):
    first = (node["first"]["nodes"] or [{}])[0]
    last = (node["last"]["nodes"] or [{}])[0]
    last_author = (last.get("author") or {}).get("login")
    body = (first.get("body") or "").strip().replace("\n", " ")
    return {
        "threadId": node["id"],
        "replyToId": first.get("databaseId"),
        "isResolved": node["isResolved"],
        "path": first.get("path"),
        "line": first.get("line"),
        "lastAuthor": last_author,
        "needsReply": bool(last_author) and last_author != me,
        "lastAt": last.get("createdAt") or "",
        "snippet": body[:200],
    }


def fetch_issue_comments(repo, pr, me):
    """REST issue comments support server-side desc sort; still paginate all."""
    raw = gh([
        "api", "--paginate",
        f"repos/{repo}/issues/{pr}/comments?sort=created&direction=desc&per_page=100",
    ])
    # --paginate may concatenate multiple JSON arrays; merge them.
    comments = []
    dec = json.JSONDecoder()
    idx, n = 0, len(raw)
    while idx < n:
        while idx < n and raw[idx] in " \r\n\t":
            idx += 1
        if idx >= n:
            break
        obj, end = dec.raw_decode(raw, idx)
        comments.extend(obj if isinstance(obj, list) else [obj])
        idx = end
    out = []
    for c in comments:
        author = (c.get("user") or {}).get("login")
        out.append({
            "id": c.get("id"),
            "author": author,
            "needsReply": bool(author) and author != me,
            "createdAt": c.get("created_at"),
            "snippet": (c.get("body") or "").strip().replace("\n", " ")[:200],
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", type=int)
    ap.add_argument("--repo")
    ap.add_argument("--me")
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--all", action="store_true", help="include resolved threads")
    ap.add_argument("--json", action="store_true", help="raw JSON output")
    ap.add_argument("--list", action="store_true",
                     help="'all' mode PR discovery instead of single-PR detail")
    ap.add_argument("--merge", action="store_true",
                     help="merge mode: with --list, keep approved + stacked PRs and report "
                          "allowed merge methods; with --pr, report merge readiness")
    a = ap.parse_args()

    repo = a.repo or detect_repo()
    me = a.me or detect_me()

    if a.list:
        if a.merge:
            default_branch, prs = list_mergeable_candidates(repo, me)
            methods = allowed_merge_methods(repo)
        else:
            default_branch, prs = list_babysittable_prs(repo, me)
            methods = []
        if a.json:
            payload = {
                "repo": repo, "me": me, "defaultBranch": default_branch,
                "prs": prs,
            }
            if a.merge:
                payload["allowedMergeMethods"] = methods
            print(json.dumps(payload, indent=2))
        elif a.merge:
            print(f"repo={repo} me={me} defaultBranch={default_branch} "
                  f"allowedMergeMethods={','.join(methods)}")
            print(f"{len(prs)} open, non-draft PR(s) incl. approved + stacked "
                  "(bottom of stack first):")
            for p in prs:
                stack = f" stacked-on=#{p['stackParent']}" if p["stackParent"] else ""
                print(f"  #{p['number']} review={p.get('reviewDecision')} "
                      f"depth={p['stackDepth']}{stack}  {p.get('title', '')}")
        else:
            print(f"repo={repo} me={me} defaultBranch={default_branch}")
            print(f"{len(prs)} open, non-draft, non-approved PR(s) targeting {default_branch} "
                  "(oldest first):")
            for p in prs:
                print(f"  #{p['number']}  {p.get('title', '')}")
        return

    if a.pr is None:
        ap.error("--pr is required unless --list is given")

    owner, name = repo.split("/", 1)

    state = fetch_pr_state(repo, a.pr)
    threads = [normalize_thread(n, me) for n in fetch_review_threads(owner, name, a.pr)]
    total = len(threads)
    unresolved = sum(1 for t in threads if not t["isResolved"])
    if not a.all:
        threads = [t for t in threads if not t["isResolved"]]
    open_count = len(threads)
    # Re-order: most recent activity first, then cap. THIS is the step that makes
    # the newest blockers visible regardless of where they sit in creation order.
    threads.sort(key=lambda t: t["lastAt"], reverse=True)
    threads = threads[: a.limit]

    issues = fetch_issue_comments(repo, a.pr, me)[: a.limit]
    reviews = fetch_reviews(owner, name, a.pr, me)[: a.limit]

    blockers = merge_blockers(state, unresolved, detect_default_branch()) if a.merge else []

    if a.json:
        out = {
            "repo": repo, "pr": a.pr, "me": me,
            "prState": state,
            "totalThreads": total, "openThreads": open_count,
            "reviewThreads": threads, "issueComments": issues,
            "reviews": reviews,
        }
        if a.merge:
            out["mergeReady"] = not blockers
            out["mergeBlockers"] = blockers
        print(json.dumps(out, indent=2))
        return

    print(f"repo={repo} pr={a.pr} me={me}")
    # PR-state banner first: these are the per-cycle exit-condition signals.
    rebase = "NEEDS-REBASE" if state["needsRebase"] else "up-to-date"
    approved = "  *** APPROVED -> HARD EXIT ***" if state["approved"] else ""
    if state["ciFailing"]:
        ci = f"CI-FAILING({','.join(state['failingChecks'])})"
    elif state["pendingChecks"]:
        ci = f"ci-pending({','.join(state['pendingChecks'])})"
    else:
        ci = "ci-ok"
    print(f"state: {state.get('state')} review={state.get('reviewDecision')} "
          f"mergeable={state.get('mergeable')} mergeState={state.get('mergeStateStatus')} "
          f"base={state.get('baseRefName')} [{rebase}] [{ci}]{approved}")
    if a.merge:
        print("merge: " + ("*** MERGE-READY ***" if not blockers
                           else "NOT-READY " + "; ".join(blockers)))
    print(f"review threads: {open_count} open / {total} total "
          f"(showing {len(threads)} newest)")
    for t in threads:
        flag = "NEEDS-REPLY" if t["needsReply"] else ("answered" if t["isResolved"] is False else "")
        print(f"\n  [{flag}] {t['lastAt']}  {t['path']}:{t['line']}")
        print(f"    thread={t['threadId']} replyTo={t['replyToId']} lastBy={t['lastAuthor']}")
        print(f"    {t['snippet']}")
    new_issues = [c for c in issues if c["needsReply"]]
    print(f"\nissue comments: {len(new_issues)} not-from-you (showing {len(issues)} newest)")
    for c in issues:
        flag = "NEEDS-REPLY" if c["needsReply"] else "yours"
        print(f"  [{flag}] {c['createdAt']}  {c['author']}: {c['snippet'][:120]}")

    # Review BODIES (from others): the should-fix summaries that live on a review
    # and never appear as an inline thread. The NEWEST one is the current verdict
    # behind reviewDecision - read it even when 0 threads are open.
    print(f"\nreview summaries (from others, newest first): {len(reviews)}")
    for i, r in enumerate(reviews):
        flag = "LATEST - read this" if i == 0 else r["state"]
        print(f"\n  [{flag}] {r['submittedAt']}  {r['author']} ({r['state']})")
        print(f"    {r['url']}")
        print(f"    {r['snippet']}")


if __name__ == "__main__":
    main()
