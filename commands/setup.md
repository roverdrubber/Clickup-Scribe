---
description: Check or set up the ClickUp API token this plugin uses
---

Help the user get a ClickUp token configured for the clickup-scribe plugin.

## 1. Report the current state

Run the check for whichever runtime the host has:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup.py" show
```
```powershell
& "${CLAUDE_PLUGIN_ROOT}/scripts/setup.ps1" show
```

It prints where the token is coming from - environment, stored file, or not
configured - and never prints the token itself.

## 2. If it is already configured

Confirm it actually works with one API call:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/reader.py" members
```

If that lists workspace members, setup is done. Tell the user the numeric ids
shown are what a spec's `assignees` takes. If it returns 401, the stored token
is wrong - continue to step 3.

## 3. If it is not configured

**Do not ask the user to paste the token into the conversation**, and do not
try to run `setup.py store` yourself: the prompt is hidden, and an agent's
shell cannot type into it. Anything typed here also lands in the transcript.

Instead, hand them a command for their own terminal. **Resolve the path
first** - `${CLAUDE_PLUGIN_ROOT}` is set for you but not for their shell, and
the plugin may be installed anywhere, so never paste the variable or a
remembered path:

```bash
echo "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup.py store"
```

Give them the line that prints. On Windows it is the same idea with
`setup.ps1` and no `python3`.

The quickest way to get the exact line without thinking about it: run any
command with no token configured. The error resolves its own location and
prints the command to run.

Explain what it does: prompts without echoing, verifies the token against the
API before saving anything, then writes it to a user-only file
(`~/.config/clickup-scribe/token`, or `%APPDATA%\clickup-scribe\token`).
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
- If the user would rather use an environment variable, `~/.claude/settings.json`
  under `env` is the right place for Claude Code sessions - the **user-level**
  file, never a project `.claude/settings.json`, which often gets committed.
