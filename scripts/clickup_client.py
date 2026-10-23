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
from datetime import datetime

API = "https://api.clickup.com/api/v2"

# ClickUp stores priority as 1-4. Nobody remembers which way round it runs, so
# both the CLI and specs accept the names and this maps them.
PRIORITIES = {"urgent": 1, "high": 2, "normal": 3, "low": 4}

# GET /task/{id}/comment returns at most this many per page.
COMMENT_PAGE = 25


def parse_priority(value) -> int | None:
    """Accept 'high', 'HIGH', or 2. Returns None for an empty value."""
    if value in (None, ""):
        return None
    if isinstance(value, int):
        if value not in PRIORITIES.values():
            raise ClickUpError(f"priority must be 1-4, got {value}")
        return value
    key = str(value).strip().lower()
    if key.isdigit():
        return parse_priority(int(key))
    if key not in PRIORITIES:
        raise ClickUpError(
            f"unknown priority '{value}'. Use one of: "
            + ", ".join(PRIORITIES)
        )
    return PRIORITIES[key]


def parse_date(value) -> tuple[int, bool] | None:
    """'2026-08-24' or '2026-08-24 17:30' -> (epoch milliseconds, has_time).

    ClickUp wants milliseconds since the epoch plus a separate flag saying
    whether the time half is meaningful; a date with no time renders as an
    all-day due date rather than midnight.
    """
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value, True
    text = str(value).strip()
    for fmt, has_time in (("%Y-%m-%d %H:%M", True), ("%Y-%m-%d", False)):
        try:
            moment = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return int(moment.timestamp() * 1000), has_time
    raise ClickUpError(
        f"cannot read date '{value}'. Use YYYY-MM-DD or YYYY-MM-DD HH:MM."
    )


def token_path() -> "Path":
    """Where a stored token lives, per platform.

    `CLICKUP_SCRIBE_HOME` overrides it, which is what the tests use and what
    lets one machine hold tokens for two workspaces.
    """
    from pathlib import Path

    if override := os.environ.get("CLICKUP_SCRIBE_HOME"):
        return Path(override) / "token"
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / "clickup-scribe" / "token"
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "clickup-scribe" / "token"


def find_token() -> str | None:
    """The token, from the environment first and the stored file second.

    Environment wins so a one-off or a CI run can override what is stored
    without disturbing it.
    """
    if value := (os.environ.get("CLICKUP_TOKEN") or "").strip():
        return value
    path = token_path()
    try:
        stored = path.read_text().strip()
    except OSError:
        return None
    return stored or None


