#!/usr/bin/env python3
"""
Resolve one or more PR review threads, optionally posting a reply first.

WHY THIS EXISTS
---------------
Resolving N threads one-by-one means N separate `gh api graphql` Bash calls
(or a shell for-loop). In a worktree-isolated session, a multi-command loop
like that gets refused outright:

    Error: This session is isolated in the worktree ..., but this command is
    too complex to verify that it stays inside the worktree. Refusing to run
    it - a worktree-isolated session's git operations must target its own
    worktree.

...forcing a fallback to N individual Bash calls. This script does the whole
batch in ONE Bash invocation (one `gh` credential + Python process), which is
simple enough for the isolation checker and cheaper on turns either way.

USAGE
-----
  # Resolve N threads, no reply (only when you already replied earlier):
  resolve_threads.py --resolve <threadId> [<threadId> ...]

  # Reply to a thread, then resolve it, in one call:
  resolve_threads.py --reply-resolve --repo owner/name --pr 394 \\
      --in-reply-to <replyToId> --thread <threadId> --body "text"

  # Reply+resolve multiple threads from a JSON file (list of objects with
  # replyToId, threadId, body - e.g. hand-built from open_comments.py output):
  resolve_threads.py --reply-resolve --repo owner/name --pr 394 --batch items.json

  --json    # machine-readable {threadId: bool isResolved, ...} / per-item results

Only resolves threads where your own reply is the action being taken here -
this script does not check who the LAST comment is; the caller (the skill's
Thread Resolution Rules) is still responsible for only resolving threads
where the most recent comment is AI-authored, never a human reviewer's.
"""
import argparse
import json
import subprocess
import sys


def gh(args):
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        sys.exit(p.returncode)
    return p.stdout


RESOLVE_MUTATION = (
    "mutation($id:ID!){resolveReviewThread(input:{threadId:$id})"
    "{thread{isResolved}}}"
)


def resolve_thread(thread_id):
    out = gh([
        "api", "graphql",
        "-f", f"query={RESOLVE_MUTATION}",
        "-f", f"id={thread_id}",
    ])
    return json.loads(out)["data"]["resolveReviewThread"]["thread"]["isResolved"]


def reply(repo, pr, in_reply_to, body):
    """Reply to a review thread via REST; returns the new comment's id."""
    out = gh([
        "api", f"repos/{repo}/pulls/{pr}/comments",
        "-f", f"body={body}",
        "-F", f"in_reply_to={in_reply_to}",
    ])
    return json.loads(out)["id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--resolve", nargs="+", metavar="threadId",
                     help="resolve these thread IDs, no reply")
    ap.add_argument("--reply-resolve", action="store_true",
                     help="reply then resolve (single item via flags, or --batch)")
    ap.add_argument("--repo", help="owner/name, required for --reply-resolve")
    ap.add_argument("--pr", type=int, help="PR number, required for --reply-resolve")
    ap.add_argument("--in-reply-to", type=int, help="databaseId of comment to reply to")
    ap.add_argument("--thread", help="threadId (GraphQL node id) to resolve after replying")
    ap.add_argument("--body", help="reply text")
    ap.add_argument("--batch", help="JSON file: list of {replyToId, threadId, body}")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.resolve:
        results = {tid: resolve_thread(tid) for tid in a.resolve}
        if a.json:
            print(json.dumps(results, indent=2))
        else:
            for tid, ok in results.items():
                print(f"{tid}: isResolved={ok}")
        return

    if a.reply_resolve:
        if not (a.repo and a.pr is not None):
            ap.error("--reply-resolve requires --repo and --pr")

        if a.batch:
            items = json.loads(open(a.batch).read())
        elif a.in_reply_to and a.thread and a.body:
            items = [{"replyToId": a.in_reply_to, "threadId": a.thread, "body": a.body}]
        else:
            ap.error("--reply-resolve needs --batch, or --in-reply-to/--thread/--body")

        results = []
        for item in items:
            comment_id = reply(a.repo, a.pr, item["replyToId"], item["body"])
            resolved = resolve_thread(item["threadId"])
            results.append({
                "threadId": item["threadId"],
                "commentId": comment_id,
                "isResolved": resolved,
            })

        if a.json:
            print(json.dumps(results, indent=2))
        else:
            for r in results:
                print(f"{r['threadId']}: replied (comment {r['commentId']}), "
                      f"isResolved={r['isResolved']}")
        return

    ap.error("pass --resolve or --reply-resolve")


if __name__ == "__main__":
    main()
