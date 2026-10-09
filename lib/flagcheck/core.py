"""Check long flags in a Bash command against the installed binary's own help text."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import hookio

PLUGIN = "flagcheck"

DEFAULTS = {
    "mode": "warn",            # warn | deny
    "help_timeout_s": 3,
    "skip": [],                # extra program names never to check
    "min_flags_in_help": 3,    # help text must list at least this many long flags to be trusted
}

# Never run `--help` on these: interpreters/runners (flags after the script belong to the script),
# things that execute their arguments, things that may download or act, and pagers/editors.
NEVER = {
    "sh", "bash", "zsh", "fish", "dash", "python", "python3", "node", "deno", "bun", "ruby", "perl", "php",
    "java", "lua", "Rscript", "osascript", "sudo", "doas", "su", "env", "nohup", "time", "timeout", "watch",
    "xargs", "exec", "eval", "nice", "ionice", "caffeinate", "npx", "bunx", "uvx", "pipx", "yes", "cat",
    "less", "more", "vi", "vim", "nvim", "nano", "emacs", "open", "xdg-open", "reboot", "shutdown", "halt",
    "kill", "killall", "pkill", "dd", "true", "false", "test", "[", "echo", "printf", "cd", "export", "source",
    ".", "set", "unset", "alias", "type", "command", "builtin", "read", "wait", "trap", "exit", "return",
    "for", "while", "if", "case", "do", "done", "then", "fi", "esac", "function", "local",
}
RUN_SUBCOMMANDS = {"run", "exec", "x", "dlx", "script", "run-script", "start", "test"}
HELP_ARGS = {"curl": [["--help", "all"]], "git": [["-h"]]}
SUBCOMMAND_RE = re.compile(r"[a-z][a-z0-9-]*")
LONG_FLAG_RE = re.compile(r"(?<![\w-])--(?:\[no-\])?[A-Za-z0-9][\w-]*")
NO_LONG_OPTS_RE = re.compile(r"(?i)illegal option -- -|unrecognized option [`'\"]?--help|invalid option -- '?-")


# ---------------------------------------------------------------- the shell Claude actually uses

def snapshot_info() -> tuple[set[str], Optional[str]]:
    """Names shadowed by functions/aliases in Claude Code's newest shell snapshot, and its PATH."""
    snapdir = hookio.home() / ".claude" / "shell-snapshots"
    try:
        snaps = sorted(snapdir.glob("snapshot-*.sh"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        snaps = []
    shims = {"grep", "egrep", "fgrep", "find", "rg", "pkill"}  # Claude Code shadows these itself
    if not snaps:
        return shims, None
    path_value = None
    try:
        text = snaps[0].read_text(errors="replace")
    except OSError:
        return shims, None
    for m in re.finditer(r"^(?:function\s+([\w.+-]+)|([\w.+-]+)\s*\(\)\s*\{?)", text, re.M):
        shims.add(m.group(1) or m.group(2))
    for m in re.finditer(r"^alias\s+(?:--\s+)?([\w.+/-]+)=", text, re.M):
        shims.add(m.group(1))
    m = re.search(r"^export PATH=['\"]?([^'\"\n]+)", text, re.M)
    if m:
        path_value = m.group(1)
    return shims, path_value


# ---------------------------------------------------------------- help text

def cache_file() -> Path:
    return hookio.data_dir(PLUGIN) / "help-cache.json"


def load_cache() -> dict:
    try:
        return json.loads(cache_file().read_text())
    except (OSError, ValueError):
        return {}


def save_cache(cache: dict) -> None:
    if len(cache) > 500:
        cache = dict(list(cache.items())[-400:])
    tmp = cache_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(cache_file())


def run_help(binary: str, args: list[str], timeout: float, cwd: str) -> str:
    env = dict(os.environ, PAGER="cat", MANPAGER="cat", GIT_PAGER="cat", TERM="dumb", NO_COLOR="1",
               COLUMNS="200", CI="1")
    try:
        r = subprocess.run([binary] + args, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env, cwd=cwd, errors="replace")
        return (r.stdout + "\n" + r.stderr)[:200_000]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""


def help_text(binary: str, sub: Optional[str], cfg: dict, cwd: str, cache: dict) -> str:
    try:
        st = os.stat(binary)
        ident = f"{os.path.realpath(binary)}|{st.st_mtime_ns}|{st.st_size}|{sub or ''}"
    except OSError:
        return ""
    key = hashlib.sha1(ident.encode()).hexdigest()
    if key in cache:
        return cache[key]
    prog = Path(binary).name
    variants = HELP_ARGS.get(prog, [["--help"]])
    base = "\n".join(run_help(binary, v, cfg["help_timeout_s"], cwd) for v in variants)
    texts = [base]
    # Only ask `prog sub --help` when the base help names `sub` as a word: with BSD getopt,
    # `prog positional --help` can treat --help as an operand and act (e.g. `mkdir x --help`).
    if sub and re.search(rf"(?<![\w-]){re.escape(sub)}(?![\w-])", base):
        texts += [run_help(binary, [sub] + v, cfg["help_timeout_s"], cwd) for v in variants]
    text = "\n".join(texts)
    cache[key] = text
    return text


def is_shell_script(binary: str) -> bool:
    try:
        with open(binary, "rb") as f:
            head = f.read(64)
    except OSError:
        return True
    return head.startswith(b"#!") and re.match(rb"#!\s*\S*/(?:env\s+)?(?:ba|z|k|da|fi)?sh\b", head) is not None


def known_flags(text: str) -> set[str]:
    out = set()
    for f in LONG_FLAG_RE.findall(text):
        if f.startswith("--[no-]"):
            base = f[len("--[no-]"):]
            out.update({"--" + base, "--no-" + base})
        else:
            out.add(f)
    return out


# ---------------------------------------------------------------- command analysis

def used_flags(words: list[str]) -> list[str]:
    flags = []
    for w in words[1:]:
        if w == "--":
            break
        if w.startswith("--") and len(w) > 2:
            flags.append(w.split("=", 1)[0])
    return flags


def check_segment(words: list[str], project: Path, cwd: str, shims: set, path_value: Optional[str],
                  cfg: dict, cache: dict) -> Optional[str]:
    prog = words[0]
    name = Path(prog).name
    if name in NEVER or name in shims or name in set(cfg.get("skip") or []):
        return None
    flags = used_flags(words)
    if not flags:
        return None
    sub = words[1] if len(words) > 1 and SUBCOMMAND_RE.fullmatch(words[1]) else None
    if sub in RUN_SUBCOMMANDS:
        return None  # flags belong to the script being run
    binary = prog if "/" in prog else shutil.which(prog, path=path_value or os.environ.get("PATH"))
    if not binary:
        return None
    binary = str((Path(cwd) / binary).resolve()) if not os.path.isabs(binary) else binary
    try:
        Path(binary).resolve().relative_to(project.resolve())
        return None  # never execute the project's own scripts
    except ValueError:
        pass
    if is_shell_script(binary):
        return None  # shell scripts rarely parse --help; running them could act
    text = help_text(binary, sub, cfg, cwd, cache)
    if not text.strip():
        return None
    known = known_flags(text)
    if len(known) < cfg["min_flags_in_help"]:
        if NO_LONG_OPTS_RE.search(text) and len(known) == 0:
            return (f"`{name}` resolves to {binary}, which rejects `--help` and lists no long options "
                    f"(likely the BSD version). {', '.join(dict.fromkeys(flags))} may not be accepted.")
        return None
    unknown = []
    for f in dict.fromkeys(flags):
        if f in known or f in ("--help", "--version"):
            continue
        if f.startswith("--no-") and ("--" + f[5:]) in known:
            continue
        prefixed = [k for k in known if k.startswith(f)]
        if len(prefixed) == 1:
            unknown.append(f"{f} (only as an abbreviation of {prefixed[0]})")
            continue
        unknown.append(f)
    if not unknown:
        return None
    where = f"`{name} {sub}`" if sub else f"`{name}`"
    return (f"{', '.join(unknown)} {'does' if len(unknown) == 1 else 'do'} not appear in the help output of "
            f"{where} ({binary}).")


def handle(event: dict) -> Optional[dict]:
    if event.get("hook_event_name") != "PreToolUse" or not hookio.is_bash_event(event):
        return None
    command = (event.get("tool_input") or {}).get("command") or ""
    if "--" not in command:
        return None
    project = hookio.project_dir(event)
    cwd = event.get("cwd") or str(project)
    cfg = hookio.load_config(PLUGIN, project, DEFAULTS)
    shims, path_value = snapshot_info()
    cache = load_cache()
    before = len(cache)
    problems = []
    for words in hookio.segments(command):
        msg = check_segment(words, project, cwd, shims, path_value, cfg, cache)
        if msg:
            problems.append(msg)
    if len(cache) != before:
        save_cache(cache)
    if not problems:
        return None
    text = "flagcheck: " + " ".join(problems)
    if cfg.get("mode") == "deny":
        return hookio.deny(text + " Check the flag spelling, or set \"mode\": \"warn\" in .claude/flagcheck.json.")
    return hookio.context("PreToolUse", text)
