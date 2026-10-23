#!/usr/bin/env python3
"""Write to a ClickUp board over REST: one task, or a whole tree.

    scribe.py add   --list 901615936216 --name "Payroll Export" \
                    --parent 86d3qqd5f --description "Export a cutoff." \
                    --status completed --assignee 88978416
    scribe.py set   86d44avm2 --status completed
    scribe.py apply spec.json                 # plan only, writes nothing
    scribe.py apply spec.json --apply         # create what is missing
    scribe.py show  --list 901615936216       # what is on the list now
    scribe.py statuses --list 901615936216    # valid status strings
    scribe.py commits 86d44avm2 --range v1.2.0..v1.3.0 --dry-run
    scribe.py commits --spec links.json --apply
    scribe.py commits --spec links.json --rollup --apply

Every write is idempotent by task name: a task that already exists is reused,
never duplicated. Re-running after an interruption resumes where it stopped.

See references/spec-format.md for the spec schema.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from clickup_client import ClickUp, ClickUpError, RateLimited  # noqa: E402


# --------------------------------------------------------------------------
# spec model
# --------------------------------------------------------------------------

class Node:
    """One planned task. `existing_id` is set when the list already has it."""

    def __init__(self, spec: dict, defaults: dict, parent: "Node | None" = None):
        self.name: str = spec["name"]
        self.description: str = spec.get("description", "")
        self.status: str = spec.get("status", defaults.get("status", "")) or ""
        self.assignees: list[int] = spec.get("assignees", defaults.get("assignees", []))
        self.priority = spec.get("priority", defaults.get("priority"))
        self.due_date = spec.get("due_date", defaults.get("due_date"))
        self.start_date = spec.get("start_date", defaults.get("start_date"))
        self.time_estimate = spec.get("time_estimate", defaults.get("time_estimate"))
        self.tags: list[str] = spec.get("tags", defaults.get("tags", []))
        self.explicit_parent: str | None = spec.get("parent")
        self.parent = parent
        self.existing_id: str | None = None
        self.created_id: str | None = None
        self.children = [
            Node(child, defaults, self) for child in spec.get("children", [])
        ]

    @property
    def task_id(self) -> str | None:
        return self.existing_id or self.created_id

    def parent_id(self) -> str | None:
        if self.parent is not None:
            return self.parent.task_id
        return self.explicit_parent

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


def load_spec(path: Path) -> tuple[str, list[dict], list[Node]]:
    spec = json.loads(path.read_text())
    for key in ("list_id", "tasks"):
        if key not in spec:
            raise SystemExit(f"spec is missing required key: {key}")
    defaults = spec.get("defaults", {})
    roots = [Node(task, defaults) for task in spec["tasks"]]
    return str(spec["list_id"]), spec.get("status_fixes", []), roots


def show_value(value) -> str:
    """Render a value the same way both runtimes do."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(item) for item in value)
    return str(value)


def render(roots: list[Node], indent: int = 0) -> None:
    for node in roots:
        mark = "=" if node.existing_id else "+"
        detail = f"  ({node.status})" if node.status else ""
        print(f"  {' ' * indent}{mark} {node.name}{detail}")
        render(node.children, indent + 2)


def read_board(api: ClickUp, list_id: str):
    """(name -> id, id -> status, id -> full task) for a list. One paged read.

    The full rows are kept because reconciliation compares against fields the
    name/status pair does not carry.
    """
    names: dict[str, str] = {}
    statuses: dict[str, str] = {}
    by_id: dict[str, dict] = {}
    for task in api.list_tasks(list_id):
        names.setdefault(task["name"], task["id"])
        statuses[task["id"]] = task["status"]["status"]
        by_id[task["id"]] = task
    return names, statuses, by_id


