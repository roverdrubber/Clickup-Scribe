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

Every write is idempotent by task name: a task that already exists is reused,
never duplicated. Re-running after an interruption resumes where it stopped.

See references/spec-format.md for the spec schema.
"""

from __future__ import annotations

import argparse
import json
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


def render(roots: list[Node], indent: int = 0) -> None:
    for node in roots:
        mark = "=" if node.existing_id else "+"
        detail = f"  ({node.status})" if node.status else ""
        print(f"  {' ' * indent}{mark} {node.name}{detail}")
        render(node.children, indent + 2)


def read_board(api: ClickUp, list_id: str) -> tuple[dict[str, str], dict[str, str]]:
    """(name -> id, id -> status) for the whole list. One paged read."""
    names: dict[str, str] = {}
    statuses: dict[str, str] = {}
    for task in api.list_tasks(list_id):
        names.setdefault(task["name"], task["id"])
        statuses[task["id"]] = task["status"]["status"]
    return names, statuses


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
        names, _ = read_board(api, args.list)
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
        assignees=args.assignee or None,
    )
    print(f"created  {created['id']}  {args.name}")
    print(f"         {created.get('url', '')}")
    return 0


def cmd_set(api: ClickUp, args) -> int:
    """Update one existing task in place."""
    fields = {}
    if args.status:
        fields["status"] = args.status
    if args.name:
        fields["name"] = args.name
    if args.description is not None:
        fields["markdown_content"] = args.description
    if not fields:
        print("Nothing to change. Pass --status, --name, or --description.")
        return 1

    if args.list and not check_status(api, args.list, {args.status}):
        return 1

    print(f"  ~ {args.task_id} -> {fields}")
    if args.dry_run:
        print("\nDry run. Drop --dry-run to write.")
        return 0

    api.update_task(args.task_id, **fields)
    print(f"updated  {args.task_id}")
    return 0


def cmd_apply(api: ClickUp, args) -> int:
    """Create a whole tree from a spec, idempotently."""
    list_id, status_fixes, roots = load_spec(args.spec)
    nodes = [node for root in roots for node in root.walk()]

    wanted = {n.status for n in nodes} | {f["status"] for f in status_fixes}
    if not check_status(api, list_id, wanted):
        return 1

    names, statuses = read_board(api, list_id)
    for node in nodes:
        node.existing_id = names.get(node.name)

    # A fix already satisfied is a wasted call on every future run.
    pending_fixes = [
        fix for fix in status_fixes
        if statuses.get(fix["task_id"]) != fix["status"]
    ]

    missing = [n for n in nodes if not n.existing_id]
    print(f"list {list_id}: {len(names)} tasks present")
    print(f"spec: {len(nodes)} entries, {len(missing)} to create\n")
    render(roots)
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
            assignees=node.assignees or None,
        )
        node.created_id = created["id"]
        print(f"created  {created['id']}  {node.name}")

    print(f"\nDone. {len(missing)} created, {api.calls} API calls.")
    return 0


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
    add.add_argument("--assignee", type=int, action="append",
                     help="numeric user id; repeat for several")
    add.add_argument("--allow-duplicate", action="store_true",
                     help="skip the name check and create unconditionally")
    add.add_argument("--dry-run", action="store_true")
    add.set_defaults(func=cmd_add)

    setc = sub.add_parser("set", help="update a single existing task")
    setc.add_argument("task_id")
    setc.add_argument("--status")
    setc.add_argument("--name")
    setc.add_argument("--description")
    setc.add_argument("--list", metavar="LIST_ID",
                      help="validate --status against this list first")
    setc.add_argument("--dry-run", action="store_true")
    setc.set_defaults(func=cmd_set)

    apply = sub.add_parser("apply", help="create a tree from a JSON spec")
    apply.add_argument("spec", type=Path)
    apply.add_argument("--apply", action="store_true",
                       help="write; without it this is a dry run")
    apply.set_defaults(func=cmd_apply)

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
