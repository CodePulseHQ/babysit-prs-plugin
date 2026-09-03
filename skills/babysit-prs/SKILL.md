---
name: babysit-prs
description: Use when the user wants to continuously monitor one or more PRs for review comments and CI failures, automatically addressing feedback and fixing builds. Accepts a PR number, "all" for all your open, non-draft, non-approved PRs targeting the repo's main branch (processed oldest-first), or defaults to the current branch's PR. Designed to be run with /loop.
---

# Babysit PRs

Continuously monitor GitHub PRs for new review comments and CI failures. Automatically address feedback, fix failing checks, and stop when the PR is approved or when discussion is going in circles.

## Arguments

- No args: monitor the PR for the current branch
- PR number (e.g. `287`): monitor that specific PR (targets any base branch — the base-branch restriction below only applies to `all` mode)
- `all`: monitor all your open, non-draft, non-approved PRs that target the repo's main branch (main/master/develop — whatever `gh repo view` reports as the default branch), processed oldest PR number first. PRs targeting any other branch (e.g. a feature or staging branch) are skipped unless babysat explicitly by number. Drafts are skipped too — there's nothing to babysit on a PR that isn't ready for review yet — but babysitting one explicitly by number still works.

## Mode Detection

```bash
# No args → current branch PR
gh pr view --json number -q .number

# Number → that PR (explicit PR number bypasses ALL "all"-mode filters below,
# including the base-branch restriction — you can babysit a PR targeting a
# feature branch if given its number directly)

# "all" → use the bundled helper's --list mode: fetches open PRs authored by
# you, filters to non-draft, NOT approved, and targeting the repo's actual
# default branch (main/master/develop — resolved dynamically, never
# hardcoded), sorted oldest-first by PR number. One helper call instead of a
# separate `gh repo view` + `gh pr list` + jq pipeline.
SKILL_DIR=~/.claude/skills/babysit-prs
python3 "$SKILL_DIR/open_comments.py" --list --json
```

When monitoring multiple PRs, process **every** PR returned above, in that sorted order (oldest PR number first), sequentially, within the same cycle. Skip any PR that has become approved since the last cycle, but do not stop working the cycle after the first PR you finish — a cycle is not complete until every PR in the freshly-fetched list has been handled. In `all`/list mode, the list itself (re-fetched at the start of every cycle) is the authoritative source of what's still being monitored — don't narrow down to a single PR based on manually-tracked state from a prior cycle.

## Per-PR Cycle

On each cycle, for each PR:

### 1. Check Exit Conditions First

`open_comments.py` (see step 2) reports all of these signals in its banner, so a single run per cycle covers both the exit-condition check AND the comment scan:

```
state: OPEN review=CHANGES_REQUESTED mergeable=MERGEABLE mergeState=BLOCKED base=develop [up-to-date] [CI-FAILING(lint,unit-tests)]
```

It prints `[NEEDS-REBASE]` when `mergeStateStatus` is `BEHIND`/`DIRTY` or `mergeable` is `CONFLICTING`, `*** APPROVED -> HARD EXIT ***` when the review decision is APPROVED, and `[CI-FAILING(<names>)]` / `[ci-pending(<names>)]` / `[ci-ok]` for CI status (via `gh pr checks`). (In `--json` mode the same data is under `prState`, including the derived `needsRebase`, `approved`, and `ciFailing` booleans, plus `failingChecks`/`pendingChecks` name lists and `baseRefName` for the rebase target.) Prefer reading these from the helper rather than a separate call.