def check_status(api: ClickUp, list_id: str, wanted: set[str]) -> bool:
    """Reject a bad status before spending calls on writes that will fail."""
    wanted = {w for w in wanted if w}
    if not wanted:
        return True
    valid = set(api.statuses(list_id))
    if unknown := sorted(wanted - valid):
        print(f"unknown status for this list: {', '.join(unknown)}")
        print(f"valid statuses: {', '.join(sorted(valid))}")
        return False
    return True


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_add(api: ClickUp, args) -> int:
    """Create a single task. Skips silently if the name is already on the list."""
    if not check_status(api, args.list, {args.status}):
        return 1

    existing_id = None
    if not args.allow_duplicate:
        names, _, _ = read_board(api, args.list)
        existing_id = names.get(args.name)

    if existing_id:
        print(f"exists   {existing_id}  {args.name}")
        print("Nothing to do. Pass --allow-duplicate to create a second copy.")
        return 0

    plan = f"{args.name}"
    if args.parent:
        plan += f"  (subtask of {args.parent})"
    if args.status:
        plan += f"  [{args.status}]"
    print(f"  + {plan}")

    if args.dry_run:
        print("\nDry run. Drop --dry-run to write.")
        return 0

    created = api.create_task(
        args.list,
        name=args.name,
        description=args.description or "",
        parent=args.parent,
        status=args.status,
        assignees=api.resolve_assignees(args.assignee),
        priority=args.priority,
        due_date=args.due,
        start_date=args.start,
        time_estimate=args.estimate,
        tags=args.tag,
    )
    print(f"created  {created['id']}  {args.name}")
    print(f"         {created.get('url', '')}")
    return 0


def cmd_set(api: ClickUp, args) -> int:
    """Update one existing task in place."""
    fields = api.task_body(
        name=args.name,
        description=args.description,
        status=args.status,
        priority=args.priority,
        due_date=args.due,
        start_date=args.start,
        time_estimate=args.estimate,
        tags=args.tag,
    )
    if args.assignee:
        # Update takes add/rem rather than a flat list.
        fields["assignees"] = {"add": api.resolve_assignees(args.assignee)}
    if not fields:
        print("Nothing to change. Pass --status, --name, --description, "
              "--priority, --due, --start, --estimate, --tag, or --assignee.")
        return 1

    if args.list and not check_status(api, args.list, {args.status}):
        return 1

    shown = ", ".join(f"{k}={show_value(v)}" for k, v in sorted(fields.items()))
    print(f"  ~ {args.task_id} -> {shown}")
    if args.dry_run:
        print("\nDry run. Drop --dry-run to write.")
        return 0

    api.update_task(args.task_id, **fields)
    print(f"updated  {args.task_id}")
    return 0


def cmd_add_folder(api: ClickUp, args) -> int:
    """Create a folder in a space, unless one of that name is already there."""
    existing = [f for f in api.folders(args.space) if f["name"] == args.name]
    if existing:
        print(f"exists   {existing[0]['id']}  {args.name}")
        return 0

    print(f"  + folder {args.name}  (in space {args.space})")
    if args.dry_run:
        print("\nDry run. Drop --dry-run to write.")
        return 0

    created = api.create_folder(args.space, args.name)
    print(f"created  {created['id']}  {args.name}")
    return 0


def cmd_add_list(api: ClickUp, args) -> int:
    """Create a list in a folder or directly in a space."""
    if bool(args.folder) == bool(args.space):
        print("add-list needs exactly one container: --folder or --space.")
        return 1

    if args.folder:
        siblings = api.folder_lists(args.folder)
        where = f"folder {args.folder}"
    else:
        siblings = api.folderless_lists(args.space)
        where = f"space {args.space}"

    existing = [l for l in siblings if l["name"] == args.name]
    if existing:
        print(f"exists   {existing[0]['id']}  {args.name}")
        return 0

    print(f"  + list {args.name}  (in {where})")
    if args.dry_run:
        print("\nDry run. Drop --dry-run to write.")
        return 0

    created = api.create_list(args.name, folder_id=args.folder,
                              space_id=args.space, content=args.content)
    print(f"created  {created['id']}  {args.name}")
    print("Put this id in a spec's list_id to fill it.")
    return 0


