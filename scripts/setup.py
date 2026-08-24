#!/usr/bin/env python3
"""Store a ClickUp API token once, so no command needs an env var again.

    setup.py store     # prompt for a token, check it, save it
    setup.py show      # where the token comes from - never the token itself
    setup.py clear     # remove the stored token

The prompt does not echo, and the token is never printed back, so it stays out
of your shell history and out of any transcript. Run this in your own terminal:
an agent's shell cannot type into a hidden prompt.
"""

from __future__ import annotations

import argparse
import getpass
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from clickup_client import (  # noqa: E402
    ClickUp, ClickUpError, token_path, token_source,
)


def cmd_show(args) -> int:
    print(f"token source: {token_source()}")
    path = token_path()
    print(f"stored file:  {path}" + ("" if path.exists() else "  (absent)"))
    if os.environ.get("CLICKUP_TOKEN"):
        print("\nThe environment wins over the stored file. Unset "
              "CLICKUP_TOKEN to use the stored one.")
    return 0


def cmd_clear(args) -> int:
    path = token_path()
    if not path.exists():
        print(f"Nothing stored at {path}")
        return 0
    path.unlink()
    print(f"removed {path}")
    if os.environ.get("CLICKUP_TOKEN"):
        print("CLICKUP_TOKEN is still set in this environment.")
    return 0


def cmd_store(args) -> int:
    if not sys.stdin.isatty():
        print("store needs a terminal: the prompt is hidden so the token is "
              "not echoed.\nRun it in your own shell, or set CLICKUP_TOKEN "
              "for a one-off.", file=sys.stderr)
        return 1

    print("ClickUp > Settings > Apps > API Token. Starts with pk_.")
    token = getpass.getpass("Token (not shown): ").strip()
    if not token:
        print("Nothing entered.", file=sys.stderr)
        return 1
    if not token.startswith("pk_"):
        print("That does not look like a personal token - they start with "
              "'pk_'. Storing it anyway.", file=sys.stderr)

    # Check it before storing, so a typo fails here rather than later.
    try:
        people = ClickUp(token=token).members()
    except ClickUpError as error:
        print(f"\nClickUp rejected it: {error}", file=sys.stderr)
        print("Nothing was stored.", file=sys.stderr)
        return 1

    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token + "\n")
    # Owner-only. Irrelevant on Windows ACLs but harmless there.
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    print(f"\nVerified: {len(people)} workspace member(s) visible.")
    print(f"Stored in {path} (owner-read only).")
    print("Every command picks it up from there now - no env var needed.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("store", help="prompt for a token, verify, save").set_defaults(
        func=cmd_store)
    sub.add_parser("show", help="report where the token comes from").set_defaults(
        func=cmd_show)
    sub.add_parser("clear", help="remove the stored token").set_defaults(
        func=cmd_clear)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
