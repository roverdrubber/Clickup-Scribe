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
import os
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

# The mocks subclass / import the real implementations, so the harness
# exercises real parsing rather than a second copy of it.
os.environ["PARITY_REAL_CLIENT"] = str(SCRIPTS / "clickup_client.py")
os.environ["PARITY_REAL_MODULE"] = str(SCRIPTS / "ClickUp.psm1")


# --------------------------------------------------------------------------
# scenarios: each is a sequence of steps run in one sandbox, so state carries
# across steps the way it does in real use.
# --------------------------------------------------------------------------

def scenarios() -> list[dict]:
    spec = str(FIXTURES / "spec.json")
    recon = str(FIXTURES / "reconcile.json")
    links = str(FIXTURES / "links.json")
    links_invalid = str(FIXTURES / "links-invalid.json")
    links_badrange = str(FIXTURES / "links-badrange.json")
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

        {"name": "members", "tool": "reader",
         "steps": [(["members"], ["members"])]},

        {"name": "add: priority, due date, tags, estimate", "tool": "scribe",
         "steps": [(["add", "--list", LIST_ID, "--name", "Holidays",
                     "--priority", "high", "--due", "2026-09-01",
                     "--tag", "calendar", "--estimate", "90"],
                    ["add", "-List", LIST_ID, "-Name", "Holidays",
                     "-Priority", "high", "-Due", "2026-09-01",
                     "-Tag", "calendar", "-Estimate", "90"])]},

        {"name": "add: rejects unknown priority", "tool": "scribe",
         "steps": [(["add", "--list", LIST_ID, "--name", "X", "--priority", "massive"],
                    ["add", "-List", LIST_ID, "-Name", "X", "-Priority", "massive"])]},

        {"name": "add: rejects unreadable date", "tool": "scribe",
         "steps": [(["add", "--list", LIST_ID, "--name", "X", "--due", "next tuesday"],
                    ["add", "-List", LIST_ID, "-Name", "X", "-Due", "next tuesday"])]},

        {"name": "add: assignee by email", "tool": "scribe",
         "steps": [(["add", "--list", LIST_ID, "--name", "Owned",
                     "--assignee", "jgb@example.com"],
                    ["add", "-List", LIST_ID, "-Name", "Owned",
                     "-Assignee", "jgb@example.com"])]},

        {"name": "add: unknown member", "tool": "scribe",
         "steps": [(["add", "--list", LIST_ID, "--name", "Owned",
                     "--assignee", "nobody@example.com"],
                    ["add", "-List", LIST_ID, "-Name", "Owned",
                     "-Assignee", "nobody@example.com"])]},

        {"name": "set: several fields at once", "tool": "scribe",
         "steps": [(["set", "t3", "--status", "completed", "--priority", "low",
                     "--due", "2026-09-30 14:00"],
                    ["set", "t3", "-Status", "completed", "-Priority", "low",
                     "-Due", "2026-09-30 14:00"])]},

        {"name": "delete: refuses without confirmation", "tool": "scribe",
         "steps": [(["delete", "t1"], ["delete", "t1"])]},

        {"name": "delete: with confirmation", "tool": "scribe",
         "steps": [
             (["delete", "t1", "--yes"], ["delete", "t1", "-Yes"]),
             (["show", "--list", LIST_ID], ["show", "-List", LIST_ID]),
         ]},

        {"name": "add-folder: create, then idempotent repeat", "tool": "scribe",
         "steps": [
             (["add-folder", "--space", "90161372882", "--name", "Reporting"],
              ["add-folder", "-Space", "90161372882", "-Name", "Reporting"]),
             (["add-folder", "--space", "90161372882", "--name", "HRIS"],
              ["add-folder", "-Space", "90161372882", "-Name", "HRIS"]),
         ]},

        {"name": "add-list: in a folder", "tool": "scribe",
         "steps": [
             (["add-list", "--folder", "901610445259", "--name", "Frontend"],
              ["add-list", "-Folder", "901610445259", "-Name", "Frontend"]),
             (["add-list", "--folder", "901610445259", "--name", "API"],
              ["add-list", "-Folder", "901610445259", "-Name", "API"]),
         ]},

        {"name": "add-list: directly in a space", "tool": "scribe",
         "steps": [(["add-list", "--space", "90161372882", "--name", "Scratch"],
                    ["add-list", "-Space", "90161372882", "-Name", "Scratch"])]},

        {"name": "add-list: needs exactly one container", "tool": "scribe",
         "steps": [
             (["add-list", "--name", "Nowhere"], ["add-list", "-Name", "Nowhere"]),
             (["add-list", "--name", "Both", "--folder", "901610445259",
               "--space", "90161372882"],
              ["add-list", "-Name", "Both", "-Folder", "901610445259",
               "-Space", "90161372882"]),
         ]},

        {"name": "add-folder: dry run writes nothing", "tool": "scribe",
         "steps": [(["add-folder", "--space", "90161372882", "--name", "Ghost",
                     "--dry-run"],
                    ["add-folder", "-Space", "90161372882", "-Name", "Ghost",
                     "-DryRun"])]},

        {"name": "setup: reports nothing configured", "tool": "setup",
         "steps": [(["show"], ["show"])]},

        {"name": "setup: store refuses without a terminal", "tool": "setup",
         "steps": [(["store"], ["store"])]},

        {"name": "setup: clear with nothing stored", "tool": "setup",
         "steps": [(["clear"], ["clear"])]},

        {"name": "reconcile: off by default, drift left alone", "tool": "scribe",
         "steps": [(["apply", recon], ["apply", recon])]},

        {"name": "reconcile: shows drift in the dry run", "tool": "scribe",
         "steps": [(["apply", recon, "--reconcile"],
                    ["apply", recon, "-Reconcile"])]},

        {"name": "reconcile: applies, then converges to a no-op", "tool": "scribe",
         "steps": [
             (["apply", recon, "--apply", "--reconcile"],
              ["apply", recon, "-Apply", "-Reconcile"]),
             (["apply", recon, "--apply", "--reconcile"],
              ["apply", recon, "-Apply", "-Reconcile"]),
         ]},

        {"name": "apply: create, then converged no-op", "tool": "scribe",
         "steps": [
             (["apply", spec, "--apply"], ["apply", spec, "-Apply"]),
             (["apply", spec, "--apply"], ["apply", spec, "-Apply"]),
         ]},

        # -- commits: a real git repo (see GIT_COMMITS), mocked comments ----

        {"name": "commits: dry run posts nothing", "tool": "scribe", "git": True,
         "steps": [(["commits", "t1", "--range", "base..v1", "--repo", "repo",
                     "--dry-run"],
                    ["commits", "t1", "-Range", "base..v1", "-Repo", "repo",
                     "-DryRun"])]},

        {"name": "commits: post, then re-run is a no-op", "tool": "scribe",
         "git": True,
         "steps": [
             (["commits", "t1", "--range", "base..v1", "--repo", "repo"],
              ["commits", "t1", "-Range", "base..v1", "-Repo", "repo"]),
             (["commits", "t1", "--range", "base..v1", "--repo", "repo"],
              ["commits", "t1", "-Range", "base..v1", "-Repo", "repo"]),
         ]},

        {"name": "commits: partly linked posts only the missing", "tool": "scribe",
         "git": True,
         "steps": [
             (["commits", "t2", "--range", "base..v1", "--repo", "repo"],
              ["commits", "t2", "-Range", "base..v1", "-Repo", "repo"]),
             # Overlapping ranges, deduplicated; two of four already linked.
             (["commits", "t2", "--range", "base..v1", "--range", "base..v2",
               "--repo", "repo", "--title", "Export, continued"],
              ["commits", "t2", "-Range", "base..v1,base..v2",
               "-Repo", "repo", "-Title", "Export, continued"]),
         ]},

        {"name": "commits: a SHA on an old comment page counts", "tool": "scribe",
         "git": True,
         "steps": [(["commits", "t4", "--range", "base..v1", "--repo", "repo"],
                    ["commits", "t4", "-Range", "base..v1", "-Repo", "repo"])]},

        {"name": "commits: spec dry run, apply, then converged", "tool": "scribe",
         "git": True,
         "steps": [
             (["commits", "--spec", links], ["commits", "-Spec", links]),
             (["commits", "--spec", links, "--apply"],
              ["commits", "-Spec", links, "-Apply"]),
             (["commits", "--spec", links, "--apply"],
              ["commits", "-Spec", links, "-Apply"]),
         ]},

        {"name": "commits: malformed spec rejected", "tool": "scribe", "git": True,
         "steps": [(["commits", "--spec", links_invalid, "--repo", "repo", "--apply"],
                    ["commits", "-Spec", links_invalid, "-Repo", "repo", "-Apply"])]},

        {"name": "commits: one bad range stops the whole spec", "tool": "scribe",
         "git": True,
         "steps": [(["commits", "--spec", links_badrange, "--repo", "repo",
                     "--apply"],
                    ["commits", "-Spec", links_badrange, "-Repo", "repo",
                     "-Apply"])]},

        {"name": "commits: bad and empty ranges", "tool": "scribe", "git": True,
         "steps": [
             (["commits", "t1", "--range", "nope..v1", "--repo", "repo"],
              ["commits", "t1", "-Range", "nope..v1", "-Repo", "repo"]),
             (["commits", "t1", "--range", "v1..v1", "--repo", "repo"],
              ["commits", "t1", "-Range", "v1..v1", "-Repo", "repo"]),
             (["commits", "t1", "--range=--output=x", "--repo", "repo"],
              ["commits", "t1", "-Range", "--output=x", "-Repo", "repo"]),
         ]},

        {"name": "commits: argument errors", "tool": "scribe", "git": True,
         "steps": [
             (["commits", "t1", "--repo", "repo"], ["commits", "t1", "-Repo", "repo"]),
             (["commits", "--repo", "repo"], ["commits", "-Repo", "repo"]),
             (["commits", "t1", "--spec", links],
              ["commits", "t1", "-Spec", links]),
             (["commits", "--spec", links, "--dry-run"],
              ["commits", "-Spec", links, "-DryRun"]),
         ]},

        {"name": "commits: unknown host needs --web-url", "tool": "scribe",
         "git": True, "remote": "git@git.example.com:acme/widgets.git",
         "steps": [
             (["commits", "t1", "--range", "base..v1", "--repo", "repo",
               "--dry-run"],
              ["commits", "t1", "-Range", "base..v1", "-Repo", "repo", "-DryRun"]),
             (["commits", "t1", "--range", "base..v1", "--repo", "repo",
               "--web-url", "https://git.example.com/acme/widgets/", "--dry-run"],
              ["commits", "t1", "-Range", "base..v1", "-Repo", "repo",
               "-WebUrl", "https://git.example.com/acme/widgets/", "-DryRun"]),
         ]},

        {"name": "commits: github https remote", "tool": "scribe", "git": True,
         "remote": "https://deploy@github.com/acme/widgets.git",
         "steps": [(["commits", "t1", "--range", "v1..v2", "--repo", "repo",
                     "--dry-run"],
                    ["commits", "t1", "-Range", "v1..v2", "-Repo", "repo",
                     "-DryRun"])]},

        {"name": "commits: gitlab ssh remote with a port", "tool": "scribe",
         "git": True, "remote": "ssh://git@gitlab.example.com:2222/acme/widgets.git",
         "steps": [(["commits", "t1", "--range", "v1..v2", "--repo", "repo",
                     "--dry-run"],
                    ["commits", "t1", "-Range", "v1..v2", "-Repo", "repo",
                     "-DryRun"])]},
    ]


