import os
import stat
import sys
from pathlib import Path

import pytest

from conftest import run_hook
from flagcheck.core import handle, known_flags

FAKE = """#!{py}
import sys, os
open(os.environ["FAKE_LOG"], "a").write(" ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:2] == ["deploy", "--help"]:
    print("usage: fakecli deploy [--force] [--region R] [--dry-run]")
elif args[:1] == ["--help"]:
    print("usage: fakecli [--alpha] [--beta=X] [--gamma] [--[no-]color]\\ncommands: deploy, status")
"""
BSD = """#!{py}
import sys
sys.stderr.write("bsdtool: illegal option -- -\\nusage: bsdtool [-abc] file\\n")
sys.exit(1)
"""


@pytest.fixture
def tools(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("fakecli", FAKE), ("bsdtool", BSD), ("shtool", "#!/bin/sh\necho ran >> \"$FAKE_LOG\"\n")):
        p = bindir / name
        p.write_text(body.format(py=sys.executable))
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.log"
    log.write_text("")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    return log


def bash(project, cmd):
    return {"hook_event_name": "PreToolUse", "cwd": str(project), "tool_name": "Bash", "tool_input": {"command": cmd}}


def ctx(out):
    return (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def test_known_flags_parsing():
    assert known_flags("[--[no-]color] --x-y, --z=VAL") == {"--color", "--no-color", "--x-y", "--z"}


def test_valid_flags_are_silent(project, tools):
    assert handle(bash(project, "fakecli --alpha --beta=3 --no-color")) is None


def test_unknown_flag_reported(project, tools):
    text = ctx(handle(bash(project, "fakecli --alpah && echo done")))
    assert "--alpah does not appear" in text and "`fakecli`" in text


def test_abbreviation_reported(project, tools):
    assert "abbreviation of --gamma" in ctx(handle(bash(project, "fakecli --gam")))


def test_subcommand_help_used_when_listed(project, tools):
    assert handle(bash(project, "fakecli deploy --force --dry-run")) is None
    assert "--forse" in ctx(handle(bash(project, "fakecli deploy --forse")))


def test_positional_never_gets_help_appended(project, tools):
    handle(bash(project, "fakecli myfile.txt --alpha --zzz"))
    calls = tools.read_text().splitlines()
    assert "myfile.txt --help" not in calls and "--help" in calls


def test_help_is_cached(project, tools):
    handle(bash(project, "fakecli --alpha --qq"))
    n = len(tools.read_text().splitlines())
    handle(bash(project, "fakecli --alpha --qq"))
    assert len(tools.read_text().splitlines()) == n


def test_bsd_style_tool(project, tools):
    assert "lists no long options" in ctx(handle(bash(project, "bsdtool --recursive x")))


def test_shell_scripts_and_project_scripts_never_run(project, tools):
    assert handle(bash(project, "shtool --whatever")) is None
    local = project / "tool.py"
    local.write_text(f"#!{sys.executable}\nraise SystemExit('should not run')\n")
    local.chmod(0o755)
    assert handle(bash(project, "./tool.py --whatever")) is None
    assert "ran" not in tools.read_text()


def test_runners_and_double_dash(project, tools):
    assert handle(bash(project, "npm run build -- --prod")) is None
    assert handle(bash(project, "python3 script.py --whatever")) is None
    assert handle(bash(project, "fakecli --alpha -- --not-a-flag")) is None


def test_deny_mode(project, tools):
    (project / ".claude").mkdir()
    (project / ".claude" / "flagcheck.json").write_text('{"mode": "deny"}')
    out = handle(bash(project, "fakecli --nope"))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_snapshot_shims_are_skipped(project, tools, isolated):
    snap = isolated / ".claude" / "shell-snapshots"
    snap.mkdir(parents=True)
    (snap / "snapshot-zsh-1-x.sh").write_text("fakecli () {\n  command fakecli \"$@\"\n}\n")
    assert handle(bash(project, "fakecli --nope")) is None


@pytest.mark.skipif(sys.platform != "darwin" or not Path("/usr/bin/sed").exists(), reason="macOS BSD sed")
def test_real_bsd_sed(project, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert "BSD" in ctx(handle(bash(project, "sed --regexp-extended s/a/b/ f")))


def test_script_end_to_end(project, tools, isolated):
    out = run_hook(bash(project, "fakecli --bogus"), isolated)
    assert "--bogus" in out["hookSpecificOutput"]["additionalContext"]
