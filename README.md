# flagcheck

Catches made-up and wrong-platform CLI flags before a command runs. For each long option
(`--something`) in a Bash command, flagcheck reads the installed binary's own `--help` and reports
any flag the help doesn't list. Results are cached per binary path, mtime and size. It reports:
- typos and invented flags (`fakecli --alpah`)
- abbreviations (`--gam`, which only works as an abbreviation of `--gamma`)
- GNU-only flags passed to BSD tools on macOS (`sed --regexp-extended`, where `/usr/bin/sed` rejects `--help`
  and lists no long options)

Safety rules, because this runs other programs:
- It never runs interpreters, runners, or programs that execute their arguments (`python`, `npx`,
  `xargs`, `sudo`, ...).
- It never runs scripts inside the project, or shell scripts anywhere.
- It never runs `prog <positional> --help`. A subcommand's help is requested only when the base help lists
  that subcommand, because BSD getopt would treat `--help` as an operand (`mkdir x --help`).
- It skips commands that Claude Code's shell shadows with functions or aliases (it reads
  `~/.claude/shell-snapshots`). For example, `grep` there is really ugrep.
- `--help` runs without a TTY, with a 3-second timeout, and with pagers disabled.

**Hook:** `PreToolUse(Bash)`. **Config:** `{"mode": "warn" | "deny", "skip": ["mytool"], "help_timeout_s": 3}`.

## Install

From Anthropic's plugin directory, once listed: search for `flagcheck` in `/plugin directory` in Claude Code, or under **Customize > Plugins** on claude.ai.

To try it from a clone for one session: `claude --plugin-dir /path/to/flagcheck`.

This plugin is for Claude Code: it is built on hooks, which claude.ai chat does not load.

## Configuration

Settings are read from `~/.claude/flagcheck.json` and then `<project>/.claude/flagcheck.json`. The project file wins, objects merge by key, and a broken file is skipped and logged.

## What it runs and stores

It is Python 3.9+ standard library only, makes no network requests, and sends nothing anywhere. Every hook fails open: an internal error is logged to `~/.claude/flagcheck/errors.log` and the hook exits 0.

- Runs installed binaries with `--help` (never interpreters, project scripts or shell scripts; 3-second timeout, no TTY).
- Caches help output in `~/.claude/flagcheck/help-cache.json` and reads `~/.claude/shell-snapshots`.

## Development

```
python3 -m pytest -q
claude plugin validate --strict .
```

`docs/HOOK_SCHEMA.md` records the hook payloads this plugin depends on.

## License

MIT. See `LICENSE`.
