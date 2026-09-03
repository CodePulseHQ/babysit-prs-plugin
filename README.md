# babysit-prs

A Claude Code plugin: continuously monitor GitHub PRs for review comments and CI failures, automatically addressing feedback and fixing builds. Designed to run with `/loop`.

## Install

Run these two commands on each machine you use Claude Code from:

```bash
claude plugin marketplace add CodePulseHQ/babysit-prs-plugin
claude plugin install babysit-prs@babysit-prs-plugin
```

This is one-time per machine — the skill, its helper scripts, and the session-naming hook all install together, so there's nothing else to copy around.

The plugin is pure Python (stdlib only) plus `git`/`gh` shell-outs, invoked as `python3 <script>` rather than executed directly, so it installs and runs identically on WSL, native Windows, macOS (Intel or Apple Silicon), and Linux — no platform-specific setup beyond the requirements below.

### Updates

`plugin.json` intentionally omits `version`, so Claude Code versions each install by this repo's resolved git commit — every push here is a new version, no manual bump needed.

Third-party marketplaces have background auto-update **off** by default. To turn it on per machine, either run `/plugin` → **Marketplaces** → select `babysit-prs-plugin` → **Enable auto-update**, or add to `settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "babysit-prs-plugin": {
      "source": { "source": "github", "repo": "CodePulseHQ/babysit-prs-plugin" },
      "autoUpdate": true
    }
  }
}
```

With auto-update off, pull the latest manually instead:

```bash
claude plugin marketplace update babysit-prs-plugin
claude plugin update babysit-prs@babysit-prs-plugin
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