# --------------------------------------------------------------------------

# The `commits` scenarios read a real git repository. Every input to a commit
# hash is pinned - author, committer, both dates, message, content - so the
# SHAs are the same on every machine, and the fixture's comments can quote
# them. Change anything here and the SHAs in board.json's "comments" move.
GIT_COMMITS = [
    ("Initial import", "base"),
    ("Add payroll export", None),
    ("Fix cutoff rounding", "v1"),
    ("Document the export", None),
    ("Release the export", "v2"),
]
DEFAULT_REMOTE = "git@bitbucket.org:acme/widgets.git"
_GIT_TEMPLATE: Path | None = None


def git_env() -> dict:
    env = dict(os.environ)
    env.update({
        "GIT_AUTHOR_NAME": "Parity", "GIT_AUTHOR_EMAIL": "parity@example.com",
        "GIT_COMMITTER_NAME": "Parity", "GIT_COMMITTER_EMAIL": "parity@example.com",
        # The developer's own config - signing, hooks, autocrlf - must not
        # reach the fixture, or the hashes stop being reproducible.
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def git_template() -> Path:
    """Build the fixture repository once; each sandbox gets a copy."""
    global _GIT_TEMPLATE
    if _GIT_TEMPLATE is not None:
        return _GIT_TEMPLATE
    repo = Path(tempfile.mkdtemp(prefix="parity-git-")) / "repo"
    repo.mkdir()
    env = git_env()

    def git(*args: str, date: str | None = None) -> None:
        extra = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date} if date else {}
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True, env={**env, **extra})

    git("init", "-q")
    git("remote", "add", "origin", DEFAULT_REMOTE)
    for day, (subject, tag) in enumerate(GIT_COMMITS, start=1):
        (repo / f"{day}.txt").write_bytes(f"{subject}\n".encode())
        git("add", f"{day}.txt")
        git("commit", "-q", "-m", subject, date=f"2026-01-0{day}T00:00:00+0000")
        if tag:
            git("tag", tag)
    _GIT_TEMPLATE = repo
    return repo