def cmd_delete(api: ClickUp, args) -> int:
    """Remove a task. Refuses without --yes: nothing else here destroys data."""
    task = api.get_task(args.task_id, subtasks=True)
    kids = task.get("subtasks") or []
    print(f"  - {task['id']}  {task['name']}")
    if kids:
        print(f"    and its {len(kids)} subtask(s):")
        for kid in kids:
            print(f"      {kid['id']:<12} {kid['name']}")

    if not args.yes:
        print("\nRefusing to delete without --yes.")
        return 1

    api.delete_task(args.task_id)
    print(f"deleted  {args.task_id}")
    print("ClickUp keeps it in the workspace trash for 30 days.")
    return 0


def cmd_apply(api: ClickUp, args) -> int:
    """Create a whole tree from a spec, idempotently."""
    list_id, status_fixes, roots = load_spec(args.spec)
    nodes = [node for root in roots for node in root.walk()]

    wanted_statuses = {n.status for n in nodes} | {f["status"] for f in status_fixes}
    if not check_status(api, list_id, wanted_statuses):
        return 1

    names, statuses, by_id = read_board(api, list_id)
    for node in nodes:
        node.existing_id = names.get(node.name)

    # A fix already satisfied is a wasted call on every future run.
    pending_fixes = [
        fix for fix in status_fixes
        if statuses.get(fix["task_id"]) != fix["status"]
    ]

    # Convergence: an existing task whose declared fields no longer match.
    updates: list[tuple[Node, dict]] = []
    if args.reconcile:
        for node in nodes:
            if not node.existing_id:
                continue
            wanted = api.task_body(
                status=node.status or None, priority=node.priority,
                due_date=node.due_date, start_date=node.start_date,
                time_estimate=node.time_estimate, tags=node.tags,
            )
            changed = api.drift(by_id[node.existing_id], wanted)
            if changed:
                updates.append((node, changed))

    missing = [n for n in nodes if not n.existing_id]
    # Both kinds of write count as an update: a reconcile fixing drift, and a
    # status_fix. They are applied by the same call and printed the same way.
    updating = len(updates) + len(pending_fixes)
    print(f"list {list_id}: {len(names)} tasks present")
    print(f"spec: {len(nodes)} entries, {len(missing)} to create, "
          f"{updating} to update\n")
    render(roots)
    for node, changed in updates:
        shown = ", ".join(f"{k}={show_value(v)}" for k, v in sorted(changed.items()))
        print(f"  ~ {node.name} -> {shown}")
    for fix in pending_fixes:
        print(f"  ~ {fix['task_id']} -> {fix['status']}")
    if skipped := len(status_fixes) - len(pending_fixes):
        print(f"  ({skipped} status fix(es) already satisfied)")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    print()
    for fix in pending_fixes:
        api.update_task(fix["task_id"], status=fix["status"])
        print(f"updated  {fix['task_id']} -> {fix['status']}")

    for node, changed in updates:
        api.update_task(node.existing_id, **changed)
        print(f"updated  {node.existing_id}  {node.name}")

    # Parents first: a child needs its parent's id, which may not exist until
    # this run creates it.
    for node in nodes:
        if node.existing_id:
            continue
        created = api.create_task(
            list_id,
            name=node.name,
            description=node.description,
            parent=node.parent_id(),
            status=node.status or None,
            assignees=api.resolve_assignees(node.assignees),
            priority=node.priority,
            due_date=node.due_date,
            start_date=node.start_date,
            time_estimate=node.time_estimate,
            tags=node.tags,
        )
        node.created_id = created["id"]
        print(f"created  {created['id']}  {node.name}")

    print(f"\nDone. {len(missing)} created, {updating} updated, "
          f"{api.calls} API calls.")
    return 0


# --------------------------------------------------------------------------
# commits: git history as task comments
# --------------------------------------------------------------------------
#
# ClickUp's API has no endpoint for the integration panel's commit links, so
# commits are recorded as a comment of web links instead. The full SHA in the
# comment text is the idempotency key: a commit whose SHA already appears in
# any comment on the task is never posted again.

class CommitsError(Exception):
    """A commits request that cannot go ahead. Printed, exit 1, nothing written."""


SHORT_SHA = 8

