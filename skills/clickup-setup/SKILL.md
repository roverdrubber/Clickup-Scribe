---
name: clickup-setup
description: Use when a ClickUp command fails with 401, "not configured", or "no token", when a user is running clickup-scribe or clickup-read for the first time, or when asked to check, store, rotate, or clear the ClickUp API token. Covers where the token is read from and how to get one stored without it landing in a transcript.
---

# Configuring the ClickUp API token

Every command in `clickup-scribe` and `clickup-read` needs a personal ClickUp
token. It is read from `CLICKUP_TOKEN` if that is set, otherwise from a stored
file, and from nowhere else. No command takes it as an argument.

## 1. Report the current state

Run the check for whichever runtime the host has:

```bash
python3 scripts/setup.py show
```
```powershell
.\scripts\setup.ps1 show
```

It prints where the token is coming from - environment, stored file, or not
configured - and never prints the token itself.

## 2. If it is already configured

Confirm it actually works with one API call:

```bash
python3 scripts/reader.py members
```

If that lists workspace members, setup is done. Tell the user the numeric ids
shown are what a spec's `assignees` takes. If it returns 401, the stored token
is wrong - continue to step 3.

## 3. If it is not configured

**Do not ask the user to paste the token into the conversation**, and do not
try to run `setup.py store` yourself: the prompt is hidden, and an agent's
shell cannot type into it. Anything typed here also lands in the transcript.

Instead, hand them a command for their own terminal. **Never paste a
remembered path or a variable** - the plugin may be installed anywhere, and
the user's shell has none of the variables yours does.

Do not construct the path by hand. Run any command with no token configured:
the error resolves its own absolute location and prints the exact line to give
them.

```bash
$ python3 scripts/reader.py members
No ClickUp token configured. Run this in your own terminal:
    python3 /abs/path/to/clickup-scribe/scripts/setup.py store
or set CLICKUP_TOKEN for a one-off.
```

`reader.ps1` prints the same thing, pointing at `setup.ps1`.

Explain what it does: prompts without echoing, verifies the token against the
API before saving anything, then writes it to a user-only file.

| Host | Stored at |
|---|---|
| Linux, macOS | `~/.config/clickup-scribe/token` |
| Windows | `%APPDATA%\clickup-scribe\token` |

Every command reads it from there afterwards, so no environment variable is
needed.

Point them at ClickUp > Settings > Apps > API Token to generate one. It starts
with `pk_`, carries their full permissions, and does not expire - so it is a
password, not a config value.

Once they say it is done, verify with step 2.

## Notes

- `CLICKUP_TOKEN` in the environment overrides the stored file, which is what
  makes a one-off or a CI run possible without disturbing the stored token.
- `setup.py clear` removes the stored token.
- `CLICKUP_SCRIBE_HOME` moves the stored file, so one machine can hold tokens
  for two workspaces.
- If the user would rather use an environment variable, put it in the
  **user-level** config their agent reads at startup, never a project-level
  file that gets committed. In Claude Code that is `~/.claude/settings.json`
  under `env`, not a project `.claude/settings.json`.

## Reference

- `../../scripts/setup.py`, `setup.ps1` - store / show / clear
- `../clickup-scribe/SKILL.md` - writing to a board
- `../clickup-read/SKILL.md` - reading a board
