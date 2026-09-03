#!/usr/bin/env python3
"""UserPromptSubmit hook: names the session "<repo>#<pr>" when /babysit-prs
(optionally wrapped in /loop) is invoked, so babysat sessions are identifiable
in /resume, the terminal tab, and the sessions sidebar.
"""
import json
import os
import re
import subprocess
import sys


def repo_name(cwd):
    try:
        toplevel = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return None
    return os.path.basename(toplevel) if toplevel else None


def current_pr_number(cwd):
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "number", "-q", ".number"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return None
    number = result.stdout.strip()
    return number if number.isdigit() else None


def main():
    data = json.load(sys.stdin)
    prompt = data.get("prompt", "")
    cwd = data.get("cwd") or os.getcwd()

    match = re.search(r"/babysit-prs(?:\s+(\S+))?", prompt)
    if not match:
        return

    arg = match.group(1)

    repo = repo_name(cwd)
    if not repo:
        return

    if arg is None:
        pr = current_pr_number(cwd)
        if not pr:
            return
        label = pr
    elif arg.lower() == "all":
        label = "ALL"
    elif arg.isdigit():
        label = arg
    else:
        # unrecognized argument shape - don't guess
        return

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "sessionTitle": f"{repo}#{label}",
        }
    }))


if __name__ == "__main__":
    main()