# https://host/org/repo, ssh://git@host:22/org/repo, user@host:org/repo.
URL_REMOTE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]+@)?([^/:]+)(?::[0-9]+)?/(.+)$")
SCP_REMOTE = re.compile(r"^(?:[^@/]+@)?([^:/]+):(.+)$")


def git(repo: str, *args: str) -> str:
    """Run git against `repo`; its first stderr line becomes the error."""
    try:
        proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                              encoding="utf-8", errors="replace")
    except OSError:
        raise CommitsError("git is not on PATH.") from None
    if proc.returncode != 0:
        lines = [line.strip() for line in proc.stderr.splitlines() if line.strip()]
        raise CommitsError(lines[0] if lines else f"git exited {proc.returncode}")
    return proc.stdout


def resolve_commits(repo: str, ranges: list[str]) -> list[tuple[str, str, str]]:
    """(sha, subject, author date) oldest first, ranges in the order given, no repeats.

    The date is YYYY-MM-DD, read in the same `git log` call as the subject.
    """
    commits: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for rng in ranges:
        # A leading dash would reach git as an option, not a revision.
        if not rng or rng.startswith("-"):
            raise CommitsError(f"'{rng}' is not a revision range.")
        try:
            # The trailing `--` stops git reading a range as a file path.
            out = git(repo, "log", "--reverse", "--no-merges", "--date=short",
                      "--format=%H%x09%ad%x09%s", rng, "--")
        except CommitsError as error:
            raise CommitsError(f"git log failed for range '{rng}': {error}") from None
        found = 0
        for line in out.splitlines():
            line = line.rstrip("\r")
            if not line:
                continue
            sha, _, rest = line.partition("\t")
            date, _, subject = rest.partition("\t")
            found += 1
            if sha not in seen:
                seen.add(sha)
                commits.append((sha, subject, date))
        if not found:
            raise CommitsError(f"range '{rng}' contains no commits.")
    return commits


def parse_remote(url: str) -> tuple[str, str] | None:
    """(host, org/repo) from any remote spelling, or None."""
    url = url.strip()
    match = URL_REMOTE.match(url) or SCP_REMOTE.match(url)
    if not match:
        return None
    path = match.group(2).rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return match.group(1).lower(), path


def commit_path(host: str) -> str | None:
    """The per-commit path segment each forge uses, or None for an unknown one."""
    if host == "bitbucket.org":
        return "/commits/"
    if host == "github.com":
        return "/commit/"
    if "gitlab" in host:
        return "/-/commit/"
    return None


def web_base(repo: str, web_url: str | None) -> tuple[str, str]:
    """(repository web URL, per-commit path) from --web-url or the origin remote."""
    if web_url:
        parsed = URL_REMOTE.match(web_url.strip())
        if not parsed:
            raise CommitsError(f"--web-url '{web_url}' is not a URL. "
                               "Use the repository's web address, https://...")
        base = web_url.strip().rstrip("/")
        if base.endswith(".git"):
            base = base[:-4]
        return base, commit_path(parsed.group(1).lower()) or "/commit/"

    try:
        remote = git(repo, "remote", "get-url", "origin").strip()
    except CommitsError:
        raise CommitsError("cannot read the 'origin' remote. "
                           "Pass --web-url with the repository's web address.") from None
    parsed = parse_remote(remote)
    if not parsed:
        raise CommitsError(f"cannot read the 'origin' remote '{remote}'. "
                           "Pass --web-url with the repository's web address.")
    host, path = parsed
    style = commit_path(host)
    if style is None:
        raise CommitsError(f"unknown git host '{host}'. "
                           "Pass --web-url with the repository's web address.")
    return f"https://{host}/{path}", style


def comment_body(title: str, commits: list[tuple[str, str, str]], base: str,
                 style: str) -> str:
    blocks = [title] + [
        f"{sha[:SHORT_SHA]} {subject}\n{base}{style}{sha}" for sha, subject, _ in commits
    ]
    return "\n\n".join(blocks)


