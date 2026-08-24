"""Minimal ClickUp REST v2 client. Standard library only.

Exists because ClickUp's MCP server meters tool calls against a small daily AI
quota, while the REST API allows 100 requests per minute on Free/Unlimited/
Business plans and has no daily cap. Structural work belongs here, not on MCP.

    export CLICKUP_TOKEN=pk_xxxxxxxx      # Settings > Apps > API Token
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.clickup.com/api/v2"


class ClickUpError(RuntimeError):
    """An API call failed and the caller should stop."""


class RateLimited(ClickUpError):
    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Rate limited. Retry after {retry_after:.0f}s.")


class ClickUp:
    """Thin wrapper over the endpoints bulk structural work actually needs.

    Every call is serialized and paced. Parallelism buys nothing against a
    per-minute limit and turns a quota exhaustion into a partially applied
    change, which is far more expensive to reconcile than a slow run.
    """

    def __init__(self, token: str | None = None, pace: float = 0.15) -> None:
        self.token = token or os.environ.get("CLICKUP_TOKEN") or ""
        if not self.token:
            raise ClickUpError(
                "CLICKUP_TOKEN is not set. Get one from ClickUp > Settings > "
                "Apps > API Token, then `export CLICKUP_TOKEN=pk_...`."
            )
        self.pace = pace
        self.calls = 0
        self._last = 0.0

    def _throttle(self) -> None:
        wait = self.pace - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _call(self, method: str, path: str, body: dict | None = None,
              _retries: int = 2) -> dict:
        self._throttle()
        request = urllib.request.Request(
            API + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": self.token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                self.calls += 1
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            if error.code == 429:
                retry_after = float(error.headers.get("Retry-After") or 60)
                # A short window is worth waiting out; a daily cap is not.
                if _retries > 0 and retry_after <= 120:
                    time.sleep(retry_after + 1)
                    return self._call(method, path, body, _retries - 1)
                raise RateLimited(retry_after) from error
            raise ClickUpError(f"{method} {path} -> {error.code}: {detail}") from error

    # -- reads ------------------------------------------------------------

    def list_tasks(self, list_id: str, include_closed: bool = True,
                   subtasks: bool = True) -> list[dict]:
        """Every task on a list, following pagination to the end.

        Stopping at page 0 caps silently at 100 tasks, which reads exactly like
        "nothing else exists" and makes an idempotency check create duplicates.
        """
        query = urllib.parse.urlencode({
            "include_closed": str(include_closed).lower(),
            "subtasks": str(subtasks).lower(),
        })
        tasks, page = [], 0
        while True:
            payload = self._call("GET", f"/list/{list_id}/task?{query}&page={page}")
            batch = payload.get("tasks", [])
            tasks.extend(batch)
            if payload.get("last_page", True) or not batch:
                return tasks
            page += 1

    def name_index(self, list_id: str) -> dict[str, str]:
        """Map task name -> id for one list. The basis of every re-run."""
        index: dict[str, str] = {}
        for task in self.list_tasks(list_id):
            index.setdefault(task["name"], task["id"])
        return index

    def get_list(self, list_id: str) -> dict:
        return self._call("GET", f"/list/{list_id}")

    def statuses(self, list_id: str) -> list[str]:
        return [s["status"] for s in self.get_list(list_id).get("statuses", [])]

    # -- writes -----------------------------------------------------------

    def create_task(self, list_id: str, name: str, description: str = "",
                    parent: str | None = None, status: str | None = None,
                    assignees: list[int] | None = None) -> dict:
        body: dict = {"name": name}
        if description:
            body["markdown_content"] = description
        if parent:
            body["parent"] = parent
        if status:
            body["status"] = status
        if assignees:
            body["assignees"] = assignees
        return self._call("POST", f"/list/{list_id}/task", body)

    def update_task(self, task_id: str, **fields) -> dict:
        return self._call("PUT", f"/task/{task_id}", fields)
