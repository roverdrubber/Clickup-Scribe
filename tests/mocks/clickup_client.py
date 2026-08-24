"""Mock ClickUp client for the parity harness.

Shadows the real `clickup_client` by sitting next to a copy of scribe.py /
reader.py in a temp directory.

It **subclasses** the real client and overrides only the methods that would
touch the network. Everything else - priority parsing, date conversion, body
building, assignee resolution, the pagination loop - is the real code, so the
harness tests the implementation rather than a second copy of it.

Reads the shared fixture; keeps mutable state in `state.json` beside itself so
a second run in the same directory sees what the first one created. The state
file is also compared across runtimes, which is how effects (not just printed
output) are held to parity.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

HERE = Path(__file__).parent
FIXTURE = Path(os.environ.get("PARITY_FIXTURE", HERE / "board.json"))
STATE = HERE / "state.json"

_REAL_PATH = os.environ["PARITY_REAL_CLIENT"]
_spec = importlib.util.spec_from_file_location("_real_clickup", _REAL_PATH)
_real = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_real)

ClickUpError = _real.ClickUpError
RateLimited = _real.RateLimited


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"created": [], "updated": [], "deleted": [], "n": 0}


def _save(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2, sort_keys=True))


class ClickUp(_real.ClickUp):
    def __init__(self, token: str | None = None, pace: float = 0.0) -> None:
        self.token = "pk_mock"
        self.pace = 0.0
        self.calls = 0
        self._last = 0.0

    def _call(self, method, path, body=None, _retries=2):
        raise AssertionError(f"mock leaked a real call: {method} {path}")

    # -- reads ------------------------------------------------------------

    def list_tasks(self, list_id, include_closed=True, subtasks=True):
        self.calls += 1
        state = _state()
        rows = list(_fixture()["tasks"].get(list_id, []))
        rows += [t for t in state["created"] if t["list_id"] == list_id]
        overrides = {u["id"]: u for u in state["updated"]}
        out = []
        for row in rows:
            if row["id"] in state["deleted"]:
                continue
            status = overrides.get(row["id"], {}).get("status") or row["status"]
            out.append({
                "id": row["id"], "name": row["name"], "parent": row.get("parent"),
                "status": {"status": status},
                "assignees": [{"username": u} for u in row.get("assignees", [])],
                "description": row.get("description", ""),
            })
        return out

    def get_list(self, list_id):
        self.calls += 1
        lst = dict(_fixture()["lists"][list_id])
        lst["statuses"] = [{"status": s} for s in _fixture()["statuses"]]
        return lst

    def get_task(self, task_id, subtasks=True):
        self.calls += 1
        for list_id, rows in _fixture()["tasks"].items():
            for row in rows:
                if row["id"] != task_id:
                    continue
                kids = [r for r in rows if r.get("parent") == task_id]
                return {
                    "id": row["id"], "name": row["name"],
                    "status": {"status": row["status"]},
                    "list": {"id": list_id,
                             "name": _fixture()["lists"][list_id]["name"]},
                    "parent": row.get("parent"),
                    "assignees": [{"username": u} for u in row.get("assignees", [])],
                    "description": row.get("description", ""),
                    "subtasks": [{"id": k["id"], "name": k["name"],
                                  "status": {"status": k["status"]}} for k in kids],
                }
        raise ClickUpError(f"task {task_id} not found")

    def teams(self):
        self.calls += 1
        teams = [dict(t) for t in _fixture()["teams"]]
        people = _fixture().get("members", [])
        for team in teams:
            team["members"] = [{"user": dict(p)} for p in people]
        return teams

    def spaces(self, team_id, archived=False):
        self.calls += 1
        return _fixture()["spaces"].get(team_id, [])

    def folders(self, space_id, archived=False):
        self.calls += 1
        return _fixture()["folders"].get(space_id, [])

    def folderless_lists(self, space_id, archived=False):
        self.calls += 1
        return _fixture()["folderless_lists"].get(space_id, [])

    def folder_lists(self, folder_id, archived=False):
        self.calls += 1
        return _fixture()["folder_lists"].get(folder_id, [])

    # -- writes -----------------------------------------------------------

    def create_task(self, list_id, name, description="", parent=None, status=None,
                    assignees=None, priority=None, due_date=None, start_date=None,
                    time_estimate=None, tags=None):
        self.calls += 1
        # The real body builder runs, so parsing is under test.
        body = self.task_body(
            name=name, description=description, parent=parent, status=status,
            assignees=assignees, priority=priority, due_date=due_date,
            start_date=start_date, time_estimate=time_estimate, tags=tags,
        )
        state = _state()
        state["n"] += 1
        task_id = f"new{state['n']:02d}"
        state["created"].append({
            "id": task_id, "list_id": list_id, "name": name,
            "parent": parent, "status": status or "completed",
            "assignees": [], "description": description, "body": body,
        })
        _save(state)
        return {"id": task_id, "url": f"https://app.clickup.com/t/{task_id}"}

    def update_task(self, task_id, **fields):
        self.calls += 1
        state = _state()
        state["updated"].append({"id": task_id, **fields})
        _save(state)
        return {}

    def delete_task(self, task_id):
        self.calls += 1
        state = _state()
        state["deleted"].append(task_id)
        _save(state)
        return {}