def plan_link(api: ClickUp, task: str, commits: list[tuple[str, str, str]],
              title: str, base: str, style: str) -> dict:
    """Read every comment on `task`; keep only the commits none of them names."""
    comments = api.get_comments(task)
    texts = [str(c.get("comment_text") or "") for c in comments]
    new = [commit for commit in commits
           if not any(commit[0] in text for text in texts)]
    return {
        "task": task, "read": len(comments), "total": len(commits), "new": new,
        "linked": len(commits) - len(new),
        "body": comment_body(title, new, base, style) if new else "",
    }


def default_title(ranges: list[str]) -> str:
    return "Commits " + ", ".join(ranges)


def load_links(path: str) -> tuple[dict, list[dict]]:
    """(file-level options, entries) from a links spec. Rejects before any read."""
    if not Path(path).is_file():
        raise CommitsError(f"no such spec: {path}")
    try:
        spec = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except ValueError:
        raise CommitsError(f"cannot read spec: {path} is not valid JSON.") from None

    options: dict = {}
    if isinstance(spec, dict) and isinstance(spec.get("links"), list):
        for key in ("repo", "web_url"):
            value = spec.get(key)
            if value is not None and not isinstance(value, str):
                raise CommitsError(f'spec "{key}" must be text.')
            options[key] = value
        entries = spec["links"]
    elif isinstance(spec, list):
        entries = spec
    else:
        raise CommitsError('spec must be a JSON array of entries, '
                           'or an object with a "links" array.')
    if not entries:
        raise CommitsError("spec has no entries.")

    links, seen = [], set()
    for number, entry in enumerate(entries, start=1):
        where = f"spec entry {number}"
        if not isinstance(entry, dict):
            raise CommitsError(f"{where}: must be an object.")
        task = entry.get("task")
        if not isinstance(task, str) or not task.strip():
            raise CommitsError(f'{where}: "task" must be a task id.')
        ranges = entry.get("ranges")
        if (not isinstance(ranges, list) or not ranges
                or not all(isinstance(r, str) and r.strip() for r in ranges)):
            raise CommitsError(f'{where}: "ranges" must be a non-empty list of ranges.')
        title = entry.get("title")
        if title is not None and not isinstance(title, str):
            raise CommitsError(f'{where}: "title" must be text.')
        if task in seen:
            raise CommitsError(f"{where}: task {task} appears more than once.")
        seen.add(task)
        links.append({"task": task, "ranges": list(ranges),
                      "title": title or default_title(ranges)})
    return options, links


def print_body(body: str) -> None:
    for line in body.split("\n"):
        print(f"    {line}")


# -- rollup: one summary comment per parent ---------------------------------
#
# The commit lists stay on the subtasks; the parent (domain) task gets one
# line per subtask instead. A line is keyed on `CU-<subtask id>` plus the
# subtask's latest full SHA: a parent comment holding both means that line is
# already rolled up. Changing the line format so either stops appearing
# breaks dedupe against every rollup already posted.

DASH = "\u2014"
ARROW = "\u2192"


def rollup_line(line: dict) -> str:
    noun = "commit" if line["count"] == 1 else "commits"
    return (f"{line['name']} (CU-{line['task']}) {DASH} {line['count']} {noun}, "
            f"{line['first']} {ARROW} {line['last']}\nlatest {line['sha']}")


def rollup_body(lines: list[dict]) -> str:
    through = max(line["last"] for line in lines)
    return "\n\n".join([f"Subtask commits {DASH} through {through}"]
                       + [rollup_line(line) for line in lines])


def rolled_up(text: str, line: dict) -> bool:
    """Whether one parent comment already carries this subtask's line.

    The id must end where `CU-<id>` ends, or CU-abc would count as CU-abcd.
    """
    marker = re.compile(re.escape(f"CU-{line['task']}") + r"(?![A-Za-z0-9])")
    return line["sha"] in text and bool(marker.search(text))


