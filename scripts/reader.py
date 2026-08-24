#!/usr/bin/env python3
"""Read a ClickUp workspace over REST: spaces, folders, lists, tasks, subtasks.

    reader.py tree                             # whole workspace structure
    reader.py tree --space 90161372882         # one space
    reader.py tasks --list 901615936216        # tasks, subtasks nested
    reader.py get 86d3qqa1e                    # one task in full
    reader.py find "payroll" --space 90161372882
    reader.py export --list 901615936216 --as-spec > spec.json

Add --json to any command for machine-readable output.

A deep traversal is many calls. Over the MCP server that exhausts a daily quota
in one go; over REST the same walk costs a minute of a per-minute budget.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from clickup_client import ClickUp, ClickUpError, RateLimited  # noqa: E402


# --------------------------------------------------------------------------
# hierarchy
# --------------------------------------------------------------------------

def walk_space(api: ClickUp, space: dict, with_lists: bool = True) -> dict:
    """One space with its folders and lists, folderless lists included."""
    node = {
        "id": space["id"],
        "name": space["name"],
        "type": "space",
        "folders": [],
        "lists": [],
    }
    if not with_lists:
        return node

    for folder in api.folders(space["id"]):
        node["folders"].append({
            "id": folder["id"],
            "name": folder["name"],
            "type": "folder",
            "lists": [
                {"id": lst["id"], "name": lst["name"], "type": "list",
                 "task_count": lst.get("task_count")}
                for lst in api.folder_lists(folder["id"])
            ],
        })

    # Easy to forget, and silently lossy when forgotten.
    node["lists"] = [
        {"id": lst["id"], "name": lst["name"], "type": "list",
         "task_count": lst.get("task_count")}
        for lst in api.folderless_lists(space["id"])
    ]
    return node


def build_tree(api: ClickUp, space_id: str | None, team_id: str | None,
               with_lists: bool = True) -> list[dict]:
    if space_id:
        spaces = [s for t in (api.teams() if not team_id else [{"id": team_id}])
                  for s in api.spaces(t["id"]) if s["id"] == space_id]
        if not spaces:
            raise ClickUpError(f"space {space_id} not found in any workspace")
        return [walk_space(api, s, with_lists) for s in spaces]

    teams = api.teams() if not team_id else [{"id": team_id, "name": team_id}]
    out = []
    for team in teams:
        out.append({
            "id": team["id"],
            "name": team.get("name", team["id"]),
            "type": "workspace",
            "spaces": [walk_space(api, s, with_lists)
                       for s in api.spaces(team["id"])],
        })
    return out


def iter_lists(nodes: list[dict]) -> list[tuple[str, str]]:
    """Every (list_id, "space / folder / list") pair under a tree."""
    found: list[tuple[str, str]] = []

    def visit_space(space: dict) -> None:
        for lst in space.get("lists", []):
            found.append((lst["id"], f"{space['name']} / {lst['name']}"))
        for folder in space.get("folders", []):
            for lst in folder.get("lists", []):
                found.append(
                    (lst["id"], f"{space['name']} / {folder['name']} / {lst['name']}")
                )

    for node in nodes:
        if node.get("type") == "workspace":
            for space in node.get("spaces", []):
                visit_space(space)
        else:
            visit_space(node)
    return found


def print_tree(nodes: list[dict], indent: int = 0) -> None:
    for node in nodes:
        pad = " " * indent
        count = node.get("task_count")
        suffix = f"  [{count} tasks]" if count is not None else ""
        print(f"{pad}{node['type']:<9} {node['id']:<14} {node['name']}{suffix}")
        for key in ("spaces", "folders", "lists"):
            print_tree(node.get(key, []), indent + 2)


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

def nest(tasks: list[dict]) -> list[dict]:
    """Turn a flat task list into parent/child trees.

    ClickUp returns subtasks flat, each carrying a `parent` id. A subtask whose
    parent is not in the same response (filtered out, or on another list) is
    surfaced at top level rather than dropped.
    """
    by_id = {t["id"]: dict(t, children=[]) for t in tasks}
    roots = []
    for task in by_id.values():
        parent = by_id.get(task.get("parent"))
        if parent is not None:
            parent["children"].append(task)
        else:
            roots.append(task)
    return sorted(roots, key=lambda t: t["name"])


def print_tasks(nodes: list[dict], indent: int = 0) -> None:
    for node in sorted(nodes, key=lambda t: t["name"]):
        status = node.get("status", {}).get("status", "?")
        who = ", ".join(a.get("username", "") for a in node.get("assignees", []))
        who = f"  ({who})" if who else ""
        print(f"  {' ' * indent}{node['id']:<12} {status:<12} {node['name']}{who}")
        print_tasks(node.get("children", []), indent + 2)


def to_spec(list_id: str, roots: list[dict]) -> dict:
    """Render tasks as a scribe spec, so a board can be replayed elsewhere."""
    def convert(task: dict) -> dict:
        entry = {"name": task["name"]}
        text = task.get("description") or ""
        if text:
            entry["description"] = text.strip()
        status = task.get("status", {}).get("status")
        if status:
            entry["status"] = status
        children = [convert(c) for c in task.get("children", [])]
        if children:
            entry["children"] = children
        return entry

    return {
        "list_id": list_id,
        "tasks": [convert(t) for t in roots],
    }


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_tree(api: ClickUp, args) -> int:
    tree = build_tree(api, args.space, args.team, with_lists=not args.spaces_only)
    if args.json:
        print(json.dumps(tree, indent=2))
    else:
        print_tree(tree)
        print(f"\n{api.calls} API calls.")
    return 0


def cmd_tasks(api: ClickUp, args) -> int:
    tasks = api.list_tasks(args.list, include_closed=not args.open_only)
    roots = nest(tasks)
    if args.json:
        print(json.dumps(roots, indent=2))
    else:
        print_tasks(roots)
        total = len(tasks)
        print(f"\n{total} tasks ({len(roots)} top level), {api.calls} API calls.")
    return 0


def cmd_get(api: ClickUp, args) -> int:
    task = api.get_task(args.task_id)
    if args.json:
        print(json.dumps(task, indent=2))
        return 0
    print(f"{task['id']}  {task['name']}")
    print(f"  status    {task.get('status', {}).get('status')}")
    print(f"  list      {task.get('list', {}).get('name')} "
          f"({task.get('list', {}).get('id')})")
    if task.get("parent"):
        print(f"  parent    {task['parent']}")
    who = ", ".join(a.get("username", "") for a in task.get("assignees", []))
    if who:
        print(f"  assignees {who}")
    if task.get("description"):
        print(f"  description\n    {task['description'].strip()}")
    subs = task.get("subtasks") or []
    if subs:
        print(f"  subtasks  ({len(subs)})")
        for sub in subs:
            print(f"    {sub['id']:<12} {sub.get('status', {}).get('status', '?'):<12} "
                  f"{sub['name']}")
    return 0


def cmd_find(api: ClickUp, args) -> int:
    """Substring match on task names across a scope.

    REST v2 has no full-text search, so this walks the lists in scope and
    filters by name. Narrow the scope with --list or --space; a whole-workspace
    walk is a lot of calls.
    """
    needle = args.keyword.lower()

    if args.list:
        list_ids = [(args.list, api.get_list(args.list).get("name", args.list))]
    else:
        list_ids = iter_lists(build_tree(api, args.space, args.team))

    hits = []
    for list_id, label in list_ids:
        for task in api.list_tasks(list_id):
            if needle in task["name"].lower():
                hits.append({"id": task["id"], "name": task["name"],
                             "status": task.get("status", {}).get("status"),
                             "location": label})

    if args.json:
        print(json.dumps(hits, indent=2))
    else:
        for hit in hits:
            print(f"{hit['id']:<12} {str(hit['status']):<12} {hit['name']}")
            print(f"{'':<12} {'':<12} {hit['location']}")
        print(f"\n{len(hits)} match(es) across {len(list_ids)} list(s), "
              f"{api.calls} API calls.")
    return 0


def cmd_export(api: ClickUp, args) -> int:
    tasks = api.list_tasks(args.list)
    roots = nest(tasks)
    if args.as_spec:
        print(json.dumps(to_spec(args.list, roots), indent=2))
    else:
        print(json.dumps(roots, indent=2))
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reader.py",
        description="Read a ClickUp workspace: spaces, folders, lists, tasks.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--json", action="store_true", help="machine-readable output")
        return p

    tree = common(sub.add_parser("tree", help="workspace structure"))
    tree.add_argument("--space", help="limit to one space id")
    tree.add_argument("--team", help="workspace id, if the token sees several")
    tree.add_argument("--spaces-only", action="store_true",
                      help="stop at spaces; skip folders and lists")
    tree.set_defaults(func=cmd_tree)

    tasks = common(sub.add_parser("tasks", help="tasks on a list, subtasks nested"))
    tasks.add_argument("--list", required=True, metavar="LIST_ID")
    tasks.add_argument("--open-only", action="store_true", help="exclude closed")
    tasks.set_defaults(func=cmd_tasks)

    get = common(sub.add_parser("get", help="one task in full"))
    get.add_argument("task_id")
    get.set_defaults(func=cmd_get)

    find = common(sub.add_parser("find", help="substring match on task names"))
    find.add_argument("keyword")
    find.add_argument("--list", metavar="LIST_ID", help="search one list")
    find.add_argument("--space", help="search one space")
    find.add_argument("--team", help="workspace id")
    find.set_defaults(func=cmd_find)

    export = common(sub.add_parser("export", help="dump a list as JSON"))
    export.add_argument("--list", required=True, metavar="LIST_ID")
    export.add_argument("--as-spec", action="store_true",
                        help="emit a scribe spec, ready to replay elsewhere")
    export.set_defaults(func=cmd_export)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.func(ClickUp(), args)
    except RateLimited as error:
        print(f"\nRate limited. Retry in {error.retry_after / 60:.0f} minutes.")
        return 2
    except ClickUpError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
