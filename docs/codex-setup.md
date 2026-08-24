# Setting up clickup-scribe in Codex

The three skills under `skills/` are plain `SKILL.md` files - YAML frontmatter
with `name` and `description`, a markdown body, `references/` alongside - which
is the shape Codex loads. Nothing needs to be ported. What follows is where to
put them so Codex finds them, and so they can find their scripts.

| Skill | Invoke with | For |
|---|---|---|
| `clickup-scribe` | `$clickup-scribe` | writing - one task, or a whole tree from a spec |
| `clickup-read` | `$clickup-read` | reading - hierarchy, tasks, search, export |
| `clickup-setup` | `$clickup-setup` | the one-time API token setup |

## Before you start

- **Codex CLI** with skills support. `/skills` in a session lists what it can
  see; if that command does not exist, your build predates skills.
- **A runtime.** Python is the default - `python3` ships on Linux and macOS,
  and most Windows machines have it. PowerShell is the fallback for a Windows
  host without Python; 5.1 is part of the OS, so it needs no install. Detect
  Python by running it, not by whether the name resolves:
  `python3 --version || python --version || py -3 --version`. Neither
  implementation needs a third-party package.
- **A ClickUp personal token**, from ClickUp > Settings > Apps > API Token.
  Get one now; you will store it in step 4.

## 1. The one layout rule

**`scripts/` must be a sibling of the directory holding the skill folders.**

All three skills share one copy of the REST client rather than carrying their
own, and each reaches it at `../../scripts/`. From `<root>/skills/<skill>/`
that resolves to `<root>/scripts/`, so wherever you put the skills, `scripts/`
goes one level up:

| Skills go in | `scripts/` goes in |
|---|---|
| `~/.codex/skills/` | `~/.codex/scripts/` |
| `<project>/.codex/skills/` | `<project>/.codex/scripts/` |
| `<repo>/.agents/skills/` | `<repo>/.agents/scripts/` |

Get this wrong and the skills load fine but every command fails with
`No such file or directory` on the script itself.

One wrinkle worth knowing if you install with symlinks: the OS resolves `..`
through a symlink physically, so `<root>/skills/clickup-scribe/../../scripts`
lands back inside the clone and the commands may work even if you forget the
`scripts` link. Do not rely on that - it does not hold for copies, and it does
not hold if you ever move the clone. Link both.

The sharing is deliberate. Three copies of `clickup_client.py` would drift, and
`tests/parity.py` only catches drift between the two *runtimes*, not between
copies of the same one.

## 2. Clone the repository

Clone it somewhere permanent - the install links back to it, so this directory
has to stay put.

```bash
git clone <repo-url> ~/src/clickup-scribe
```

Do not lift a single skill folder out and drop it into a skills root on its
own. It will load, and it will have no tool to run.

## 3. Install the skills

### Option A: user-level, available in every project (recommended)

Codex reads `$CODEX_HOME/skills`, defaulting to `~/.codex/skills`.

```bash
SRC=~/src/clickup-scribe
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

mkdir -p "$CODEX_HOME/skills"
for s in clickup-scribe clickup-read clickup-setup; do
  ln -s "$SRC/skills/$s" "$CODEX_HOME/skills/$s"
done
ln -s "$SRC/scripts" "$CODEX_HOME/scripts"
```

Symlinks rather than copies so `git pull` in the clone updates the install with
no further steps. If you would rather copy, swap each `ln -s` for `cp -r` and
re-copy whenever you update.

**On Windows**, symlinks need Developer Mode or an elevated shell:

```powershell
$src = "$HOME\src\clickup-scribe"
$codex = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { "$HOME\.codex" }

New-Item -ItemType Directory -Force -Path "$codex\skills" | Out-Null
foreach ($s in 'clickup-scribe', 'clickup-read', 'clickup-setup') {
    New-Item -ItemType SymbolicLink -Path "$codex\skills\$s" -Target "$src\skills\$s"
}
New-Item -ItemType SymbolicLink -Path "$codex\scripts" -Target "$src\scripts"
```