def plan_rollups(api: ClickUp, links: list[dict]) -> tuple[list[dict], list[str]]:
    """(one plan per parent in first-seen order, top-level task ids skipped).

    Every task is read for its parent and name, and every parent's comments
    to the end, before anything is written.
    """
    tasks: dict[str, dict] = {}
    groups: dict[str, list[dict]] = {}
    skipped: list[str] = []
    for link in links:
        task_id = link["task"]
        if task_id not in tasks:
            tasks[task_id] = api.get_task(task_id, subtasks=False)
        task = tasks[task_id]
        parent = task.get("parent")
        if not parent:
            skipped.append(task_id)
            continue
        commits = link["commits"]
        groups.setdefault(str(parent), []).append({
            "task": task_id, "name": str(task.get("name") or ""),
            "count": len(commits), "first": commits[0][2], "last": commits[-1][2],
            "sha": commits[-1][0],
        })

    plans = []
    for parent, lines in groups.items():
        lines.sort(key=lambda line: (line["last"], line["name"]))
        comments = api.get_comments(parent)
        texts = [str(c.get("comment_text") or "") for c in comments]
        new = [line for line in lines
               if not any(rolled_up(text, line) for text in texts)]
        plans.append({
            "parent": parent, "read": len(comments), "lines": lines, "new": new,
            "body": rollup_body(new) if new else "",
        })
    return plans, skipped


def print_rollups(plans: list[dict], skipped: list[str]) -> None:
    for plan in plans:
        print(f"\nrollup {plan['parent']}: {plan['read']} comment(s) read, "
              f"{len(plan['lines'])} subtask(s), {len(plan['new'])} new, "
              f"{len(plan['lines']) - len(plan['new'])} already rolled up")
        for line in plan["lines"]:
            mark = "+" if line in plan["new"] else "="
            print(f"  {mark} {line['task']}  {line['name']}")
        if plan["new"]:
            print()
            print_body(plan["body"])
    for task_id in skipped:
        print(f"\nrollup skipped: {task_id} has no parent")


def post_rollups(api: ClickUp, plans: list[dict], lead: str) -> int:
    """Post each parent's comment; `lead` goes before the first line printed."""
    posted_count = 0
    for plan in plans:
        if not plan["new"]:
            continue
        posted = api.add_comment(plan["parent"], plan["body"], notify_all=False)
        print(f"{lead}rolled up  {plan['parent']}  {len(plan['new'])} subtask(s)  "
              f"(comment {posted.get('id', '')})")
        lead = ""
        posted_count += 1
    return posted_count


def commits_single(api: ClickUp, args) -> int:
    if not args.ranges:
        print("commits needs at least one --range, e.g. --range v1.2.0..v1.3.0")
        return 1
    if args.apply:
        print("--apply is for --spec. A single task posts unless --dry-run.")
        return 1
    repo = args.repo or "."
    commits = resolve_commits(repo, args.ranges)
    base, style = web_base(repo, args.web_url)
    plan = plan_link(api, args.task_id, commits,
                     args.title or default_title(args.ranges), base, style)
    rollups: list[dict] = []
    skipped: list[str] = []
    if args.rollup:
        rollups, skipped = plan_rollups(api, [{"task": args.task_id,
                                               "commits": commits}])

    print(f"task {plan['task']}: {plan['read']} comment(s) read, "
          f"{plan['total']} commit(s), {len(plan['new'])} new, "
          f"{plan['linked']} already linked")
    if not plan["new"] and not args.rollup:
        print("Nothing to do. Every commit is already linked.")
        return 0
    if plan["new"]:
        print()
        print_body(plan["body"])
    else:
        print("Every commit is already linked.")
    if args.rollup:
        print_rollups(rollups, skipped)
        if not plan["new"] and not any(r["new"] for r in rollups):
            print("\nNothing to do. Every commit is already linked and rolled up.")
            return 0

    if args.dry_run:
        print("\nDry run. Drop --dry-run to write.")
        return 0

    lead = "\n"
    if plan["new"]:
        posted = api.add_comment(plan["task"], plan["body"], notify_all=False)
        print(f"\ncommented  {plan['task']}  {len(plan['new'])} commit(s)  "
              f"(comment {posted.get('id', '')})")
        lead = ""
    post_rollups(api, rollups, lead)
    return 0


