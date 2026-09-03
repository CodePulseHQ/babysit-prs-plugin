# babysit-prs

A Claude Code plugin: continuously monitor GitHub PRs for review comments and CI failures, automatically addressing feedback and fixing builds. Designed to run with `/loop`.

## Install

```bash
claude plugin marketplace add CodePulseHQ/babysit-prs-plugin
claude plugin install babysit-prs@babysit-prs-plugin
```

## Usage

```bash
# Monitor current branch's PR every 5 minutes
/loop 5m /babysit-prs

# Monitor a specific PR every 5 minutes
/loop 5m /babysit-prs 287

# Monitor all your open, non-approved PRs every 10 minutes
/loop 10m /babysit-prs all
```

See `skills/babysit-prs/SKILL.md` for the full behavior.

## Session naming

Bundles a `UserPromptSubmit` hook that renames the session to `<repo>#<pr>` (or `<repo>#ALL` for `all` mode) whenever `/babysit-prs` is invoked, so babysat sessions are identifiable in `/resume`, the terminal tab, and the sessions sidebar. Requires the `gh` CLI on `PATH` and authenticated.

## Requirements

- [`gh`](https://cli.github.com/) CLI, authenticated
- `python3` on `PATH`