def token_source() -> str:
    """Where the token is coming from. Never returns the token itself."""
    if (os.environ.get("CLICKUP_TOKEN") or "").strip():
        return "environment (CLICKUP_TOKEN)"
    path = token_path()
    try:
        if path.read_text().strip():
            return f"file ({path})"
    except OSError:
        pass
    return "not configured"


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
        self.token = token or find_token() or ""
        if not self.token:
            # Resolved rather than written down: the plugin may be installed
            # anywhere, and a path in prose goes stale the moment it moves.
            from pathlib import Path
            setup = Path(__file__).resolve().with_name("setup.py")
            raise ClickUpError(
                "No ClickUp token configured. Run this in your own terminal:\n"
                f"    python3 {setup} store\n"
                "or set CLICKUP_TOKEN for a one-off."
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
        assert method in ("GET", "POST", "PUT", "DELETE"), method
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

    def get_task(self, task_id: str, subtasks: bool = True) -> dict:
        query = "?include_subtasks=true" if subtasks else ""
        return self._call("GET", f"/task/{task_id}{query}")

    def get_comments(self, task_id: str) -> list[dict]:
        """Every comment on a task, newest first, following pagination to the end.

        The endpoint returns the newest 25 only. Older ones need `start` (the
        oldest received comment's `date`) and `start_id` (its `id`) together.
        Stopping at the first page silently forgets old comments, which turns
        an "already linked" check into a duplicate post.
        """
        comments: list[dict] = []
        seen: set[str] = set()
        query = ""
        while True:
            payload = self._call("GET", f"/task/{task_id}/comment{query}")
            batch = payload.get("comments") or []
            fresh = 0
            for comment in batch:
                key = str(comment.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                comments.append(comment)
                fresh += 1
            # A short page is the last one. A page with nothing new means the
            # cursor did not move; stopping beats looping forever.
            if len(batch) < COMMENT_PAGE or not fresh:
                return comments
            oldest = batch[-1]
            query = "?" + urllib.parse.urlencode({
                "start": str(oldest.get("date")),
                "start_id": str(oldest.get("id")),
            })

    # -- hierarchy --------------------------------------------------------
    #
    # ClickUp's own vocabulary, which the UI hides: a Workspace is a "team" in
    # the API. Lists live either in a Folder or directly in a Space, and the
    # folderless ones are a separate endpoint - miss it and whole lists vanish
    # from a traversal without any error.

    def teams(self) -> list[dict]:
        """Workspaces the token can see."""
        return self._call("GET", "/team").get("teams", [])

    def members(self) -> list[dict]:
        """Everyone across every visible workspace: id, username, email.

        `GET /team` already embeds members, so this costs one call rather than
        one per workspace.
        """
        people: dict[int, dict] = {}
        for team in self.teams():
            for member in team.get("members", []):
                user = member.get("user", member)
                if user.get("id") is None:
                    continue
                people[user["id"]] = {
                    "id": user["id"],
                    "username": user.get("username") or "",
                    "email": user.get("email") or "",
                    "workspace": team.get("name", ""),
                }
        return sorted(people.values(), key=lambda p: (p["username"], p["id"]))

    def resolve_assignees(self, values) -> list[int]:
        """Turn ids, emails, or usernames into numeric ids.

        Numeric values pass straight through, so the common case costs nothing.
        A lookup happens only when a name or email is actually present.
        """
        wanted = [v for v in (values or []) if v not in (None, "")]
        if not wanted:
            return []
        if all(str(v).isdigit() for v in wanted):
            return [int(v) for v in wanted]

        people = self.members()
        resolved = []
        for value in wanted:
            text = str(value)
            if text.isdigit():
                resolved.append(int(text))
                continue
            match = [
                p for p in people
                if p["email"].lower() == text.lower()
                or p["username"].lower() == text.lower()
            ]
            if not match:
                raise ClickUpError(
                    f"no workspace member matches '{value}'. "
                    "Run the reader's `members` command to list them."
                )
            if len(match) > 1:
                ids = ", ".join(str(p["id"]) for p in match)
                raise ClickUpError(
                    f"'{value}' matches several members ({ids}). Use the id."
                )
            resolved.append(match[0]["id"])
        return resolved

    def spaces(self, team_id: str, archived: bool = False) -> list[dict]:
        return self._call(
            "GET", f"/team/{team_id}/space?archived={str(archived).lower()}"
        ).get("spaces", [])

    def folders(self, space_id: str, archived: bool = False) -> list[dict]:
        return self._call(
            "GET", f"/space/{space_id}/folder?archived={str(archived).lower()}"
        ).get("folders", [])

    def folderless_lists(self, space_id: str, archived: bool = False) -> list[dict]:
        """Lists sitting directly in a Space, with no Folder above them."""
        return self._call(
            "GET", f"/space/{space_id}/list?archived={str(archived).lower()}"
        ).get("lists", [])

    def folder_lists(self, folder_id: str, archived: bool = False) -> list[dict]:
        return self._call(
            "GET", f"/folder/{folder_id}/list?archived={str(archived).lower()}"
        ).get("lists", [])

    # -- writes -----------------------------------------------------------

    def create_task(self, list_id: str, name: str, description: str = "",
                    parent: str | None = None, status: str | None = None,
                    assignees: list[int] | None = None,
                    priority=None, due_date=None, start_date=None,
                    time_estimate: int | None = None,
                    tags: list[str] | None = None) -> dict:
        body = self.task_body(
            name=name, description=description, parent=parent, status=status,
            assignees=assignees, priority=priority, due_date=due_date,
            start_date=start_date, time_estimate=time_estimate, tags=tags,
        )
        return self._call("POST", f"/list/{list_id}/task", body)

    @staticmethod
    def task_body(name: str | None = None, description: str | None = None,
                  parent: str | None = None, status: str | None = None,
                  assignees: list[int] | None = None, priority=None,
                  due_date=None, start_date=None,
                  time_estimate: int | None = None,
                  tags: list[str] | None = None) -> dict:
        """The fields shared by create and update, omitting anything unset.

        Sending a field as null is not the same as omitting it - ClickUp reads
        an explicit null as "clear this" - so only what the caller actually
        supplied goes into the body.
        """
        body: dict = {}
        if name:
            body["name"] = name
        if description:
            body["markdown_content"] = description
        if parent:
            body["parent"] = parent
        if status:
            body["status"] = status
        if assignees:
            body["assignees"] = list(assignees)
        if (level := parse_priority(priority)) is not None:
            body["priority"] = level
        if (due := parse_date(due_date)) is not None:
            body["due_date"], body["due_date_time"] = due
        if (start := parse_date(start_date)) is not None:
            body["start_date"], body["start_date_time"] = start
        if time_estimate:
            # The API counts milliseconds; the CLI and specs take minutes,
            # which is what a person means and what the MCP also accepts.
            body["time_estimate"] = int(time_estimate) * 60_000
        if tags:
            body["tags"] = list(tags)
        return body

    def delete_task(self, task_id: str) -> dict:
        """Move a task to the workspace trash. Recoverable there for 30 days."""
        return self._call("DELETE", f"/task/{task_id}")

    @staticmethod
    def drift(current: dict, wanted: dict) -> dict:
        """Fields in `wanted` that the live task does not already match.

        Only what the spec declares is compared, so a field the spec is silent
        about is never touched - a priority set by hand in the UI survives a
        spec that says nothing about priority.

        `description` is deliberately not reconciled. We send `markdown_content`
        and ClickUp returns a normalised `description`, so equality is not
        reliable and every run would re-send the same text.
        """
        changed: dict = {}

        def current_priority():
            raw = current.get("priority")
            if isinstance(raw, dict):
                raw = raw.get("priority")
            return int(raw) if raw not in (None, "") else None

        def current_int(key):
            raw = current.get(key)
            return int(raw) if raw not in (None, "") else None

        for key, value in wanted.items():
            if key in ("name", "markdown_content", "parent", "assignees",
                       "due_date_time", "start_date_time"):
                continue
            if key == "status":
                if (current.get("status") or {}).get("status") != value:
                    changed["status"] = value
            elif key == "priority":
                if current_priority() != value:
                    changed["priority"] = value
            elif key in ("due_date", "start_date", "time_estimate"):
                if current_int(key) != value:
                    changed[key] = value
                    flag = f"{key}_time"
                    if flag in wanted:
                        changed[flag] = wanted[flag]
            elif key == "tags":
                live = sorted(
                    t.get("name", t) if isinstance(t, dict) else t
                    for t in (current.get("tags") or [])
                )
                if live != sorted(value):
                    changed["tags"] = value
        return changed

    # -- structure --------------------------------------------------------
    #
    # A List is created against whichever container holds it, so the endpoint
    # differs: inside a Folder, or directly in a Space. There is no single
    # "create list" call that takes a parent of either kind.

    def create_folder(self, space_id: str, name: str) -> dict:
        return self._call("POST", f"/space/{space_id}/folder", {"name": name})

    def create_list(self, name: str, folder_id: str | None = None,
                    space_id: str | None = None,
                    content: str | None = None) -> dict:
        if bool(folder_id) == bool(space_id):
            raise ClickUpError(
                "a list needs exactly one container: folder_id or space_id."
            )
        body: dict = {"name": name}
        if content:
            body["content"] = content
        if folder_id:
            return self._call("POST", f"/folder/{folder_id}/list", body)
        return self._call("POST", f"/space/{space_id}/list", body)

    def add_comment(self, task_id: str, text: str,
                    notify_all: bool = False) -> dict:
        """Post a plain-text comment. Returns `{id, hist_id, date}`."""
        return self._call("POST", f"/task/{task_id}/comment",
                          {"comment_text": text, "notify_all": bool(notify_all)})

    def update_task(self, task_id: str, **fields) -> dict:
        return self._call("PUT", f"/task/{task_id}", fields)
