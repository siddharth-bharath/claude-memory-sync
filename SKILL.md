---
name: claude-memory-sync
description: Two-way sync Claude Code's project memory, session transcripts, and global skills between machines via a Dropbox folder. Use when the user says "sync memory", "sync claude", "pull memory from other machine", "/claude-memory-sync", or is switching between laptop and desktop and wants context carried over.
---

# claude-memory-sync

Two-way syncs, per run:

1. **Project memory + sessions** — `~/.claude/projects/<key>/memory/` and `~/.claude/projects/<key>/*.jsonl` with `Dropbox/claude-memory-sync/projects/<basename>/` for the project in the current working directory.
2. **Global skills** — `~/.claude/skills/` with `Dropbox/claude-memory-sync/skills/`, independent of any project.

Newest-mtime-wins per file. Never deletes. Differing files are backed up to `Dropbox/claude-memory-sync/backups/<timestamp>/` before being overwritten.

## Running it

From the project directory:

```bash
python ~/.claude/skills/claude-memory-sync/sync.py
```

On Windows:

```bash
python "$USERPROFILE/.claude/skills/claude-memory-sync/sync.py"
```

Flags:
- `--dry-run` — show what would change, don't copy
- `--project /abs/path` — sync a different project than cwd
- `--all` — sync every project in the registry that exists on this machine
- `--list` — print the registry JSON
- `--no-skills` — skip the global `~/.claude/skills/` sync
- `--skills-only` — only sync skills; skip project memory/sessions

## When to run

- **Start of a session** on a given machine: pull down anything the other machine wrote since last sync.
- **End of a session**: push this machine's changes up.
- Before switching machines.

## Dropbox layout

```
Dropbox/claude-memory-sync/
├── registry.json                  # canonical-name → per-machine metadata
├── projects/<basename>/
│   ├── memory/                    # mirrors ~/.claude/projects/<key>/memory/
│   └── sessions/                  # mirrors *.jsonl from project root
├── skills/                        # mirrors ~/.claude/skills/
├── backups/<timestamp>/           # pre-overwrite copies, for recovery
└── logs/<host>-<timestamp>.log
```

## What does NOT sync

- Other files in `~/.claude/projects/<key>/` (e.g. `todos.json`) — intentional.
- Global `~/.claude/` config (settings.json, keybindings.json), agents, and other top-level files. Only `~/.claude/skills/` is included.

## Troubleshooting

- "Could not find Dropbox root": set `CLAUDE_SYNC_DROPBOX=/path/to/Dropbox`.
- Collision warning: two machines registered the same folder basename with different project folder names. Rename one project or use `CLAUDE_SYNC_DROPBOX` to point to a separate sync root.
- If a project folder doesn't yet exist in `~/.claude/projects/`, the script creates it; Claude Code will pick up the memories the next time it opens.