def add_git_repo(path: Path, remote: str | None) -> None:
    """Copy the fixture repository into a sandbox, pointing origin at `remote`."""
    target = path / "repo"
    shutil.copytree(git_template(), target)
    subprocess.run(["git", "-C", str(target), "remote", "set-url", "origin",
                    remote or DEFAULT_REMOTE],
                   check=True, capture_output=True, env=git_env())


def powershell() -> str | None:
    for candidate in ("pwsh", "powershell"):
        if shutil.which(candidate):
            return candidate
    return None


def sandbox(kind: str, scenario: dict | None = None) -> Path:
    """A temp dir holding the real script beside the mock client."""
    path = Path(tempfile.mkdtemp(prefix=f"parity-{kind}-"))
    if scenario and scenario.get("git"):
        add_git_repo(path, scenario.get("remote"))
    if kind == "py":
        shutil.copy(MOCKS / "clickup_client.py", path)
        for script in ("scribe.py", "reader.py", "setup.py"):
            shutil.copy(SCRIPTS / script, path)
    else:
        shutil.copy(MOCKS / "ClickUp.psm1", path)
        for script in ("scribe.ps1", "reader.ps1", "setup.ps1"):
            shutil.copy(SCRIPTS / script, path)
    shutil.copy(FIXTURES / "board.json", path)
    return path


