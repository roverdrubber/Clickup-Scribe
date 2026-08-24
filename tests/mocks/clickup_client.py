"""Mock ClickUp client for the parity harness.

Shadows the real `clickup_client` by sitting next to a copy of scribe.py /
reader.py in a temp directory. Reads the shared fixture, keeps mutable state in
`state.json` beside itself so a second run in the same directory sees what the
first one created.

Kept deliberately dumb: it must mirror the PowerShell mock exactly, so any
cleverness here is a place the two can disagree for reasons that have nothing
to do with the code under test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

HERE = Path(__file__).parent
FIXTURE = Path(os.environ.get("PARITY_FIXTURE", HERE / "board.json"))
STATE = HERE / "state.json"


class ClickUpError(RuntimeError):
    pass


class RateLimited(ClickUpError):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__("rate limited")


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"created": [], "updated": [], "n": 0}


def _save(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2))


class ClickUp:
    def __init__(self, token: str | None = None, pace: float = 0.0) -> None:
        self.calls = 0

    # -- reads ------------------------------------------------------------

    def list_tasks(self, list_id: str, include_closed: bool = True,
                   subtasks: bool = True) -> list[dict]:
        self.calls += 1
        rows = list(_fixture()["tasks"].get(list_id, []))
        rows += [t for t in _state()["created"] if t["list_id"] == list_id]
        status_overrides = {u["id"]: u["status"] for u in _state()["updated"]}
        out = []
        for row in rows:
            out.append({
                "id": row["id"],
                "name": row["name"],
                "parent": row.get("parent"),
                "status": {"status": status_overrides.get(row["id"], row["status"])},
                "assignees": [{"username": u} for u in row.get("assignees", [])],
                "description": row.get("description", ""),
            })
        return out

    def get_list(self, list_id: str) -> dict:
        self.calls += 1
        lst = dict(_fixture()["lists"][list_id])
        lst["statuses"] = [{"status": s} for s in _fixture()["statuses"]]
        return lst

    def statuses(self, list_id: str) -> list[str]:
        return [s["status"] for s in self.get_list(list_id).get("statuses", [])]

    def get_task(self, task_id: str, subtasks: bool = True) -> dict:
        self.calls += 1
        for list_id, rows in _fixture()["tasks"].items():
            for row in rows:
                if row["id"] != task_id:
                    continue
                kids = [r for r in rows if r.get("parent") == task_id]
                return {
                    "id": row["id"], "name": row["name"],
                    "status": {"status": row["status"]},
                    "list": {"id": list_id, "name": _fixture()["lists"][list_id]["name"]},
                    "parent": row.get("parent"),
                    "assignees": [{"username": u} for u in row.get("assignees", [])],
                    "description": row.get("description", ""),
                    "subtasks": [
                        {"id": k["id"], "name": k["name"],
                         "status": {"status": k["status"]}} for k in kids
                    ],
                }
        raise ClickUpError(f"task {task_id} not found")

    def teams(self) -> list[dict]:
        self.calls += 1
        return _fixture()["teams"]

    def spaces(self, team_id: str, archived: bool = False) -> list[dict]:
        self.calls += 1
        return _fixture()["spaces"].get(team_id, [])

    def folders(self, space_id: str, archived: bool = False) -> list[dict]:
        self.calls += 1
        return _fixture()["folders"].get(space_id, [])

    def folderless_lists(self, space_id: str, archived: bool = False) -> list[dict]:
        self.calls += 1
        return _fixture()["folderless_lists"].get(space_id, [])

    def folder_lists(self, folder_id: str, archived: bool = False) -> list[dict]:
        self.calls += 1
        return _fixture()["folder_lists"].get(folder_id, [])

    # -- writes -----------------------------------------------------------

    def create_task(self, list_id: str, name: str, description: str = "",
                    parent: str | None = None, status: str | None = None,
                    assignees: list[int] | None = None) -> dict:
        self.calls += 1
        state = _state()
        state["n"] += 1
        task_id = f"new{state['n']:02d}"
        state["created"].append({
            "id": task_id, "list_id": list_id, "name": name,
            "parent": parent, "status": status or "completed",
            "assignees": [], "description": description,
        })
        _save(state)
        return {"id": task_id, "url": f"https://app.clickup.com/t/{task_id}"}

    def update_task(self, task_id: str, **fields) -> dict:
        self.calls += 1
        state = _state()
        state["updated"].append({"id": task_id, "status": fields.get("status")})
        _save(state)
        return {}
