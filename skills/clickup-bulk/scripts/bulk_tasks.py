#!/usr/bin/env python3
"""Create a tree of ClickUp tasks from a JSON spec. Idempotent, dry-run first.

    python3 bulk_tasks.py spec.json            # plan only, writes nothing
    python3 bulk_tasks.py spec.json --apply    # create what is missing
    python3 bulk_tasks.py spec.json --verify   # print what is on the list now

Re-running is always safe: existing tasks are matched by name and reused as
parents, so a run interrupted halfway resumes exactly where it stopped.

See references/spec-format.md for the schema.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from clickup_client import ClickUp, ClickUpError, RateLimited  # noqa: E402


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


def load_spec(path: Path) -> tuple[str, dict, list[dict], list[Node]]:
    spec = json.loads(path.read_text())
    for key in ("list_id", "tasks"):
        if key not in spec:
            raise SystemExit(f"spec is missing required key: {key}")
    defaults = spec.get("defaults", {})
    roots = [Node(task, defaults) for task in spec["tasks"]]
    return str(spec["list_id"]), defaults, spec.get("status_fixes", []), roots


def render(roots: list[Node], indent: int = 0) -> None:
    for node in roots:
        mark = "=" if node.existing_id else "+"
        detail = f"  ({node.status})" if node.status else ""
        print(f"  {' ' * indent}{mark} {node.name}{detail}")
        render(node.children, indent + 2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--apply", action="store_true", help="write to ClickUp")
    parser.add_argument("--verify", action="store_true",
                        help="print the list's current contents and exit")
    args = parser.parse_args()

    list_id, _defaults, status_fixes, roots = load_spec(args.spec)

    try:
        api = ClickUp()
    except ClickUpError as error:
        return fail(error)

    try:
        if args.verify:
            for task in sorted(api.list_tasks(list_id), key=lambda t: t["name"]):
                parent = f"  parent={task['parent']}" if task.get("parent") else ""
                print(f"{task['id']}  {task['status']['status']:<12} "
                      f"{task['name']}{parent}")
            return 0

        # Catch a bad status before spending calls on creates that will fail.
        valid = set(api.statuses(list_id))
        wanted = {n.status for r in roots for n in r.walk() if n.status}
        wanted |= {fix["status"] for fix in status_fixes}
        if unknown := sorted(wanted - valid):
            print(f"unknown status for this list: {', '.join(unknown)}")
            print(f"valid statuses: {', '.join(sorted(valid))}")
            return 1

        present = api.list_tasks(list_id)
        index: dict[str, str] = {}
        current_status: dict[str, str] = {}
        for task in present:
            index.setdefault(task["name"], task["id"])
            current_status[task["id"]] = task["status"]["status"]

        nodes = [node for root in roots for node in root.walk()]
        for node in nodes:
            node.existing_id = index.get(node.name)

        # A fix already satisfied is a wasted call on every future run.
        pending_fixes = [
            fix for fix in status_fixes
            if current_status.get(fix["task_id"]) != fix["status"]
        ]

        missing = [n for n in nodes if not n.existing_id]
        print(f"list {list_id}: {len(present)} tasks present")
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

        # Parents first: a child needs its parent's id, which may not exist
        # until this run creates it.
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

    except RateLimited as error:
        print(f"\nRate limited with work outstanding. Retry in "
              f"{error.retry_after / 60:.0f} minutes.")
        print("Re-running is safe: what already landed will be skipped.")
        return 2
    except ClickUpError as error:
        return fail(error)


def fail(error: Exception) -> int:
    print(str(error), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