def run(cmd: list[str], cwd: Path, extra_env: dict | None = None) -> tuple[str, int]:
    env = dict(os.environ)
    # Each sandbox gets its own token directory, so token-source scenarios
    # cannot see the developer's real stored token.
    env["CLICKUP_SCRIBE_HOME"] = str(cwd / "cfg")
    env.pop("CLICKUP_TOKEN", None)
    env.update(extra_env or {})
    # stdin closed: a command that waits for input would otherwise hang the
    # whole run rather than failing.
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          timeout=120, env=env, stdin=subprocess.DEVNULL)
    return (proc.stdout + proc.stderr), proc.returncode


# Each implementation names its own flags - `--dry-run` against `-DryRun` - so
# messages that quote them differ by design. Canonicalise rather than compare.
FLAG = re.compile(r"(?<![\w-])--?([A-Za-z][\w-]*)")

# Sandbox paths differ per runtime and per run; the shape is what matters.
SANDBOX = re.compile(r"/tmp/parity-(py|ps)-\w+")

# Each runtime names its own setup script, and prefixes it differently.
SETUP = re.compile(r"(python3 )?\S*setup\.(py|ps1)")

# PowerShell writes an environment variable as $env:NAME. Same variable.
ENVVAR = re.compile(r"\$env:([A-Z_]+)")


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
        line = SANDBOX.sub("<sandbox>", line)
        line = SETUP.sub("<setup>", line)
        line = ENVVAR.sub(r"\1", line)
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def canonical(value):
    """Make two JSON trees comparable across runtimes."""
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


def compare_state(py_dir: Path, ps_dir: Path) -> list[str]:
    py_state, ps_state = py_dir / "state.json", ps_dir / "state.json"
    if py_state.exists() != ps_state.exists():
        which = "python" if py_state.exists() else "powershell"
        return [f"only {which} wrote state"]
    if not py_state.exists():
        return []
    left = canonical(json.loads(py_state.read_text()))
    right = canonical(json.loads(ps_state.read_text()))
    return [] if left == right else ["recorded writes differ"]


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
        py_dir, ps_dir = sandbox("py", scenario), sandbox("ps", scenario)
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
            # Printed output can match while the writes differ. Compare the
            # recorded effects too - bodies, updates, deletions.
            problems += compare_state(py_dir, ps_dir)
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

    if _GIT_TEMPLATE is not None:
        shutil.rmtree(_GIT_TEMPLATE.parent, ignore_errors=True)

    total = len(scenarios())
    print(f"\n{total - failures}/{total} scenarios match.")
    if failures:
        print("The implementations have drifted. Fix before shipping.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