**CI status is folded into this same call specifically so it can never be silently skipped** — treat `ciFailing`/`CI-FAILING` exactly like a new comment: it is "new activity" requiring action (step 4's "Failing CI check" bucket) even when there are zero new review comments this cycle. Do not let step 4's "No new activity: do nothing" bucket apply when CI is failing — that bucket is for the case where comments AND checks are both clean.

The standalone form, if you need it:

```bash
# Check approval status, merge state, and mergeable status
gh pr view $PR --json reviewDecision,state,mergeable,mergeStateStatus
```

**If approved → HARD STOP on this PR immediately.** Do not comment, push, or reply to it — make no further changes to an approved PR, ever. Remove it from the monitor list.
- **Single-PR mode** (no args, or an explicit PR number): this was the only PR being monitored, so exit the entire loop now — the job is done.
- **Multi-PR mode (`all`)**: this PR is done, but the others are not. Continue the cycle with the remaining PRs; do not exit the loop just because one PR was approved. Only exit the loop once every monitored PR has hit an exit condition (see Exit Conditions Summary).

**If merged/closed →** remove this PR from the monitor list. If it was the only PR, exit the loop entirely.

**If not mergeable (conflicts) →** rebase onto the base branch and resolve conflicts:

```bash
# Check for conflicts specifically
MERGEABLE=$(gh pr view $PR --json mergeable -q .mergeable)
MERGE_STATE=$(gh pr view $PR --json mergeStateStatus -q .mergeStateStatus)
# MERGEABLE: MERGEABLE, CONFLICTING, UNKNOWN
# MERGE_STATE: BLOCKED, BEHIND, DIRTY, HAS_HOOKS, CLEAN, UNSTABLE
```

If `MERGEABLE` is `CONFLICTING` or `MERGE_STATE` is `DIRTY` or `BEHIND`:

1. Determine the base branch: `gh pr view $PR --json baseRefName -q .baseRefName`
2. Fetch latest: `git fetch origin $BASE_BRANCH`
3. Rebase onto it: `git rebase origin/$BASE_BRANCH`
4. For each conflicting file, read the conflict markers, understand both sides, and resolve them intelligently — you have full context of the PR's purpose from the diff and PR description
5. After resolving each file: `git add <file>` then `git rebase --continue`
6. Force push the rebased branch: `git push --force-with-lease`

This is a core responsibility of the skill — always attempt to resolve conflicts yourself. Only if you genuinely cannot determine the correct resolution (e.g., both sides made substantive, incompatible changes to the same logic that require a product decision), leave a PR comment explaining exactly which files/lines conflict and why you couldn't resolve them, then skip this PR for this cycle.

Do not waste cycles checking for comments or CI on a PR that has merge conflicts — reviewers won't review until conflicts are resolved.

**If looping on same issues →** detect when:
- The same reviewer has commented again on threads you already replied to
- No new concerns are raised (just re-discussion of already-addressed points)
- You've already addressed the same thread 2+ times
- Step 4's bucket classification has landed the same thread in "already declined in a prior round" 2+ times running — that bucket exists precisely to catch this

When detected: leave a single comment summarising your position on all outstanding items, then stop monitoring this PR. **Do not respond to a repeat by widening scope** — a reviewer re-raising a declined point, or raising a new but out-of-scope point, is answered with the existing justification or a scope-creep deferral (step 4), never by absorbing more work into the PR to make the loop stop.

### 2. Check for New Comments (Reviews, Issue Comments, and Review Threads)

**Use the bundled helper — do not hand-roll an unpaginated query.** The single most common babysit failure is missing fresh review threads because `reviewThreads(first: 100)` SILENTLY TRUNCATES. On a long-lived PR the threads come back in creation order, so the newest *unresolved* threads sort to the very END. A single page on a PR with 300+ threads shows you 100 ancient, already-resolved threads and hides the 4 fresh blockers. Always paginate everything, re-order newest-first, then cap.

The helper (`open_comments.py`, in this skill's directory) does exactly that — paginates ALL review threads + issue comments, filters to open/unanswered, re-orders by most-recent activity, and caps the output:

`open_comments.py` lives in this skill's base directory (shown to you as "Base directory for this skill" when the skill is invoked, e.g. `~/.claude/skills/babysit-prs/open_comments.py`). Run it from the repo you're babysitting so `gh repo view` auto-detects the right repo:

```bash
SKILL_DIR=~/.claude/skills/babysit-prs   # = this skill's base directory

# Default: open (unresolved) review threads + recent issue comments, newest-first,
# capped at 30. Auto-detects repo (gh repo view) and your login (gh api user).
python3 "$SKILL_DIR/open_comments.py" --pr $PR

# Machine-readable, e.g. to drive replies/resolves:
python3 "$SKILL_DIR/open_comments.py" --pr $PR --json --limit 30

# Include already-resolved threads too (rarely needed):
python3 "$SKILL_DIR/open_comments.py" --pr $PR --all
```

Each review thread row gives you everything you need to act:
- `threadId` — pass to `resolve_threads.py` (see below) to resolve
- `replyToId` — pass to `resolve_threads.py --reply-resolve` (or `gh api .../pulls/$PR/comments -F in_reply_to=<id>` to just reply)
- `needsReply` — `true` when the last comment is NOT from you (genuinely new); `false` + unresolved means you replied but haven't resolved yet
- `lastAuthor`, `lastAt`, `path:line`, `snippet`

**Resolving threads — use the bundled helper, not a shell loop.** `resolve_threads.py` (same directory as `open_comments.py`) replies-then-resolves or bulk-resolves in ONE Bash call. This matters beyond convenience: in a worktree-isolated session, a multi-command shell loop over several `gh api graphql` calls gets refused outright ("too complex to verify that it stays inside the worktree") — one Python process making the same calls does not trip that check.

```bash
SKILL_DIR=~/.claude/skills/babysit-prs

# Resolve N threads you already replied to earlier (no new reply):
python3 "$SKILL_DIR/resolve_threads.py" --resolve <threadId1> <threadId2> <threadId3>

# Reply to one thread, then resolve it, in a single call:
python3 "$SKILL_DIR/resolve_threads.py" --reply-resolve --repo owner/name --pr $PR \
    --in-reply-to <replyToId> --thread <threadId> --body "Fixed in <sha>: ..."

# Reply+resolve a whole batch at once, from a JSON list of
# {replyToId, threadId, body} (e.g. hand-built after triaging step 4's buckets):
python3 "$SKILL_DIR/resolve_threads.py" --reply-resolve --repo owner/name --pr $PR --batch items.json
```

This script does not itself check who the last commenter is — you still apply the Thread Resolution Rules below before calling it.

The helper prints `N open / M total (showing K newest)` — if `open` exceeds the cap, raise `--limit` and re-run so nothing is silently dropped.

**Review summaries (do not skip this section).** Below the threads and issue comments the helper prints `review summaries (from others, newest first)` — the BODY of each review by someone other than you, newest first, with the top one flagged `[LATEST - read this]`. This is where a bot (codepulse) or a human posts a should-fix / must-fix SUMMARY that has **no inline thread attached** — the single most-missed signal, because it never appears in `reviewThreads` or issue comments. A re-submitted `CHANGES_REQUESTED` review keeps `reviewDecision=CHANGES_REQUESTED` even when every inline thread is resolved, so **a `CHANGES_REQUESTED` with `0 open` threads is NOT automatically stale** — read the latest review body every cycle and treat any unaddressed should-fix/must-fix in it as a blocker to action (fix or reply), exactly like an open thread. Only conclude the decision is stale once the newest review body has nothing outstanding.

The banner line above the threads carries the PR-state signals from step 1 (review decision, mergeable, merge state, base branch, `[NEEDS-REBASE]`, APPROVED hard-exit) — that's the same data step 1 needs, fetched in this one call.

If you ever query the GraphQL API directly instead of using the helper, you MUST cursor-paginate (`pageInfo { hasNextPage endCursor }`) until `hasNextPage` is false, then sort the unresolved nodes by their latest comment's `createdAt` descending before reading. Never act on a single unpaginated page.

Track which comments you've already seen/addressed (by comment ID and timestamp) to identify genuinely new activity. Look for new comments of ALL types — review thread comments, standalone review comments, and general issue comments.

### 3. Check CI Status

Already fetched in step 1/2's `open_comments.py` call — read `ciFailing`/`failingChecks` (`--json`) or the `[CI-FAILING(...)]` banner flag rather than making a separate call. Standalone form, if you need per-check detail (link, bucket) beyond the name list:

```bash
gh pr checks $PR --json name,state,bucket,link
```

### 4. Take Action

**Bot reviewers are real gatekeepers — appease them.** codepulse (and any other bot reviewer capable of setting `reviewDecision`) is not noise to be triaged loosely — it can grant or withhold approval exactly like a human, and its `CHANGES_REQUESTED` will not flip to approved until every should-fix/must-fix item it raised has a resolution. Every single one of its findings must end this cycle with EITHER a code fix OR a reply carrying concrete justification for not fixing it (a repro, a grep result, a citation of existing behavior, or an explicit scope-boundary reason). **Never leave a bot finding silently unaddressed** — an ignored bot finding just resurfaces (or keeps `CHANGES_REQUESTED` pinned) next cycle, burning cycles without progress.

**New comment (review thread, standalone review, or issue comment):**

1. Read the relevant file and full thread context.
2. **If the comment is from an automated reviewer (codepulse, codex, copilot, github-actions, or similar) and raises a "critical"/"important"/should-fix-severity claim, verify it before acting on it** — bots hallucinate behavior claims. Use the cheapest verification that settles it:
   - Codebase claim ("this guard is missing", "X doesn't exist") → grep to confirm presence/absence.
   - Behavior claim ("X throws", "X is nil here", "X is called twice") → a small repro, or trace the call path by reading the actual code.
   - Check whether this exact finding (or its underlying reasoning) was already raised and addressed/declined in a prior cycle — bots repeat themselves across rounds; don't re-litigate a settled point, cite the prior commit/reply instead.
3. Classify the comment (human or bot, post-verification) into one bucket, and act accordingly:
   - **Net-new, legit** → make the fix, stage it (do not commit/push yet — see step 4b).
   - **Already addressed** in a prior commit on this PR → reply citing that commit SHA; no code change.
   - **Factually incorrect** (verification in step 2 contradicts the claim) → reply with the verification evidence as justification; no code change.
   - **Already declined in a prior round**, same reasoning still holds → reply citing the prior decision/SHA; no code change. This is also a looping signal — see step 1's looping detection.
   - **Cosmetic / non-blocking** → reply with a one-line rationale for not changing it; no code change.
   - **Scope creep / out of scope for this PR** → reply acknowledging the point and noting where it'll be tracked (a follow-up ticket/issue) instead of expanding this PR's diff to cover it. **Do not let reviewer suggestions grow the PR's scope** — a should-fix item that is a genuinely different concern from the PR's stated purpose gets deferred, not absorbed, even if it's valid.
   - **Structural / invariant-breaking finding** → a should-fix that isn't "wrong code" but "this change breaks a guarantee elsewhere in the system" (a compliance/privacy promise, an API contract, an SLA, another subsystem's invariant). If closing the gap is a small, bounded change using an existing pattern, treat it as a normal fix. If it isn't, see step 4a below — do not design new architecture live inside this PR.
4. **Only resolve threads where the most recent comment is from you (AI-authored).** Never resolve threads where the last comment is from a human reviewer — that's their prerogative.

### 4a. Structural findings — descope instead of building reactively

**Why this exists:** a real case — a one-line TTL constant change — spiraled into 4 review rounds because round 1's finding ("this now conflicts with the published data-deletion promise") got patched inline instead of descoped. Each patch was narrow enough to leave the next race for the next round to find: a purge sweep, then a discovery-gap fix, then an authorization/TOCTOU fix, each correct-but-incomplete. The reviewer was right every round; the mistake was absorbing an unbounded fix into a PR that was supposed to be small.

**Recognize it:** the finding names a conflict with something *outside* the PR's own diff — a doc, policy, contract, or invariant the PR didn't touch but now invalidates. Ask: can the gap be closed with a small, bounded change using an existing pattern (call an existing helper, adjust an existing constant)? If yes, it's a normal fix — do it inline.

**If not bounded** — closing the gap needs new architecture, a new subsystem, or changes fanned out across multiple call sites — do not design and build it live inside this PR under review pressure:

1. **Descope this PR back to its stated purpose.** If a partial fix for the structural gap was already started this cycle and isn't a complete, sound solution, drop it — a half-built subsystem is worse than none.
2. **Capture the gap so it isn't lost.** Check what this project uses for tracking follow-up work, in priority order:
   - An issue tracker referenced in `CLAUDE.md`/project docs (GitHub Issues, Jira via the `atlassian` MCP tools, Linear, etc.) — file a ticket there: what the finding was, why it isn't a bounded fix, and the reviewer's evidence.
   - If the repo uses GitHub Issues but none is referenced explicitly, `gh issue create` in the same repo is a sane default.
   - If neither exists, fall back to a "Known limitations" section in the PR body itself, describing the gap and citing the reviewer's finding — durable enough not to be silently lost even without a tracker.
3. **Reply on the PR** citing the ticket (or PR-body note): state plainly this is being descoped as a separate design effort, why (new architecture, not a bounded correction), and that the current PR ships without it as a documented, known limitation.
4. **Do not keep iterating fixes for the structural gap in this PR** after descoping. Further reviewer pressure on the same structural point gets the "already declined in a prior round" treatment (step 1's looping rules), citing the ticket.
5. **When genuinely unclear whether it's safe to ship without the fix** (e.g., the gap is actively exploitable now, not just theoretical), say so explicitly in the PR reply and let the human decide — don't pick unilaterally either way.

**Trigger this even before 2+ repeat cycles** if a fix built in round N specifically to satisfy round N-1's finding becomes itself the subject of a *new* correctness/security finding in round N+1 — that pattern (each fix spawning the next subsystem the reviewer then finds a hole in) is the signal, not raw repeat count. See also 4b's loop-detection escalation for the same-dimension-repeating case.

**Failing CI check:**
1. Read the check output/logs
2. Diagnose the failure
3. Fix it, stage it (do not commit/push yet — see step 4b)

**No new activity:**
Do nothing. Wait for next cycle.

### 4b. Regression Self-Review (mandatory before every push)

**Why this exists:** the most common babysit failure isn't missing a comment — it's fixing exactly what was flagged while introducing a *new* bug elsewhere, which the reviewer then catches next cycle, prompting another narrow fix that introduces yet another regression. A real example went through 12+ rounds of `CHANGES_REQUESTED` this way. A reviewer running a thorough multi-perspective pass (correctness, security, resilience, symmetry) every cycle will keep finding something as long as fixes are made one flagged-line-at-a-time without checking the blast radius of each fix.

Before staging becomes a commit, if you made ANY code change this cycle, re-read it as a whole and explicitly reason through — do not just skim:

1. **Full-diff read** — `git diff` (or `git diff --cached`) everything changed this cycle, together, not hunk-by-hunk in isolation. A fix that looks correct in isolation can break an assumption made two hunks away.
2. **Ripple/callers** — for each changed function or code path: what else calls it, and does this change alter its behavior for those callers (timeouts, retries, error classification, state transitions, return shape)?
3. **Symmetry** — if a sibling/parallel code path exists (e.g. two providers implementing the same interface, check-in vs check-out, success vs failure branch of the same flow), does this fix now leave them inconsistent? A fix applied to one side of a symmetric pair and not the other is the single most common regression source.
4. **Completeness, not just the reported line** — did the fix address the *class* of problem the reviewer described, or only the one instance they pointed at? Search for other instances of the same pattern in the same file/module before assuming the fix is done.
5. **Reviewer's own methodology** — if the review body states which perspectives it checks (e.g. codepulse's correctness/security/resilience/standards passes), explicitly re-check the diff against those same perspectives yourself before pushing, not just the specific line flagged.

If this review surfaces a new issue, fix it now, in the same cycle, before it ever reaches the reviewer — do not push and wait for it to come back as a new comment next cycle.

**Loop-detection escalation:** track, across cycles, which review *dimension* keeps getting flagged (e.g. "Correctness" called out 2+ cycles running even with this self-review in place). That pattern means the fix approach itself is wrong, not that another surface patch will land it. When detected:
- Stop making another narrow patch.
- Step back and re-read the broader design intent of the change (PR description, linked ticket) — the recurring findings are usually symptoms of one structural gap, not N unrelated bugs.
- Leave a PR comment naming the pattern explicitly (which dimension, how many cycles, your hypothesis for the structural cause) and either propose the structural fix or ask the human to weigh in, rather than pushing another one-line patch.

### 5. Commit and Push

Group all changes from this cycle into a single commit per PR. Use initials `ar` if creating any branches.

```bash
git add <changed files>
git commit -m "Address review feedback on PR #$PR

- <summary of what was addressed>"
git push
```

## State Tracking Between Cycles

Re-run `open_comments.py` at the START of every cycle — bots (codepulse, Copilot) post a fresh review on each push, so new threads appear continuously and always sort to the end of the raw list. The helper's newest-first ordering is what keeps them visible.

Maintain awareness of what you've already processed to detect:
- **New vs already-seen comments** — track by comment database ID across all comment types (review threads, standalone reviews, issue comments)
- **Looping detection** — track how many times you've addressed the same thread
- **Approval changes** — check at the start of every cycle (hard exit if approved)
- **Merge conflicts** — check mergeability before doing any comment/CI work

## Thread Resolution Rules

This is critical — get it right:

| Last comment author | Action |
|---|---|
| You (AI) | Safe to resolve after replying |
| Human reviewer | NEVER resolve — they decide when they're satisfied |
| Mixed thread, your reply is latest | Safe to resolve |

## Conventions

If the project has `.claude/pr-review-conventions.md` or similar PR review conventions, read and follow them. Check at the start of the first cycle:

```bash
cat .claude/pr-review-conventions.md 2>/dev/null
```

## Multi-PR Mode Specifics

When running with `all`:
- At the start of **every** cycle, re-run `open_comments.py --list --json` to refresh the list of open, non-draft, non-approved PRs targeting the repo's default branch, sorted oldest-first by PR number (new PRs may have appeared, others may have been approved, moved out of draft, moved into draft, or had their base branch changed). This list — not memory of what you processed last cycle — is what defines "all PRs currently being monitored" for this cycle. A PR converted to draft mid-loop simply drops out of the next cycle's list on its own; there's no separate exit condition to handle for it.
- Process **every** PR in that freshly-fetched list, independently, in oldest-first order, before the cycle ends. Do not treat finishing one PR (including hitting its exit condition) as reason to end the cycle or the loop early — move on to the next PR in the list.
- **The loop-exit check is the list itself, not manual bookkeeping**: only exit the loop entirely once a freshly-fetched `open_comments.py --list --json` comes back empty (no open, non-draft, non-approved PRs targeting the default branch remain). If the list still has entries — even just one — keep looping and process it/them next cycle. Never exit the loop because a single PR in this cycle hit approved/merged/closed while the list still contains others.
- Check out each PR's branch before working on it, then return to the original branch

```bash
ORIGINAL_BRANCH=$(git branch --show-current)
# ... for each PR ...
gh pr checkout $PR
# ... do work ...
git checkout $ORIGINAL_BRANCH
```

## Exit Conditions Summary

Stop monitoring a PR when:
1. **Approved** — any reviewer approves. **Hard stop on this PR immediately: make no further changes to it** — no comment, no push, no reply. Remove it from the monitor list. In single-PR mode this ends the loop (nothing left to monitor); in multi-PR (`all`) mode, keep processing the other monitored PRs this cycle and every cycle after — an approval on one PR never halts work on the rest.
2. **Looping** — same threads re-discussed 2+ times with no new concerns. Leave a summary comment, then stop monitoring this PR.
3. **Merged/Closed** — PR is no longer open. Remove from monitor list.
4. **Unresolvable conflicts** — PR has merge conflicts that can't be auto-resolved. Leave a comment, skip for this cycle (but keep monitoring for when conflicts are resolved externally).

Stop the entire loop only when every monitored PR has hit exit condition 1, 2, or 3 — never stop the loop early just because one of several monitored PRs was approved. In `all`/list mode, don't determine this by memory of what happened this cycle: re-run `open_comments.py --list --json` and only exit once it returns empty (see Multi-PR Mode Specifics). In single-PR mode (no args, or an explicit PR number) there was only ever one PR to monitor, so its own exit condition ends the loop.

**"Stop the entire loop" means actually unscheduling the recurring trigger, not just returning without further action** — otherwise `/loop` re-fires this skill again next interval and the loop never really ends:
- If `/loop` is running in self-paced/dynamic mode (invoked without a fixed interval), call `ScheduleWakeup` with `stop: true` instead of scheduling another wakeup.
- If `/loop <interval>` is running on a fixed interval, it is backed by a cron task — find and remove it (e.g. `CronList` to find the task driving this loop, then `CronDelete` it) so it stops re-triggering.
- Only take this step once *every* monitored PR has hit exit condition 1, 2, or 3 (per the list-emptiness check above in `all` mode) — don't unschedule the loop just because the current cycle finished with nothing left to do this round.

## Example Invocations

```
# Monitor current branch's PR every 5 minutes
/loop 5m /babysit-prs

# Monitor specific PR every 5 minutes
/loop 5m /babysit-prs 287

# Monitor all your open non-approved PRs every 10 minutes
/loop 10m /babysit-prs all
```
