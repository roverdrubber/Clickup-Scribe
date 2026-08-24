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