If that is refused, use `Copy-Item -Recurse` instead. Copies work identically;
they just do not follow the clone.

### Option B: one project only

`<project>/.codex/skills` takes precedence over the user-level root, so use it
to pin a specific version for one repository.

```bash
SRC=~/src/clickup-scribe
mkdir -p .codex/skills
for s in clickup-scribe clickup-read clickup-setup; do
  ln -s "$SRC/skills/$s" ".codex/skills/$s"
done
ln -s "$SRC/scripts" ".codex/scripts"
```

This writes inside the project, so decide whether `.codex/` is committed or
ignored before you do it.

`<repo>/.agents/skills` works the same way and is picked up from any ancestor
directory of your working directory.

There is **no `config.toml` setting for skill roots** - the `[skills]` section
has no such field, and the only other mechanism is an ephemeral runtime API.
The directories above are how skills get installed.

## 4. Store the token

Run `$clickup-setup` and let it walk you through, or do it directly. Either
way, **run the store command in your own terminal, not through Codex**: the
prompt is hidden, an agent's shell cannot type into it, and anything typed in
a session lands in the transcript.

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/scripts/setup.py" store
```
```powershell
& "$(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { "$HOME\.codex" })\scripts\setup.ps1" store
```

It prompts without echoing, verifies the token against the API before saving
anything, then writes it owner-only to `~/.config/clickup-scribe/token` (or
`%APPDATA%\clickup-scribe\token`). Every command reads it from there
afterwards, so no environment variable is needed.

`CLICKUP_TOKEN` in the environment overrides the stored file, for a one-off or
a CI run. `setup.py show` reports the source without printing the token;
`setup.py clear` removes it.

## 5. Verify

In a Codex session:

```
/skills
```

`clickup-scribe`, `clickup-read`, and `clickup-setup` should be listed. Then
confirm the token and the scripts in one call:

```bash
python3 "${CODEX_HOME:-$HOME/.codex}/scripts/reader.py" members
```

A list of workspace members means everything is wired: skills found, scripts
reachable, token valid. The numeric ids it prints are what a spec's `assignees`
field takes.

## 6. Use it

Invoke a skill explicitly with `$`:

```
$clickup-read walk the workspace and show me the HRIS space
$clickup-scribe put this roadmap on list 901615936216, dry run first
```

Codex also matches skills implicitly against their `description`, so asking for
ClickUp work in plain language usually pulls the right one in without the `$`.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Skill not in `/skills` | Wrong root, or the folder was renamed. The directory name must match the `name` in the frontmatter |
| `No such file or directory` on `scribe.py` / `reader.py` | `scripts/` is not a sibling of the skills root - see step 1 |
| `ModuleNotFoundError: clickup_client` | A script was copied out of `scripts/` on its own. `clickup_client.py` (or `ClickUp.psm1`) has to sit beside it |
| `No ClickUp token configured` | Not stored yet. The error prints the exact absolute command to run; use that line rather than rebuilding the path |
| `401` from any command | Token wrong or revoked. `setup.py clear`, then `setup.py store` |
| `python` opens the Microsoft Store | That is the Store stub, not a Python. Windows ships none by default; use the `.ps1` half, which needs nothing installed |
| First call fails like a network fault on Windows | PowerShell 5.1 does not negotiate TLS 1.2 by default. `ClickUp.psm1` forces it; make sure you are running the shipped module, not a copy |

## What Codex ignores

`.claude-plugin/` and `commands/` are Claude Code's. Codex skips them, and the
only thing lost is the `/clickup-scribe:setup` alias - which is a pointer to
the `clickup-setup` skill that Codex loads directly anyway. For that side, see
[`claude-code-setup.md`](claude-code-setup.md).

## A caveat

Codex's skills system is moving quickly. The directories and the `$skill-name`
invocation above were verified against the current `openai/codex` source, but
if something here does not match your build, `/skills` is the ground truth for
what Codex can actually see.
