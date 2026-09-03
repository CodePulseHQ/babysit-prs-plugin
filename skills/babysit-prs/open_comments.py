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
"""
import argparse
import json
import subprocess
import sys


def gh(args):
    """Run a gh command, return stdout, exit on failure."""
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        sys.exit(p.returncode)
    return p.stdout


def detect_repo():
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
        "--json", "number,title,reviewDecision,baseRefName,url,isDraft",
    ]))
    out = [
        p for p in prs
        if not p.get("isDraft")
        and p.get("reviewDecision") != "APPROVED"
        and p.get("baseRefName") == default_branch
    ]
    out.sort(key=lambda p: p["number"])
    return default_branch, out


def fetch_pr_checks(pr):
    """CI check status. `gh pr checks` exits non-zero when checks are failing
    (1) or still pending (8) - that's normal signal, not a call failure, so
    this does NOT use the gh() helper (which sys.exit()s on non-zero). Only
    an empty/unparseable stdout (no checks configured, transient gh error)
    is treated as "no checks"."""
    p = subprocess.run(
        ["gh", "pr", "checks", str(pr), "--json", "name,state,bucket,link"],
        capture_output=True, text=True,
    )
    out = p.stdout.strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def fetch_pr_state(pr):
    """One `gh pr view` (+ `gh pr checks`) covering every per-cycle
    exit-condition signal, INCLUDING CI - folded in here so a cycle can never
    skip checking CI just because there are no new comments."""
    s = json.loads(gh([
        "pr", "view", str(pr), "--json",
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

    checks = fetch_pr_checks(pr)
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
    a = ap.parse_args()

    repo = a.repo or detect_repo()
    me = a.me or detect_me()

    if a.list:
        default_branch, prs = list_babysittable_prs(repo, me)
        if a.json:
            print(json.dumps({
                "repo": repo, "me": me, "defaultBranch": default_branch,
                "prs": prs,
            }, indent=2))
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

    state = fetch_pr_state(a.pr)
    threads = [normalize_thread(n, me) for n in fetch_review_threads(owner, name, a.pr)]
    total = len(threads)
    if not a.all:
        threads = [t for t in threads if not t["isResolved"]]
    open_count = len(threads)
    # Re-order: most recent activity first, then cap. THIS is the step that makes
    # the newest blockers visible regardless of where they sit in creation order.
    threads.sort(key=lambda t: t["lastAt"], reverse=True)
    threads = threads[: a.limit]

    issues = fetch_issue_comments(repo, a.pr, me)[: a.limit]
    reviews = fetch_reviews(owner, name, a.pr, me)[: a.limit]

    if a.json:
        print(json.dumps({
            "repo": repo, "pr": a.pr, "me": me,
            "prState": state,
            "totalThreads": total, "openThreads": open_count,
            "reviewThreads": threads, "issueComments": issues,
            "reviews": reviews,
        }, indent=2))
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