def commits_spec(api: ClickUp, args) -> int:
    if args.task_id or args.ranges or args.title:
        print("--spec takes the task, --range and --title from the file. "
              "Pass a task id or --spec, not both.")
        return 1
    if args.dry_run:
        print("--spec is a dry run unless --apply. Drop --dry-run.")
        return 1
    options, links = load_links(args.spec)
    repo = args.repo or options.get("repo") or "."
    base, style = web_base(repo, args.web_url or options.get("web_url"))

    # Every range resolves before any read, and every read before any write:
    # a bad entry anywhere stops the run with the board untouched.
    for link in links:
        try:
            link["commits"] = resolve_commits(repo, link["ranges"])
        except CommitsError as error:
            raise CommitsError(f"task {link['task']}: {error}") from None
    plans = [plan_link(api, link["task"], link["commits"], link["title"], base, style)
             for link in links]
    rollups: list[dict] = []
    skipped: list[str] = []
    if args.rollup:
        rollups, skipped = plan_rollups(api, links)
    rolling = [r for r in rollups if r["new"]]

    posting = [p for p in plans if p["new"]]
    tally = (f"spec: {len(plans)} task(s), {len(posting)} comment(s) to post, "
             f"{len(plans) - len(posting)} task(s) already linked")
    if args.rollup:
        tally += f", {len(rolling)} rollup comment(s) to post"
    print(tally + "\n")
    for plan in plans:
        mark = "+" if plan["new"] else "="
        print(f"  {mark} {plan['task']}  {len(plan['new'])} new, "
              f"{plan['linked']} already linked")
        for sha, subject, _ in plan["new"]:
            print(f"      {sha[:SHORT_SHA]} {subject}")
    if args.rollup:
        print_rollups(rollups, skipped)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
        return 0

    if posting or rolling:
        print()
    for plan in posting:
        posted = api.add_comment(plan["task"], plan["body"], notify_all=False)
        print(f"commented  {plan['task']}  {len(plan['new'])} commit(s)  "
              f"(comment {posted.get('id', '')})")
    if not args.rollup:
        print(f"\nDone. {len(posting)} posted, {api.calls} API calls.")
        return 0
    rolled = post_rollups(api, rollups, "")
    print(f"\nDone. {len(posting)} posted, {rolled} rollup comment(s) posted, "
          f"{api.calls} API calls.")
    return 0


def cmd_commits(api: ClickUp, args) -> int:
    """Post one comment of commit links per task, skipping commits already linked."""
    # A rollup prints an em dash and an arrow, and so may a commit subject. A
    # Windows pipe's code page may lack them; replace rather than crash.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    try:
        if args.spec:
            return commits_spec(api, args)
        if not args.task_id:
            print("commits needs a task id, or --spec for several tasks.")
            return 1
        return commits_single(api, args)
    except CommitsError as error:
        print(str(error))
        return 1


def cmd_show(api: ClickUp, args) -> int:
    for task in sorted(api.list_tasks(args.list), key=lambda t: t["name"]):
        parent = f"  parent={task['parent']}" if task.get("parent") else ""
        print(f"{task['id']}  {task['status']['status']:<12} {task['name']}{parent}")
    return 0


