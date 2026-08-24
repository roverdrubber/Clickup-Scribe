#!/usr/bin/env python3
"""Prove the Python and PowerShell implementations still behave identically.

    python3 tests/parity.py            # run every scenario
    python3 tests/parity.py -v         # show both outputs side by side on failure

Two implementations of the same tool drift silently: a fix lands in one, the
other keeps the bug, and nothing complains until someone on the other platform
hits it. This runs both against the same fixture and diffs stdout and exit
codes, so drift fails loudly at the moment it is introduced.

Every PowerShell-only defect found so far - $PSBoundParameters scope, Get-Prop
on hashtables, exit() consuming stdout - would have been caught here without
anyone going looking for it.

Requires python3 and pwsh (or powershell) on PATH. Skips the PowerShell half
with a warning if neither is present, rather than reporting false parity.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
MOCKS = Path(__file__).resolve().parent / "mocks"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

LIST_ID = "901615936216"


# --------------------------------------------------------------------------
# scenarios: each is a sequence of steps run in one sandbox, so state carries
# across steps the way it does in real use.
# --------------------------------------------------------------------------

def scenarios() -> list[dict]:
    spec = str(FIXTURES / "spec.json")
    return [
        {"name": "tree", "tool": "reader",
         "steps": [(["tree"], ["tree"])]},

        {"name": "tree --spaces-only", "tool": "reader",
         "steps": [(["tree", "--spaces-only"], ["tree", "-SpacesOnly"])]},

        {"name": "tasks (nesting + orphan)", "tool": "reader",
         "steps": [(["tasks", "--list", LIST_ID], ["tasks", "-List", LIST_ID])]},

        {"name": "tasks --json", "tool": "reader", "json": True,
         "steps": [(["tasks", "--list", LIST_ID, "--json"],
                    ["tasks", "-List", LIST_ID, "-Json"])]},

        {"name": "get", "tool": "reader",
         "steps": [(["get", "t1"], ["get", "t1"])]},

        {"name": "find", "tool": "reader",
         "steps": [(["find", "payroll"], ["find", "payroll"])]},

        {"name": "export --as-spec", "tool": "reader", "json": True,
         "steps": [(["export", "--list", LIST_ID, "--as-spec"],
                    ["export", "-List", LIST_ID, "-AsSpec"])]},

        {"name": "statuses", "tool": "scribe",
         "steps": [(["statuses", "--list", LIST_ID], ["statuses", "-List", LIST_ID])]},

        {"name": "add: create, then idempotent repeat", "tool": "scribe",
         "steps": [
             (["add", "--list", LIST_ID, "--name", "Holidays",
               "--description", "Company calendar.", "--status", "completed"],
              ["add", "-List", LIST_ID, "-Name", "Holidays",
               "-Description", "Company calendar.", "-Status", "completed"]),
             (["add", "--list", LIST_ID, "--name", "Holidays"],
              ["add", "-List", LIST_ID, "-Name", "Holidays"]),
         ]},

        {"name": "add: rejects unknown status", "tool": "scribe",
         "steps": [(["add", "--list", LIST_ID, "--name", "X", "--status", "donezo"],
                    ["add", "-List", LIST_ID, "-Name", "X", "-Status", "donezo"])]},

        {"name": "add: dry run writes nothing", "tool": "scribe",
         "steps": [
             (["add", "--list", LIST_ID, "--name", "Ghost", "--dry-run"],
              ["add", "-List", LIST_ID, "-Name", "Ghost", "-DryRun"]),
             (["show", "--list", LIST_ID], ["show", "-List", LIST_ID]),
         ]},

        {"name": "set: status change", "tool": "scribe",
         "steps": [(["set", "t5", "--status", "completed"],
                    ["set", "t5", "-Status", "completed"])]},

        {"name": "set: nothing to change", "tool": "scribe",
         "steps": [(["set", "t5"], ["set", "t5"])]},

        {"name": "apply: dry run", "tool": "scribe",
         "steps": [([  "apply", spec], ["apply", spec])]},

        {"name": "apply: create, then converged no-op", "tool": "scribe",
         "steps": [
             (["apply", spec, "--apply"], ["apply", spec, "-Apply"]),
             (["apply", spec, "--apply"], ["apply", spec, "-Apply"]),
         ]},
    ]


# --------------------------------------------------------------------------

def powershell() -> str | None:
    for candidate in ("pwsh", "powershell"):
        if shutil.which(candidate):
            return candidate
    return None


def sandbox(kind: str) -> Path:
    """A temp dir holding the real script beside the mock client."""
    path = Path(tempfile.mkdtemp(prefix=f"parity-{kind}-"))
    if kind == "py":
        shutil.copy(MOCKS / "clickup_client.py", path)
        for script in ("scribe.py", "reader.py"):
            shutil.copy(SCRIPTS / script, path)
    else:
        shutil.copy(MOCKS / "ClickUp.psm1", path)
        for script in ("scribe.ps1", "reader.ps1"):
            shutil.copy(SCRIPTS / script, path)
    shutil.copy(FIXTURES / "board.json", path)
    return path


def run(cmd: list[str], cwd: Path) -> tuple[str, int]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
    return (proc.stdout + proc.stderr), proc.returncode


# Each implementation names its own flags - `--dry-run` against `-DryRun` - so
# messages that quote them differ by design. Canonicalise rather than compare.
FLAG = re.compile(r"(?<![\w-])--?([A-Za-z][\w-]*)")


def normalize(text: str) -> str:
    """Strip what legitimately differs; keep everything that must match.

    Call counts differ because the mocks count their own calls, not the real
    client's. Flag spellings differ because each CLI names its own options.
    Everything else is compared exactly.
    """
    lines = []
    for line in text.replace("\r\n", "\n").split("\n"):
        line = line.rstrip()
        if "API calls." in line:
            continue
        line = FLAG.sub(lambda m: "<flag:" + m.group(1).lower().replace("-", "") + ">",
                        line)
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def compare(label: str, py_out: str, ps_out: str, py_rc: int, ps_rc: int,
            as_json: bool) -> list[str]:
    problems = []
    if py_rc != ps_rc:
        problems.append(f"exit code: python={py_rc} powershell={ps_rc}")

    if as_json:
        try:
            py_val = json.loads(py_out)
        except json.JSONDecodeError as error:
            problems.append(f"python output is not JSON: {error}")
            py_val = None
        try:
            ps_val = json.loads(ps_out)
        except json.JSONDecodeError as error:
            problems.append(f"powershell output is not JSON: {error}")
            ps_val = None
        if py_val is not None and ps_val is not None:
            # PowerShell renders a single-element array as a bare object.
            if isinstance(py_val, list) and not isinstance(ps_val, list):
                ps_val = [ps_val]
            if isinstance(ps_val, list) and not isinstance(py_val, list):
                py_val = [py_val]
            if py_val != ps_val:
                problems.append("JSON differs")
    else:
        if normalize(py_out) != normalize(ps_out):
            problems.append("stdout differs")
    return problems


def main() -> int:
    verbose = "-v" in sys.argv
    shell = powershell()
    if not shell:
        print("[WARN] no pwsh/powershell on PATH - cannot verify parity.")
        print("       Install PowerShell or run this on a machine that has it.")
        return 3

    print(f"parity: python3 vs {shell}\n")
    failures = 0

    for scenario in scenarios():
        tool = scenario["tool"]
        as_json = scenario.get("json", False)
        py_dir, ps_dir = sandbox("py"), sandbox("ps")
        problems: list[str] = []
        last = ("", "")

        try:
            for py_args, ps_args in scenario["steps"]:
                py_out, py_rc = run(
                    [sys.executable, f"{tool}.py", *py_args], py_dir)
                ps_out, ps_rc = run(
                    [shell, "-NoProfile", "-File", f"./{tool}.ps1", *ps_args], ps_dir)
                last = (py_out, ps_out)
                problems += compare(scenario["name"], py_out, ps_out,
                                    py_rc, ps_rc, as_json)
        finally:
            shutil.rmtree(py_dir, ignore_errors=True)
            shutil.rmtree(ps_dir, ignore_errors=True)

        if problems:
            failures += 1
            print(f"  [FAIL] {scenario['name']}")
            for problem in dict.fromkeys(problems):
                print(f"         {problem}")
            if verbose:
                print("         --- python ---")
                for line in normalize(last[0]).split("\n"):
                    print(f"         | {line}")
                print("         --- powershell ---")
                for line in normalize(last[1]).split("\n"):
                    print(f"         | {line}")
        else:
            print(f"  [OK]   {scenario['name']}")

    total = len(scenarios())
    print(f"\n{total - failures}/{total} scenarios match.")
    if failures:
        print("The implementations have drifted. Fix before shipping.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
