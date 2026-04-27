# claude-memory-sync

I work between an Ubuntu laptop when I'm travelling and a Windows desktop at home, and I keep project files in sync through Dropbox. Claude Code's memory feature is great — it remembers who I am, how I like to work, and what's going on in each project, so I don't re-explain it every session — but the memories live on one machine at a time. Each time I switched laptops, the other Claude had stale context, or none at all. I kept telling it to ignore its own memory because the memory was wrong. I also lost the ability to `/resume` recent sessions on the other machine.

So Claude Code helped me write this. It's a ~300-line Python script that two-way syncs Claude Code's memory files, session transcripts, and skills between machines via a folder in Dropbox.

## Why not tawanorg/claude-sync?

I found [tawanorg/claude-sync](https://github.com/tawanorg/claude-sync) first, and it's genuinely more serious than this: Go binary, Cloudflare R2 / S3 / GCS backend, end-to-end encryption with age, proper conflict detection. If you need any of that, use it.

The reason it didn't work for me: it identifies projects by absolute filesystem path. My project lives at `~/Dropbox/Projects/foo` on Linux and `D:\Dropbox\Projects\foo` on Windows, which Claude Code treats as two unrelated projects. tawanorg's recommended workaround is to keep identical paths on every machine or to use symlinks. I didn't want to do either.

I already pay for Dropbox, already trust it with my code, and wanted something I could read in one sitting. This tool identifies projects by folder *basename* and keeps a small registry mapping each machine's local path. `foo` on Linux and `foo` on Windows end up as one logical entry.

## What it does

- Two-way syncs `~/.claude/projects/<project>/memory/` (your memory files) and the project's `*.jsonl` session transcripts so `/resume` works across machines.
- Also syncs `~/.claude/skills/` globally, so a skill you install on one laptop shows up on the other.
- Uses the project folder's basename as identity, recording each machine's absolute path in `Dropbox/claude-memory-sync/registry.json`.
- Newest-mtime-wins per file. Never touches local files except to overwrite with newer versions from Dropbox.
- Before overwriting a file whose contents differ, copies the older version to `Dropbox/claude-memory-sync/backups/<timestamp>/` — so last-write-wins is at least recoverable.
- Refuses to sync if two machines try to register different projects under the same basename (e.g. two different `website/` directories), and tells you which one collided.
- Stores session transcripts gzipped on Dropbox (`*.jsonl.gz`); local copies stay raw `.jsonl` so `/resume` keeps working.
- Treats Dropbox as a transport buffer, not an archive: prunes session files older than 90 days and old backup snapshots (keeps last 10 runs or anything from the past 7 days, whichever is more) at the end of every sync. Local copies on each machine are the canonical archive and are never pruned.

## Setup — do this once per machine

Requires Python 3.8+ and the Dropbox desktop client running.

**1. Clone into your Claude skills directory.** The folder name matters — it must be `claude-memory-sync`, because that's the path you'll invoke.

```bash
git clone https://github.com/siddharth-bharath/claude-memory-sync ~/.claude/skills/claude-memory-sync
```

On Windows (Git Bash / PowerShell with `$USERPROFILE`):

```bash
git clone https://github.com/siddharth-bharath/claude-memory-sync "$USERPROFILE/.claude/skills/claude-memory-sync"
```

Cloning into `~/.claude/skills/` also makes it show up as a Claude Code skill, so you can say "sync my memory" in a session and Claude will find it.

**2. If your Dropbox root isn't auto-detected**, export `CLAUDE_SYNC_DROPBOX=/path/to/Dropbox`. The script checks `~/Dropbox`, `~/Dropbox (Personal)`, and a few others; Windows users whose Dropbox lives on `D:` usually need to set this.

**3. Run a real sync from any project directory** (not just `--list` — `--list` is read-only and does *not* register the machine):

```bash
python ~/.claude/skills/claude-memory-sync/sync.py
```

**4. Verify the machine is registered:**

```bash
python ~/.claude/skills/claude-memory-sync/sync.py --list
```

Under the current project you should see your hostname listed alongside any other machines already syncing that project. If you only see one hostname after running on both machines, something is wrong — most likely the other machine is running an older fork of this script that points at a different Dropbox subfolder (check for a stray `~/.claude/skills/claude-sync/` and delete it).

## Everyday use

From any project directory:

```bash
python ~/.claude/skills/claude-memory-sync/sync.py
```

Syncs that project's memory and sessions, plus global skills. Run it at the start and end of a work session, or whenever you switch machines. If you installed the skill, you can also just say "sync my memory" in a Claude Code session.

Useful flags:

- `--all` — sync every project registered from this machine (run once per project first to register).
- `--dry-run` — show what would change, copy nothing.
- `--skills-only` / `--no-skills` — sync just skills, or skip them.
- `--project /abs/path` — sync a project other than the current directory.
- `--list` — print the registry. Does *not* sync or register anything.
- `--exclude NAME` — add a project's canonical name (folder basename) to the excluded list. It is removed from the registry and will be silently skipped by all future syncs, including `--all`. Useful for home-directory or other catch-all project roots you never want synced.
- `--unexclude NAME` — remove a name from the excluded list so it can be synced again.
- `--no-prune` — skip the end-of-sync pruning of old sessions and old backups (one-off use; the next normal sync will resume pruning).
- `--export-bundle PATH` — write a single tar.gz at PATH containing every project's memory + sessions + global skills from this machine, for bootstrapping a brand-new computer with full history (Dropbox only carries the rolling 90-day window). See below.

## Bootstrapping a new machine with full history

Dropbox only carries roughly the last 90 days of session transcripts. If you set up a third machine — or wipe one — and want it to start with the *full* history of memories and sessions you've accumulated, run this on the machine that has it all:

```bash
python ~/.claude/skills/claude-memory-sync/sync.py --export-bundle ~/claude-bundle.tar.gz
```

This writes a single tar.gz containing:

- `bundle/manifest.json` — what's inside, with `abs_path` and `local_key` for each project as known by the source machine.
- `bundle/skills/` — your global skills.
- `bundle/projects/<canonical>/{memory,sessions}/` — every project that has been synced at least once (i.e. has a canonical-name registry entry).
- `bundle/unregistered/<local_key>/{memory,sessions}/` — every other project directory in `~/.claude/projects/` on the source machine that has any content. These were never synced, so the canonical mapping isn't known; you decide on the receiving end.

To restore on the new machine: copy the bundle over, extract it, then for each project:

1. `cd` into where the project lives locally on this machine.
2. Run `python ~/.claude/skills/claude-memory-sync/sync.py` once. This creates `~/.claude/projects/<new-key>/` and registers this machine.
3. Copy `bundle/projects/<canonical>/{memory,sessions}/` into `~/.claude/projects/<new-key>/` to backfill the older history that isn't on Dropbox.

For the `unregistered/` entries, look at the `local_key` to figure out which project each one came from (the keys are derived from the source machine's absolute path), then do the same dance after deciding whether the project is something you actually use on the new machine.

## Honest caveats

Please read these before you rely on this tool.

**Last-write-wins conflict handling.** If you edit the same memory file on both machines between syncs, the older edit is overwritten. The `backups/` folder keeps a copy so you can recover, but there's no diff or merge. If you need proper multi-writer conflict handling, use tawanorg's tool.

**No encryption.** Your memory and session files sit in your Dropbox folder as plaintext. Session transcripts can contain API keys, code, and other secrets. You're trusting Dropbox's at-rest encryption and your account security. If that's not acceptable, use tawanorg's encrypted version.

**Dropbox is a hard dependency.** If the Dropbox client isn't running on both machines, nothing useful happens. This is a thin layer on top of Dropbox, not a replacement for it. Any other folder-syncing tool (iCloud Drive, OneDrive, Syncthing) should work if you point `CLAUDE_SYNC_DROPBOX` at it, but I only use Dropbox.

**Single-writer assumption.** Running Claude Code on the same project on both machines simultaneously, with syncs in between, will race.

**Basename collisions.** The tool now detects when two unrelated projects share a folder name and refuses to sync either, but you'll have to rename one to proceed.

## Credit

Inspired by [tawanorg/claude-sync](https://github.com/tawanorg/claude-sync), which is more feature-complete, encrypted, and cloud-backed. Start there if you need any of that. This is the simpler, Dropbox-only, works-even-when-your-paths-don't-match version.

## License

MIT.