def cmd_statuses(api: ClickUp, args) -> int:
    for status in api.statuses(args.list):
        print(status)
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scribe.py",
        description="Write to a ClickUp board over REST: one task, or a whole tree.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add = sub.add_parser("add", help="create a single task")
    add.add_argument("--list", required=True, metavar="LIST_ID")
    add.add_argument("--name", required=True)
    add.add_argument("--description", default="")
    add.add_argument("--parent", help="existing task id, to create a subtask")
    add.add_argument("--status")
    add.add_argument("--assignee", action="append",
                     help="user id, email, or username; repeat for several")
    add.add_argument("--priority", help="urgent, high, normal, or low")
    add.add_argument("--due", metavar="YYYY-MM-DD[ HH:MM]")
    add.add_argument("--start", metavar="YYYY-MM-DD[ HH:MM]")
    add.add_argument("--estimate", type=int, metavar="MINUTES")
    add.add_argument("--tag", action="append", help="repeat for several")
    add.add_argument("--allow-duplicate", action="store_true",
                     help="skip the name check and create unconditionally")
    add.add_argument("--dry-run", action="store_true")
    add.set_defaults(func=cmd_add)

    setc = sub.add_parser("set", help="update a single existing task")
    setc.add_argument("task_id")
    setc.add_argument("--status")
    setc.add_argument("--name")
    setc.add_argument("--description")
    setc.add_argument("--priority", help="urgent, high, normal, or low")
    setc.add_argument("--due", metavar="YYYY-MM-DD[ HH:MM]")
    setc.add_argument("--start", metavar="YYYY-MM-DD[ HH:MM]")
    setc.add_argument("--estimate", type=int, metavar="MINUTES")
    setc.add_argument("--tag", action="append", help="repeat for several")
    setc.add_argument("--assignee", action="append",
                      help="add an assignee: id, email, or username")
    setc.add_argument("--list", metavar="LIST_ID",
                      help="validate --status against this list first")
    setc.add_argument("--dry-run", action="store_true")
    setc.set_defaults(func=cmd_set)

    apply = sub.add_parser("apply", help="create a tree from a JSON spec")
    apply.add_argument("spec", type=Path)
    apply.add_argument("--apply", action="store_true",
                       help="write; without it this is a dry run")
    apply.add_argument("--reconcile", action="store_true",
                       help="also update existing tasks whose declared fields "
                            "have drifted from the spec")
    apply.set_defaults(func=cmd_apply)

    delete = sub.add_parser("delete", help="remove one task")
    delete.add_argument("task_id")
    delete.add_argument("--yes", action="store_true",
                        help="required; without it this only shows what would go")
    delete.set_defaults(func=cmd_delete)

    add_folder = sub.add_parser("add-folder", help="create a folder in a space")
    add_folder.add_argument("--space", required=True, metavar="SPACE_ID")
    add_folder.add_argument("--name", required=True)
    add_folder.add_argument("--dry-run", action="store_true")
    add_folder.set_defaults(func=cmd_add_folder)

    add_list = sub.add_parser("add-list", help="create a list in a folder or space")
    add_list.add_argument("--name", required=True)
    add_list.add_argument("--folder", metavar="FOLDER_ID")
    add_list.add_argument("--space", metavar="SPACE_ID")
    add_list.add_argument("--content", help="the list's description")
    add_list.add_argument("--dry-run", action="store_true")
    add_list.set_defaults(func=cmd_add_list)

    commits = sub.add_parser(
        "commits", help="link git commits to a task as one comment of web links")
    commits.add_argument("task_id", nargs="?")
    commits.add_argument("--range", action="append", dest="ranges", metavar="A..B",
                         help="git revision range; repeat for several")
    commits.add_argument("--spec", metavar="FILE",
                         help="JSON file of {task, ranges, title} entries")
    commits.add_argument("--repo", metavar="PATH",
                         help="git repository to read (default: current dir)")
    commits.add_argument("--web-url", metavar="URL",
                         help="repository web address, when origin cannot say")
    commits.add_argument("--title", help="first line of the comment")
    commits.add_argument("--dry-run", action="store_true",
                         help="single task: show the comment, post nothing")
    commits.add_argument("--apply", action="store_true",
                         help="--spec: post; without it this is a dry run")
    commits.add_argument("--rollup", action="store_true",
                         help="also post one summary comment per parent task")
    commits.set_defaults(func=cmd_commits)

    show = sub.add_parser("show", help="print what is on a list")
    show.add_argument("--list", required=True, metavar="LIST_ID")
    show.set_defaults(func=cmd_show)

    statuses = sub.add_parser("statuses", help="print a list's valid statuses")
    statuses.add_argument("--list", required=True, metavar="LIST_ID")
    statuses.set_defaults(func=cmd_statuses)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(ClickUp(), args)
    except RateLimited as error:
        print(f"\nRate limited with work outstanding. Retry in "
              f"{error.retry_after / 60:.0f} minutes.")
        print("Re-running is safe: what already landed will be skipped.")
        return 2
    except ClickUpError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
